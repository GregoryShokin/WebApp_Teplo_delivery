"""Перепроводка ДДС документа по статьям его ПОЗИЦИЙ.

Разнос оплаты по статьям ДДС считается один раз — в момент оплаты (накладная) или создания
(чек Кассы) — из строк документа. Правка ОПЛАЧЕННОГО документа (``adjust_paid_invoice``)
пересобирает строки, а проводки остаются с прежним разрезом: статьи позиций и статьи ДДС
расходятся молча. Здесь — пересчёт разреза.

Что НЕ меняется: сумма каждой физической оплаты, кошелёк, дата и привязки (аллокация,
``bank_operation.cashflow_transaction_id``). Меняется ТОЛЬКО распределение этой суммы по
статьям. Деньги уже ушли — перепроводка их не двигает.

Ручная разметка (``quality_status='manual_override'``, окно «Разобрать» в ДДС) при
АВТОМАТИЧЕСКОМ вызове не трогается: человек ставил её осознанно, и молча перебивать её правкой
позиций нельзя (решение владельца 29.07.2026). Явный вызов (``force=True``, кнопка
«Перепровести ДДС по позициям») перебивает и её — это уже осознанное действие человека.

Переплата (правка уменьшила сумму, излишек ушёл в дебиторку — ``_spill_overpayment_to_prepayment``)
садится на «Оплата поставщикам»: аванс поставщику — та же статья, что и товар, и размазывать
его по расходным статьям позиций нельзя.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    BankOperation,
    CashflowTransaction,
    DdsArticle,
    InvoiceLineItem,
    InvoicePaymentAllocation,
    SupplierInvoice,
)
from app.services.banking.cashflow_classify import EXCLUDED_QUALITY, MANUAL_QUALITY
from app.services.banking.classifier import AWAITING_BANK_QUALITY
from app.services.warehouse_invoices import SUPPLIER_PAYMENT_ARTICLE_NAME

# Проводки, которые перепроводка не трогает:
# - excluded — исключена из ДДС оператором (не деньги документа);
# - awaiting_bank — плейсхолдер пендинг-чека: он ОДНОЙ строкой на всю сумму и будет разнесён
#   по статьям при матче с выпиской (``_pending_article_sums``), уже по новым позициям.
UNTOUCHED_QUALITY = (EXCLUDED_QUALITY, AWAITING_BANK_QUALITY)

CENT = Decimal("0.01")


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class ReprojectReport:
    """Итог перепроводки — для тоста в UI и тестов."""

    updated: int = 0
    created: int = 0
    deleted: int = 0
    skipped_manual: int = 0
    skipped_ambiguous: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "updated": self.updated,
            "created": self.created,
            "deleted": self.deleted,
            "skipped_manual": self.skipped_manual,
            "skipped_ambiguous": self.skipped_ambiguous,
        }


async def _supplier_article_id(session: AsyncSession) -> uuid.UUID | None:
    return await session.scalar(
        select(DdsArticle.id).where(DdsArticle.name == SUPPLIER_PAYMENT_ARTICLE_NAME).limit(1)
    )


async def invoice_article_breakdown(
    session: AsyncSession, invoice: SupplierInvoice
) -> list[tuple[uuid.UUID, Decimal]]:
    """Разбивка суммы документа по статьям ДДС из его ПОЗИЦИЙ (Σ == ``invoice.amount``).

    Чек Кассы держит статью в КАЖДОЙ строке (``is_staff`` у него всегда false) — суммируем по
    ним, возвращённые позиции пропускаем (документ проведён net). Накладная помечает персонал
    флагом ``is_staff``, а товарные строки статьи не несут — их место «Оплата поставщикам»;
    это и делает ``_invoice_article_amounts`` (тот же расчёт, что при оплате). Порядок долей
    стабилен (по сумме убыв.), чтобы перепроводка не тасовала проводки между прогонами.
    """
    if invoice.source == "kassa_cheque":
        rows = (
            await session.scalars(
                select(InvoiceLineItem).where(
                    InvoiceLineItem.invoice_id == invoice.id,
                    InvoiceLineItem.is_return.is_(False),
                )
            )
        ).all()
        fallback = await _supplier_article_id(session)
        sums: dict[uuid.UUID, Decimal] = {}
        for line in rows:
            article_id = line.dds_article_id or fallback
            if article_id is None:
                continue
            sums[article_id] = sums.get(article_id, Decimal("0.00")) + _money(line.sum)
        return sorted(sums.items(), key=lambda item: (-item[1], str(item[0])))

    from app.services.warehouse_payments import _invoice_article_amounts

    breakdown = await _invoice_article_amounts(session, invoice)
    return sorted(breakdown, key=lambda item: (-item[1], str(item[0])))


async def _document_transactions(
    session: AsyncSession, invoice_id: uuid.UUID
) -> list[CashflowTransaction]:
    rows = (
        await session.scalars(
            select(CashflowTransaction)
            .where(
                CashflowTransaction.source_id == invoice_id,
                CashflowTransaction.quality_status.not_in(UNTOUCHED_QUALITY),
            )
            .order_by(CashflowTransaction.created_at, CashflowTransaction.id)
        )
    ).all()
    return list(rows)


def _scaled_targets(
    breakdown: list[tuple[uuid.UUID, Decimal]],
    *,
    invoice_amount: Decimal,
    paid_total: Decimal,
    supplier_article_id: uuid.UUID | None,
) -> list[tuple[uuid.UUID, Decimal]]:
    """Целевая разбивка ФАКТИЧЕСКИ проведённых денег по статьям (Σ == ``paid_total``).

    Оплачено больше суммы документа (правка уменьшила её, излишек — дебиторка) → разница на
    «Оплата поставщикам» отдельной долей. Оплачено меньше (частичная оплата) → доли позиций
    ужимаются пропорционально. Остаток округления всегда достаётся последней доле.
    """
    if paid_total <= 0 or not breakdown:
        return []
    if invoice_amount <= 0:
        return [(breakdown[0][0], paid_total)]
    if paid_total > invoice_amount and supplier_article_id is not None:
        excess = _money(paid_total - invoice_amount)
        merged = dict(breakdown)
        merged[supplier_article_id] = merged.get(supplier_article_id, Decimal("0.00")) + excess
        return sorted(merged.items(), key=lambda item: (-item[1], str(item[0])))
    if paid_total == invoice_amount:
        return list(breakdown)
    targets: list[tuple[uuid.UUID, Decimal]] = []
    allocated = Decimal("0.00")
    last = len(breakdown) - 1
    for index, (article_id, share) in enumerate(breakdown):
        piece = (
            _money(paid_total - allocated)
            if index == last
            else _money(paid_total * share / invoice_amount)
        )
        allocated += piece
        if piece > 0:
            targets.append((article_id, piece))
    return targets


def _split_group_targets(
    targets: list[tuple[uuid.UUID, Decimal]], *, group_sum: Decimal, paid_total: Decimal
) -> list[tuple[uuid.UUID, Decimal]]:
    """Доля группы (одной физической оплаты) в целевой разбивке — пропорционально её сумме.

    Оплата (карта/наличные) и статьи (позиции) — независимые разбиения одной суммы, поэтому
    каждую оплату делим между статьями пропорционально, как это делает ``_allocate_by_articles``
    при создании чека. Остаток округления — последней статье, чтобы Σ долей == сумме группы.
    """
    if group_sum <= 0 or not targets:
        return []
    out: list[tuple[uuid.UUID, Decimal]] = []
    allocated = Decimal("0.00")
    last = len(targets) - 1
    for index, (article_id, share) in enumerate(targets):
        piece = (
            _money(group_sum - allocated)
            if index == last
            else _money(group_sum * share / paid_total)
        )
        allocated += piece
        if piece > 0:
            out.append((article_id, piece))
    return out


async def _anchor_ids(session: AsyncSession, invoice_id: uuid.UUID) -> set[uuid.UUID]:
    """Проводки, на которые кто-то ССЫЛАЕТСЯ, — их id обязан пережить перепроводку.

    Аллокация оплаты (``InvoicePaymentAllocation.cashflow_transaction_id``) и банковская
    операция (``BankOperation.cashflow_transaction_id``) держат id первой проводки своей части.
    Удалить её — оборвать связь «операция ↔ расход» и получить операцию, снова требующую разбора.
    """
    allocation_ids = (
        await session.scalars(
            select(InvoicePaymentAllocation.cashflow_transaction_id).where(
                InvoicePaymentAllocation.invoice_id == invoice_id,
                InvoicePaymentAllocation.cashflow_transaction_id.is_not(None),
            )
        )
    ).all()
    operation_ids = (
        await session.scalars(
            select(BankOperation.cashflow_transaction_id)
            .join(
                InvoicePaymentAllocation,
                InvoicePaymentAllocation.bank_operation_id == BankOperation.id,
            )
            .where(
                InvoicePaymentAllocation.invoice_id == invoice_id,
                BankOperation.cashflow_transaction_id.is_not(None),
            )
        )
    ).all()
    return {i for i in (*allocation_ids, *operation_ids) if i is not None}


async def dds_articles_mismatch(session: AsyncSession, invoice: SupplierInvoice) -> bool:
    """Разошлись ли статьи проводок ДДС со статьями позиций (на копейку и больше).

    Считается по суммам на статью, а не по порядку строк: перепроводка могла разложить одну
    проводку на две — это не расхождение.
    """
    transactions = await _document_transactions(session, invoice.id)
    if not transactions:
        return False
    paid_total = _money(sum((_money(t.amount) for t in transactions), Decimal("0.00")))
    breakdown = await invoice_article_breakdown(session, invoice)
    targets = _scaled_targets(
        breakdown,
        invoice_amount=_money(invoice.amount),
        paid_total=paid_total,
        supplier_article_id=await _supplier_article_id(session),
    )
    if not targets:
        return False
    actual: dict[uuid.UUID | None, Decimal] = {}
    for txn in transactions:
        actual[txn.article_id] = actual.get(txn.article_id, Decimal("0.00")) + _money(txn.amount)
    expected = dict(targets)
    for article_id in set(actual) | set(expected):
        if abs(actual.get(article_id, Decimal("0.00")) - expected.get(article_id, Decimal("0.00"))) > CENT:
            return True
    return False


async def reproject_invoice_dds(
    session: AsyncSession, invoice: SupplierInvoice, *, force: bool = False
) -> ReprojectReport:
    """Переразнести проводки ДДС документа по статьям его позиций. НЕ коммитит.

    ``force=False`` (авто, после правки оплаченной) — группу с ручной разметкой пропускаем.
    ``force=True`` (кнопка «Перепровести ДДС по позициям») — перепроводим и её, статус
    возвращается в ``final``: разрез снова считает система, а не человек.
    """
    report = ReprojectReport()
    transactions = await _document_transactions(session, invoice.id)
    if not transactions:
        return report
    paid_total = _money(sum((_money(t.amount) for t in transactions), Decimal("0.00")))
    if paid_total <= 0:
        return report
    breakdown = await invoice_article_breakdown(session, invoice)
    targets = _scaled_targets(
        breakdown,
        invoice_amount=_money(invoice.amount),
        paid_total=paid_total,
        supplier_article_id=await _supplier_article_id(session),
    )
    if not targets:
        return report

    anchors = await _anchor_ids(session, invoice.id)
    # Группа = одна физическая оплата: так их и создавали (все доли одной карт-операции/наличной
    # части несут общий кошелёк, дату и назначение). Перепроводим внутри группы, чтобы сумма
    # каждой оплаты осталась прежней.
    groups: dict[tuple[uuid.UUID, date, str | None], list[CashflowTransaction]] = {}
    for txn in transactions:
        groups.setdefault((txn.wallet_id, txn.operation_date, txn.payment_purpose), []).append(txn)

    for group in groups.values():
        group_anchors = [t for t in group if t.id in anchors]
        if len(group_anchors) > 1:
            # Две оплаты слились в одну группу (одинаковые кошелёк/дата/назначение): какая доля
            # чьей операции — уже не восстановить, а удалить якорь нельзя. Не трогаем.
            report.skipped_ambiguous += len(group)
            continue
        if not force and any(t.quality_status == MANUAL_QUALITY for t in group):
            report.skipped_manual += len(group)
            continue
        group_sum = _money(sum((_money(t.amount) for t in group), Decimal("0.00")))
        group_targets = _split_group_targets(targets, group_sum=group_sum, paid_total=paid_total)
        if not group_targets:
            continue
        # Якорь идёт первым: он всегда переиспользуется, а не удаляется.
        ordered = sorted(group, key=lambda t: (t.id not in anchors, t.created_at, str(t.id)))
        template = ordered[0]
        for index, (article_id, amount) in enumerate(group_targets):
            if index < len(ordered):
                txn = ordered[index]
                if txn.article_id != article_id or _money(txn.amount) != amount:
                    report.updated += 1
                txn.article_id = article_id
                txn.amount = amount
                txn.quality_status = "final"
            else:
                session.add(
                    CashflowTransaction(
                        wallet_id=template.wallet_id,
                        direction=template.direction,
                        amount=amount,
                        operation_date=template.operation_date,
                        article_id=article_id,
                        counterparty_id=template.counterparty_id,
                        location_id=template.location_id,
                        lease_id=template.lease_id,
                        source_kind=template.source_kind,
                        source_id=template.source_id,
                        payment_purpose=template.payment_purpose,
                        comment=template.comment,
                        quality_status="final",
                    )
                )
                report.created += 1
        for txn in ordered[len(group_targets) :]:
            await session.delete(txn)
            report.deleted += 1
    await session.flush()
    return report
