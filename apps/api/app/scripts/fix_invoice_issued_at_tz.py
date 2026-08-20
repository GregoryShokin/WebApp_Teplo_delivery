"""Разовый ремонт: время накладных и чеков записано по Гринвичу вместо Москвы.

ЧТО ЧИНИМ. Поле ввода ``<input type="datetime-local">`` отдаёт строку без зоны
(«2026-08-20T15:16»), pydantic делал из неё наивный ``datetime``, а колонка ``issued_at`` —
``timestamptz``, и контейнер API живёт в UTC. Набранные оператором московские цифры уезжали в
базу как гринвичские: накладная 515287 создана в 15:17 МСК, а «выписана» в 18:16 — на три часа
позже собственного создания. Затронуты ВСЕ 190 строк с ``issued_at`` (kassa_cheque 94,
kassa_invoice 75, manual 21): у каждой ровно нулевые секунды и микросекунды — подпись ручного
ввода, машинные значения так не выглядят.

ВТОРОЕ, ЧТО ЧИНИМ — дата документа. Обратный синк перетирал ``invoice_date`` датой, вернувшейся
из iiko, а тот отдаёт нашу же присланную полночь на три часа раньше (послали 2026-08-20T00:00,
получили 2026-08-19T21:00+03:00). Полночь минус три часа = предыдущий день, поэтому у 45
накладных Кассы дата в списке отставала на сутки от даты в карточке. Границу месяца не перешла
ни одна — в отчётность искажение не попало.

ЧТО СЧИТАЕМ ИСТИНОЙ. Цифры, которые набрал оператор. Они лежат в базе как есть, просто с чужой
меткой зоны, поэтому:

* новое ``issued_at`` = старое минус 3 часа (те же цифры, но уже по Москве);
* новая ``invoice_date`` = дата ЭТИХ цифр, то есть UTC-дата старого ``issued_at``.

Второе меняет только те строки, где синк успел перетереть дату; у остальных она уже верна и
пересчёт вернёт то же значение.

ПОЧЕМУ ЕСТЬ ``--before``. После выкатки фикса новые строки пишутся уже правильно, а выглядят
так же (нулевые секунды). Повторный прогон без границы сдвинул бы их второй раз. Граница —
момент, когда на прод встал новый образ; строки, созданные позже, скрипт не трогает.

БЕЗ ``--apply`` СКРИПТ НИЧЕГО НЕ МЕНЯЕТ, а печатает план и контрольные суммы.

Запуск::

    python -m app.scripts.fix_invoice_issued_at_tz --before 2026-08-20T18:30:00+03:00
    python -m app.scripts.fix_invoice_issued_at_tz --before 2026-08-20T18:30:00+03:00 --apply
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models import SupplierInvoice
from app.services.clock import MOSCOW_TZ

# Источники, где ``issued_at`` набирает человек в форме. Остальные его не заполняют вовсе.
HUMAN_SOURCES = ("kassa_cheque", "kassa_invoice", "manual")


async def _plan(session: AsyncSession, *, before: datetime) -> list[dict]:
    rows = (
        await session.scalars(
            select(SupplierInvoice)
            .where(
                SupplierInvoice.issued_at.is_not(None),
                SupplierInvoice.source.in_(HUMAN_SOURCES),
                SupplierInvoice.created_at < before,
            )
            .order_by(SupplierInvoice.created_at)
        )
    ).all()
    plan: list[dict] = []
    for invoice in rows:
        old_issued = invoice.issued_at
        assert old_issued is not None  # noqa: S101 - отфильтровано запросом
        # Цифры, которые видел оператор: значение, прочитанное как UTC-стенное время.
        typed = old_issued.astimezone(UTC).replace(tzinfo=None)
        new_issued = typed.replace(tzinfo=MOSCOW_TZ)
        plan.append(
            {
                "invoice": invoice,
                "number": invoice.number,
                "source": invoice.source,
                "old_issued": old_issued,
                "new_issued": new_issued,
                "old_date": invoice.invoice_date,
                "new_date": typed.date(),
            }
        )
    return plan


def _report(plan: list[dict]) -> None:
    by_source: dict[str, int] = {}
    date_fixes = [item for item in plan if item["old_date"] != item["new_date"]]
    for item in plan:
        by_source[item["source"]] = by_source.get(item["source"], 0) + 1

    print(f"К правке времени: {len(plan)} строк")
    for source, count in sorted(by_source.items(), key=lambda kv: -kv[1]):
        print(f"    {source:<14} {count}")
    print(f"Из них с неверной датой документа: {len(date_fixes)}")
    for item in date_fixes[:10]:
        print(
            f"    №{item['number']}: дата {item['old_date']} → {item['new_date']}, "
            f"время {item['old_issued']:%Y-%m-%d %H:%M%z} → {item['new_issued']:%Y-%m-%d %H:%M%z}"
        )
    if len(date_fixes) > 10:
        print(f"    … и ещё {len(date_fixes) - 10}")
    # Контроль: сдвиг не должен переносить документ в другой месяц — это уехало бы в отчётность.
    month_moves = [
        item
        for item in plan
        if item["old_date"] is not None
        and item["old_date"].replace(day=1) != item["new_date"].replace(day=1)
    ]
    print(f"Пересекают границу месяца: {len(month_moves)}")
    for item in month_moves:
        print(f"    ВНИМАНИЕ №{item['number']}: {item['old_date']} → {item['new_date']}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--before",
        required=True,
        help="ISO-момент выкатки фикса: строки, созданные позже, не трогаем",
    )
    parser.add_argument("--apply", action="store_true", help="записать изменения")
    args = parser.parse_args()

    before = datetime.fromisoformat(args.before)
    if before.tzinfo is None:
        before = before.replace(tzinfo=MOSCOW_TZ)

    async with AsyncSessionLocal() as session:
        plan = await _plan(session, before=before)
        _report(plan)
        if not args.apply:
            print("\nВхолостую: ничего не записано. Повторить с --apply.")
            return
        for item in plan:
            invoice = item["invoice"]
            invoice.issued_at = item["new_issued"]
            invoice.invoice_date = item["new_date"]
        await session.commit()
        print(f"\nЗаписано: {len(plan)} строк.")


if __name__ == "__main__":
    asyncio.run(main())
