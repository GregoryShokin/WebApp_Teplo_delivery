"""Сторож задвоенного возврата: тот же возврат, проведённый наличными и выпиской.

Возврат переплаты гасит дебиторку без аллокации: пересборка берёт все приходы контрагента с
возвратной статьёй. С 25.09 она стоит на всех дверях разбора выписки, и ошибка оператора —
провести один возврат и «Новым платежом» в Сейф, и разбором выписки — сразу гасит аванс на
двойную сумму. Код расчёта здесь прав (в ДДС два прихода), поэтому сторож не чинит, а
предупреждает: окна разбора и «Новый платёж» спрашивают ``/dds/refund-twins`` до проведения.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, timedelta
from decimal import Decimal

from cp_helpers import (
    make_account,
    make_bank_operation,
    make_counterparty,
    make_expense_article,
    make_wallet,
)
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import CashflowTransaction, DdsArticle, Wallet
from app.services.banking.cashflow_classify import EXCLUDED_QUALITY
from app.services.banking.classifier import apply_operation_action
from app.services.refund_twins import REFUND_TWIN_WINDOW_DAYS, find_refund_twins
from app.services.supplier_prepayments import (
    SUPPLIER_REFUND_ARTICLE_CODE,
    create_supplier_prepayment,
    resync_counterparty_refunds,
)

OP_DATE = date(2026, 7, 20)
HEADERS = {"X-User-Role": "finance_manager"}


async def _refund_article(session: AsyncSession) -> DdsArticle:
    return await make_expense_article(
        session, code=SUPPLIER_REFUND_ARTICLE_CODE, name="Возврат переплаты от поставщиков"
    )


async def _income(
    session: AsyncSession,
    *,
    wallet: Wallet,
    counterparty_id: uuid.UUID,
    article_id: uuid.UUID,
    amount: str,
    on: date = OP_DATE,
    source_kind: str = "new_payment_income",
    quality_status: str = "final",
) -> CashflowTransaction:
    txn = CashflowTransaction(
        wallet_id=wallet.id,
        direction="in",
        amount=Decimal(amount),
        operation_date=on,
        article_id=article_id,
        counterparty_id=counterparty_id,
        source_kind=source_kind,
        payment_purpose="Возврат переплаты",
        quality_status=quality_status,
    )
    session.add(txn)
    await session.flush()
    return txn


async def test_cash_refund_then_the_same_statement_is_flagged_and_would_double_settle(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Возврат 300 наличными, следом та же выписка на 300: сторож видит пару с обеих сторон.

    Без предупреждения оператор размечает выписку возвратом, и аванс 1 000 гасится на 600 —
    ровно та дыра, которую сторож должен показать ДО разбора.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Двойной возврат", inn="6155030101")
        safe = await make_wallet(session, name="Сейф", wallet_type="cash_safe")
        refund = await _refund_article(session)
        await make_expense_article(session, code="advance_to_supplier", name="Аванс поставщику")
        advance = await create_supplier_prepayment(
            session,
            counterparty_id=cp.id,
            wallet_id=safe.id,
            amount=Decimal("1000.00"),
            operation_date=OP_DATE - timedelta(days=10),
        )
        cash = await _income(
            session, wallet=safe, counterparty_id=cp.id, article_id=refund.id, amount="300.00"
        )
        account = await make_account(session)
        await make_wallet(session, name="Т-Банк", wallet_type="bank", account_id=account.id)
        operation = await make_bank_operation(
            session,
            amount="300.00",
            direction="in",
            account_id=account.id,
            operation_date=OP_DATE + timedelta(days=2),
        )
        await resync_counterparty_refunds(session, cp.id)
        await session.commit()

        twins = await find_refund_twins(
            session,
            counterparty_id=cp.id,
            amount=Decimal("300.00"),
            on_date=operation.operation_date,
            channel="bank",
        )
        assert [(t.transaction_id, t.channel, t.wallet_name) for t in twins.items] == [
            (cash.id, "cash", "Сейф")
        ]
        assert not twins.combined

        # Оператор не внял — разметил выписку возвратом: аванс погашен вдвое.
        await apply_operation_action(
            session, operation, action="set_article", article_id=refund.id, counterparty_id=cp.id
        )
        await session.commit()
        await session.refresh(advance)
        assert advance.amount_settled == Decimal("600.00")

        # Теперь и «Новый платёж» видит выписку двойником.
        twins = await find_refund_twins(
            session,
            counterparty_id=cp.id,
            amount=Decimal("300.00"),
            on_date=OP_DATE,
            channel="cash",
        )
        assert [(t.channel, t.source_kind) for t in twins.items] == [("bank", "bank_operation")]


async def test_only_a_same_amount_refund_of_the_other_channel_nearby_is_a_twin(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Двойник — только возврат того же контрагента на ту же сумму, рядом, другим каналом.

    Два прихода выписки одной суммы — два реальных поступления, и своя же проводка при
    переразметке не должна находить саму себя. Исключённая проводка и гашение бартерного займа
    деньгами дебиторку не гасят — значит и двойниками не считаются.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Не двойник", inn="6155030202")
        other_cp = await make_counterparty(session, name="Чужой контрагент", inn="6155030203")
        safe = await make_wallet(session, name="Сейф", wallet_type="cash_safe")
        bank = await make_wallet(session, name="Т-Банк", wallet_type="bank")
        refund = await _refund_article(session)
        income = await make_expense_article(session, code="prochie_dohody", name="Прочие доходы")

        async def bank_income(**kwargs) -> CashflowTransaction:
            return await _income(
                session,
                wallet=bank,
                counterparty_id=kwargs.pop("counterparty_id", cp.id),
                article_id=kwargs.pop("article_id", refund.id),
                source_kind="bank_operation",
                **kwargs,
            )

        await bank_income(amount="300.01")  # другая сумма
        await bank_income(amount="300.00", counterparty_id=other_cp.id)  # другой контрагент
        await bank_income(amount="300.00", article_id=income.id)  # не возврат
        await bank_income(
            amount="300.00", on=OP_DATE + timedelta(days=REFUND_TWIN_WINDOW_DAYS + 1)
        )  # вне окна
        await bank_income(amount="300.00", quality_status=EXCLUDED_QUALITY)  # исключена
        await _income(  # тот же канал — наличные против наличных
            session, wallet=safe, counterparty_id=cp.id, article_id=refund.id, amount="300.00"
        )
        await session.commit()

        assert (
            await find_refund_twins(
                session,
                counterparty_id=cp.id,
                amount=Decimal("300.00"),
                on_date=OP_DATE,
                channel="cash",
            )
        ).items == []

        edge = await bank_income(
            amount="300.00", on=OP_DATE - timedelta(days=REFUND_TWIN_WINDOW_DAYS)
        )
        await session.commit()
        twins = await find_refund_twins(
            session,
            counterparty_id=cp.id,
            amount=Decimal("300"),
            on_date=OP_DATE,
            channel="cash",
        )
        assert [t.transaction_id for t in twins.items] == [edge.id], "край окна включён"


async def test_refund_split_into_shares_is_compared_as_one_payment(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Выписка 300, разнесённая двумя возвратными долями по 150, — один платёж на 300.

    Пересборка спишет аванс на все 300, а построчное сравнение с наличными 300 молчало бы
    (находка скептика 25.09): доли одной операции складываются, окно разбора спрашивает суммой.
    """
    from app.services.banking.classifier import OperationSplitLine, apply_operation_split

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Возврат долями", inn="6155030404")
        safe = await make_wallet(session, name="Сейф", wallet_type="cash_safe")
        refund = await _refund_article(session)
        account = await make_account(session)
        await make_wallet(session, name="Т-Банк", wallet_type="bank", account_id=account.id)
        operation = await make_bank_operation(
            session, amount="300.00", direction="in", account_id=account.id, operation_date=OP_DATE
        )
        await session.commit()
        await apply_operation_split(
            session,
            operation,
            splits=[
                OperationSplitLine(refund.id, Decimal("150.00")),
                OperationSplitLine(refund.id, Decimal("150.00")),
            ],
            counterparty_id=cp.id,
        )
        await session.commit()

        # «Новый платёж» на 300 наличными видит выписку одним платежом на 300.
        twins = await find_refund_twins(
            session,
            counterparty_id=cp.id,
            amount=Decimal("300.00"),
            on_date=OP_DATE,
            channel="cash",
        )
        assert [(t.amount, t.source_kind) for t in twins.items] == [
            (Decimal("300.00"), "bank_operation")
        ]
        assert not twins.combined

        # И наоборот: наличные 300 уже есть — окно разбора спрашивает суммой долей (300).
        await _income(
            session, wallet=safe, counterparty_id=cp.id, article_id=refund.id, amount="300.00"
        )
        await session.commit()
        twins = await find_refund_twins(
            session,
            counterparty_id=cp.id,
            amount=Decimal("300.00"),
            on_date=OP_DATE,
            channel="bank",
        )
        assert [t.channel for t in twins.items] == ["cash"]


async def test_refund_entered_in_parts_is_flagged_by_the_window_total(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Наличные 150 + 150 двумя приходами против выписки на 300 — та же ошибка в два приёма."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Возврат частями", inn="6155030505")
        safe = await make_wallet(session, name="Сейф", wallet_type="cash_safe")
        refund = await _refund_article(session)
        first = await _income(
            session, wallet=safe, counterparty_id=cp.id, article_id=refund.id, amount="150.00"
        )
        second = await _income(
            session,
            wallet=safe,
            counterparty_id=cp.id,
            article_id=refund.id,
            amount="150.00",
            on=OP_DATE + timedelta(days=1),
        )
        await session.commit()

        twins = await find_refund_twins(
            session,
            counterparty_id=cp.id,
            amount=Decimal("300.00"),
            on_date=OP_DATE + timedelta(days=2),
            channel="bank",
        )
        assert twins.combined
        assert [t.transaction_id for t in twins.items] == [first.id, second.id]

        # Сумма окна не сходится — молчим: 150 + 150 не двойник выписки на 250.
        assert (
            await find_refund_twins(
                session,
                counterparty_id=cp.id,
                amount=Decimal("250.00"),
                on_date=OP_DATE,
                channel="bank",
            )
        ).items == []


def test_refund_twins_endpoint_answers_for_an_operation_and_for_a_wallet(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Эндпоинт: операция выписки — канал банк и её дата; кошелёк — его канал и переданная дата."""

    async def seed() -> dict[str, str]:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Возврат через API", inn="6155030303")
            safe = await make_wallet(session, name="Сейф API", wallet_type="cash_safe")
            refund = await _refund_article(session)
            cash = await _income(
                session, wallet=safe, counterparty_id=cp.id, article_id=refund.id, amount="450.50"
            )
            account = await make_account(session)
            bank = await make_wallet(
                session, name="Р/с API", wallet_type="bank", account_id=account.id
            )
            operation = await make_bank_operation(
                session,
                amount="450.50",
                direction="in",
                account_id=account.id,
                operation_date=OP_DATE + timedelta(days=1),
            )
            await session.commit()
            return {
                "cp": str(cp.id),
                "cash": str(cash.id),
                "operation": str(operation.id),
                "safe": str(safe.id),
                "bank": str(bank.id),
            }

    ids = asyncio.run(seed())
    url = "/api/v1/dds/refund-twins"

    by_operation = client.get(
        url,
        params={
            "counterparty_id": ids["cp"],
            "amount": "450.50",
            "bank_operation_id": ids["operation"],
        },
        headers=HEADERS,
    )
    assert by_operation.status_code == 200, by_operation.text
    body = by_operation.json()
    assert body["window_days"] == REFUND_TWIN_WINDOW_DAYS
    assert body["combined"] is False
    assert [
        (item["transaction_id"], item["channel"], item["amount"]) for item in body["items"]
    ] == [(ids["cash"], "cash", "450.50")]

    # Разбор ручной проводки на банковском счёте: тот же двойник по кошельку и дате.
    by_wallet = client.get(
        url,
        params={
            "counterparty_id": ids["cp"],
            "amount": "450.50",
            "wallet_id": ids["bank"],
            "operation_date": str(OP_DATE),
        },
        headers=HEADERS,
    )
    assert [item["transaction_id"] for item in by_wallet.json()["items"]] == [ids["cash"]]

    # Тот же канал — не двойник: наличный возврат не находит сам себя.
    same_channel = client.get(
        url,
        params={
            "counterparty_id": ids["cp"],
            "amount": "450.50",
            "wallet_id": ids["safe"],
            "operation_date": str(OP_DATE),
        },
        headers=HEADERS,
    )
    assert same_channel.json()["items"] == []

    missing = client.get(
        url, params={"counterparty_id": ids["cp"], "amount": "450.50"}, headers=HEADERS
    )
    assert missing.status_code == 422

    forbidden = client.get(
        url,
        params={"counterparty_id": ids["cp"], "amount": "450.50", "wallet_id": ids["safe"]},
        headers={"X-User-Role": "kitchen"},
    )
    assert forbidden.status_code == 403
