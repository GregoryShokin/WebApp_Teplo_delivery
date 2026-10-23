"""Сторож оплат вхолостую: что он нашёл бы в iiko прямо сейчас.

Тот же разбор, что у фонового джоба ``watch_iiko_invoice_payments``, но ТОЛЬКО чтение: iiko —
отчёт по проводкам и Cloud ``get``, база — выборка без записи (транзакция откатывается). Нужен,
чтобы до включения сторожа или после спорного случая увидеть картину целиком, не заводя случаев.

    docker exec -w /app/apps/api <api-контейнер> python -m app.scripts.iiko_payment_watch_report
"""

from __future__ import annotations

import asyncio

from app.db.session import AsyncSessionLocal
from app.models import SupplierInvoice
from app.services.iiko_payment_watch import LiveIikoWatchSource, collect_payment_drift
from app.services.iiko_sync import _load_source_credential_env


async def main() -> None:
    """Прочитать iiko, напечатать найденное. Ничего не пишет ни в базу, ни в iiko."""
    async with AsyncSessionLocal() as session:
        await _load_source_credential_env(session)
        report = await collect_payment_drift(session, LiveIikoWatchSource())
        print(
            f"проверено оплат: {report.checked_payments}, пар исправления: {report.checked_pairs},"
            f" не прочитано: {report.unavailable}, расхождений: {len(report.findings)}"
        )
        for drift in report.findings:
            invoice = await session.get(SupplierInvoice, drift.invoice_id)
            number = invoice.number if invoice is not None else drift.invoice_id
            print(f"\n— накладная {number} · {drift.reason_code} · {drift.amount}")
            for deviation in drift.deviations:
                print(f"  [{deviation['code']}] {deviation['text']}")
        await session.rollback()


if __name__ == "__main__":
    asyncio.run(main())
