"""Разовый ремонт: наличные выплаты услуговым контрагентам без следа в ДЗ/КЗ.

ЧТО ЧИНИМ. Наличная выдача из Сейфа услуговому контрагенту обязана оставлять дебиторку —
его расход закрывается признанием, а не платежом. До 06.08.2026 такой след возникал только
если адресный механизм узнавал платёж: аренда по договору, коммуналка по лицевому счёту. У
Станислава Юрьевича 08.07.2026 из Сейфа ушло 9 879 ₽ «За воду», а лицевой счёт «Вода» завели
03.08 — позже платежа. Коммунальная ветка потока не нашла и вышла ни с чем: деньги остались
вне расчётов вовсе.

ЧЕМ ЭТО КОНЧИЛОСЬ. Пришедшая 03.08 платёжка на воду искала, чем закрыться, своих денег не
нашла и взяла единственный свободный аванс контрагента — АРЕНДУ ЗА АВГУСТ. Из 50 000 ₽ ушло
9 654,25 ₽, и 31.08 закрывающий по аренде выставил бы к оплате уже оплаченный месяц. Плюс
реестр остатков (он считает по авансам) разошёлся с карточкой сверки (она считает по деньгам)
ровно на потерянные 9 879 ₽.

ПОЧЕМУ СКРИПТ, А НЕ ПРАВКА ТАБЛИЦ. Дебиторку заводит правило 1 канона
(``sync_manual_payment_receivable``): оно само гасит открытую кредиторку контрагента и само
кладёт остаток в дебиторку. Руками в таблицы — значит завести вторую реализацию канона,
которая разойдётся с первой на следующем же изменении.

БЕЗ ``--apply`` СКРИПТ НИЧЕГО НЕ МЕНЯЕТ, а печатает план и контрольные суммы. Причина в коде
закрыта (``safe_allocations``), здесь только уже случившееся.

Запуск::

    python -m app.scripts.repair_cash_prepayments            # вхолостую
    python -m app.scripts.repair_cash_prepayments --apply    # боевой
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models import (
    CashflowTransaction,
    Counterparty,
    CounterpartyPayableProfile,
    InvoicePaymentAllocation,
    SupplierInvoice,
    SupplierPrepayment,
)

#: Механизмы наличной выдачи. Банковские платежи проходят правило 1 сами.
CASH_SOURCE_KINDS = ("safe_payout", "kassa_payout", "kassa_target_payout")


def _money(value) -> Decimal:
    return Decimal(value or 0).quantize(Decimal("0.01"))


async def _orphan_payments(session: AsyncSession) -> list[tuple[CashflowTransaction, str]]:
    """Наличные выплаты услуговым контрагентам, не оставившие следа в ДЗ/КЗ."""
    rows = (
        await session.execute(
            select(CashflowTransaction, Counterparty.name)
            .join(Counterparty, Counterparty.id == CashflowTransaction.counterparty_id)
            .join(
                CounterpartyPayableProfile,
                CounterpartyPayableProfile.counterparty_id == CashflowTransaction.counterparty_id,
            )
            .where(
                CashflowTransaction.direction == "out",
                CashflowTransaction.source_kind.in_(CASH_SOURCE_KINDS),
                CashflowTransaction.counterparty_id.is_not(None),
                CounterpartyPayableProfile.service_billing_mode.is_not(None),
                # Уже размеченные периодом до начала учёта закрывают долг, которого в системе
                # нет: дебиторку по ним заводить не за что.
                CashflowTransaction.expense_month.is_(None),
            )
            .order_by(CashflowTransaction.operation_date)
        )
    ).all()

    orphans: list[tuple[CashflowTransaction, str]] = []
    for txn, name in rows:
        has_prepayment = await session.scalar(
            select(SupplierPrepayment.id).where(
                SupplierPrepayment.cashflow_transaction_id == txn.id
            )
        )
        has_allocation = await session.scalar(
            select(InvoicePaymentAllocation.id).where(
                InvoicePaymentAllocation.cashflow_transaction_id == txn.id
            )
        )
        if has_prepayment is None and has_allocation is None:
            orphans.append((txn, name))
    return orphans


async def _own_prepayment(
    session: AsyncSession, invoice: SupplierInvoice
) -> SupplierPrepayment | None:
    """Свободная дебиторка, предназначенная ИМЕННО этому документу.

    Пара «счёт + закрывающий» рождается из одной бумаги, и оплата счёта заводит по канону
    дебиторку ``kind='prepaid_bill'``, привязанную к счёту. Она и есть законный источник
    гашения закрывающего — вместо чужого аванса, взятого лишь потому, что своих денег в тот
    момент ещё не было.
    """
    twin_ids = select(SupplierInvoice.id).where(
        SupplierInvoice.counterparty_id == invoice.counterparty_id,
        SupplierInvoice.number == invoice.number,
        SupplierInvoice.source == invoice.source,
    )
    return await session.scalar(
        select(SupplierPrepayment)
        .where(
            SupplierPrepayment.bill_invoice_id.in_(twin_ids),
            SupplierPrepayment.status.in_(("open", "partially_settled")),
            SupplierPrepayment.amount > SupplierPrepayment.amount_settled,
        )
        .limit(1)
    )


async def _foreign_settlements(session: AsyncSession) -> list[dict]:
    """Зачёты, где документ закрыт авансом ЧУЖОГО назначения.

    Признак: статья аванса не совпадает со статьёй документа-получателя. Так вода закрылась
    арендными деньгами: адресный подбор выбирает аванс в момент прихода документа, и если
    своих денег ещё нет, берёт любые свободные того же контрагента — назначение платежа он
    не читает. Деньги при этом не двигаются, но арендный аванс худеет, и в свою дату аренда
    выставляет к оплате то, что уже оплачено.
    """
    rows = (
        await session.execute(
            select(InvoicePaymentAllocation, SupplierPrepayment, SupplierInvoice)
            .join(
                SupplierPrepayment,
                SupplierPrepayment.id == InvoicePaymentAllocation.prepayment_id,
            )
            .join(SupplierInvoice, SupplierInvoice.id == InvoicePaymentAllocation.invoice_id)
            .where(InvoicePaymentAllocation.prepayment_id.is_not(None))
        )
    ).all()

    foreign: list[dict] = []
    for allocation, prepayment, invoice in rows:
        invoice_article = invoice.dds_article_id
        if prepayment.article_id is None or invoice_article is None:
            continue
        if prepayment.article_id != invoice_article:
            foreign.append(
                {
                    "allocation": allocation,
                    "prepayment": prepayment,
                    "invoice": invoice,
                }
            )
    return foreign


async def _report(session: AsyncSession, counterparty_ids: set[uuid.UUID]) -> dict:
    """Контрольные суммы по затронутым контрагентам: уплачено, дебиторка, свободные авансы."""
    snapshot: dict = {}
    for counterparty_id in counterparty_ids:
        name = await session.scalar(
            select(Counterparty.name).where(Counterparty.id == counterparty_id)
        )
        paid = await session.scalar(
            select(CashflowTransaction.amount).where(
                CashflowTransaction.counterparty_id == counterparty_id,
                CashflowTransaction.direction == "out",
            )
        )
        free = (
            await session.execute(
                select(SupplierPrepayment.amount - SupplierPrepayment.amount_settled).where(
                    SupplierPrepayment.counterparty_id == counterparty_id,
                    SupplierPrepayment.status.in_(("open", "partially_settled")),
                )
            )
        ).scalars()
        snapshot[name or str(counterparty_id)] = {
            "свободные авансы": sum((_money(v) for v in free), Decimal("0.00")),
            "последний платёж": _money(paid),
        }
    return snapshot


async def main(apply: bool) -> None:
    from app.services.supplier_prepayments import sync_manual_payment_receivable

    async with AsyncSessionLocal() as session:
        orphans = await _orphan_payments(session)
        foreign = await _foreign_settlements(session)
        touched = {txn.counterparty_id for txn, _ in orphans if txn.counterparty_id}
        touched |= {item["prepayment"].counterparty_id for item in foreign}

        print("=== НАЛИЧНЫЕ БЕЗ СЛЕДА В ДЗ/КЗ ===")
        for txn, name in orphans:
            print(f"  {txn.operation_date}  {_money(txn.amount):>12}  {name}  · {txn.id}")
        if not orphans:
            print("  нет")

        print("\n=== ДОКУМЕНТЫ, ЗАКРЫТЫЕ ЧУЖИМ АВАНСОМ ===")
        for item in foreign:
            allocation, prepayment, invoice = (
                item["allocation"],
                item["prepayment"],
                item["invoice"],
            )
            print(
                f"  {invoice.number or invoice.id}: {_money(allocation.amount)} ₽ "
                f"из аванса {prepayment.id} (статьи разные)"
            )
        if not foreign:
            print("  нет")

        print("\n=== ДО ===")
        for name, values in (await _report(session, touched)).items():
            print(f"  {name}: {values}")

        if not apply:
            print("\nВхолостую: ничего не изменено. Боевой запуск — с флагом --apply")
            return

        # Порядок значим: сперва отпускаем чужие деньги, потом заводим свои. Иначе новая
        # дебиторка не найдёт документа, который уже числится закрытым, и повиснет авансом.
        from app.services.supplier_prepayments import settle_invoice_from_prepayment

        for item in foreign:
            allocation, prepayment = item["allocation"], item["prepayment"]
            prepayment.amount_settled = _money(prepayment.amount_settled) - _money(
                allocation.amount
            )
            prepayment.status = (
                "open"
                if _money(prepayment.amount_settled) <= 0
                else "partially_settled"
            )
            await session.delete(allocation)
            await session.flush()
            from app.services import counterparty_matching

            await counterparty_matching._recompute_status(session, item["invoice"])

            # Освободившийся документ закрываем ЕГО СОБСТВЕННОЙ дебиторкой, если она есть.
            # Без этого шага ремонт только отпускал чужие деньги, а документ оставался
            # неоплаченным — и следующий же прогон подбора занял бы их снова.
            own = await _own_prepayment(session, item["invoice"])
            if own is not None:
                await settle_invoice_from_prepayment(
                    session,
                    invoice_id=item["invoice"].id,
                    prepayment_id=own.id,
                    amount=_money(allocation.amount),
                )
                print(
                    f"  → «{item['invoice'].number}» закрыт своей дебиторкой "
                    f"{_money(allocation.amount)} ₽"
                )

        for txn, _name in orphans:
            await sync_manual_payment_receivable(session, txn, money_is_free=True)

        await session.commit()
        print("\n=== ПОСЛЕ ===")
        for name, values in (await _report(session, touched)).items():
            print(f"  {name}: {values}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="боевой запуск")
    asyncio.run(main(parser.parse_args().apply))
