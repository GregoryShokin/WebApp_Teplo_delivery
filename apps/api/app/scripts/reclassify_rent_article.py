"""Разобрать историю статьи «Аренда торговых точек»: аренда, коммуналка, содержание.

Под арендной статьёй за июнь–июль 2026 лежит 21 проводка, и арендой из них являются ровно
две — 2×50 000 от 01.07 (аренда Черниковой за июль). Остальное владелец разметил так
(23.07.2026): вывоз мусора и ресурсы — «Коммунальные платежи», охрана — «Содержание торговых
точек». Всё это расходы конкретной точки, поэтому каждой проводке проставляется помещение.

Скрипт идемпотентен: повторный прогон не двигает уже разнесённые проводки (смотрит на текущую
статью и заполненность помещения). Сначала прогоняется на превью-стенде, затем на проде.

    docker exec -w /app/apps/api -e PYTHONPATH=/app/apps/api <api> \
        python -m app.scripts.reclassify_rent_article --location "Черникова" [--apply]

Без ``--apply`` только показывает план — правка боевых проводок не должна быть побочкой
запуска скрипта.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models import CashflowTransaction, Counterparty, DdsArticle, Location, LocationLease
from app.services.supplier_prepayments import ensure_prepayment_from_bank_transaction

RENT_ARTICLE_CODE = "arenda_torgovyh_tochek"
UTILITIES_ARTICLE_NAME = "Коммунальные платежи"
MAINTENANCE_ARTICLE_NAME = "Содержание торговых точек"

# Охрана: единственная группа, которая идёт в «Содержание торговых точек».
SECURITY_COUNTERPARTY_HINTS = ("ЧОО", "ЧОП", "охран")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reclassify historical rent-article payments.")
    parser.add_argument(
        "--location", required=True, help="Название помещения (например, Черникова)"
    )
    parser.add_argument("--since", default="2026-06-01", help="С какой даты разбирать")
    parser.add_argument(
        "--rent-amount",
        default="50000",
        help="Сумма одной арендной проводки — по ней отличаем аренду от прочего",
    )
    parser.add_argument("--apply", action="store_true", help="Записать изменения")
    return parser.parse_args()


def _is_security(counterparty_name: str | None) -> bool:
    if not counterparty_name:
        return False
    lowered = counterparty_name.lower()
    return any(hint.lower() in lowered for hint in SECURITY_COUNTERPARTY_HINTS)


async def async_main() -> None:
    args = parse_args()
    since = date.fromisoformat(args.since)
    rent_amount = Decimal(args.rent_amount)

    async with AsyncSessionLocal() as session:
        location = await session.scalar(select(Location).where(Location.name == args.location))
        if location is None:
            raise SystemExit(f"Помещение «{args.location}» не найдено")

        rent_article = await session.scalar(
            select(DdsArticle).where(DdsArticle.code == RENT_ARTICLE_CODE)
        )
        utilities = await session.scalar(
            select(DdsArticle).where(DdsArticle.name == UTILITIES_ARTICLE_NAME)
        )
        maintenance = await session.scalar(
            select(DdsArticle).where(DdsArticle.name == MAINTENANCE_ARTICLE_NAME)
        )
        if rent_article is None or utilities is None or maintenance is None:
            raise SystemExit("Не найдены статьи: аренда / коммунальные / содержание точек")

        lease = await session.scalar(
            select(LocationLease).where(
                LocationLease.location_id == location.id,
                LocationLease.ended_on.is_(None),
            )
        )

        rows = (
            await session.execute(
                select(CashflowTransaction, Counterparty.name)
                .outerjoin(Counterparty, Counterparty.id == CashflowTransaction.counterparty_id)
                .where(
                    CashflowTransaction.article_id == rent_article.id,
                    CashflowTransaction.operation_date >= since,
                )
                .order_by(CashflowTransaction.operation_date)
            )
        ).all()

        plan: list[tuple[CashflowTransaction, str, DdsArticle | None]] = []
        for txn, counterparty_name in rows:
            if Decimal(txn.amount) == rent_amount:
                # Аренда остаётся своей статьёй — ей нужны помещение и договор.
                plan.append((txn, "аренда", None))
            elif _is_security(counterparty_name):
                plan.append((txn, "содержание точек", maintenance))
            else:
                plan.append((txn, "коммунальные", utilities))

        print(f"Помещение: {location.name} · договор: {'есть' if lease else 'НЕТ'}")
        print(f"Проводок под арендной статьёй с {since}: {len(plan)}")
        for txn, target, article in plan:
            already = txn.location_id is not None and (
                article is None or txn.article_id == article.id
            )
            mark = "уже разнесено" if already else "→"
            print(
                f"  {txn.operation_date} {txn.amount:>10} "
                f"{(counterparty_name_of(rows, txn) or '—'):22} {mark} {target}"
            )

        if not args.apply:
            print("\nсухой прогон: изменения не записаны (добавьте --apply)")
            return

        changed = 0
        rent_transactions: list[CashflowTransaction] = []
        for txn, _target, article in plan:
            if article is not None and txn.article_id != article.id:
                txn.article_id = article.id
                changed += 1
            if txn.location_id is None:
                txn.location_id = location.id
                changed += 1
            if article is None and lease is not None:
                if txn.lease_id is None:
                    txn.lease_id = lease.id
                    txn.counterparty_id = lease.counterparty_id
                    changed += 1
                rent_transactions.append(txn)
        await session.flush()

        # Правило 1: платёж арендодателю, за которым ещё нет закрывающего документа, —
        # это дебиторка. Разметка задним числом его не запускает, поэтому зовём явно:
        # иначе оплаченная вперёд аренда не появилась бы в ДЗ.
        for txn in rent_transactions:
            await ensure_prepayment_from_bank_transaction(session, txn)

        await session.commit()
        print(f"\nзаписано изменений: {changed}")
        if rent_transactions:
            print(f"правило 1 применено к арендным проводкам: {len(rent_transactions)}")


def counterparty_name_of(rows, txn) -> str | None:
    for candidate, name in rows:
        if candidate.id == txn.id:
            return name
    return None


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
