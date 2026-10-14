"""Признанный расход, которому в отчёте нет строки, обязан назвать, ЧЕЙ он и ЧТО нажать.

С июля по сентябрь 2026 акты Яндекс Еды (комиссия агрегатора: 8 768,51 / 12 381,21 /
14 333,47 ₽) приходили через СБИС и признавались по периоду документа, но статьи ДДС у них не
было ни на документе, ни в карточке контрагента. Проектор складывал их в корзину без статьи,
строки для неё нет — и отчёт писал только «признанный расход без статьи ДДС», не называя ни
документа, ни контрагента. Строка «Комиссия агрегатору» при этом стояла подтверждённым нулём.

Рядом жила дыра тише: признание по статье, которую завели уже ПОСЛЕ разметки источников
ОПиУ. Строки у неё нет, и ``_apply_recognition`` пропускал её без единого слова — касса по той
же статье честно называется неразнесённой, признание пропадало молча.

Замки ниже держат три вещи:
* расход без статьи называет контрагента и сумму;
* статья в карточке контрагента («Комиссия агрегатору») доводит акт до строки
  ``aggregator_commission`` целиком, без поправки руками;
* признание по статье без разметки ОПиУ даёт тревогу, а по статье, явно размеченной
  «вне ОПиУ», — нет: там отказ — решение, а не пробел.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

sys.path.append(str(Path(__file__).parent / "counterparties"))

from app.models import (
    CounterpartyPayableProfile,
    DdsArticle,
    PnlArticleRule,
    SupplierExpenseAccrual,
)
from app.services.pnl import projector

AUGUST = date(2026, 8, 1)
ACT_AMOUNT = Decimal("12381.21")


def _line(report, code: str):
    return next(line for line in report.lines if line.code == code)


def _warning(report, code: str):
    return next((w for w in report.warnings if w.code == code), None)


async def _yandex_act(session, *, article_id=None):
    """Контрагент-агрегатор и его акт за август, признанный по периоду документа."""
    from cp_helpers import make_counterparty, make_invoice

    yandex = await make_counterparty(session, name="Яндекс.Еда", inn="9705114405")
    act = await make_invoice(
        session,
        counterparty_id=yandex.id,
        amount=ACT_AMOUNT,
        vat_total="2232.68",
        source="sbis",
        doc_kind="closing",
        operational_scope="finance",
        number="BFA-A0036079423",
        invoice_date=date(2026, 8, 31),
    )
    session.add(
        SupplierExpenseAccrual(
            counterparty_id=yandex.id,
            invoice_id=act.id,
            article_id=article_id,
            amount=ACT_AMOUNT,
            status="recognized",
            service_period_start=date(2026, 8, 1),
            service_period_end=date(2026, 8, 31),
            recognition_month=AUGUST,
        )
    )
    await session.flush()
    return yandex


async def _aggregator_article(session) -> DdsArticle:
    """Статья, размеченная в строку «Комиссия агрегатору» миграцией источников ОПиУ."""
    article_id = await session.scalar(
        select(PnlArticleRule.article_id).where(
            PnlArticleRule.line_code == "aggregator_commission",
            PnlArticleRule.is_active.is_(True),
            PnlArticleRule.in_pnl.is_(True),
        )
    )
    assert article_id is not None, "разметка ОПиУ потеряла статью «Комиссия агрегатору»"
    return await session.get(DdsArticle, article_id)


def test_act_without_article_names_the_counterparty(async_session_factory) -> None:
    """Акт агрегатора без статьи: в строку не попал, и тревога называет Яндекс.Еду."""

    async def scenario() -> None:
        async with async_session_factory() as session:
            await _yandex_act(session)
            await session.commit()

            report = await projector.build_report(session, AUGUST)

            assert report.quality["unattributed"] == ACT_AMOUNT
            assert _line(report, "aggregator_commission").amount in (None, Decimal("0.00"))
            alarm = _warning(report, "recognition_unattributed")
            assert alarm is not None
            assert alarm.amount == ACT_AMOUNT
            text = alarm.message.replace(" ", " ")
            assert "Яндекс.Еда — 12 381,21 ₽" in text, (
                f"тревога не назвала, чей документ без статьи: {alarm.message}"
            )
            assert "карточке контрагента" in text

    asyncio.run(scenario())


def test_card_article_brings_the_act_into_aggregator_commission(async_session_factory) -> None:
    """Статья в карточке — и акт целиком в «Комиссии агрегатору», тревоги нет.

    Исправление на данных — одно поле карточки, без правки документов: проектор подставляет
    статью контрагента в момент сборки, поэтому оживают и уже признанные июль–сентябрь.
    """

    async def scenario() -> None:
        async with async_session_factory() as session:
            yandex = await _yandex_act(session)
            article = await _aggregator_article(session)
            profile = await session.scalar(
                select(CounterpartyPayableProfile).where(
                    CounterpartyPayableProfile.counterparty_id == yandex.id
                )
            )
            profile.default_dds_article_id = article.id
            await session.commit()

            report = await projector.build_report(session, AUGUST)

            line = _line(report, "aggregator_commission")
            assert line.amount == ACT_AMOUNT
            assert any(
                c.stream == "recognition" and c.amount == ACT_AMOUNT for c in line.components
            )
            assert report.quality["unattributed"] == Decimal("0.00")
            assert _warning(report, "recognition_unattributed") is None
            assert _warning(report, "recognition_unmapped_article") is None

    asyncio.run(scenario())


def test_recognition_under_unruled_article_is_not_silent(async_session_factory) -> None:
    """Статья без разметки ОПиУ: тревога с её именем. Статья «вне ОПиУ»: тишина по праву."""

    async def scenario() -> None:
        async with async_session_factory() as session:
            unruled = DdsArticle(
                code="test_new_after_rules",
                name="Покупка бензина",
                movement_type="outflow",
                activity_type="operating",
            )
            outside = DdsArticle(
                code="test_outside_pnl",
                name="Вне отчёта по решению",
                movement_type="outflow",
                activity_type="operating",
            )
            session.add_all([unruled, outside])
            await session.flush()
            session.add(
                PnlArticleRule(
                    article_id=outside.id,
                    line_code=None,
                    in_pnl=False,
                    owner_stream="cash",
                    sign=1,
                    applies_to="both",
                    is_active=True,
                )
            )
            await _yandex_act(session, article_id=unruled.id)
            from cp_helpers import make_counterparty

            other = await make_counterparty(session, name="Разметка вне ОПиУ", inn="6155037999")
            session.add(
                SupplierExpenseAccrual(
                    counterparty_id=other.id,
                    article_id=outside.id,
                    amount=Decimal("5000.00"),
                    status="recognized",
                    service_period_start=date(2026, 8, 1),
                    service_period_end=date(2026, 8, 31),
                    recognition_month=AUGUST,
                )
            )
            await session.commit()

            report = await projector.build_report(session, AUGUST)

            alarm = _warning(report, "recognition_unmapped_article")
            assert alarm is not None, "признание по неразмеченной статье пропало молча"
            assert alarm.amount == ACT_AMOUNT, (
                "статья, явно размеченная «вне ОПиУ», попала в тревогу о пробеле"
            )
            text = alarm.message.replace(" ", " ")
            assert "«Покупка бензина» — 12 381,21 ₽" in text
            assert "Вне отчёта по решению" not in text

    asyncio.run(scenario())
