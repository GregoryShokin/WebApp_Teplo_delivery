"""Этап 3: банк-черновик выдачи депозита (курьеры + производство).

Проверяем общий модуль ``deposit_bank_draft``:
1. транзит банк→Сейф (пара проводок, идемпотентность, no-op без кошельков);
2. черновик платежа в Т-Банк на ИП Шокину (вызывается при наличии реквизитов, no-op без них);
3. производственная выдача с ``payout_method='bank_draft'`` списывает расход с Сейфа.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin_payout_split import _payer_wallet, _safe_wallet
from test_payroll_payouts import RecordingBankClient

from app.models import (
    AppSetting,
    CashflowTransaction,
    DdsArticle,
    DepositTransaction,
    Employee,
)
from app.services.deposit_bank_draft import (
    PRODUCTION_DEPOSIT_PAYOUT_DRAFT_SOURCE_KIND,
    book_deposit_bank_to_safe_transfer,
    send_deposit_payout_bank_draft,
)
from app.services.deposit_service import (
    PRODUCTION_DEPOSIT_PAYOUT_ARTICLE_CODE,
    PRODUCTION_DEPOSIT_PAYOUT_SOURCE_KIND,
    book_production_deposit_payout_cashflow,
)

REQUISITES_KEY = "payroll.bank_payout_requisites"


async def _seed_article(session: AsyncSession, code: str, name: str) -> uuid.UUID:
    existing = await session.scalar(select(DdsArticle).where(DdsArticle.code == code))
    if existing is not None:
        return existing.id
    article = DdsArticle(
        id=uuid.uuid4(),
        code=code,
        name=name,
        movement_type="outflow",
        activity_type="operating",
    )
    session.add(article)
    await session.flush()
    return article.id


async def _seed_employee(session: AsyncSession) -> Employee:
    employee = Employee(
        id=uuid.uuid4(),
        full_name=f"Повар {uuid.uuid4().hex[:6]}",
        iiko_id=f"iiko-{uuid.uuid4()}",
        category="category_2",
        status="active",
        is_senior=False,
        is_deputy_senior=False,
        pin_hash="hashed-pin",
        pin_set_at=datetime(2026, 5, 1, tzinfo=UTC),
        created_at=datetime(2026, 5, 1, tzinfo=UTC),
        updated_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    session.add(employee)
    await session.flush()
    return employee


async def _transfer_txns(
    session: AsyncSession, source_id: uuid.UUID
) -> list[CashflowTransaction]:
    return list(
        (
            await session.scalars(
                select(CashflowTransaction).where(
                    CashflowTransaction.source_kind
                    == PRODUCTION_DEPOSIT_PAYOUT_DRAFT_SOURCE_KIND,
                    CashflowTransaction.source_id == source_id,
                )
            )
        ).all()
    )


async def test_bank_to_safe_transfer_books_pair_idempotently(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        _account, payer_wallet = await _payer_wallet(session)
        safe_wallet = await _safe_wallet(session)
        source_id = uuid.uuid4()

        created = await book_deposit_bank_to_safe_transfer(
            session,
            source_kind=PRODUCTION_DEPOSIT_PAYOUT_DRAFT_SOURCE_KIND,
            source_id=source_id,
            amount=Decimal("5000"),
            operation_date=date(2026, 6, 22),
            purpose="Выдача депозита (через Сейф)",
        )
        assert created is True

        rows = await _transfer_txns(session, source_id)
        assert len(rows) == 2
        bank_leg = next(t for t in rows if t.wallet_id == payer_wallet.id)
        safe_leg = next(t for t in rows if t.wallet_id == safe_wallet.id)
        assert bank_leg.direction == "out" and bank_leg.amount == Decimal("5000.00")
        assert safe_leg.direction == "in" and safe_leg.amount == Decimal("5000.00")

        # Идемпотентность: повторный вызов с тем же source_id не дублирует пару.
        again = await book_deposit_bank_to_safe_transfer(
            session,
            source_kind=PRODUCTION_DEPOSIT_PAYOUT_DRAFT_SOURCE_KIND,
            source_id=source_id,
            amount=Decimal("5000"),
            operation_date=date(2026, 6, 22),
            purpose="Выдача депозита (через Сейф)",
        )
        assert again is False
        assert len(await _transfer_txns(session, source_id)) == 2


async def test_bank_to_safe_transfer_noop_without_safe_wallet(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Без активного Сейф-кошелька транзит не создаётся (выдачу не валим)."""
    async with async_session_factory() as session:
        await _payer_wallet(session)
        # Сейф деактивируем, чтобы resolve вернул None.
        safe_wallet = await _safe_wallet(session)
        safe_wallet.status = "inactive"
        await session.flush()
        source_id = uuid.uuid4()
        created = await book_deposit_bank_to_safe_transfer(
            session,
            source_kind=PRODUCTION_DEPOSIT_PAYOUT_DRAFT_SOURCE_KIND,
            source_id=source_id,
            amount=Decimal("5000"),
            operation_date=date(2026, 6, 22),
            purpose="Выдача депозита (через Сейф)",
        )
        assert created is False
        assert await _transfer_txns(session, source_id) == []


async def test_send_bank_draft_calls_client_with_requisites(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Реквизиты ИП Шокиной засеяны миграцией 0077 — отдельно сеять не нужно.
    async with async_session_factory() as session:
        client = RecordingBankClient()
        sent = await send_deposit_payout_bank_draft(
            session,
            document_id="teplo-deposit-abc",
            amount=Decimal("5000"),
            purpose="Выдача депозита (через Сейф)",
            bank_client=client,
        )
        assert sent is True
        assert len(client.drafts) == 1
        draft = client.drafts[0]
        assert draft["document_id"] == "teplo-deposit-abc"
        assert draft["amount"] == Decimal("5000")
        assert draft["requisites"]["recipientName"] == "Шокина Кристина Юрьевна"
        assert draft["requisites"]["inn"] == "890307589201"
        assert "kpp" not in draft["requisites"]
        assert draft["requisites"]["bankAcnt"] == "40817810800023540968"
        assert draft["requisites"]["bankBik"] == "044525974"
        assert draft["requisites"]["corrAccount"] == "30101810145250000974"


async def test_send_bank_draft_uses_code_constant_without_setting(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        # Даже удаление настройки не может перенаправить/сломать финансовый получатель:
        # runtime использует зафиксированный владельцем эталон из кода.
        await session.execute(delete(AppSetting).where(AppSetting.key == REQUISITES_KEY))
        await session.flush()
        client = RecordingBankClient()
        sent = await send_deposit_payout_bank_draft(
            session,
            document_id="teplo-deposit-xyz",
            amount=Decimal("5000"),
            purpose="Выдача депозита (через Сейф)",
            bank_client=client,
        )
        assert sent is True
        assert client.drafts[0]["requisites"]["bankAcnt"] == "40817810800023540968"


async def test_production_payout_bank_draft_books_expense_from_safe(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """payout_method='bank_draft' → расход «Выдача депозита» списывается с Сейфа, не с ТК."""
    async with async_session_factory() as session:
        safe_wallet = await _safe_wallet(session)
        await _seed_article(
            session, PRODUCTION_DEPOSIT_PAYOUT_ARTICLE_CODE, "Выдача депозита"
        )
        employee = await _seed_employee(session)
        tx = DepositTransaction(
            id=uuid.uuid4(),
            employee_id=employee.id,
            run_id=None,
            transaction_type="payout",
            amount=Decimal("5000"),
        )
        session.add(tx)
        await session.flush()

        wallet = await book_production_deposit_payout_cashflow(
            session,
            transaction=tx,
            payout_method="bank_draft",
            transaction_date=date(2026, 6, 22),
            comment=None,
        )
        assert wallet is not None and wallet.code == "cash_safe"

        expense = await session.scalar(
            select(CashflowTransaction).where(
                CashflowTransaction.source_kind == PRODUCTION_DEPOSIT_PAYOUT_SOURCE_KIND,
                CashflowTransaction.source_id == tx.id,
            )
        )
        assert expense is not None
        assert expense.direction == "out"
        assert expense.wallet_id == safe_wallet.id
        assert expense.amount == Decimal("5000.00")
