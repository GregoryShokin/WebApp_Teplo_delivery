"""Интеграционные тесты банк-выдачи авансов/займов (Фаза 2).

Поток: выдача с банк-счёта создаёт черновик платежа (аванс «ожидает выплаты», долга нет) →
поллинг ловит «исполнен» → транзит банк→Сейф + резерв Сейфа со статьёй аванса/займа →
кнопка «Выплачено» списывает резерв с Сейфа и формирует долг (аванс → issued). Отмена до
выплаты освобождает резерв (деньги остаются в Сейфе).

Тесты в dev-контейнере не прогоняются (рассинхрон веток/миграций) — запускаются на сошедшихся
ветках/проде; проверка боевого пути — smoke на проде.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_payroll_advance_service import AS_OF, _make_okladnik, _set_oklad
from test_payroll_payouts import RecordingBankClient

from app.models import (
    Account,
    CashflowTransaction,
    DdsArticle,
    SafeAllocation,
    SalaryAdvance,
    SalaryAdvanceBankDraft,
    Wallet,
)
from app.services.banking.classifier import (
    SAFE_WALLET_CODE,
    TRANSFER_IN_ARTICLE_CODE,
    TRANSFER_OUT_ARTICLE_CODE,
)
from app.services.banking.safe_allocations import SAFE_PAYOUT_SOURCE_KIND, pay_allocation
from app.services.payroll_advance_service import (
    ADVANCE_BANK_TO_SAFE_SOURCE_KIND,
    apply_advance_draft_status,
    cancel_advance,
    disburse_bank_advance,
    issue_advance,
    sync_advance_after_allocation_change,
)
from app.services.payroll_payouts import MOCK_PAYER_ACCOUNT


@pytest.fixture(autouse=True)
def _bank_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Детерминированный счёт плательщика (совпадает с тестовым банк-кошельком)."""
    monkeypatch.setattr(
        "app.services.payroll_advance_service.get_settings",
        lambda: SimpleNamespace(
            tbank_api_account_number=MOCK_PAYER_ACCOUNT,
            teplo_bank_client_mode="mock",
        ),
    )


async def _make_bank_wallet(session: AsyncSession) -> Wallet:
    """Банк-кошелёк, чей счёт совпадает с плательщиком (чтобы транзит нашёл его)."""
    account = Account(
        id=uuid.uuid4(),
        bank_code="tbank",
        account_number=MOCK_PAYER_ACCOUNT,
        bic="044525974",
        legal_entity="ИП Шокина Е.А.",
        status="active",
    )
    session.add(account)
    await session.flush()
    wallet = Wallet(
        id=uuid.uuid4(),
        code=f"bank-test-{uuid.uuid4().hex[:8]}",
        name="Тест банк (плательщик)",
        type="bank",
        status="active",
        account_id=account.id,
        opening_balance=Decimal("0"),
    )
    session.add(wallet)
    await session.flush()
    return wallet


async def _cashflows(
    factory: async_sessionmaker[AsyncSession], *, source_kind: str, source_id: uuid.UUID
) -> list[CashflowTransaction]:
    async with factory() as session:
        return list(
            (
                await session.scalars(
                    select(CashflowTransaction).where(
                        CashflowTransaction.source_kind == source_kind,
                        CashflowTransaction.source_id == source_id,
                    )
                )
            ).all()
        )


async def _issue_bank_advance(
    session: AsyncSession,
    *,
    wallet: Wallet,
    amount: Decimal = Decimal("10000"),
    requested_kind: str | None = None,
    allow_loan: bool = False,
) -> SalaryAdvance:
    emp = await _make_okladnik(session)
    await _set_oklad(session, position="Управляющий", amount=Decimal("90000"))
    await session.commit()
    return await issue_advance(
        session,
        employee_id=emp.id,
        amount=amount,
        allow_loan=allow_loan,
        requested_kind=requested_kind,
        issued_on=AS_OF,
        payout_method="transfer",
        wallet_id=wallet.id,
        bank_client=RecordingBankClient(),
    )


async def _draft(factory: async_sessionmaker[AsyncSession], advance_id: uuid.UUID):
    async with factory() as session:
        return await session.scalar(
            select(SalaryAdvanceBankDraft).where(
                SalaryAdvanceBankDraft.advance_id == advance_id
            )
        )


async def test_issue_bank_advance_creates_draft_awaiting_payout(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        wallet = await _make_bank_wallet(session)
        advance = await _issue_bank_advance(session, wallet=wallet)

    assert advance.status == "awaiting_payout"
    draft = await _draft(async_session_factory, advance.id)
    assert draft is not None
    assert draft.status == "created"
    assert draft.provider_ref and draft.provider_ref.startswith("mock-")
    assert draft.safe_allocation_id is None
    # До исполнения банком расхода/резерва ещё нет.
    assert await _cashflows(
        async_session_factory, source_kind="salary_advance", source_id=advance.id
    ) == []
    assert await _cashflows(
        async_session_factory,
        source_kind=ADVANCE_BANK_TO_SAFE_SOURCE_KIND,
        source_id=advance.id,
    ) == []


async def test_bank_advance_paid_books_transit_and_reserve(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        wallet = await _make_bank_wallet(session)
        advance = await _issue_bank_advance(session, wallet=wallet)

    draft = await _draft(async_session_factory, advance.id)
    async with async_session_factory() as session:
        status = await apply_advance_draft_status(
            session, draft=draft, raw_status="EXECUTED", commit=True
        )
    assert status == "paid"

    # Транзит: банк-нога out + Сейф-нога in.
    legs = await _cashflows(
        async_session_factory,
        source_kind=ADVANCE_BANK_TO_SAFE_SOURCE_KIND,
        source_id=advance.id,
    )
    assert len(legs) == 2
    directions = sorted(leg.direction for leg in legs)
    assert directions == ["in", "out"]
    async with async_session_factory() as session:
        out_article = await session.get(
            DdsArticle,
            next(leg.article_id for leg in legs if leg.direction == "out"),
        )
        in_article = await session.get(
            DdsArticle,
            next(leg.article_id for leg in legs if leg.direction == "in"),
        )
        safe_wallet = await session.scalar(
            select(Wallet).where(Wallet.code == SAFE_WALLET_CODE)
        )
    assert out_article.code == TRANSFER_OUT_ARTICLE_CODE
    assert in_article.code == TRANSFER_IN_ARTICLE_CODE

    # Резерв Сейфа создан, привязан к черновику, статья — «Авансы сотрудникам».
    draft = await _draft(async_session_factory, advance.id)
    assert draft.status == "paid"
    assert draft.safe_allocation_id is not None
    async with async_session_factory() as session:
        allocation = await session.get(SafeAllocation, draft.safe_allocation_id)
        article = await session.get(DdsArticle, allocation.article_id)
        refreshed = await session.get(SalaryAdvance, advance.id)
    assert allocation.status == "reserved"
    assert allocation.amount == Decimal("10000.00")
    assert allocation.wallet_id == safe_wallet.id
    assert article.code == "employee_advance"
    # Долга ещё нет — аванс ждёт фактической выдачи.
    assert refreshed.status == "awaiting_payout"
    assert await _cashflows(
        async_session_factory, source_kind=SAFE_PAYOUT_SOURCE_KIND, source_id=allocation.id
    ) == []


async def test_disburse_bank_advance_books_expense_and_forms_debt(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        wallet = await _make_bank_wallet(session)
        advance = await _issue_bank_advance(session, wallet=wallet)
    draft = await _draft(async_session_factory, advance.id)
    async with async_session_factory() as session:
        await apply_advance_draft_status(session, draft=draft, raw_status="EXECUTED")

    async with async_session_factory() as session:
        disbursed = await disburse_bank_advance(session, advance_id=advance.id)
    assert disbursed.status == "issued"

    draft = await _draft(async_session_factory, advance.id)
    assert draft.status == "disbursed"
    # Расход с Сейфа по статье выдачи (через резерв → safe_payout).
    payouts = await _cashflows(
        async_session_factory,
        source_kind=SAFE_PAYOUT_SOURCE_KIND,
        source_id=draft.safe_allocation_id,
    )
    assert len(payouts) == 1
    assert payouts[0].direction == "out"
    assert payouts[0].amount == Decimal("10000.00")
    async with async_session_factory() as session:
        allocation = await session.get(SafeAllocation, draft.safe_allocation_id)
        article = await session.get(DdsArticle, payouts[0].article_id)
    assert allocation.status == "paid"
    assert article.code == "employee_advance"


async def test_loan_paid_reserve_uses_loan_article(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        wallet = await _make_bank_wallet(session)
        loan = await _issue_bank_advance(
            session,
            wallet=wallet,
            amount=Decimal("50000"),
            requested_kind="loan",
            allow_loan=True,
        )
    draft = await _draft(async_session_factory, loan.id)
    async with async_session_factory() as session:
        await apply_advance_draft_status(session, draft=draft, raw_status="EXECUTED")
    draft = await _draft(async_session_factory, loan.id)
    async with async_session_factory() as session:
        allocation = await session.get(SafeAllocation, draft.safe_allocation_id)
        article = await session.get(DdsArticle, allocation.article_id)
    assert article.code == "vydacha_zaymov_sotrudnikam"


async def test_paid_is_idempotent_no_double_reserve(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        wallet = await _make_bank_wallet(session)
        advance = await _issue_bank_advance(session, wallet=wallet)
    draft = await _draft(async_session_factory, advance.id)
    async with async_session_factory() as session:
        await apply_advance_draft_status(session, draft=draft, raw_status="EXECUTED")
    # Повторная доставка статуса (дубль webhook/poll) не должна задвоить транзит/резерв.
    draft = await _draft(async_session_factory, advance.id)
    async with async_session_factory() as session:
        await apply_advance_draft_status(session, draft=draft, raw_status="EXECUTED")

    legs = await _cashflows(
        async_session_factory,
        source_kind=ADVANCE_BANK_TO_SAFE_SOURCE_KIND,
        source_id=advance.id,
    )
    assert len(legs) == 2
    async with async_session_factory() as session:
        reserves = (
            await session.scalars(
                select(SafeAllocation).where(SafeAllocation.purpose.like("Выдача аванса%"))
            )
        ).all()
    assert len(reserves) == 1


async def test_cancel_after_paid_frees_reserve_keeps_money_in_safe(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        wallet = await _make_bank_wallet(session)
        advance = await _issue_bank_advance(session, wallet=wallet)
    draft = await _draft(async_session_factory, advance.id)
    async with async_session_factory() as session:
        await apply_advance_draft_status(session, draft=draft, raw_status="EXECUTED")

    async with async_session_factory() as session:
        cancelled = await cancel_advance(session, advance.id)
    assert cancelled.status == "cancelled"

    draft = await _draft(async_session_factory, advance.id)
    assert draft.status == "cancelled"
    async with async_session_factory() as session:
        allocation = await session.get(SafeAllocation, draft.safe_allocation_id)
    assert allocation.status == "cancelled"
    # Резерв освобождён, но выдачи не было — расхода с Сейфа нет (деньги остаются в Сейфе).
    assert await _cashflows(
        async_session_factory, source_kind=SAFE_PAYOUT_SOURCE_KIND, source_id=allocation.id
    ) == []


async def test_disburse_rejected_before_bank_executes(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.services.payroll_runner import PayrollConflictError

    async with async_session_factory() as session:
        wallet = await _make_bank_wallet(session)
        advance = await _issue_bank_advance(session, wallet=wallet)
    # Черновик ещё «created» (банк не исполнил) → «Выплачено» должно отклоняться.
    async with async_session_factory() as session:
        with pytest.raises(PayrollConflictError):
            await disburse_bank_advance(session, advance_id=advance.id)


async def test_paid_without_wallets_keeps_draft_pending_for_retry(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Если кошельки не настроены, «исполнено» НЕ переводит черновик в paid (поллинг повторит),
    а не оставляет аванс в тупике без резерва."""
    async with async_session_factory() as session:
        wallet = await _make_bank_wallet(session)
        advance = await _issue_bank_advance(session, wallet=wallet)
    draft = await _draft(async_session_factory, advance.id)
    # Плательщик указывает на несуществующий счёт → банк-кошелёк не найден → транзит не заводится.
    monkeypatch.setattr(
        "app.services.payroll_advance_service.get_settings",
        lambda: SimpleNamespace(
            tbank_api_account_number="99999999999999999999",
            teplo_bank_client_mode="mock",
        ),
    )
    async with async_session_factory() as session:
        status = await apply_advance_draft_status(
            session, draft=draft, raw_status="EXECUTED", commit=True
        )
    assert status in ("created", "updated")
    draft = await _draft(async_session_factory, advance.id)
    assert draft.status in ("created", "updated")
    assert draft.safe_allocation_id is None
    assert await _cashflows(
        async_session_factory,
        source_kind=ADVANCE_BANK_TO_SAFE_SOURCE_KIND,
        source_id=advance.id,
    ) == []


async def test_disburse_triggers_iiko_bank(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """«Выплачено» по банк-выдаче → изъятие в iiko «эквайринг» (source='bank'); до выдачи — нет."""
    calls: list[dict] = []
    monkeypatch.setattr(
        "app.services.payroll_advance_service.post_advance_payout_to_iiko",
        lambda **kw: calls.append(kw),
    )
    async with async_session_factory() as session:
        wallet = await _make_bank_wallet(session)
        advance = await _issue_bank_advance(session, wallet=wallet)
    draft = await _draft(async_session_factory, advance.id)
    async with async_session_factory() as session:
        await apply_advance_draft_status(session, draft=draft, raw_status="EXECUTED")
    # Деньги ещё в резерве Сейфа (не выданы) → iiko не дёргается.
    assert calls == []
    async with async_session_factory() as session:
        await disburse_bank_advance(session, advance_id=advance.id)
    assert len(calls) == 1
    assert calls[0]["source"] == "bank"
    assert calls[0]["source_id"] == advance.id
    assert calls[0]["amount"] == Decimal("10000.00")


async def test_pay_reserve_from_safe_card_signals_disbursement(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Оплата резерва из карточки Сейфа выдаёт банк-аванс и возвращает его (роут → iiko)."""
    async with async_session_factory() as session:
        wallet = await _make_bank_wallet(session)
        advance = await _issue_bank_advance(session, wallet=wallet)
    draft = await _draft(async_session_factory, advance.id)
    async with async_session_factory() as session:
        await apply_advance_draft_status(session, draft=draft, raw_status="EXECUTED")
    draft = await _draft(async_session_factory, advance.id)
    async with async_session_factory() as session:
        allocation = await session.get(
            SafeAllocation, draft.safe_allocation_id, with_for_update=True
        )
        await pay_allocation(
            session, allocation, amount=allocation.amount, operation_date=AS_OF
        )
        disbursed = await sync_advance_after_allocation_change(
            session, allocation_id=allocation.id
        )
        await session.commit()
    # sync вернул аванс → роут проведёт iiko после commit; аванс выдан, черновик disbursed.
    assert disbursed is not None
    assert disbursed.id == advance.id
    refreshed = await _draft(async_session_factory, advance.id)
    assert refreshed.status == "disbursed"
    async with async_session_factory() as session:
        refreshed_advance = await session.get(SalaryAdvance, advance.id)
    assert refreshed_advance.status == "issued"


async def test_advance_settled_by_payment_status(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Advance-черновик доводится статусом платёжного документа (webhook по provider_ref):
    при executed — транзит банк→Сейф + резерв Сейфа. Операции по счёту advance больше не
    доводят (этот контур переведён на статусный webhook)."""
    from app.services.payroll_advance_service import apply_advance_draft_status

    async with async_session_factory() as session:
        wallet = await _make_bank_wallet(session)
        advance = await _issue_bank_advance(session, wallet=wallet)
    draft = await _draft(async_session_factory, advance.id)
    async with async_session_factory() as session:
        status = await apply_advance_draft_status(session, draft=draft, raw_status="executed")
        await session.commit()
    assert status == "paid"

    draft = await _draft(async_session_factory, advance.id)
    assert draft.status == "paid"
    assert draft.safe_allocation_id is not None
    legs = await _cashflows(
        async_session_factory,
        source_kind=ADVANCE_BANK_TO_SAFE_SOURCE_KIND,
        source_id=advance.id,
    )
    assert len(legs) == 2
