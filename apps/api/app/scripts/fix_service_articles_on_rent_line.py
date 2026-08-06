"""Разовый ремонт: услуги, оплаченные по арендной статье.

ЧТО ЧИНИМ. Охрана (ЧОО, СПЕЦАВТО ЮГ) и вывоз мусора (ЭкоЦентр) оплачивались по статье
«Аренда торговых точек», хотя расход по ним признаётся на своих строках — «Содержание
торговых точек» и «Коммунальные платежи». Строка признания берётся из карточки контрагента
(``default_dds_article_id``), а статья платежа — из правила классификации по ИНН, и эти два
источника разошлись: 6 995,59 ₽ в месяц уходят по арендной строке, а признаются по другой.

ПОЧЕМУ ЭТО НЕ ВИДНО СЕГОДНЯ. Все их платежи имеют аллокацию на счёт, поэтому в сверку
«оплачено, но не признано» не попадают вовсе (``ledger_known``). Как только такой платёж
пройдёт наличными или без счёта, отчёт закричит на здоровых данных — предупреждением
«расход признан по другой строке». Владелец подтвердил 06.08.2026: это ошибка разметки.

ЧИНИМ ОБА КОНЦА. Правило классификации — чтобы будущие платежи шли верно; уже прошедшие
проводки — чтобы отчёт сошёлся задним числом. Заодно проставляется помещение: все три
статьи требуют аналитику «где», а у этих проводок её нет вовсе.

Источник правды о правильной статье — КАРТОЧКА контрагента, а не список в коде: она же
определяет строку признания, и брать её значит чинить расхождение к тому концу, который
владелец видит на экране признания.

Без ``--apply`` печатает план и ничего не меняет::

    python -m app.scripts.fix_service_articles_on_rent_line
    python -m app.scripts.fix_service_articles_on_rent_line --apply
"""

from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models import (
    CashflowTransaction,
    ClassificationRule,
    Counterparty,
    CounterpartyPayableProfile,
    DdsArticle,
    Location,
)
from app.services import accounting_periods

RENT_ARTICLE_CODE = "arenda_torgovyh_tochek"


async def _rent_article_id(session: AsyncSession):
    return await session.scalar(
        select(DdsArticle.id).where(DdsArticle.code == RENT_ARTICLE_CODE)
    )


async def _mismatched(session: AsyncSession, rent_article_id) -> list[dict]:
    """Контрагенты, чьи платежи идут по аренде, а признание — по другой статье карточки."""
    rows = (
        await session.execute(
            select(Counterparty, CounterpartyPayableProfile.default_dds_article_id)
            .join(
                CounterpartyPayableProfile,
                CounterpartyPayableProfile.counterparty_id == Counterparty.id,
            )
            .where(
                CounterpartyPayableProfile.default_dds_article_id.is_not(None),
                CounterpartyPayableProfile.default_dds_article_id != rent_article_id,
            )
        )
    ).all()

    found: list[dict] = []
    for counterparty, default_article_id in rows:
        payments = (
            await session.scalars(
                select(CashflowTransaction).where(
                    CashflowTransaction.counterparty_id == counterparty.id,
                    CashflowTransaction.article_id == rent_article_id,
                    CashflowTransaction.direction == "out",
                )
            )
        ).all()
        if not payments:
            continue
        article_name = await session.scalar(
            select(DdsArticle.name).where(DdsArticle.id == default_article_id)
        )
        rules = (
            await session.scalars(
                select(ClassificationRule).where(
                    ClassificationRule.article_id == rent_article_id,
                    ClassificationRule.counterparty_inn_match == counterparty.inn,
                )
            )
            if counterparty.inn
            else []
        )
        found.append(
            {
                "counterparty": counterparty,
                "article_id": default_article_id,
                "article_name": article_name,
                "payments": payments,
                "rules": list(rules),
            }
        )
    return found


async def main(apply: bool) -> None:
    async with AsyncSessionLocal() as session:
        rent_article_id = await _rent_article_id(session)
        if rent_article_id is None:
            print("Статья «Аренда торговых точек» не найдена — ремонтировать нечего")
            return

        # Помещение для аналитики «где»: активная торговая точка. Если их несколько, выбор
        # делает человек — скрипт не угадывает.
        locations = (
            await session.scalars(
                select(Location).where(Location.status == "active", Location.name != "Склад")
            )
        ).all()
        if len(locations) != 1:
            print(f"Активных торговых точек {len(locations)} — помещение проставит человек")
            location_id = None
        else:
            location_id = locations[0].id
            print(f"Помещение для аналитики: {locations[0].name}")

        closed = await accounting_periods.closed_months(session)
        targets = await _mismatched(session, rent_article_id)

        total = Decimal("0.00")
        for item in targets:
            counterparty = item["counterparty"]
            print(f"\n=== {counterparty.name} → «{item['article_name']}» ===")
            for rule in item["rules"]:
                print(f"  правило: {rule.name}")
            for payment in item["payments"]:
                month = accounting_periods.month_start(payment.operation_date)
                mark = " (МЕСЯЦ ЗАКРЫТ — пропуск)" if month in closed else ""
                print(f"  {payment.operation_date}  {payment.amount:>10}{mark}")
                if month not in closed:
                    total += Decimal(payment.amount)
        if not targets:
            print("Расхождений нет")
            return
        print(f"\nИТОГО к переразметке: {total} ₽")

        if not apply:
            print("\nВхолостую: ничего не изменено. Боевой запуск — с флагом --apply")
            return

        for item in targets:
            for rule in item["rules"]:
                rule.article_id = item["article_id"]
            for payment in item["payments"]:
                if accounting_periods.month_start(payment.operation_date) in closed:
                    continue
                payment.article_id = item["article_id"]
                if payment.location_id is None and location_id is not None:
                    payment.location_id = location_id
        await session.commit()
        print("\nГотово: правила и проводки переразмечены")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="боевой запуск")
    asyncio.run(main(parser.parse_args().apply))
