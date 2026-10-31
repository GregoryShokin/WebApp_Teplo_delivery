"""Строки овердрафта Т-Банка: тело долга вне ОПиУ, плата банка в своей строке.

Решение владельца 31.10.2026: выдачу на 36 916,03 ₽ вручную отнесли к статье комиссии, приход
в расходной статье считается возвратом расхода и поднял чистую прибыль октября. Правил на эти
строки не было, настоящую плату забирало правило «плата за» → РКО, а запомненное правило не
узнавало свою строку: банк пишет в назначении двойной пробел.

Скрипт починки запускается настоящей точкой входа ``async_main``: без ``--apply`` ничего не
пишет, повторный запуск ничего не меняет.
"""

from __future__ import annotations

import sys
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    BankOperation,
    CashflowTransaction,
    ClassificationRule,
    DdsArticle,
    ReconciliationCase,
)
from app.scripts import repair_overdraft_classification
from app.services.banking.classifier import (
    apply_operation_action,
    create_or_update_reconciliation_case,
    run_classification_rules,
)
from app.services.pnl import projector

OCTOBER = date(2026, 10, 1)
DRAW_TEXT = "Предоставление  овердрафта 7036075757"
REPAY_TEXT = "Погашение разрешенного овердрафта 7036075757"
ACTIVE_FEE_TEXT = "Плата за активный овердрафт"
LIMIT_FEE_TEXT = "Плата за использованный лимит овердрафта"
DRAW_AMOUNT = Decimal("36916.03")
REPAY_AMOUNT = Decimal("4158.20")
FEE_AMOUNT = Decimal("490.00")
FEE_ARTICLE_CODE = "overdraft"
DRAW_ARTICLE_CODE = "poluchenie_overdrafta"
REPAY_ARTICLE_CODE = "pogashenie_overdrafta"
SERVICE_ARTICLE_CODE = "bank_service_fee"


@pytest.fixture
def run_script(
    monkeypatch: pytest.MonkeyPatch,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> Any:
    """Запустить скрипт его точкой входа: своя сессия, свои ключи."""

    async def run(*args: str) -> None:
        monkeypatch.setattr(
            repair_overdraft_classification, "AsyncSessionLocal", async_session_factory
        )
        monkeypatch.setattr(sys, "argv", ["repair", *args])
        await repair_overdraft_classification.async_main()

    return run


async def article_id(session: AsyncSession, code: str) -> uuid.UUID:
    """Статья по коду: все они заведены миграциями."""
    found = await session.scalar(select(DdsArticle.id).where(DdsArticle.code == code))
    assert found is not None
    return found


async def add_service_fee_rule(session: AsyncSession) -> None:
    """Правило боя «плата за» → РКО: оно есть только в данных, новые правила идут раньше."""
    session.add(
        ClassificationRule(
            name="T-Bank: РКО — плата за обслуживание/пакет/услуги",
            priority=30,
            is_active=True,
            provider="tbank",
            direction="out",
            purpose_pattern="плата за",
            action="set_article",
            article_id=await article_id(session, SERVICE_ARTICLE_CODE),
        )
    )
    await session.flush()


def bank_line(
    purpose: str,
    direction: str,
    amount: Decimal,
    *,
    day: date = date(2026, 10, 30),
) -> BankOperation:
    """Строка выписки Т-Банка от самого банка."""
    return BankOperation(
        provider="tbank",
        provider_operation_id=str(uuid.uuid4()),
        operation_date=day,
        direction=direction,
        amount=amount,
        currency="RUB",
        counterparty_name_raw='АО "ТБанк"',
        counterparty_inn_raw="7710140679",
        payment_purpose=purpose,
        raw_payload={},
    )


async def article_code_of(session: AsyncSession, operation: BankOperation) -> str | None:
    """Код статьи, на которой стоит операция; ``None``, пока проводки нет."""
    if operation.cashflow_transaction_id is None:
        return None
    transaction = await session.get(CashflowTransaction, operation.cashflow_transaction_id)
    assert transaction is not None
    article = await session.get(DdsArticle, transaction.article_id)
    assert article is not None
    return article.code


@pytest.mark.parametrize(
    ("purpose", "direction", "expected_article"),
    [
        (DRAW_TEXT, "in", DRAW_ARTICLE_CODE),
        (REPAY_TEXT, "out", REPAY_ARTICLE_CODE),
        (ACTIVE_FEE_TEXT, "out", FEE_ARTICLE_CODE),
        (LIMIT_FEE_TEXT, "out", FEE_ARTICLE_CODE),
        ("Плата за обслуживание счета", "out", SERVICE_ARTICLE_CODE),
    ],
)
@pytest.mark.asyncio
async def test_bank_line_finds_its_article_by_itself(
    async_session_factory: async_sessionmaker[AsyncSession],
    purpose: str,
    direction: str,
    expected_article: str,
) -> None:
    async with async_session_factory() as session:
        await add_service_fee_rule(session)
        operation = bank_line(purpose, direction, Decimal("100.00"))
        session.add(operation)
        await session.flush()

        await run_classification_rules(session, [operation])

        assert operation.classification_status == "classified"
        assert await article_code_of(session, operation) == expected_article


@pytest.mark.asyncio
async def test_remembered_text_recognises_line_with_double_space(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """«Запомнить» хранит текст без лишних пробелов, а банк их ставит."""
    async with async_session_factory() as session:
        session.add(
            ClassificationRule(
                name="Запомненная строка",
                priority=50,
                is_active=True,
                provider="tbank",
                direction="in",
                purpose_pattern="Перевод от банка 123",
                action="set_article",
                article_id=await article_id(session, DRAW_ARTICLE_CODE),
            )
        )
        operation = bank_line("Перевод  от  банка 123", "in", Decimal("100.00"))
        session.add(operation)
        await session.flush()

        await run_classification_rules(session, [operation])

        assert await article_code_of(session, operation) == DRAW_ARTICLE_CODE


@pytest.mark.asyncio
async def test_only_bank_fee_reaches_overdraft_line_of_report(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Выдача и погашение не трогают прибыль, плата стоит в «Комиссии за овердрафт»."""
    async with async_session_factory() as session:
        await add_service_fee_rule(session)
        operations = [
            bank_line(DRAW_TEXT, "in", DRAW_AMOUNT),
            bank_line(REPAY_TEXT, "out", REPAY_AMOUNT),
            bank_line(ACTIVE_FEE_TEXT, "out", FEE_AMOUNT),
        ]
        session.add_all(operations)
        await session.flush()
        await run_classification_rules(session, operations)
        await session.flush()

        report = await projector.build_report(session, OCTOBER)

        line = next(line for line in report.lines if line.code == "overdraft_fee")
        assert line.title == "Комиссия за овердрафт"
        assert line.amount == FEE_AMOUNT


@pytest.mark.asyncio
async def test_fee_article_is_named_like_its_report_line(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Статья «Овердрафт» рядом с «Получением овердрафта» и привела к ошибке выбора."""
    async with async_session_factory() as session:
        article = await session.scalar(
            select(DdsArticle).where(DdsArticle.code == FEE_ARTICLE_CODE)
        )
        assert article is not None
        assert article.name == "Комиссия за овердрафт"


async def seed_broken_state(
    session: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Состояние боя 31.10: выдача на статье комиссии, правило запомнено, погашение ждёт разбора."""
    fee_article_id = await article_id(session, FEE_ARTICLE_CODE)
    draw = bank_line(DRAW_TEXT, "in", DRAW_AMOUNT)
    repay = bank_line(REPAY_TEXT, "out", REPAY_AMOUNT, day=date(2026, 10, 31))
    repay.classification_status = "needs_review"
    wrong_rule = ClassificationRule(
        name="Owner review tbank выдача",
        priority=50,
        is_active=True,
        provider="tbank",
        direction="in",
        purpose_pattern="Предоставление овердрафта 7036075757",
        action="set_article",
        article_id=fee_article_id,
        comment="Created from operation review",
    )
    session.add_all([draw, repay, wrong_rule])
    await session.flush()
    await apply_operation_action(
        session,
        draw,
        action="set_article",
        article_id=fee_article_id,
        quality_status="owner_review",
    )
    case = await create_or_update_reconciliation_case(
        session,
        kind="unclassified_operation",
        provider=repay.provider,
        bank_operation_id=repay.id,
        payload={},
    )
    await session.flush()
    return draw.id, repay.id, wrong_rule.id, case.id


async def repair_state(
    session: AsyncSession,
    draw_id: uuid.UUID,
    repay_id: uuid.UUID,
    rule_id: uuid.UUID,
    case_id: uuid.UUID,
) -> tuple[str | None, str | None, bool, str]:
    """Всё, что чинит скрипт: статьи двух строк, состояние правила и вопроса."""
    draw = await session.get(BankOperation, draw_id)
    repay = await session.get(BankOperation, repay_id)
    rule = await session.get(ClassificationRule, rule_id)
    case = await session.get(ReconciliationCase, case_id)
    assert draw is not None and repay is not None and rule is not None and case is not None
    return (
        await article_code_of(session, draw),
        await article_code_of(session, repay),
        rule.is_active,
        case.status,
    )


@pytest.mark.asyncio
async def test_repair_moves_draw_and_is_safe_to_repeat(
    async_session_factory: async_sessionmaker[AsyncSession],
    run_script: Any,
) -> None:
    async with async_session_factory() as session:
        await add_service_fee_rule(session)
        ids = await seed_broken_state(session)
        await session.commit()

    await run_script()

    async with async_session_factory() as session:
        assert await repair_state(session, *ids) == (FEE_ARTICLE_CODE, None, True, "pending")

    await run_script("--apply")

    async with async_session_factory() as session:
        repaired = await repair_state(session, *ids)
        assert repaired == (DRAW_ARTICLE_CODE, REPAY_ARTICLE_CODE, False, "resolved")
        rule = await session.get(ClassificationRule, ids[2])
        assert rule is not None
        assert rule.comment is not None
        assert repair_overdraft_classification.RULE_OFF_NOTE in rule.comment
        draw = await session.get(BankOperation, ids[0])
        assert draw is not None
        transaction = await session.get(CashflowTransaction, draw.cashflow_transaction_id)
        assert transaction is not None
        first_comment = rule.comment
        assert (transaction.direction, transaction.amount) == ("in", DRAW_AMOUNT)
        report = await projector.build_report(session, OCTOBER)
        line = next(line for line in report.lines if line.code == "overdraft_fee")
        assert not line.amount

    await run_script("--apply")

    async with async_session_factory() as session:
        assert await repair_state(session, *ids) == repaired
        rule = await session.get(ClassificationRule, ids[2])
        assert rule is not None
        assert rule.comment == first_comment
