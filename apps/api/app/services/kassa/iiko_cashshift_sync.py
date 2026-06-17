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
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CashflowTransaction,
    DdsArticle,
    IikoCashShift,
    IikoCashShiftPayout,
    InvoicePaymentAllocation,
    SupplierInvoice,
    Wallet,
)

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

# Кошелёк наличного контура и статьи ДДС (резолвятся по ИМЕНИ — code/uuid различны dev/prod).
CASH_WALLET_CODE = "tk_chernikova"
CASH_REVENUE_ARTICLE = "Поступление денег с торг. точек"
COURIER_PAYOUT_ARTICLE = "Курьерская служба -"
CASH_ADJUSTMENT_PLUS_ARTICLE = "Корретировки кассы +"
CASH_ADJUSTMENT_MINUS_ARTICLE = "Корретировки кассы -"

# source_kind движений ДДС, порождённых сменой.
SHIFT_SOURCE_KIND = "kassa_cashshift"
ADJUSTMENT_SOURCE_KIND = "kassa_cashshift_adjustment"


@dataclass(slots=True)
class CashShiftSyncReport:
    fetched: int = 0
    created: int = 0
    updated: int = 0
    payouts: int = 0
    posted: int = 0
    skipped: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "fetched": self.fetched,
            "created": self.created,
            "updated": self.updated,
            "payouts": self.payouts,
            "posted": self.posted,
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


def _fetch_cash_sales_by_day(token: str, date_from: date, date_to: date) -> dict[date, Decimal]:
    """Достоверная наличная выручка по дням из OLAP SALES (тип оплаты «Наличные»).

    iiko `salesCash` кассовой смены завышен зачтёнными предоплатами — поэтому наличную
    выручку берём из OLAP по типам оплат (`PayTypes.Group='CASH'`, сумма со скидкой).
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
    result: dict[date, Decimal] = {}
    for row in rows:
        if str(row.get("PayTypes.Group") or "") != "CASH":
            continue
        day = _parse_date(row.get("OpenDate.Typed"))
        if day is None:
            continue
        amount = _dec(row.get("DishDiscountSumInt")) or Decimal("0")
        result[day] = result.get(day, Decimal("0")) + amount
    return result


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
    # Достоверная наличная выручка по дням (OLAP по типам оплат), не из salesCash.
    cash_by_day = _fetch_cash_sales_by_day(token, date_from, date_to)

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

        # Наличная выручка из OLAP по дню открытия смены (не из завышенного salesCash).
        shift_day = (shift.open_date or shift.close_date).date()
        shift.cash_sales = cash_by_day.get(shift_day)

        # Разнос изъятий и проводку делаем только пока смена не проведена в ДДС.
        if not shift.posted:
            if accounts is None:
                accounts = _fetch_accounts_map(token)
            await _sync_shift_payouts(session, shift, token, accounts, report)
            if await post_shift_cash_circuit(session, shift):
                report.posted += 1

    await session.flush()
    logger.info(
        "iiko cashshift sync: fetched=%s created=%s updated=%s payouts=%s posted=%s skipped=%s",
        report.fetched,
        report.created,
        report.updated,
        report.payouts,
        report.posted,
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
    main_total = sum(
        (p.amount for p in payouts if p.category == "main_cash"), Decimal("0.00")
    )
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


# --- read (витрина) ------------------------------------------------------------


async def _cash_cheque_total(session: AsyncSession, work_date: date) -> Decimal:
    """Сумма наличных частей чеков (``kassa_cheque``) за день — «прочие наличные расходы по
    кассе»: админ взял наличные из дневной выручки и купил, оформив чек."""
    total = await session.scalar(
        select(func.coalesce(func.sum(InvoicePaymentAllocation.amount), 0))
        .select_from(InvoicePaymentAllocation)
        .join(SupplierInvoice, SupplierInvoice.id == InvoicePaymentAllocation.invoice_id)
        .where(
            SupplierInvoice.source == "kassa_cheque",
            SupplierInvoice.invoice_date == work_date,
            InvoicePaymentAllocation.source_kind == "cash",
        )
    )
    return Decimal(str(total or 0))


async def compute_real_cash_diff(session: AsyncSession, shift: IikoCashShift) -> Decimal | None:
    """Реальное расхождение кассы (сверка денежного ящика, методология владельца):

    ``наличная выручка + внесения − изъятия − наличные чеки − изменение остатка``,
    где изменение остатка = ``остаток − старт флоута``. Наличная выручка — из ``cash_sales``
    (OLAP, тип «Наличные»), НЕ из завышенного iiko ``salesCash``.

    Положительное = недостача (физически налички меньше, чем должно), отрицательное = излишек.
    Изменение остатка вычитается, чтобы разгрузка/пополнение флоута не считались недостачей
    (см. смену 1140: курьеров доплатили из остатка → это не расхождение). Алиса сокращается
    сама: она входит и в наличную выручку, и в изъятия (физически в кассу не приходила).
    """
    if (
        shift.cash_sales is None
        or shift.pay_out is None
        or shift.cash_remain is None
        or shift.session_start_cash is None
    ):
        return None
    pay_in = shift.pay_in or Decimal("0")
    moment = shift.close_date or shift.open_date
    cash_cheques = (
        await _cash_cheque_total(session, moment.date()) if moment is not None else Decimal("0")
    )
    float_change = shift.cash_remain - shift.session_start_cash
    return shift.cash_sales + pay_in - shift.pay_out - cash_cheques - float_change


async def _shift_summary(session: AsyncSession, shift: IikoCashShift) -> dict[str, Any]:
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
        "pay_in": shift.pay_in,
        "pay_out": shift.pay_out,
        "cash_remain": shift.cash_remain,
        "cash_diff": shift.cash_diff,
        "real_cash_diff": await compute_real_cash_diff(session, shift),
        "posted": shift.posted,
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
    return [await _shift_summary(session, shift) for shift in shifts]


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
    payload = await _shift_summary(session, shift)
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
    return payload
