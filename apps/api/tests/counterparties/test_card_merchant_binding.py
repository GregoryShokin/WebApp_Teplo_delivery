"""Карт-операции опознаются по имени мерчанта, а не по реквизитам эквайера.

Инцидент 03.08.2026: «Запомнить» на карт-оплате хостинга ihc.ru расширило правило до «любой
расход с ИНН 7710140679» (это ИНН самого T-Банка — он стоит получателем во ВСЕХ карт-оплатах),
и покупка в Ozon на 5 972 ₽, «Магнит» и «Магистр» уехали в статью «Оплаты систем автоматизации»
к контрагенту IHC.ru. Тесты закрепляют новый контур: личность продавца берётся из текста
назначения, а чужие правила при этом не расширяются.
"""

from __future__ import annotations

from decimal import Decimal

from cp_helpers import (
    make_account,
    make_bank_operation,
    make_counterparty,
    make_expense_article,
    make_wallet,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.v1.routes.dds import _remember_binding_rule
from app.models import (
    CashflowTransaction,
    ClassificationRule,
    CounterpartyAlias,
    CounterpartyPayableProfile,
)
from app.services.banking.classifier import run_classification_rules

ACQUIRER_INN = "7710140679"
ACQUIRER_NAME = 'АО "ТБанк"'
ACQUIRER_TRANSIT_ACCOUNT = "30232810700020000002"


async def _card_operation(
    session: AsyncSession, *, purpose: str, amount: str, account_id, op_id: str
):
    operation = await make_bank_operation(
        session,
        amount=Decimal(amount),
        direction="out",
        inn=ACQUIRER_INN,
        name=ACQUIRER_NAME,
        account=ACQUIRER_TRANSIT_ACCOUNT,
        account_id=account_id,
        provider_operation_id=op_id,
    )
    operation.payment_purpose = purpose
    await session.flush()
    return operation


async def _set_default_article(session: AsyncSession, counterparty_id, article_id) -> None:
    profile = (
        await session.execute(
            select(CounterpartyPayableProfile).where(
                CounterpartyPayableProfile.counterparty_id == counterparty_id
            )
        )
    ).scalar_one()
    profile.default_dds_article_id = article_id


async def test_remember_on_card_operation_binds_merchant_not_acquirer(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Регрессия инцидента: «запомнить» больше не превращается в правило по ИНН банка."""
    async with async_session_factory() as session:
        account = await make_account(session)
        await make_wallet(session, name="T-Bank", wallet_type="bank", account_id=account.id)
        ihc = await make_counterparty(session, name="IHC.ru (поставщик серверов)")
        automation = await make_expense_article(
            session, code="automation_systems", name="Оплаты систем автоматизации"
        )
        # Сеяное правило банка по тому же ИНН: платежи «за обслуживание» — это РКО.
        bank_fee = await make_expense_article(session, code="bank_fee", name="РКО")
        session.add(
            ClassificationRule(
                name="T-Bank: РКО — плата за обслуживание",
                priority=30,
                is_active=True,
                provider="tbank",
                direction="out",
                counterparty_inn_match=ACQUIRER_INN,
                purpose_pattern="плата за",
                action="set_article",
                article_id=bank_fee.id,
            )
        )
        await session.flush()

        paid = await _card_operation(
            session,
            purpose="Оплата в YM*ihc.ru MOSKVA RUS",
            amount="5000.00",
            account_id=account.id,
            op_id="card-ihc-1",
        )
        remembered = await _remember_binding_rule(
            session,
            paid,
            article_id=automation.id,
            counterparty_id=ihc.id,
            comment="test",
        )
        await session.flush()

        assert remembered.warning is None
        rule = remembered.rule
        assert rule is not None
        # Правило про продавца: подстрока назначения, без ИНН и без имени из выписки.
        assert rule.purpose_pattern == "ihc.ru"
        assert rule.counterparty_inn_match is None
        assert rule.counterparty_name_pattern is None
        assert rule.counterparty_id == ihc.id

        # Сеяное правило РКО не тронуто — ни статья, ни паттерн.
        bank_rule = await session.scalar(
            select(ClassificationRule).where(
                ClassificationRule.name == "T-Bank: РКО — плата за обслуживание"
            )
        )
        assert bank_rule is not None
        assert bank_rule.purpose_pattern == "плата за"
        assert bank_rule.article_id == bank_fee.id
        assert bank_rule.counterparty_id is None

        # Имя мерчанта заодно стало псевдонимом карточки.
        alias = await session.scalar(
            select(CounterpartyAlias).where(CounterpartyAlias.counterparty_id == ihc.id)
        )
        assert alias is not None
        assert alias.alias == "ihc.ru"

        # Покупка в Ozon — тот же ИНН и тот же счёт эквайера — на IHC.ru НЕ уходит.
        ozon = await _card_operation(
            session,
            purpose="Оплата в OZON Moskva RUS",
            amount="5972.00",
            account_id=account.id,
            op_id="card-ozon-1",
        )
        # А следующая оплата хостинга (другой город в тексте) разберётся сама.
        next_ihc = await _card_operation(
            session,
            purpose="Оплата в YM*ihc.ru Sankt-Peterburg RUS",
            amount="600.00",
            account_id=account.id,
            op_id="card-ihc-2",
        )
        await run_classification_rules(session, [ozon, next_ihc])
        await session.commit()

        assert ozon.classification_status == "needs_review"
        assert next_ihc.classification_status == "classified"
        booked = await session.get(CashflowTransaction, next_ihc.cashflow_transaction_id)
        assert booked is not None
        assert booked.counterparty_id == ihc.id
        assert booked.article_id == automation.id


async def test_registry_matches_card_merchant_by_counterparty_name(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Правил нет — но продавец назван в тексте, и такая карточка в реестре одна."""
    async with async_session_factory() as session:
        account = await make_account(session)
        await make_wallet(session, name="T-Bank", wallet_type="bank", account_id=account.id)
        ihc = await make_counterparty(session, name="IHC.ru (поставщик серверов)")
        article = await make_expense_article(
            session, code="automation_systems", name="Оплаты систем автоматизации"
        )
        await _set_default_article(session, ihc.id, article.id)
        await session.flush()

        operation = await _card_operation(
            session,
            purpose="Оплата в YM*ihc.ru MOSKVA RUS",
            amount="5000.00",
            account_id=account.id,
            op_id="card-registry-1",
        )
        result = await run_classification_rules(session, [operation])
        await session.commit()

        assert result.classified == 1
        assert operation.classification_status == "classified"
        booked = await session.get(CashflowTransaction, operation.cashflow_transaction_id)
        assert booked is not None
        assert booked.counterparty_id == ihc.id
        assert booked.article_id == article.id


async def test_registry_match_needs_default_article(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Карточка нашлась, а статьи по умолчанию нет — статью не выдумываем."""
    async with async_session_factory() as session:
        account = await make_account(session)
        await make_wallet(session, name="T-Bank", wallet_type="bank", account_id=account.id)
        await make_counterparty(session, name="IHC.ru (поставщик серверов)")
        await session.flush()

        operation = await _card_operation(
            session,
            purpose="Оплата в YM*ihc.ru MOSKVA RUS",
            amount="5000.00",
            account_id=account.id,
            op_id="card-registry-2",
        )
        await run_classification_rules(session, [operation])
        await session.commit()

        assert operation.classification_status == "needs_review"


async def test_remember_refuses_to_hijack_overlapping_merchant_rule(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Чужой паттерн-надмножество не расширяем молча — возвращаем причину владельцу."""
    async with async_session_factory() as session:
        account = await make_account(session)
        await make_wallet(session, name="T-Bank", wallet_type="bank", account_id=account.id)
        mango = await make_counterparty(session, name="Манго Телеком, ООО", inn="7709501144")
        other = await make_counterparty(session, name="Прочий продавец")
        telecom = await make_expense_article(session, code="telecom", name="Телекоммуникации")
        automation = await make_expense_article(
            session, code="automation_systems", name="Оплаты систем автоматизации"
        )
        session.add(
            ClassificationRule(
                name="T-Bank: Телекоммуникации — Mango Office",
                priority=28,
                is_active=True,
                provider="tbank",
                direction="out",
                purpose_pattern="mango",
                action="set_article",
                article_id=telecom.id,
                counterparty_id=mango.id,
            )
        )
        await session.flush()

        operation = await _card_operation(
            session,
            purpose="Оплата в MANGO-OFFICE.RU MOSKVA RUS",
            amount="5000.00",
            account_id=account.id,
            op_id="card-mango-1",
        )
        remembered = await _remember_binding_rule(
            session,
            operation,
            article_id=automation.id,
            counterparty_id=other.id,
            comment="test",
        )
        await session.flush()

        assert remembered.rule is None
        assert remembered.warning is not None
        assert "Mango Office" in remembered.warning
        # Чужое правило осталось как было.
        existing = await session.scalar(
            select(ClassificationRule).where(ClassificationRule.purpose_pattern == "mango")
        )
        assert existing is not None
        assert existing.counterparty_id == mango.id
        assert existing.article_id == telecom.id


async def test_remember_refuses_rule_without_any_condition(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ни ИНН, ни назначения — правило без единого условия забрало бы весь исходящий поток."""
    async with async_session_factory() as session:
        account = await make_account(session)
        await make_wallet(session, name="T-Bank", wallet_type="bank", account_id=account.id)
        cp = await make_counterparty(session, name="Безымянный получатель")
        article = await make_expense_article(session, code="other", name="Прочие расходы")
        operation = await make_bank_operation(
            session,
            amount=Decimal("1000.00"),
            direction="out",
            account_id=account.id,
            provider_operation_id="blank-1",
        )
        await session.flush()

        remembered = await _remember_binding_rule(
            session, operation, article_id=article.id, counterparty_id=cp.id, comment="test"
        )

        assert remembered.rule is None
        assert remembered.warning is not None


async def test_remember_keeps_inn_binding_for_real_counterparty(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """У обычной платёжки ИНН настоящий — привязка по нему остаётся прежней."""
    async with async_session_factory() as session:
        account = await make_account(session)
        await make_wallet(session, name="T-Bank", wallet_type="bank", account_id=account.id)
        supplier = await make_counterparty(session, name="ООО Поставщик", inn="6168026120")
        article = await make_expense_article(session, code="supplier", name="Оплата поставщикам")

        operation = await make_bank_operation(
            session,
            amount=Decimal("12000.00"),
            direction="out",
            inn="6168026120",
            name="ООО Поставщик",
            account_id=account.id,
            provider_operation_id="wire-1",
        )
        operation.payment_purpose = "Оплата по счету 513573 от 16.06.2026"
        await session.flush()

        remembered = await _remember_binding_rule(
            session,
            operation,
            article_id=article.id,
            counterparty_id=supplier.id,
            comment="test",
        )
        await session.flush()

        assert remembered.warning is None
        assert remembered.rule is not None
        assert remembered.rule.counterparty_inn_match == "6168026120"
        # Текст с номером счёта и датой в правило не попадает — иначе оно сработает один раз.
        assert remembered.rule.purpose_pattern is None
