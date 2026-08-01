"""«Запомнить» при разборе операции: правило по ИНН, а не по тексту назначения.

Подписочный контрагент (Манго) списывает деньги сам, и в назначении каждого списания —
номер счёта и даты, которые меняются от месяца к месяцу. Старое правило прибивало полный
текст назначения, поэтому срабатывало ровно один раз: июльское списание с другим номером
счёта снова падало в ручной разбор, и владелец разбирал одного и того же контрагента
каждый месяц.

Теперь личность отправителя — его ИНН: правило матчит только по нему (и направлению),
текст и банк-провайдер не прибиваются. Один ИНН — одно правило: повторное «запомнить»
обновляет существующее, а не копит дубли.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
from cp_helpers import (
    admin_headers,
    make_account,
    make_bank_operation,
    make_counterparty,
    make_expense_article,
    make_wallet,
)
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    BankOperation,
    CashflowTransaction,
    ClassificationRule,
    SupplierPrepayment,
)
from app.services.banking.classifier import run_classification_rules

pytestmark = pytest.mark.usefixtures("migrated_db")

MANGO_INN = "7709501144"


def _admin(factory) -> dict[str, str]:
    return asyncio.run(admin_headers(factory))


def _seed(factory: async_sessionmaker[AsyncSession]):
    async def _run():
        async with factory() as session:
            account = await make_account(session)
            await make_wallet(session, wallet_type="bank", account_id=account.id)
            article = await make_expense_article(session, code="svyaz", name="Услуги связи")
            cp = await make_counterparty(session, name="Манго Телеком", inn=MANGO_INN)
            op = await make_bank_operation(
                session,
                amount="5000.00",
                direction="out",
                inn=MANGO_INN,
                name="МАНГО ТЕЛЕКОМ АО",
                account_id=account.id,
            )
            op.payment_purpose = "Оплата по счету № 101 от 30.06.2026 за июнь, НДС не облагается"
            await session.commit()
            return account.id, article.id, cp.id, op.id

    return asyncio.run(_run())


def test_remembered_inn_rule_survives_changed_purpose(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Июньское списание запомнили — июльское с другим текстом разобралось само.

    Проверяется весь путь владельца: один раз указать контрагента в разборе → галка
    «Запомнить» → следующее списание классифицируется без человека, деньги встают
    дебиторкой контрагента (правило 1 канона — предоплату гасит будущий УПД).
    """
    account_id, article_id, cp_id, op_id = _seed(async_session_factory)
    response = client.post(
        f"/api/v1/dds/operations/{op_id}/classify",
        headers=_admin(async_session_factory),
        json={
            "action": "split",
            "splits": [
                {
                    "article_id": str(article_id),
                    "amount": "5000.00",
                    "counterparty_id": str(cp_id),
                }
            ],
            "remember_as_rule": True,
        },
    )
    assert response.status_code == 200, response.text
    rule_id = response.json()["rule_id"]
    assert rule_id is not None

    async def check_rule() -> None:
        async with async_session_factory() as session:
            rule = await session.get(ClassificationRule, uuid.UUID(rule_id))
            assert rule.counterparty_inn_match == MANGO_INN
            assert rule.counterparty_id == uuid.UUID(str(cp_id))
            # Ничего лишнего не прибито: другой текст назначения и другой банк не помешают.
            assert rule.purpose_pattern is None
            assert rule.counterparty_name_pattern is None
            assert rule.provider is None

    asyncio.run(check_rule())

    async def next_month() -> None:
        async with async_session_factory() as session:
            op = await make_bank_operation(
                session,
                amount="10000.00",
                direction="out",
                inn=MANGO_INN,
                name="МАНГО ТЕЛЕКОМ АО",
                account_id=account_id,
                # Другой банк: привязка не должна зависеть от того, чья выписка.
                provider="sber",
            )
            op.payment_purpose = "Оплата по счету № 202 от 31.07.2026 за июль"
            await session.flush()
            await run_classification_rules(session, [op])
            await session.commit()

            refreshed = await session.get(BankOperation, op.id)
            assert refreshed.classification_status == "classified"
            tx = await session.scalar(
                select(CashflowTransaction).where(
                    CashflowTransaction.source_kind == "bank_operation",
                    CashflowTransaction.source_id == op.id,
                )
            )
            assert tx is not None
            assert tx.counterparty_id == uuid.UUID(str(cp_id))
            # Деньги не потерялись в «прочих расходах»: это дебиторка контрагента,
            # которую закроет его будущий УПД.
            prepayment = await session.scalar(
                select(SupplierPrepayment).where(
                    SupplierPrepayment.cashflow_transaction_id == tx.id
                )
            )
            assert prepayment is not None
            assert prepayment.amount == Decimal("10000.00")

    asyncio.run(next_month())


def test_second_remember_updates_the_same_rule(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Повторное «запомнить» по тому же ИНН обновляет правило, а не создаёт второе."""
    account_id, article_id, cp_id, op_id = _seed(async_session_factory)
    headers = _admin(async_session_factory)
    first = client.post(
        f"/api/v1/dds/operations/{op_id}/classify",
        headers=headers,
        json={
            "action": "split",
            "splits": [
                {
                    "article_id": str(article_id),
                    "amount": "5000.00",
                    "counterparty_id": str(cp_id),
                }
            ],
            "remember_as_rule": True,
        },
    )
    assert first.status_code == 200, first.text

    async def second_op() -> tuple[uuid.UUID, uuid.UUID]:
        async with async_session_factory() as session:
            other_article = await make_expense_article(
                session, code="svyaz-2", name="Услуги связи 2"
            )
            op = await make_bank_operation(
                session,
                amount="7000.00",
                direction="out",
                inn=MANGO_INN,
                name="МАНГО ТЕЛЕКОМ АО",
                account_id=account_id,
            )
            op.payment_purpose = "Оплата по счету № 303 от 31.08.2026 за август"
            await session.commit()
            return op.id, other_article.id

    op2_id, other_article_id = asyncio.run(second_op())
    second = client.post(
        f"/api/v1/dds/operations/{op2_id}/classify",
        headers=headers,
        json={
            "action": "split",
            "splits": [
                {
                    "article_id": str(other_article_id),
                    "amount": "7000.00",
                    "counterparty_id": str(cp_id),
                }
            ],
            "remember_as_rule": True,
        },
    )
    assert second.status_code == 200, second.text

    async def check() -> None:
        async with async_session_factory() as session:
            rules = (
                await session.scalars(
                    select(ClassificationRule).where(
                        ClassificationRule.counterparty_inn_match == MANGO_INN
                    )
                )
            ).all()
            assert len(rules) == 1
            # Человек пере-решил — новая статья победила старую.
            assert rules[0].article_id == uuid.UUID(str(other_article_id))

    asyncio.run(check())
