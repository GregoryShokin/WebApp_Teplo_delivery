"""Заливка выручки iiko в базу налога (``iiko_revenue_period``).

Два источника:

* ``--from-iiko`` — живой OLAP iiko, подневно (то же, что ночная задача планировщика и
  кнопка на странице «Налоги»). Запускается в прод-контейнере: iiko привязан к его IP.
  Без ``--date-from`` окно считается само — с начала прошлого месяца плюс все месяцы
  года без выручки;
* по умолчанию — локальный кэш выгрузок ``revenue_by_direction`` (месячные JSON), которым
  залиты январь–июнь 2026.

Usage:
    python -m app.scripts.sync_tax_revenue --from-iiko                         # dry-run
    python -m app.scripts.sync_tax_revenue --from-iiko --apply
    python -m app.scripts.sync_tax_revenue --from-iiko --date-from 2026-07-01 --apply
    python -m app.scripts.sync_tax_revenue                       # dry-run, весь кэш
    python -m app.scripts.sync_tax_revenue --apply               # записать
    python -m app.scripts.sync_tax_revenue --apply --year 2026   # только 2026
    python -m app.scripts.sync_tax_revenue --cache-dir /path/to/revenue_by_direction
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import date
from pathlib import Path

from app.db.session import AsyncSessionLocal
from app.services.taxes.repository import DEFAULT_DEPARTMENT
from app.services.taxes.revenue_sync import (
    RevenueSyncError,
    records_from_iiko_monthly,
    revenue_sync_window,
    sync_revenue_from_iiko,
    sync_revenue_periods,
)

# Кэш выгрузок в основном репозитории; ignored by git, обновляется скриптом экспорта iiko.
DEFAULT_CACHE_DIR = Path(
    "/Users/grigorii/Developer/Projects/Teplo-all-for-business"
    "/research/raw/iiko/revenue_by_direction"
)
_FILE_RE = re.compile(r"revenue_by_direction_(\d{4})-(\d{2})\.json$")


def _iter_cache_files(cache_dir: Path, year: int | None):
    for path in sorted(cache_dir.glob("revenue_by_direction_*.json")):
        m = _FILE_RE.search(path.name)
        if not m:
            continue
        y, mth = int(m.group(1)), int(m.group(2))
        if year is not None and y != year:
            continue
        yield path, y, mth


def _load_records(cache_dir: Path, year: int | None, department: str):
    for path, y, mth in _iter_cache_files(cache_dir, year):
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("data") or []
        if not rows:
            print(f"  пропуск {path.name}: пустая выгрузка")
            continue
        yield records_from_iiko_monthly(rows, year=y, month=mth, department=department), y, mth


async def _run_from_iiko(args: argparse.Namespace) -> None:
    async with AsyncSessionLocal() as session:
        date_from, date_to = await revenue_sync_window(
            session, today=date.today(), department=args.department
        )
        if args.date_from:
            date_from = date.fromisoformat(args.date_from)
        if args.date_to:
            date_to = date.fromisoformat(args.date_to)
        print(f"Источник: iiko OLAP, {date_from.isoformat()} … {date_to.isoformat()}")
        try:
            report = await sync_revenue_from_iiko(
                session,
                date_from=date_from,
                date_to=date_to,
                department=args.department,
                sync_reason="backfill",
            )
        except RevenueSyncError as exc:
            raise SystemExit(f"Подкачка не удалась: {exc}") from exc
        for month, total in report.months.items():
            print(f"  {month}: выручка (со скидками) {total:,.2f}")
        print(f"\nДней: {report.days}, сумма {report.total:,.2f}")
        if args.apply:
            await session.commit()
            print(f"Записано: {report.result.as_dict()}")
        else:
            await session.rollback()
            print(f"DRY-RUN (без записи): {report.result.as_dict()}")
            print("Повторите с --apply, чтобы записать.")


async def _run(args: argparse.Namespace) -> None:
    if args.from_iiko:
        await _run_from_iiko(args)
        return
    cache_dir = Path(args.cache_dir)
    if not cache_dir.is_dir():
        raise SystemExit(f"Каталог кэша не найден: {cache_dir}")

    records = []
    print(f"Источник: {cache_dir}")
    for record, y, mth in _load_records(cache_dir, args.year, args.department):
        records.append(record)
        print(f"  {y}-{mth:02d}: выручка (со скидками) {record.revenue_net:,.2f}")

    if not records:
        raise SystemExit("Нет данных для заливки (проверьте --year и каталог кэша).")

    total = sum(r.revenue_net for r in records)
    print(f"\nВсего к заливке: {len(records)} мес., сумма {total:,.2f}")

    async with AsyncSessionLocal() as session:
        result = await sync_revenue_periods(
            session,
            records,
            sync_reason="backfill",
            apply=args.apply,
        )
        if args.apply:
            await session.commit()
            print(f"\nЗаписано: {result.as_dict()}")
        else:
            print(f"\nDRY-RUN (без записи): {result.as_dict()}")
            print("Повторите с --apply, чтобы записать.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Заливка выручки iiko в базу налога.")
    parser.add_argument("--apply", action="store_true", help="Записать (по умолчанию dry-run)")
    parser.add_argument("--year", type=int, default=None, help="Только указанный год")
    parser.add_argument("--department", default=DEFAULT_DEPARTMENT, help="Департамент iiko")
    parser.add_argument(
        "--cache-dir", default=str(DEFAULT_CACHE_DIR), help="Каталог выгрузок iiko"
    )
    parser.add_argument(
        "--from-iiko", action="store_true", help="Брать выручку живым запросом к iiko"
    )
    parser.add_argument("--date-from", default=None, help="Начало окна, ГГГГ-ММ-ДД (--from-iiko)")
    parser.add_argument(
        "--date-to", default=None, help="Конец окна, ГГГГ-ММ-ДД (--from-iiko); по умолчанию вчера"
    )
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
