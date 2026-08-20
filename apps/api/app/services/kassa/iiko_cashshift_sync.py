"""Синк закрытых кассовых смен iiko в витрину «Касса → Закрытие смены».

Грузит сводку смен (`GET /resto/api/v2/cashshifts/list`) и разнос изъятий
(`GET /resto/api/v2/cashshifts/payments/list/{id}` → ``payOutsRecords``), резолвит
категорию каждого изъятия по счёту-назначению (через `/v2/entities/accounts/list`):
``main_cash`` (инкассация в кассу Черниковой), ``courier_salary`` (ЗП курьеров),
``alisa`` (наличные у партнёра, дебиторка УДКЗ), ``unknown`` (прочее).

Эта стадия — ТОЛЬКО витрина: создаёт ``IikoCashShift`` + ``IikoCashShiftPayout``, но
движения ДДС НЕ книжит (это отдельная стадия — авто-проводка наличного контура). Идёт
через ``http.client`` напрямую (legacy IikoClient блокируется iiko WAF в части окружений),
по образцу ``couriers/iiko_olap_sync``. Смену сохраняем по наличию ``closeDate``, а НЕ по
``sessionStatus`` — в проде закрытые смены приходят со статусом ``UNACCEPTED``.

Рядом с синком живут два наблюдателя за инкассацией, которые в БД ничего не пишут:

* ``fetch_open_shift`` — витрина ТЕКУЩЕЙ незакрытой смены. Синк её не видит (запрос идёт
  со ``status=CLOSED``), поэтому инкассация появляется в системе только вечером, после
  закрытия смены. Живая проверка 20.08.2026 на боевом iiko: ``/v2/cashshifts/list`` со
  ``status=OPEN`` отдаёт открытую смену, а ``payments/list`` по ней — её изъятия. Читаем
  их напрямую, чтобы в течение дня было видно, проводили инкассацию или нет; в ДДС
  по-прежнему книжится только закрытая смена (иначе задвоение с ``posted``-контуром).
* ``compute_uncollected_cash`` — сигнал «деньги зависли в ящике»: закрытая смена оставила
  остаток больше флоута, которым её открыли. Так выглядит забытая (или неполная)
  инкассация — инцидент 19.08.2026, смена 1206: 2 821,83 ₽ остались на ночь в кассе.
"""

from __future__ import annotations

import http.client as hc
import json
import logging
import os
import ssl
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import anyio.to_thread
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AppSetting,
    CashflowTransaction,
    DdsArticle,
    Employee,
    IikoCashShift,
    IikoCashShiftPayout,
    KassaShiftPenalty,
    PayrollAdjustment,
    ShiftLedgerEntry,
    Wallet,
)
from app.services.payroll_adjustment_service import is_production_date_locked
from app.services.staff_taxonomy import CASHIER_PAYROLL_ROLES

logger = logging.getLogger(__name__)

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# Счета-назначения изъятий iiko → категория проводки (см. project_dds_iiko_cash_circuit).
MAIN_CASH_ACCOUNT_ID = "8ccc8f0f-24f6-64d2-5eea-04f829ba381f"  # Главная касса (ТК Черникова)
COURIER_SALARY_ACCOUNT_ID = "02b27b89-cf8c-482f-b428-34cf95258f94"  # Зарплата курьеров
ALISA_ACCOUNT_ID = "3f21b36a-a866-4208-bc9e-21444d9c2a29"  # Алиса наличные (дебиторка партнёра)

_CATEGORY_BY_ACCOUNT = {
    MAIN_CASH_ACCOUNT_ID: "main_cash",
    COURIER_SALARY_ACCOUNT_ID: "courier_salary",
    ALISA_ACCOUNT_ID: "alisa",
}
# Имена трёх известных счетов — чтобы витрина открытой смены не ходила за справочником
# счетов ради подписи (лишний запрос к iiko на каждое открытие вкладки).
_ACCOUNT_NAME_BY_ID = {
    MAIN_CASH_ACCOUNT_ID: "Главная касса",
    COURIER_SALARY_ACCOUNT_ID: "Зарплата курьеров",
    ALISA_ACCOUNT_ID: "Алиса наличные",
}

# Кошелёк наличного контура и статьи ДДС (резолвятся по ИМЕНИ — code/uuid различны dev/prod).
CASH_WALLET_CODE = "tk_chernikova"
CASH_REVENUE_ARTICLE = "Поступление денег с торг. точек"
COURIER_PAYOUT_ARTICLE = "Курьерская служба -"
CASH_ADJUSTMENT_PLUS_ARTICLE = "Корретировки кассы +"
CASH_ADJUSTMENT_MINUS_ARTICLE = "Корретировки кассы -"

# source_kind движений ДДС, порождённых сменой.
SHIFT_SOURCE_KIND = "kassa_cashshift"
ADJUSTMENT_SOURCE_KIND = "kassa_cashshift_adjustment"

# --- авто-штраф кассирам за недостачу смены (Фаза 10) ---------------------------
MONEY = Decimal("0.01")
# Порог недостачи (% от всей выручки смены), ниже которого штрафа нет. Настройка.
SHORTAGE_THRESHOLD_PCT_KEY = "kassa.shortage_penalty_threshold_pct"
DEFAULT_SHORTAGE_THRESHOLD_PCT = Decimal("1.0")
# Должность, в роль которой садится штраф (попадает в недельную ведомость кассира).
CASHIER_POSITION = "Кассир"
# Итоги авто-штрафа смены (поле iiko_cash_shift.penalty_status).
PENALTY_STATUS_NONE = "none"
PENALTY_STATUS_APPLIED = "applied"
PENALTY_STATUS_WAIVED = "waived"
PENALTY_STATUS_MANUAL_REVIEW = "manual_review"

# --- сигнал «наличка зависла в ящике» (забытая/неполная инкассация) --------------
# Сколько рублей сверх стартового флоута может остаться в кассе закрытой смены, прежде
# чем это считается пропущенной инкассацией. Настройка (Настройки → Касса).
STUCK_CASH_THRESHOLD_KEY = "kassa.stuck_cash_threshold_rub"
DEFAULT_STUCK_CASH_THRESHOLD = Decimal("1000")
# Итог проверки инкассации по смене (вычисляемое поле витрины, в БД не хранится).
UNCOLLECTED_NONE = "none"  # ящик закрыли на флоуте — инкассировать было нечего или всё вынесли
UNCOLLECTED_PARTIAL = "partial"  # инкассация была, но сверх флоута всё равно осталось > порога
UNCOLLECTED_MISSING = "missing"  # инкассации не было вовсе, а деньги в ящике остались


@dataclass(slots=True)
class CashShiftSyncReport:
    fetched: int = 0
    created: int = 0
    updated: int = 0
    payouts: int = 0
    posted: int = 0
    penalized: int = 0
    skipped: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "fetched": self.fetched,
            "created": self.created,
            "updated": self.updated,
            "payouts": self.payouts,
            "posted": self.posted,
            "penalized": self.penalized,
            "skipped": self.skipped,
        }


def _payout_category(account_id: str) -> str:
    return _CATEGORY_BY_ACCOUNT.get(account_id, "unknown")


# --- iiko HTTP (http.client напрямую, в обход WAF/прокси) ----------------------


def _iiko_host_and_port() -> tuple[str, int]:
    base = os.environ.get("IIKO_SERVER_BASE_URL", "").strip()
    if not base:
        raise RuntimeError("IIKO_SERVER_BASE_URL is missing")
    parsed = urllib.parse.urlparse(base if "://" in base else f"https://{base}")
    return parsed.hostname or "", parsed.port or 443


def _auth_token() -> str:
    host, port = _iiko_host_and_port()
    login = os.environ.get("IIKO_SERVER_LOGIN", "").strip()
    password_sha1 = os.environ.get("IIKO_SERVER_PASSWORD_SHA1", "").strip()
    if not password_sha1 and (raw := os.environ.get("IIKO_SERVER_PASSWORD", "").strip()):
        import hashlib

        password_sha1 = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    if not login or not password_sha1:
        raise RuntimeError("IIKO_SERVER_LOGIN / PASSWORD env missing")
    ctx = ssl._create_unverified_context()
    conn = hc.HTTPSConnection(host, port, timeout=30, context=ctx)
    try:
        query = urllib.parse.urlencode({"login": login, "pass": password_sha1})
        conn.request("GET", f"/resto/api/auth?{query}")
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", "replace").strip()
        if resp.status != 200 or not body or "<" in body or "\n" in body:
            raise RuntimeError(f"iiko auth failed: {resp.status} {body[:200]}")
        return body
    finally:
        conn.close()


def _iiko_get(token: str, path: str, params: dict[str, str]) -> Any:
    host, port = _iiko_host_and_port()
    query = urllib.parse.urlencode({**params, "key": token})
    ctx = ssl._create_unverified_context()
    conn = hc.HTTPSConnection(host, port, timeout=120, context=ctx)
    try:
        conn.request("GET", f"{path}?{query}")
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"iiko GET {path} failed: {resp.status} {raw[:300]!r}")
        return json.loads(raw)
    finally:
        conn.close()


def _fetch_cashshifts_list(token: str, date_from: date, date_to: date) -> list[dict[str, Any]]:
    data = _iiko_get(
        token,
        "/resto/api/v2/cashshifts/list",
        {
            "openDateFrom": date_from.isoformat(),
            "openDateTo": date_to.isoformat(),
            "status": "CLOSED",
        },
    )
    return data if isinstance(data, list) else []


def _fetch_open_cashshifts(token: str, date_from: date, date_to: date) -> list[dict[str, Any]]:
    """Открытые (ещё не закрытые) смены. Тот же эндпоинт, что и у синка, но ``status=OPEN``.

    Параметр ``status`` у iiko обязателен: с пустым значением ручка отвечает 409
    «Параметр 'status' не может быть пустым» (проверено на боевом сервере 20.08.2026).
    """
    data = _iiko_get(
        token,
        "/resto/api/v2/cashshifts/list",
        {
            "openDateFrom": date_from.isoformat(),
            "openDateTo": date_to.isoformat(),
            "status": "OPEN",
        },
    )
    return data if isinstance(data, list) else []


def _fetch_shift_payments(token: str, session_id: str) -> dict[str, Any]:
    data = _iiko_get(
        token,
        f"/resto/api/v2/cashshifts/payments/list/{session_id}",
        {"hideAccepted": "false"},
    )
    return data if isinstance(data, dict) else {}


def _fetch_accounts_map(token: str) -> dict[str, str]:
    data = _iiko_get(token, "/resto/api/v2/entities/accounts/list", {"includeDeleted": "false"})
    rows = data if isinstance(data, list) else []
    return {
        row["id"]: row.get("name", "") for row in rows if isinstance(row, dict) and row.get("id")
    }


def _iiko_post(token: str, path: str, body: dict[str, Any]) -> Any:
    host, port = _iiko_host_and_port()
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    ctx = ssl._create_unverified_context()
    conn = hc.HTTPSConnection(host, port, timeout=120, context=ctx)
    try:
        conn.request(
            "POST",
            f"{path}?key={token}",
            body=payload,
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"iiko POST {path} failed: {resp.status} {raw[:300]!r}")
        return json.loads(raw)
    finally:
        conn.close()


def _fetch_cash_sales_by_day(
    token: str, date_from: date, date_to: date
) -> tuple[dict[date, Decimal], dict[date, Decimal]]:
    """Выручка по дням из OLAP SALES: наличная (тип «Наличные») и ВСЯ (все типы оплат).

    iiko `salesCash`/`salesCard` кассовой смены ненадёжны (нал завышен зачтёнными
    предоплатами), поэтому обе берём из одного OLAP-отчёта по типам оплат
    (`PayTypes.Group`, сумма со скидкой). Наличная — база расхождения кассы; вся выручка
    (нал + безнал + прочее) — база порога авто-штрафа за недостачу.

    Возвращает ``(cash_by_day, total_by_day)``.
    """
    body = {
        "reportType": "SALES",
        "buildSummary": False,
        "groupByRowFields": ["OpenDate.Typed", "PayTypes.Group"],
        "groupByColFields": [],
        "aggregateFields": ["DishDiscountSumInt"],
        "filters": {
            "OpenDate.Typed": {
                "filterType": "DateRange",
                "periodType": "CUSTOM",
                "from": f"{date_from.isoformat()}T00:00:00.000",
                "to": f"{(date_to + timedelta(days=1)).isoformat()}T00:00:00.000",
                "includeLow": True,
                "includeHigh": False,
            }
        },
    }
    data = _iiko_post(token, "/resto/api/v2/reports/olap", body)
    rows = data.get("data", []) if isinstance(data, dict) else []
    cash: dict[date, Decimal] = {}
    total: dict[date, Decimal] = {}
    for row in rows:
        day = _parse_date(row.get("OpenDate.Typed"))
        if day is None:
            continue
        amount = _dec(row.get("DishDiscountSumInt")) or Decimal("0")
        total[day] = total.get(day, Decimal("0")) + amount
        if str(row.get("PayTypes.Group") or "") == "CASH":
            cash[day] = cash.get(day, Decimal("0")) + amount
    return cash, total


# --- parsing helpers -----------------------------------------------------------


def _parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = (
            datetime.fromisoformat(text)
            if "T" in text
            else datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        )
    except ValueError:
        return None
    return parsed.replace(tzinfo=MOSCOW_TZ) if parsed.tzinfo is None else parsed


def _parse_date(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _shift_values(row: dict[str, Any], close_date: datetime) -> dict[str, Any]:
    return {
        "session_number": row.get("sessionNumber"),
        "point_of_sale_id": row.get("pointOfSaleId"),
        "manager_id": row.get("managerId"),
        "open_date": _parse_dt(row.get("openDate")),
        "close_date": close_date,
        "session_status": row.get("sessionStatus"),
        "session_start_cash": _dec(row.get("sessionStartCash")),
        "sales_cash": _dec(row.get("salesCash")),
        "sales_card": _dec(row.get("salesCard")),
        "pay_in": _dec(row.get("payIn")),
        "pay_out": _dec(row.get("payOut")),
        "cash_remain": _dec(row.get("cashRemain")),
        "cash_diff": _dec(row.get("cashDiff")),
        "raw": row,
        "synced_at": datetime.now(tz=MOSCOW_TZ),
    }


# --- sync ----------------------------------------------------------------------


async def sync_iiko_cashshifts(
    session: AsyncSession, *, date_from: date, date_to: date
) -> CashShiftSyncReport:
    """Загрузить закрытые смены iiko за период, обновить витрину и провести наличный
    контур в ДДС (Модель A: приход «Поступление денег с торг. точек» = инкассация+курьеры
    на ТК Черникова, расход «Курьерская служба -» оттуда же; Алиса → УДКЗ без движения)."""
    token = _auth_token()
    shifts = _fetch_cashshifts_list(token, date_from, date_to)
    report = CashShiftSyncReport(fetched=len(shifts))
    # Достоверная выручка по дням (OLAP по типам оплат), не из salesCash/salesCard:
    # наличная — для расхождения, полная — для порога авто-штрафа.
    cash_by_day, total_by_day = _fetch_cash_sales_by_day(token, date_from, date_to)

    session_ids = [row.get("id") for row in shifts if row.get("id")]
    existing: dict[str, IikoCashShift] = {}
    if session_ids:
        rows = await session.scalars(
            select(IikoCashShift).where(IikoCashShift.iiko_session_id.in_(session_ids))
        )
        existing = {shift.iiko_session_id: shift for shift in rows.all()}

    accounts: dict[str, str] | None = None
    for row in shifts:
        session_id = row.get("id")
        close_date = _parse_dt(row.get("closeDate"))
        if not session_id or close_date is None:
            # Смена без даты закрытия — не закрыта, пропускаем (фильтр по closeDate).
            report.skipped += 1
            continue

        values = _shift_values(row, close_date)
        shift = existing.get(session_id)
        if shift is None:
            shift = IikoCashShift(id=uuid.uuid4(), iiko_session_id=session_id, **values)
            session.add(shift)
            await session.flush()
            report.created += 1
        else:
            for key, value in values.items():
                setattr(shift, key, value)
            report.updated += 1

        # Выручка из OLAP по дню открытия смены (не из завышенного salesCash/salesCard).
        shift_day = (shift.open_date or shift.close_date).date()
        shift.cash_sales = cash_by_day.get(shift_day)
        shift.total_sales = total_by_day.get(shift_day)

        # Разнос изъятий и проводку делаем только пока смена не проведена в ДДС.
        if not shift.posted:
            if accounts is None:
                accounts = _fetch_accounts_map(token)
            await _sync_shift_payouts(session, shift, token, accounts, report)
            if await post_shift_cash_circuit(session, shift):
                report.posted += 1

        # Авто-штраф кассирам, если недостача > порога (идемпотентно по смене).
        if await apply_shortage_penalty(session, shift):
            report.penalized += 1

    await session.flush()
    logger.info(
        "iiko cashshift sync: fetched=%s created=%s updated=%s payouts=%s posted=%s "
        "penalized=%s skipped=%s",
        report.fetched,
        report.created,
        report.updated,
        report.payouts,
        report.posted,
        report.penalized,
        report.skipped,
    )
    return report


async def _sync_shift_payouts(
    session: AsyncSession,
    shift: IikoCashShift,
    token: str,
    accounts: dict[str, str],
    report: CashShiftSyncReport,
) -> None:
    payments = _fetch_shift_payments(token, shift.iiko_session_id)
    payouts = payments.get("payOutsRecords") or []
    # Идемпотентность: пересобираем строки изъятий смены (движений ДДС ещё нет).
    await session.execute(
        delete(IikoCashShiftPayout).where(IikoCashShiftPayout.shift_id == shift.id)
    )
    for record in payouts:
        info = record.get("info") or {}
        account_id = str(info.get("accountId") or "")
        amount = _dec(record.get("actualSum"))
        if amount is None:
            amount = _dec(info.get("sum")) or Decimal("0.00")
        session.add(
            IikoCashShiftPayout(
                shift_id=shift.id,
                iiko_payout_id=info.get("id"),
                account_id_iiko=account_id,
                account_name=accounts.get(account_id),
                category=_payout_category(account_id),
                amount=amount,
                comment=info.get("comment") or None,
                raw=record,
            )
        )
    report.payouts += len(payouts)


# --- витрина ТЕКУЩЕЙ (незакрытой) смены ----------------------------------------


def _open_shift_raw() -> dict[str, Any] | None:
    """Синхронная часть витрины открытой смены: смена, её изъятия и выручка дня.

    Все запросы к iiko собраны в одну функцию, чтобы уйти в поток одним прыжком
    (``anyio.to_thread``): ``http.client`` блокирующий, а ручка дёргается на каждое
    открытие вкладки «Смены», а не раз в 20 минут, как синк.

    Справочник счетов (`/v2/entities/accounts/list`) тянем ТОЛЬКО если среди изъятий
    попался незнакомый счёт: три штатных назначения подписываются из ``_ACCOUNT_NAME_BY_ID``.
    """
    token = _auth_token()
    today = datetime.now(tz=MOSCOW_TZ).date()
    # Смена открывается утром и закрывается вечером того же дня, но окно берём с запасом
    # в сутки в обе стороны — на случай ночного закрытия и расхождения часовых поясов.
    rows = _fetch_open_cashshifts(token, today - timedelta(days=1), today + timedelta(days=1))
    row = next(
        (item for item in rows if item.get("id") and not item.get("closeDate")),
        None,
    )
    if row is None:
        return None
    payments = _fetch_shift_payments(token, str(row["id"]))
    records = payments.get("payOutsRecords") or []
    accounts: dict[str, str] = {}
    if any(
        _payout_category(str((record.get("info") or {}).get("accountId") or "")) == "unknown"
        for record in records
    ):
        accounts = _fetch_accounts_map(token)
    open_date = _parse_dt(row.get("openDate"))
    day = open_date.date() if open_date is not None else today
    # Наличная выручка «на сейчас» — из OLAP по дню открытия, как и у закрытых смен
    # (salesCash смены завышен зачтёнными предоплатами).
    cash_by_day, _ = _fetch_cash_sales_by_day(token, day, day)
    return {
        "row": row,
        "records": records,
        "accounts": accounts,
        "cash_sales": cash_by_day.get(day),
    }


async def fetch_open_shift() -> dict[str, Any] | None:
    """Текущая незакрытая смена iiko для витрины «идёт». ``None`` — открытой смены нет.

    Только чтение: ни строки в БД, ни движения ДДС. Смысл — видеть в течение дня, сколько
    налички в ящике и проводили ли инкассацию, не дожидаясь вечернего закрытия смены.

    ``cash_in_drawer`` считаем сами, потому что ``cashRemain`` у открытой смены iiko не
    отдаёт (приходит ``null``). Формула — тождество, которое iiko выдерживает на закрытых
    сменах: ``старт + наличная выручка + внесения − изъятия`` (сверено на сменах 1200–1206
    прода: сходится копейка-в-копейку). Без выручки из OLAP остаток не считаем — врать
    приблизительной цифрой в кассовой витрине нельзя.
    """
    payload = await anyio.to_thread.run_sync(_open_shift_raw)
    if payload is None:
        return None

    row: dict[str, Any] = payload["row"]
    accounts: dict[str, str] = payload["accounts"]
    payouts: list[dict[str, Any]] = []
    for record in payload["records"]:
        info = record.get("info") or {}
        account_id = str(info.get("accountId") or "")
        amount = _dec(record.get("actualSum"))
        if amount is None:
            amount = _dec(info.get("sum")) or Decimal("0.00")
        payouts.append(
            {
                "iiko_payout_id": info.get("id"),
                "account_id_iiko": account_id,
                "account_name": accounts.get(account_id) or _ACCOUNT_NAME_BY_ID.get(account_id),
                "category": _payout_category(account_id),
                "amount": amount,
                "comment": info.get("comment") or None,
            }
        )
    payouts.sort(key=lambda item: item["amount"], reverse=True)

    start_cash = _dec(row.get("sessionStartCash"))
    pay_in = _dec(row.get("payIn")) or Decimal("0.00")
    # Итог изъятий берём у iiko, а не суммой строк: если в смене окажется изъятие,
    # которого нет в payOutsRecords, остаток ящика должен остаться правдой.
    pay_out = _dec(row.get("payOut"))
    if pay_out is None:
        pay_out = sum((item["amount"] for item in payouts), Decimal("0.00"))
    cash_sales: Decimal | None = payload["cash_sales"]
    cash_in_drawer = (
        start_cash + cash_sales + pay_in - pay_out
        if start_cash is not None and cash_sales is not None
        else None
    )
    collected = sum(
        (item["amount"] for item in payouts if item["category"] == "main_cash"), Decimal("0.00")
    )
    return {
        "iiko_session_id": str(row["id"]),
        "session_number": row.get("sessionNumber"),
        "point_of_sale_id": row.get("pointOfSaleId"),
        "open_date": _parse_dt(row.get("openDate")),
        "session_status": row.get("sessionStatus"),
        "session_start_cash": start_cash,
        "cash_sales": cash_sales,
        "pay_in": pay_in,
        "pay_out": pay_out,
        "cash_in_drawer": cash_in_drawer,
        "collected_cash": collected,
        "payouts": payouts,
        "fetched_at": datetime.now(tz=MOSCOW_TZ),
    }


# --- проводка наличного контура в ДДС ------------------------------------------


async def _article_id_by_name(session: AsyncSession, name: str) -> uuid.UUID:
    article_id = await session.scalar(
        select(DdsArticle.id).where(DdsArticle.name == name, DdsArticle.is_active.is_(True))
    )
    if article_id is None:
        raise RuntimeError(f"Статья ДДС не найдена: {name}")
    return article_id


async def _cash_wallet(session: AsyncSession) -> Wallet:
    wallet = await session.scalar(
        select(Wallet).where(Wallet.code == CASH_WALLET_CODE, Wallet.status == "active")
    )
    if wallet is None:
        raise RuntimeError(f"Кошелёк «{CASH_WALLET_CODE}» не найден")
    return wallet


def _shift_label(shift: IikoCashShift) -> str:
    return str(shift.session_number) if shift.session_number is not None else shift.iiko_session_id


def _shift_op_date(shift: IikoCashShift) -> date:
    moment = shift.close_date or shift.open_date or datetime.now(tz=MOSCOW_TZ)
    return moment.date()


async def post_shift_cash_circuit(session: AsyncSession, shift: IikoCashShift) -> bool:
    """Провести наличный контур смены в ДДС (commit-less, идемпотентно по ``posted``).

    Модель A: приход «Поступление денег с торг. точек» = Σmain_cash + Σcourier_salary на
    ТК Черникова; расход «Курьерская служба -» = Σcourier_salary оттуда же. Остаток на
    кассе = инкассация. Алиса/unknown — движений ДДС не создают (Алиса = дебиторка УДКЗ).
    Возвращает True, если провёл; False, если смена уже проведена.
    """
    if shift.posted:
        return False

    payouts = (
        await session.scalars(
            select(IikoCashShiftPayout).where(IikoCashShiftPayout.shift_id == shift.id)
        )
    ).all()
    main_total = sum((p.amount for p in payouts if p.category == "main_cash"), Decimal("0.00"))
    courier_total = sum(
        (p.amount for p in payouts if p.category == "courier_salary"), Decimal("0.00")
    )

    wallet = await _cash_wallet(session)
    op_date = _shift_op_date(shift)
    label = _shift_label(shift)

    inflow_txn: CashflowTransaction | None = None
    inflow_amount = main_total + courier_total
    if inflow_amount > 0:
        inflow_txn = CashflowTransaction(
            wallet_id=wallet.id,
            direction="in",
            amount=inflow_amount,
            operation_date=op_date,
            article_id=await _article_id_by_name(session, CASH_REVENUE_ARTICLE),
            source_kind=SHIFT_SOURCE_KIND,
            source_id=shift.id,
            payment_purpose=f"Наличная выручка смены {label}",
            quality_status="final",
        )
        session.add(inflow_txn)
        await session.flush()

    courier_txn: CashflowTransaction | None = None
    if courier_total > 0:
        courier_txn = CashflowTransaction(
            wallet_id=wallet.id,
            direction="out",
            amount=courier_total,
            operation_date=op_date,
            article_id=await _article_id_by_name(session, COURIER_PAYOUT_ARTICLE),
            source_kind=SHIFT_SOURCE_KIND,
            source_id=shift.id,
            payment_purpose=f"ЗП курьеров смены {label}",
            quality_status="final",
        )
        session.add(courier_txn)
        await session.flush()

    for payout in payouts:
        if payout.category == "main_cash" and inflow_txn is not None:
            payout.cashflow_transaction_id = inflow_txn.id
        elif payout.category == "courier_salary" and courier_txn is not None:
            payout.cashflow_transaction_id = courier_txn.id
        # alisa/unknown — без движения ДДС (Алиса учитывается в УДКЗ отдельно).

    shift.posted = True
    await session.flush()
    return True


async def post_shift_adjustment(session: AsyncSession, shift: IikoCashShift) -> CashflowTransaction:
    """Провести расхождение кассы (``cash_diff``) отдельной корректировкой ДДС.

    Точечная операция под правом ``kassa.adjustments.create`` — НЕ часть авто-проводки.
    ``cash_diff`` > 0 → приход «Корретировки кассы +»; < 0 → расход «Корретировки кассы -».
    Идемпотентно: повторная корректировка по той же смене запрещена.
    """
    diff = await compute_real_cash_diff(session, shift)
    if diff is None or diff == 0:
        raise RuntimeError("У смены нет реального расхождения кассы")
    existing = await session.scalar(
        select(CashflowTransaction.id)
        .where(
            CashflowTransaction.source_kind == ADJUSTMENT_SOURCE_KIND,
            CashflowTransaction.source_id == shift.id,
        )
        .limit(1)
    )
    if existing is not None:
        raise RuntimeError("Корректировка по смене уже проведена")

    wallet = await _cash_wallet(session)
    # diff > 0 — наличных в кассе меньше расчёта (недостача) → расход «Корретировки кассы -»;
    # diff < 0 — излишек → приход «Корретировки кассы +».
    if diff > 0:
        article_name, direction = CASH_ADJUSTMENT_MINUS_ARTICLE, "out"
    else:
        article_name, direction = CASH_ADJUSTMENT_PLUS_ARTICLE, "in"
    transaction = CashflowTransaction(
        wallet_id=wallet.id,
        direction=direction,
        amount=abs(diff),
        operation_date=_shift_op_date(shift),
        article_id=await _article_id_by_name(session, article_name),
        source_kind=ADJUSTMENT_SOURCE_KIND,
        source_id=shift.id,
        payment_purpose=f"Корректировка кассы смены {_shift_label(shift)}",
        quality_status="final",
    )
    session.add(transaction)
    await session.flush()
    return transaction


# --- недостача → авто-штраф кассирам (Фаза 10) ---------------------------------


async def _shortage_threshold_pct(session: AsyncSession) -> Decimal:
    """Порог авто-штрафа (% от всей выручки смены) из настройки, с дефолтом 1.0."""
    raw = await session.scalar(
        select(AppSetting.value).where(AppSetting.key == SHORTAGE_THRESHOLD_PCT_KEY)
    )
    if raw is None:
        return DEFAULT_SHORTAGE_THRESHOLD_PCT
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return DEFAULT_SHORTAGE_THRESHOLD_PCT


def _shift_penalty_work_date(shift: IikoCashShift) -> date | None:
    """Дата штрафа = дата ОТКРЫТИЯ смены (совпадает с work_date явок кассиров)."""
    moment = shift.open_date or shift.close_date
    return moment.date() if moment is not None else None


async def _shift_cashiers(session: AsyncSession, work_date: date) -> list[uuid.UUID]:
    """Кассиры смены = сотрудники с явкой роли «кассир» (``administrator``) на дату.

    Источник — ``ShiftLedgerEntry`` («Учёт смен»): в нём ``payroll_role`` заполнен явной
    ролью (в отличие от ``attendance_entry.role``, который в данных пуст). Порядок по
    ``employee_id`` детерминирован — для воспроизводимого делёжа остатка копеек.
    """
    rows = await session.scalars(
        select(ShiftLedgerEntry.employee_id)
        .where(
            ShiftLedgerEntry.work_date == work_date,
            ShiftLedgerEntry.payroll_role.in_(CASHIER_PAYROLL_ROLES),
        )
        .distinct()
        .order_by(ShiftLedgerEntry.employee_id)
    )
    return list(rows.all())


def _split_evenly(total: Decimal, n: int) -> list[Decimal]:
    """Поделить сумму поровну на ``n`` долей до копеек; остаток — первой доле."""
    total = total.quantize(MONEY)
    base = (total / n).quantize(MONEY, rounding=ROUND_DOWN)
    shares = [base] * n
    shares[0] = (base + (total - base * n)).quantize(MONEY)
    return shares


def _mark_penalty_review(shift: IikoCashShift, reason: str) -> bool:
    """Недостача > порога, но авто-штраф невозможен — пометить «ручной разбор»."""
    shift.penalty_status = PENALTY_STATUS_MANUAL_REVIEW
    shift.penalty_review_reason = reason
    return False


async def apply_shortage_penalty(session: AsyncSession, shift: IikoCashShift) -> bool:
    """Авто-штраф кассирам смены, если недостача > порога % от выручки. Идемпотентно.

    Недостача (``compute_real_cash_diff`` > 0) делится РОВНО поровну между кассирами
    смены (явка роли «кассир» на дату открытия). Каждому — ``PayrollAdjustment``
    (penalty) в недельную ведомость + строка ``KassaShiftPenalty`` для трассировки.
    Повторный синк не дублирует и не воскрешает отменённые штрафы (барьер — наличие
    строк ``kassa_shift_penalty`` по смене). Возвращает ``True``, если создал штрафы
    в этом вызове.

    Если недостача > порога, но штраф нельзя создать автоматически (нет кассиров на
    дату / период зарплаты зафиксирован / нет данных о выручке) — смена помечается
    ``penalty_status='manual_review'`` и штраф не создаётся.
    """
    existing = (
        await session.scalars(
            select(KassaShiftPenalty).where(KassaShiftPenalty.shift_id == shift.id)
        )
    ).all()
    if existing:
        # Уже разобрано: не пересоздаём и не воскрешаем waived — только синхронизируем итог.
        shift.penalty_status = (
            PENALTY_STATUS_WAIVED
            if all(penalty.status == "waived" for penalty in existing)
            else PENALTY_STATUS_APPLIED
        )
        return False

    diff = await compute_real_cash_diff(session, shift)
    if diff is None or diff <= 0:
        shift.penalty_status = PENALTY_STATUS_NONE
        shift.penalty_review_reason = None
        return False

    if shift.total_sales is None or shift.total_sales <= 0:
        return _mark_penalty_review(shift, "Нет данных о выручке смены для расчёта порога")

    pct = await _shortage_threshold_pct(session)
    threshold = (shift.total_sales * pct / Decimal("100")).quantize(MONEY)
    if diff <= threshold:
        shift.penalty_status = PENALTY_STATUS_NONE
        shift.penalty_review_reason = None
        return False

    work_date = _shift_penalty_work_date(shift)
    if work_date is None:
        return _mark_penalty_review(shift, "У смены нет даты открытия")
    if await is_production_date_locked(session, work_date):
        return _mark_penalty_review(
            shift, "Зарплатный период зафиксирован — назначьте штраф вручную"
        )

    cashiers = await _shift_cashiers(session, work_date)
    if not cashiers:
        return _mark_penalty_review(
            shift, "Не найдены кассиры смены на дату — распределите штраф вручную"
        )

    shares = _split_evenly(diff, len(cashiers))
    label = _shift_label(shift)
    now = datetime.now(tz=MOSCOW_TZ)
    created = 0
    for employee_id, amount in zip(cashiers, shares, strict=True):
        if amount <= 0:
            continue
        adjustment = PayrollAdjustment(
            employee_id=employee_id,
            work_date=work_date,
            role=CASHIER_POSITION,
            type="penalty",
            category_id=None,
            custom_label=f"Недостача кассы (смена № {label})",
            amount=amount,
            comment=(
                f"Авто-штраф: недостача смены {diff.quantize(MONEY)} ₽ "
                f"> порога {threshold} ₽ ({pct}% выручки), делёж на {len(cashiers)}"
            ),
            created_by_user_id=None,
            created_by_label="Касса (авто)",
            created_at=now,
            updated_at=now,
        )
        session.add(adjustment)
        await session.flush()
        session.add(
            KassaShiftPenalty(
                shift_id=shift.id,
                employee_id=employee_id,
                payroll_adjustment_id=adjustment.id,
                amount=amount,
                status="active",
            )
        )
        created += 1

    if created == 0:
        # Все доли вышли нулевыми (вырожденный случай) — на ручной разбор.
        return _mark_penalty_review(shift, "Недостача не делится на доли > 0")

    shift.penalty_status = PENALTY_STATUS_APPLIED
    shift.penalty_review_reason = None
    await session.flush()
    return True


async def waive_shift_penalty(
    session: AsyncSession, shift: IikoCashShift, *, actor_user_id: uuid.UUID | None
) -> int:
    """Отменить авто-штрафы смены: удалить ``PayrollAdjustment`` и пометить waived.

    Точечная операция под правом ``kassa.penalty.waive``. Удаление ``PayrollAdjustment``
    убирает штраф из расчёта зарплаты (расчёт читает корректировки по дате в реальном
    времени). Работает независимо от величины недостачи (в т. ч. > порога). Возвращает
    число отменённых штрафов; ``RuntimeError``, если отменять нечего или период
    зарплаты зафиксирован.
    """
    penalties = (
        await session.scalars(
            select(KassaShiftPenalty).where(
                KassaShiftPenalty.shift_id == shift.id,
                KassaShiftPenalty.status == "active",
            )
        )
    ).all()
    if not penalties:
        raise RuntimeError("У смены нет активных авто-штрафов")

    work_date = _shift_penalty_work_date(shift)
    if work_date is not None and await is_production_date_locked(session, work_date):
        raise RuntimeError("Зарплатный период зафиксирован — отмена невозможна")

    now = datetime.now(tz=MOSCOW_TZ)
    for penalty in penalties:
        if penalty.payroll_adjustment_id is not None:
            adjustment = await session.get(PayrollAdjustment, penalty.payroll_adjustment_id)
            penalty.payroll_adjustment_id = None
            if adjustment is not None:
                await session.delete(adjustment)
        penalty.status = "waived"
        penalty.waived_by_user_id = actor_user_id
        penalty.waived_at = now
    shift.penalty_status = PENALTY_STATUS_WAIVED
    await session.flush()
    return len(penalties)


# --- read (витрина) ------------------------------------------------------------


async def compute_real_cash_diff(session: AsyncSession, shift: IikoCashShift) -> Decimal | None:
    """Реальное расхождение кассы (сверка денежного ящика смены, методология владельца):

    ``наличная выручка + внесения − изъятия − изменение остатка``,
    где изменение остатка = ``остаток − старт флоута``. Наличная выручка — из ``cash_sales``
    (OLAP, тип «Наличные»), НЕ из завышенного iiko ``salesCash``.

    Положительное = недостача (физически налички меньше, чем должно), отрицательное = излишек.
    Изменение остатка вычитается, чтобы разгрузка/пополнение флоута не считались недостачей
    (см. смену 1140: курьеров доплатили из остатка → это не расхождение). Алиса сокращается
    сама: она входит и в наличную выручку, и в изъятия (физически в кассу не приходила).

    Наличные чеки (``kassa_cheque``) в сверку смены НЕ входят: изымать из кассового ящика
    смены запрещено, такие покупки всегда оплачиваются из ТК Черникова (= Главная касса) и
    уже учтены как расход ДДС с этого счёта — к ящику кассирской смены отношения не имеют.
    """
    if (
        shift.cash_sales is None
        or shift.pay_out is None
        or shift.cash_remain is None
        or shift.session_start_cash is None
    ):
        return None
    pay_in = shift.pay_in or Decimal("0")
    float_change = shift.cash_remain - shift.session_start_cash
    return shift.cash_sales + pay_in - shift.pay_out - float_change


# --- сигнал «наличка зависла в ящике» ------------------------------------------


async def _stuck_cash_threshold(session: AsyncSession) -> Decimal:
    """Порог зависшей налички (₽ сверх стартового флоута) из настройки, с дефолтом."""
    raw = await session.scalar(
        select(AppSetting.value).where(AppSetting.key == STUCK_CASH_THRESHOLD_KEY)
    )
    if raw is None:
        return DEFAULT_STUCK_CASH_THRESHOLD
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return DEFAULT_STUCK_CASH_THRESHOLD


def compute_uncollected_cash(shift: IikoCashShift) -> Decimal | None:
    """Наличные, оставшиеся в ящике смены сверх флоута, которым её открыли.

    Инкассация — единственный штатный способ вынуть выручку из денежного ящика в Главную
    кассу (см. правило владельца в ``project_dds_iiko_cash_circuit``: из торговой кассы
    платят только курьерам). Поэтому остаток выше стартового флоута читается однозначно:
    инкассацию либо забыли, либо провели не на всю наличку.

    Отрицательное значение — ящик, наоборот, разгрузили (вынесли вчерашний хвост); это не
    сигнал, а норма следующего дня после пропуска.
    """
    if shift.cash_remain is None or shift.session_start_cash is None:
        return None
    return shift.cash_remain - shift.session_start_cash


def uncollected_status(uncollected: Decimal | None, collected: Decimal, threshold: Decimal) -> str:
    """Итог проверки инкассации смены: ``none`` / ``partial`` / ``missing``."""
    if uncollected is None or uncollected <= threshold:
        return UNCOLLECTED_NONE
    return UNCOLLECTED_MISSING if collected <= 0 else UNCOLLECTED_PARTIAL


async def _collected_totals(
    session: AsyncSession, shift_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Decimal]:
    """Σ инкассации (``main_cash``) по сменам — одним запросом на весь список."""
    if not shift_ids:
        return {}
    rows = await session.execute(
        select(IikoCashShiftPayout.shift_id, func.sum(IikoCashShiftPayout.amount))
        .where(
            IikoCashShiftPayout.shift_id.in_(shift_ids),
            IikoCashShiftPayout.category == "main_cash",
        )
        .group_by(IikoCashShiftPayout.shift_id)
    )
    return {shift_id: total or Decimal("0.00") for shift_id, total in rows.all()}


async def _shift_summary(
    session: AsyncSession,
    shift: IikoCashShift,
    *,
    collected: Decimal | None = None,
    stuck_threshold: Decimal | None = None,
) -> dict[str, Any]:
    if collected is None:
        collected = (await _collected_totals(session, [shift.id])).get(shift.id, Decimal("0.00"))
    if stuck_threshold is None:
        stuck_threshold = await _stuck_cash_threshold(session)
    uncollected = compute_uncollected_cash(shift)
    return {
        "id": shift.id,
        "iiko_session_id": shift.iiko_session_id,
        "session_number": shift.session_number,
        "point_of_sale_id": shift.point_of_sale_id,
        "open_date": shift.open_date,
        "close_date": shift.close_date,
        "session_status": shift.session_status,
        "session_start_cash": shift.session_start_cash,
        "sales_cash": shift.sales_cash,
        "cash_sales": shift.cash_sales,
        "sales_card": shift.sales_card,
        "total_sales": shift.total_sales,
        "pay_in": shift.pay_in,
        "pay_out": shift.pay_out,
        "cash_remain": shift.cash_remain,
        "cash_diff": shift.cash_diff,
        "real_cash_diff": await compute_real_cash_diff(session, shift),
        "collected_cash": collected,
        "uncollected_cash": uncollected,
        "uncollected_status": uncollected_status(uncollected, collected, stuck_threshold),
        "posted": shift.posted,
        "penalty_status": shift.penalty_status,
        "synced_at": shift.synced_at,
    }


async def list_shifts(
    session: AsyncSession, *, date_from: date | None = None, date_to: date | None = None
) -> list[dict[str, Any]]:
    conditions = []
    if date_from is not None:
        conditions.append(
            IikoCashShift.close_date
            >= datetime.combine(date_from, datetime.min.time(), tzinfo=MOSCOW_TZ)
        )
    if date_to is not None:
        conditions.append(
            IikoCashShift.close_date
            <= datetime.combine(date_to + timedelta(days=1), datetime.min.time(), tzinfo=MOSCOW_TZ)
        )
    shifts = (
        await session.scalars(
            select(IikoCashShift).where(*conditions).order_by(IikoCashShift.close_date.desc())
        )
    ).all()
    # Порог и суммы инкассации берём разом на весь список — иначе два запроса на смену.
    stuck_threshold = await _stuck_cash_threshold(session)
    collected = await _collected_totals(session, [shift.id for shift in shifts])
    return [
        await _shift_summary(
            session,
            shift,
            collected=collected.get(shift.id, Decimal("0.00")),
            stuck_threshold=stuck_threshold,
        )
        for shift in shifts
    ]


async def get_shift(session: AsyncSession, shift_id: uuid.UUID) -> dict[str, Any] | None:
    shift = await session.get(IikoCashShift, shift_id)
    if shift is None:
        return None
    payouts = (
        await session.scalars(
            select(IikoCashShiftPayout)
            .where(IikoCashShiftPayout.shift_id == shift.id)
            .order_by(IikoCashShiftPayout.amount.desc())
        )
    ).all()
    stuck_threshold = await _stuck_cash_threshold(session)
    payload = await _shift_summary(
        session,
        shift,
        collected=sum(
            (payout.amount for payout in payouts if payout.category == "main_cash"),
            Decimal("0.00"),
        ),
        stuck_threshold=stuck_threshold,
    )
    payload["uncollected_threshold"] = stuck_threshold
    payload["payouts"] = [
        {
            "id": payout.id,
            "account_id_iiko": payout.account_id_iiko,
            "account_name": payout.account_name,
            "category": payout.category,
            "amount": payout.amount,
            "comment": payout.comment,
        }
        for payout in payouts
    ]
    payload.update(await _shift_penalty_payload(session, shift))
    return payload


async def _shift_penalty_payload(session: AsyncSession, shift: IikoCashShift) -> dict[str, Any]:
    """Блок авто-штрафа смены для детали: порог, % недостачи, список оштрафованных."""
    pct = await _shortage_threshold_pct(session)
    diff = await compute_real_cash_diff(session, shift)
    threshold = (
        (shift.total_sales * pct / Decimal("100")).quantize(MONEY)
        if shift.total_sales is not None and shift.total_sales > 0
        else None
    )
    shortage_pct = (
        (diff / shift.total_sales * Decimal("100")).quantize(Decimal("0.01"))
        if diff is not None and shift.total_sales is not None and shift.total_sales > 0
        else None
    )
    rows = (
        await session.execute(
            select(KassaShiftPenalty, Employee.full_name)
            .join(Employee, Employee.id == KassaShiftPenalty.employee_id)
            .where(KassaShiftPenalty.shift_id == shift.id)
            .order_by(KassaShiftPenalty.created_at)
        )
    ).all()
    return {
        "penalty_status": shift.penalty_status,
        "penalty_review_reason": shift.penalty_review_reason,
        "shortage_threshold_pct": pct,
        "shortage_threshold_amount": threshold,
        "shortage_pct_of_revenue": shortage_pct,
        "penalties": [
            {
                "id": penalty.id,
                "employee_id": penalty.employee_id,
                "employee_full_name": full_name,
                "amount": penalty.amount,
                "status": penalty.status,
                "waived_at": penalty.waived_at,
            }
            for penalty, full_name in rows
        ],
    }
