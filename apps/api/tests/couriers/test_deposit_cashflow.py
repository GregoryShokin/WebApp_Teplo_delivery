"""Денежная связь курьерского депозита с ДДС.

Пополнение (TOP_UP) — приход на «Торговую кассу Черникова» по статье «Курьерская служба +».
Удержание (FORFEIT) — учётная операция, в ДДС не попадает. Статья/счёт/должности берутся
из baseline-миграций тестовой БД.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import CashflowTransaction, DdsArticle, Employee, User, Wallet
from app.services.couriers import deposit_service
from app.services.position_registry import (
    courier_positions,
    refresh_position_registry,
    reset_position_registry_for_tests,
)


async def _courier(session: AsyncSession) -> Employee:
    await refresh_position_registry(session)
    positions = courier_positions()
    assert positions, "курьерские должности засеяны миграциями"
    employee = Employee(
        id=uuid.uuid4(),
        full_name="Курьер Тест",
        iiko_id=f"iiko-{uuid.uuid4()}",
        position=positions[0],
        status="active",
    )
    session.add(employee)
    await session.flush()
    return employee


async def _user(session: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex[:8]}@teplo.local",
        hashed_password="x",
        full_name="Админ Тест",
    )
    session.add(user)
    await session.flush()
    return user


async def test_topup_books_inflow_to_chernikova(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    reset_position_registry_for_tests()
    try:
        async with async_session_factory() as session:
            employee = await _courier(session)
            user = await _user(session)

            await deposit_service.create_transaction(
                session,
                employee_id=employee.id,
                transaction_type="top_up",
                amount_cents=50_000,  # 500 ₽
                transaction_date=date(2026, 6, 26),
                comment="пополнение",
                created_by_user_id=user.id,
            )

            rows = (
                await session.scalars(
                    select(CashflowTransaction).where(
                        CashflowTransaction.source_kind == "courier_deposit_topup"
                    )
                )
            ).all()
            assert len(rows) == 1
            cashflow = rows[0]
            assert cashflow.direction == "in"
            assert cashflow.amount == Decimal("500.00")
            assert cashflow.quality_status == "final"

            wallet = await session.get(Wallet, cashflow.wallet_id)
            article = await session.get(DdsArticle, cashflow.article_id)
            assert wallet is not None
            assert article is not None
            assert wallet.code == deposit_service.DEPOSIT_RETURN_CASH_WALLET_CODE
            assert article.code == deposit_service.COURIER_DEPOSIT_TOPUP_ARTICLE_CODE
    finally:
        reset_position_registry_for_tests()


async def test_forfeit_books_no_cashflow(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    reset_position_registry_for_tests()
    try:
        async with async_session_factory() as session:
            employee = await _courier(session)
            user = await _user(session)

            await deposit_service.create_transaction(
                session,
                employee_id=employee.id,
                transaction_type="forfeit",
                amount_cents=10_000,
                transaction_date=date(2026, 6, 26),
                comment="штраф",
                created_by_user_id=user.id,
            )

            rows = (await session.scalars(select(CashflowTransaction))).all()
            assert rows == []
    finally:
        reset_position_registry_for_tests()
