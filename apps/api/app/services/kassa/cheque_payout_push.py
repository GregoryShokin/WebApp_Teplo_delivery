"""Проводка прочих (не-складских) расходов чека местного закупа в iiko изъятием.

Не-складские строки чека (питание/прочие затраты на персонал, содержание точек)
дублируются в iiko финансовой проводкой-изъятием ``POST /v2/payInOuts/addPayOut``. Тип
изъятия (``payInOutType``) уже инкапсулирует счёт-источник + корсчёт расхода + статью ДДС
iiko, поэтому достаточно выбрать нужный тип по структуре и передать сумму. Счёт-источник
зависит от способа оплаты доли: наличные → «Главная касса», карта → «Денежные средства,
эквайринг» (решение владельца, см. [[project_dds_iiko_cash_circuit]]).

Проводки iiko НЕОБРАТИМЫ через API (нет delete, нет внесения — addPayOut отвергает PAYIN),
поэтому идемпотентность держим таблицей ``kassa_cheque_iiko_payout`` (не задвоить
``posted``). Ошибка проведения НЕ отменяет уже созданный чек (как push накладной): строку
помечаем ``failed``, повтор не задвоит прошедшее.

iiko-доступ — синхронные ``http.client``-хелперы из ``iiko_cashshift_sync`` (в обход
прокси/WAF), вызываются прямо из async по образцу ``sync_iiko_cashshifts`` (операция редкая —
создание чека).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ChequeIikoPayout,
    DdsArticle,
    InvoiceLineItem,
    InvoicePaymentAllocation,
    SupplierInvoice,
)
from app.services.kassa.iiko_cashshift_sync import (
    _auth_token,
    _fetch_accounts_map,
    _iiko_get,
    _iiko_post,
)

logger = logging.getLogger(__name__)

# Подразделение проводки — «Foodmarket Тепло Черникова» (departmentSumMap).
CHERNIKOVA_DEPARTMENT_ID = "d8d4a22e-3abd-4f02-b82d-7d4712f32729"

# Счёт-источник изъятия по способу оплаты доли. Резолвится по ИМЕНИ (id dev/prod различны).
CHIEF_ACCOUNT_NAME_BY_SOURCE = {
    "cash": "Главная касса",
    "card": "Денежные средства, эквайринг",
}

# Наша статья чека (DdsArticle.name) → (корсчёт-имя iiko, код статьи ДДС iiko).
# Код различает две статьи под общим корсчётом «Затраты на персонал» — имя CashFlowCategory
# resto API не отдаёт (определено тестом 1 ₽ 2026-06-18, см. project_dds_iiko_cash_circuit).
ARTICLE_TO_IIKO: dict[str, tuple[str, str]] = {
    "Расходы на питание персонала": ("Затраты на персонал", "34"),
    "Расходы на персонал": ("Затраты на персонал", "3.1"),
    "Содержание торговых точек": ("Содержание торговых точек", "36"),
}

PAYOUT_TYPES_PATH = "/resto/api/v2/entities/payInOutTypes/list"
ADD_PAYOUT_PATH = "/resto/api/v2/payInOuts/addPayOut"


class ChequePayoutError(RuntimeError):
    """Доменная ошибка проведения изъятия чека в iiko."""


def _money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class ChequePayoutReport:
    posted: int = 0
    failed: int = 0
    skipped: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"posted": self.posted, "failed": self.failed, "skipped": self.skipped}


def _build_payout_type_index(
    accounts: dict[str, str], types: list[dict[str, Any]]
) -> dict[tuple[str, str, str], str]:
    """Индекс (chiefAccount-имя, account-имя, cfc.code) → payOutTypeId по PAYOUT-типам.

    Резолв по ИМЕНАМ счетов + коду статьи устойчив к различию id на dev/prod.
    """
    index: dict[tuple[str, str, str], str] = {}
    for entry in types:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("transactionType") or "").upper() != "PAYOUT":
            continue
        chief = accounts.get(str(entry.get("chiefAccount") or ""))
        account = accounts.get(str(entry.get("account") or ""))
        cfc = entry.get("cashFlowCategory")
        code = str(cfc.get("code")) if isinstance(cfc, dict) and cfc.get("code") is not None else ""
        type_id = entry.get("id")
        if chief and account and code and type_id:
            index[(chief, account, code)] = str(type_id)
    return index


def _resolve_pay_out_type(
    index: dict[tuple[str, str, str], str], article_name: str, source: str
) -> str | None:
    mapping = ARTICLE_TO_IIKO.get(article_name)
    chief_name = CHIEF_ACCOUNT_NAME_BY_SOURCE.get(source)
    if mapping is None or chief_name is None:
        return None
    account_name, cfc_code = mapping
    return index.get((chief_name, account_name, cfc_code))


def _add_pay_out(
    token: str, pay_out_type_id: str, pay_out_date: str, amount: Decimal, comment: str
) -> None:
    body = {
        "payOutTypeId": pay_out_type_id,
        "payOutDate": pay_out_date,
        "departmentSumMap": {CHERNIKOVA_DEPARTMENT_ID: float(amount)},
        "comment": comment,
    }
    data = _iiko_post(token, ADD_PAYOUT_PATH, body)
    result = data.get("result") if isinstance(data, dict) else None
    if result != "SUCCESS":
        raise ChequePayoutError(f"addPayOut вернул не SUCCESS: {data}")


def _split_by_source(
    article_total: Decimal, bank_total: Decimal, paid_total: Decimal
) -> list[tuple[str, Decimal]]:
    """Разнести сумму статьи по источникам пропорционально оплате (карта/наличные).

    card-доля = ``article_total · bank_total / paid_total``; cash-доля — остаток (чтобы
    Σ долей == ``article_total`` без потери копейки на округлении).
    """
    card_share = (
        _money(article_total * bank_total / paid_total) if bank_total > 0 else Decimal("0.00")
    )
    cash_share = _money(article_total - card_share)
    return [("card", card_share), ("cash", cash_share)]


async def _post_one(
    session: AsyncSession,
    invoice: SupplierInvoice,
    article: DdsArticle,
    source: str,
    amount: Decimal,
    pay_out_date: str,
    index: dict[tuple[str, str, str], str],
    token: str,
    report: ChequePayoutReport,
) -> None:
    """Провести одну долю (статья × источник) с идемпотентностью по таблице трекинга."""
    existing = await session.scalar(
        select(ChequeIikoPayout).where(
            ChequeIikoPayout.invoice_id == invoice.id,
            ChequeIikoPayout.dds_article_id == article.id,
            ChequeIikoPayout.source == source,
        )
    )
    if existing is not None and existing.status == "posted":
        report.skipped += 1
        return

    comment = f"Чек {invoice.number} · {article.name}"
    pay_out_type_id = _resolve_pay_out_type(index, article.name, source)
    status = "posted"
    error: str | None = None
    if pay_out_type_id is None:
        status, error = "failed", "Тип изъятия iiko не найден (счёт/статья не настроены)"
    else:
        try:
            _add_pay_out(token, pay_out_type_id, pay_out_date, amount, comment)
        except Exception as exc:  # noqa: BLE001 — фиксируем причину, чек не валим
            status, error = "failed", str(exc)[:500]
            logger.warning(
                "Чек %s: изъятие %s/%s не проведено в iiko: %s",
                invoice.number,
                article.name,
                source,
                error,
            )

    if existing is not None:
        existing.status = status
        existing.error = error
        existing.amount = amount
        existing.comment = comment
        existing.pay_out_type_id = pay_out_type_id or existing.pay_out_type_id
    else:
        session.add(
            ChequeIikoPayout(
                invoice_id=invoice.id,
                dds_article_id=article.id,
                source=source,
                pay_out_type_id=pay_out_type_id or "",
                amount=amount,
                comment=comment,
                status=status,
                error=error,
            )
        )
    if status == "posted":
        report.posted += 1
    else:
        report.failed += 1


async def post_cheque_expenses_to_iiko(
    session: AsyncSession, invoice_id: uuid.UUID
) -> ChequePayoutReport:
    """Провести прочие расходы чека в iiko изъятиями. Идемпотентно; ошибка не валит чек.

    Складские строки («Оплата поставщикам») сюда не попадают — они уходят приходной
    накладной (``warehouse_invoice_push``). НЕ коммитит: пишет в сессию, commit — снаружи.
    """
    report = ChequePayoutReport()
    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None or invoice.source != "kassa_cheque":
        return report

    lines = (
        await session.scalars(
            select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == invoice_id)
        )
    ).all()
    sums_by_article: dict[uuid.UUID, Decimal] = {}
    for line in lines:
        if line.dds_article_id is None:
            continue
        sums_by_article[line.dds_article_id] = sums_by_article.get(
            line.dds_article_id, Decimal("0.00")
        ) + _money(line.sum)
    if not sums_by_article:
        return report

    articles = {
        article.id: article
        for article in (
            await session.scalars(select(DdsArticle).where(DdsArticle.id.in_(sums_by_article)))
        ).all()
    }
    # Только «прочие» статьи (в маппинге); складская «Оплата поставщикам» идёт накладной.
    expenses = {
        article_id: total
        for article_id, total in sums_by_article.items()
        if (article := articles.get(article_id)) is not None and article.name in ARTICLE_TO_IIKO
    }
    if not expenses:
        return report

    allocations = (
        await session.scalars(
            select(InvoicePaymentAllocation).where(
                InvoicePaymentAllocation.invoice_id == invoice_id
            )
        )
    ).all()
    # ``card_pending`` — ручной пендинг-чек (банк ещё не подтвердил): для iiko это та же
    # карточная доля (эквайринг), как и обычная 'bank'. Покупка физически уже совершена.
    bank_total = sum(
        (
            _money(a.amount)
            for a in allocations
            if a.source_kind in ("bank", "card_pending")
        ),
        Decimal("0.00"),
    )
    cash_total = sum(
        (_money(a.amount) for a in allocations if a.source_kind == "cash"), Decimal("0.00")
    )
    paid_total = _money(bank_total + cash_total)
    if paid_total <= 0:
        return report

    pay_out_date = invoice.issued_at.date() if invoice.issued_at else invoice.invoice_date
    pay_out_date_iso = pay_out_date.isoformat()

    token = _auth_token()
    accounts = _fetch_accounts_map(token)
    raw_types = _iiko_get(token, PAYOUT_TYPES_PATH, {"includeDeleted": "false"})
    index = _build_payout_type_index(accounts, raw_types if isinstance(raw_types, list) else [])

    for article_id, article_total in expenses.items():
        article = articles[article_id]
        for source, share in _split_by_source(article_total, bank_total, paid_total):
            if share <= 0:
                continue
            await _post_one(
                session, invoice, article, source, share, pay_out_date_iso, index, token, report
            )

    await session.flush()
    return report
