"""Переразобрать период услуги в уже принятых счетах — по сохранённому PDF.

Период распознаётся при приёме письма, но правила распознавания с тех пор поправились:
добавлены сокращения месяцев («Авг. 2026») и месяц в скобках после длинного адреса
(«(Июль 2026 г.)»). Документы, принятые до правки, остались без периода — и очередь
признания просит у человека то, что написано в самом счёте.

Скрипт берёт СОХРАНЁННЫЙ PDF (``email_invoice_intake.pdf_bytes``), прогоняет его через тот же
детерминированный слой, что и приём почты, и проставляет период счёту через
``set_invoice_service_period`` — то есть с пересчётом начисления, а не голым UPDATE.

ТОЛЬКО ДЕТЕРМИНИРОВАННЫЙ СЛОЙ: LLM здесь не зовём, чтобы массовый прогон не стоил денег и не
зависел от внешнего сервиса. Что не распозналось правилами — остаётся человеку.

Период, проставленный человеком вручную, не трогается: заполняем только пустое.

    python -m app.scripts.reparse_invoice_periods
    python -m app.scripts.reparse_invoice_periods --apply
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models import Counterparty, EmailInvoiceIntake, SupplierInvoice
from app.services.invoice_recognition import (
    deterministic_recognize_pages,
    extract_pdf_pages,
)
from app.services.supplier_service_periods import set_invoice_service_period


async def main(*, apply: bool) -> None:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(EmailInvoiceIntake, SupplierInvoice, Counterparty.name)
                .join(SupplierInvoice, SupplierInvoice.id == EmailInvoiceIntake.invoice_id)
                .outerjoin(Counterparty, Counterparty.id == SupplierInvoice.counterparty_id)
                .where(
                    EmailInvoiceIntake.pdf_bytes.is_not(None),
                    SupplierInvoice.payment_status != "void",
                    SupplierInvoice.service_period_status != "ready",
                )
                .order_by(SupplierInvoice.invoice_date)
            )
        ).all()

        found: list[tuple[SupplierInvoice, str, object]] = []
        for intake, invoice, cp_name in rows:
            try:
                pages = extract_pdf_pages(intake.pdf_bytes)
            except Exception as error:  # noqa: BLE001 — битый PDF не должен ронять прогон
                print(f"  ! {cp_name or '—'} {invoice.number or '—'}: PDF не читается ({error})")
                continue
            rec = deterministic_recognize_pages(pages, context_text=intake.subject)
            if rec.service_period_start is None or rec.service_period_end is None:
                continue
            found.append((invoice, cp_name or "—", rec))

        print(f"Счетов без периода: {len(rows)}; период распознан у {len(found)}")
        for invoice, cp_name, rec in found:
            print(
                f"  {cp_name[:30]:32} {invoice.number or '—':20} "
                f"{rec.service_period_start:%d.%m}–{rec.service_period_end:%d.%m.%Y} "
                f"({rec.service_period_source or 'текст'})"
            )

        if not apply:
            print("\nПробный прогон. Чтобы применить — запустите с --apply")
            return

        for invoice, _cp_name, rec in found:
            await set_invoice_service_period(
                session,
                invoice=invoice,
                start=rec.service_period_start,
                end=rec.service_period_end,
                actor_user_id=None,
                reason="переразбор периода из PDF",
            )
            # Откуда взялся период — видно в карточке и в отчётах: «reparse» отличает
            # переразбор от периода, распознанного при первом приёме.
            invoice.service_period_source = "reparse"
        await session.commit()
        print(f"\nПериод проставлен: {len(found)} счетов")


if __name__ == "__main__":
    asyncio.run(main(apply="--apply" in sys.argv))
