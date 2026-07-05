"""Сервисные тесты кассового журнала и «Выплаты из кассы» (ТК Черникова).

Маршрутизация трёх веток (аванс → леджер, предоплата → дебиторка, прочее →
голая проводка), фильтр статей по флагу «касса», запрет флага на защищённых
статьях, журнал строго по одному кошельку, правка/удаление только своей
записи в день создания.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    CashflowTransaction,
    Counterparty,
    CounterpartyPayableProfile,
    DdsArticle,
    Employee,
    EmployeePositionAssignment,
    PayrollRate,
    SalaryAdvance,
    SupplierPrepayment,
    User,
    Wallet,
)
from app.services.kassa.payouts import (
    EMPLOYEE_ADVANCE_ARTICLE_CODE,
    EMPLOYEE_LOAN_ARTICLE_CODE,
    KASSA_PAYOUT_SOURCE_KIND,
    KASSA_WALLET_CODE,
    PROTECTED_ARTICLE_CODES,
    SUPPLIER_PREPAYMENT_ARTICLE_CODE,
    KassaPayoutError,
    create_payout,
    delete_payout,
    ensure_article_kassa_eligible,
    kassa_journal,
    kassa_today,
    list_payout_articles,
    update_payout,
)


async def _make_user(session: AsyncSession, *, name: str = "Кассир Тест") -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"kassa-{uuid.uuid4().hex[:10]}@test.local",
        hashed_password="hashed",
        full_name=name,
    )
    session.add(user)
    await session.flush()
    return user


async def _seeded_article(session: AsyncSession, code: str) -> DdsArticle:
    article = await session.scalar(select(DdsArticle).where(DdsArticle.code == code))
    assert article is not None, f"статья {code} должна быть в сидах каталога"
    return article


async def _enable_kassa(session: AsyncSession, code: str) -> DdsArticle:
    article = await _seeded_article(session, code)
    article.kassa_enabled = True
    article.is_active = True
    await session.flush()
    return article


async def _make_expense_article(session: AsyncSession) -> DdsArticle:
    article = DdsArticle(
        code=f"arenda_test_{uuid.uuid4().hex[:6]}",
        name="Аренда помещения (тест)",
        movement_type="outflow",
        activity_type="operating",
        kassa_enabled=True,
    )
    session.add(article)
    await session.flush()
    return article


async def _make_okladnik(session: AsyncSession) -> Employee:
    employee = Employee(
        id=uuid.uuid4(),
        full_name="Окладник Кассовый",
        iiko_id=f"iiko-{uuid.uuid4()}",
        status="active",
        is_senior=False,
        is_deputy_senior=False,
        pin_hash="hashed-pin",
        pin_set_at=datetime(2026, 1, 1, tzinfo=UTC),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session.add(employee)
    await session.flush()
    session.add(
        EmployeePositionAssignment(
            id=uuid.uuid4(),
            employee_id=employee.id,
            position="Управляющий",
            effective_from=date(2026, 1, 1),
            effective_to=None,
        )
    )
    session.add(
        PayrollRate(
            id=uuid.uuid4(),
            employee_id=None,
            position_group="Управляющий",
            category="admin",
            station=None,
            rate_type="monthly",
            amount=Decimal("90000"),
            is_active=True,
            effective_from=date(2026, 1, 1),
        )
    )
    await session.flush()
    return employee


async def _make_kassa_supplier(
    session: AsyncSession, *, kassa_enabled: bool = True
) -> Counterparty:
    counterparty = Counterparty(
        name=f"Поставщик Кассы {uuid.uuid4().hex[:6]}",
        type="legal_entity",
        status="active",
    )
    session.add(counterparty)
    await session.flush()
    session.add(
        CounterpartyPayableProfile(
            counterparty_id=counterparty.id,
            kassa_enabled=kassa_enabled,
        )
    )
    await session.flush()
    return counterparty


async def _tk_wallet(session: AsyncSession) -> Wallet:
    wallet = await session.scalar(select(Wallet).where(Wallet.code == KASSA_WALLET_CODE))
    assert wallet is not None, "кошелёк ТК Черникова должен быть в сидах"
    return wallet


# --------------------------------------------------------------------------- #
# Коды-маршруты и защитный список соответствуют профильным сервисам
# --------------------------------------------------------------------------- #


def test_route_and_protected_codes_match_source_services() -> None:
    from app.services.counterparty_payments import DEFAULT_SUPPLIER_ARTICLE_CODE
    from app.services.couriers.deposit_service import (
        COURIER_DEPOSIT_RETURN_ARTICLE_CODE,
        COURIER_DEPOSIT_TOPUP_ARTICLE_CODE,
    )
    from app.services.deposit_service import PRODUCTION_DEPOSIT_PAYOUT_ARTICLE_CODE
    from app.services.payroll_advance_service import (
        ADVANCE_ARTICLE_CODE,
        ADVANCE_TK_WALLET_CODE,
        LOAN_ARTICLE_CODE,
    )
    from app.services.payroll_payout_allocation import DDS_ARTICLE_ADMIN_PAYROLL
    from app.services.payroll_payouts import PAYROLL_PAYOUT_ARTICLE_CODE
    from app.services.supplier_prepayments import PREPAYMENT_ARTICLE_CODE
    from app.services.wallets import (
        DDS_ARTICLE_TRANSFER_IN_CODE,
        DDS_ARTICLE_TRANSFER_OUT_CODE,
    )
    from app.services.warehouse_payments import DEFAULT_STAFF_ARTICLE_CODE

    assert KASSA_WALLET_CODE == ADVANCE_TK_WALLET_CODE
    assert EMPLOYEE_ADVANCE_ARTICLE_CODE == ADVANCE_ARTICLE_CODE
    assert EMPLOYEE_LOAN_ARTICLE_CODE == LOAN_ARTICLE_CODE
    assert SUPPLIER_PREPAYMENT_ARTICLE_CODE == PREPAYMENT_ARTICLE_CODE
    for code in (
        DDS_ARTICLE_TRANSFER_IN_CODE,
        DDS_ARTICLE_TRANSFER_OUT_CODE,
        COURIER_DEPOSIT_RETURN_ARTICLE_CODE,
        COURIER_DEPOSIT_TOPUP_ARTICLE_CODE,
        PRODUCTION_DEPOSIT_PAYOUT_ARTICLE_CODE,
        PAYROLL_PAYOUT_ARTICLE_CODE,
        DDS_ARTICLE_ADMIN_PAYROLL,
        DEFAULT_SUPPLIER_ARTICLE_CODE,
        DEFAULT_STAFF_ARTICLE_CODE,
    ):
        assert code in PROTECTED_ARTICLE_CODES, code


# --------------------------------------------------------------------------- #
# Маршрутизация трёх веток
# --------------------------------------------------------------------------- #


async def test_expense_payout_books_bare_transaction(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        user = await _make_user(session)
        article = await _make_expense_article(session)
        await session.commit()

        result = await create_payout(
            session,
            article_id=article.id,
            amount=Decimal("1500"),
            comment="Аренда за июль",
            actor_user_id=user.id,
        )
        assert result.kind == "expense"
        assert result.transaction_id is not None

        wallet = await _tk_wallet(session)
        transaction = await session.get(CashflowTransaction, result.transaction_id)
        assert transaction is not None
        assert transaction.wallet_id == wallet.id
        assert transaction.direction == "out"
        assert transaction.source_kind == KASSA_PAYOUT_SOURCE_KIND
        assert transaction.amount == Decimal("1500.00")
        assert transaction.operation_date == kassa_today()
        assert transaction.created_by_user_id == user.id
        assert transaction.payment_purpose == "Аренда за июль"


async def test_advance_payout_routes_to_ledger(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        user = await _make_user(session)
        employee = await _make_okladnik(session)
        article = await _enable_kassa(session, EMPLOYEE_ADVANCE_ARTICLE_CODE)
        await session.commit()

        result = await create_payout(
            session,
            article_id=article.id,
            amount=Decimal("1000"),
            comment="Аванс из кассы",
            employee_id=employee.id,
            actor_user_id=user.id,
        )
        assert result.kind == "employee_advance"
        advance = result.advance
        assert advance is not None
        assert advance.kind == "advance"
        assert advance.status == "issued"
        assert advance.issued_on == kassa_today()
        assert advance.created_by_user_id == user.id

        # Проводка книжится авансовым контуром (source_kind=salary_advance), не голой.
        wallet = await _tk_wallet(session)
        transaction = await session.get(CashflowTransaction, result.transaction_id)
        assert transaction is not None
        assert transaction.source_kind == "salary_advance"
        assert transaction.source_id == advance.id
        assert transaction.wallet_id == wallet.id


async def test_advance_payout_requires_employee(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        user = await _make_user(session)
        article = await _enable_kassa(session, EMPLOYEE_ADVANCE_ARTICLE_CODE)
        await session.commit()

        with pytest.raises(KassaPayoutError, match="сотрудника"):
            await create_payout(
                session,
                article_id=article.id,
                amount=Decimal("1000"),
                actor_user_id=user.id,
            )


async def test_prepayment_payout_routes_to_receivable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        user = await _make_user(session)
        supplier = await _make_kassa_supplier(session)
        article = await _enable_kassa(session, SUPPLIER_PREPAYMENT_ARTICLE_CODE)
        await session.commit()

        result = await create_payout(
            session,
            article_id=article.id,
            amount=Decimal("2500"),
            comment="Предоплата за овощи",
            counterparty_id=supplier.id,
            actor_user_id=user.id,
        )
        assert result.kind == "supplier_prepayment"
        prepayment = result.prepayment
        assert prepayment is not None
        assert prepayment.status == "open"
        assert prepayment.amount == Decimal("2500.00")
        assert prepayment.created_by_user_id == user.id

        wallet = await _tk_wallet(session)
        transaction = await session.get(CashflowTransaction, result.transaction_id)
        assert transaction is not None
        assert transaction.source_kind == "supplier_prepayment"
        assert transaction.wallet_id == wallet.id
        assert transaction.counterparty_id == supplier.id


async def test_prepayment_requires_kassa_enabled_supplier(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        user = await _make_user(session)
        supplier = await _make_kassa_supplier(session, kassa_enabled=False)
        article = await _enable_kassa(session, SUPPLIER_PREPAYMENT_ARTICLE_CODE)
        await session.commit()

        with pytest.raises(KassaPayoutError, match="не активен в Кассе"):
            await create_payout(
                session,
                article_id=article.id,
                amount=Decimal("500"),
                counterparty_id=supplier.id,
                actor_user_id=user.id,
            )


# --------------------------------------------------------------------------- #
# Фильтр статей по флагу и защита флага
# --------------------------------------------------------------------------- #


async def test_payout_articles_filtered_by_flag(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        expense = await _make_expense_article(session)
        # employee_advance в сидах есть, но флаг выключен — в списке быть не должна.
        await _seeded_article(session, EMPLOYEE_ADVANCE_ARTICLE_CODE)
        await session.commit()

        articles = await list_payout_articles(session)
        codes = {item["code"] for item in articles}
        assert expense.code in codes
        assert EMPLOYEE_ADVANCE_ARTICLE_CODE not in codes

        await _enable_kassa(session, EMPLOYEE_ADVANCE_ARTICLE_CODE)
        await session.commit()
        articles = await list_payout_articles(session)
        by_code = {item["code"]: item for item in articles}
        assert by_code[expense.code]["flow"] == "expense"
        assert by_code[EMPLOYEE_ADVANCE_ARTICLE_CODE]["flow"] == "employee_advance"


async def test_payout_denied_for_unflagged_article(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        user = await _make_user(session)
        article = await _make_expense_article(session)
        article.kassa_enabled = False
        await session.commit()

        with pytest.raises(KassaPayoutError, match="не разрешена"):
            await create_payout(
                session,
                article_id=article.id,
                amount=Decimal("100"),
                actor_user_id=user.id,
            )


def test_protected_articles_cannot_be_flagged() -> None:
    # Каждый защищённый код (движковые переводы, возврат депозита курьера, ЗП,
    # оплата накладных…) — отказ, даже если статья расходная.
    for code in PROTECTED_ARTICLE_CODES:
        article = DdsArticle(
            code=code,
            name=f"Защищённая ({code})",
            movement_type="outflow",
            activity_type="operating",
            kassa_enabled=True,
        )
        with pytest.raises(KassaPayoutError, match="собственный контур"):
            ensure_article_kassa_eligible(article)

    # Приходную статью в кассу включить нельзя (только расход).
    inflow = DdsArticle(
        code=f"prihod_test_{uuid.uuid4().hex[:6]}",
        name="Приход (тест)",
        movement_type="inflow",
        activity_type="operating",
        kassa_enabled=True,
    )
    with pytest.raises(KassaPayoutError, match="расходные"):
        ensure_article_kassa_eligible(inflow)


# --------------------------------------------------------------------------- #
# Журнал: строго один кошелёк, метрики, фильтры
# --------------------------------------------------------------------------- #


async def test_journal_shows_only_tk_chernikova(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        user = await _make_user(session)
        article = await _make_expense_article(session)
        safe_wallet = await session.scalar(select(Wallet).where(Wallet.code == "cash_safe"))
        assert safe_wallet is not None
        await session.commit()

        result = await create_payout(
            session,
            article_id=article.id,
            amount=Decimal("300"),
            comment="Хозрасходы",
            actor_user_id=user.id,
        )
        # Чужая проводка на Сейфе — в кассовом журнале появиться не должна.
        session.add(
            CashflowTransaction(
                wallet_id=safe_wallet.id,
                direction="out",
                amount=Decimal("9999"),
                operation_date=kassa_today(),
                source_kind="safe_payout",
                payment_purpose="Расход Сейфа (не касса)",
            )
        )
        await session.commit()

        today = kassa_today()
        journal = await kassa_journal(
            session,
            date_from=today.replace(day=1),
            date_to=today,
            actor_user_id=user.id,
        )
        ids = {item["id"] for item in journal["items"]}
        assert result.transaction_id in ids
        purposes = {item["purpose"] for item in journal["items"]}
        assert "Расход Сейфа (не касса)" not in purposes
        assert journal["period_out"] == 300.0
        assert journal["wallet_name"]


async def test_journal_metrics_directions_and_search(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        user = await _make_user(session)
        article = await _make_expense_article(session)
        wallet = await _tk_wallet(session)
        today = kassa_today()
        # Системный приход (выручка смены) — журнал его видит, но не даёт править.
        session.add(
            CashflowTransaction(
                wallet_id=wallet.id,
                direction="in",
                amount=Decimal("1000"),
                operation_date=today,
                source_kind="iiko_cashshift",
                payment_purpose="Выручка смены (тест)",
            )
        )
        await session.commit()

        await create_payout(
            session,
            article_id=article.id,
            amount=Decimal("400"),
            comment="Вода для офиса",
            actor_user_id=user.id,
        )

        journal = await kassa_journal(
            session,
            date_from=today.replace(day=1),
            date_to=today,
            actor_user_id=user.id,
        )
        assert journal["period_in"] == 1000.0
        assert journal["period_out"] == 400.0
        by_purpose = {item["purpose"]: item for item in journal["items"]}
        assert by_purpose["Выручка смены (тест)"]["editable"] is False
        assert by_purpose["Вода для офиса"]["editable"] is True
        assert by_purpose["Вода для офиса"]["kassa_flow"] == "expense"

        only_in = await kassa_journal(
            session,
            date_from=today.replace(day=1),
            date_to=today,
            direction="in",
            actor_user_id=user.id,
        )
        assert {item["direction"] for item in only_in["items"]} == {"in"}

        found = await kassa_journal(
            session,
            date_from=today.replace(day=1),
            date_to=today,
            search="вода",
            actor_user_id=user.id,
        )
        assert len(found["items"]) == 1
        assert found["items"][0]["purpose"] == "Вода для офиса"


# --------------------------------------------------------------------------- #
# Правка/удаление: своя запись сегодня; отказ чужому, вчерашней и системной
# --------------------------------------------------------------------------- #


async def test_edit_and_delete_own_today(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        user = await _make_user(session)
        article = await _make_expense_article(session)
        await session.commit()

        result = await create_payout(
            session,
            article_id=article.id,
            amount=Decimal("700"),
            comment="Опечатка",
            actor_user_id=user.id,
        )
        updated = await update_payout(
            session,
            transaction_id=result.transaction_id,
            article_id=article.id,
            amount=Decimal("750"),
            comment="Исправлено",
            actor_user_id=user.id,
        )
        assert updated.transaction_id == result.transaction_id  # правка на месте
        transaction = await session.get(CashflowTransaction, result.transaction_id)
        assert transaction is not None
        assert transaction.amount == Decimal("750.00")
        assert transaction.payment_purpose == "Исправлено"

        await delete_payout(
            session, transaction_id=result.transaction_id, actor_user_id=user.id
        )
        assert await session.get(CashflowTransaction, result.transaction_id) is None


async def test_edit_denied_for_other_user_yesterday_and_system(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        author = await _make_user(session, name="Автор")
        stranger = await _make_user(session, name="Другой")
        article = await _make_expense_article(session)
        wallet = await _tk_wallet(session)
        await session.commit()

        result = await create_payout(
            session,
            article_id=article.id,
            amount=Decimal("100"),
            actor_user_id=author.id,
        )

        # Чужому — отказ.
        with pytest.raises(KassaPayoutError, match="свои"):
            await delete_payout(
                session, transaction_id=result.transaction_id, actor_user_id=stranger.id
            )

        # После полуночи (вчерашняя дата операции) — отказ даже автору.
        transaction = await session.get(CashflowTransaction, result.transaction_id)
        assert transaction is not None
        transaction.operation_date = kassa_today() - timedelta(days=1)
        await session.commit()
        with pytest.raises(KassaPayoutError, match="день создания"):
            await update_payout(
                session,
                transaction_id=result.transaction_id,
                article_id=article.id,
                amount=Decimal("1"),
                actor_user_id=author.id,
            )

        # Запись системного контура (выручка) не правится вовсе.
        system_tx = CashflowTransaction(
            wallet_id=wallet.id,
            direction="in",
            amount=Decimal("500"),
            operation_date=kassa_today(),
            source_kind="iiko_cashshift",
        )
        session.add(system_tx)
        await session.commit()
        with pytest.raises(KassaPayoutError, match="системным контуром"):
            await delete_payout(
                session, transaction_id=system_tx.id, actor_user_id=author.id
            )


async def test_advance_delete_cancels_ledger_and_removes_cashflow(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        user = await _make_user(session)
        employee = await _make_okladnik(session)
        article = await _enable_kassa(session, EMPLOYEE_ADVANCE_ARTICLE_CODE)
        await session.commit()

        result = await create_payout(
            session,
            article_id=article.id,
            amount=Decimal("1000"),
            employee_id=employee.id,
            actor_user_id=user.id,
        )
        advance_id = result.advance.id

        await delete_payout(
            session, transaction_id=result.transaction_id, actor_user_id=user.id
        )
        advance = await session.get(SalaryAdvance, advance_id)
        assert advance is not None
        assert advance.status == "cancelled"
        # Проводка снята — леджер чист, расход из кассы не висит.
        assert await session.get(CashflowTransaction, result.transaction_id) is None


async def test_advance_edit_recreates_issue(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        user = await _make_user(session)
        employee = await _make_okladnik(session)
        article = await _enable_kassa(session, EMPLOYEE_ADVANCE_ARTICLE_CODE)
        await session.commit()

        first = await create_payout(
            session,
            article_id=article.id,
            amount=Decimal("1000"),
            employee_id=employee.id,
            actor_user_id=user.id,
        )
        second = await update_payout(
            session,
            transaction_id=first.transaction_id,
            article_id=article.id,
            amount=Decimal("1200"),
            comment="Сумма исправлена",
            employee_id=employee.id,
            actor_user_id=user.id,
        )
        assert second.advance is not None
        assert second.advance.id != first.advance.id
        assert second.advance.amount == Decimal("1200.00")

        old_advance = await session.get(SalaryAdvance, first.advance.id)
        assert old_advance is not None
        assert old_advance.status == "cancelled"
        assert await session.get(CashflowTransaction, first.transaction_id) is None
        assert await session.get(CashflowTransaction, second.transaction_id) is not None


async def test_prepayment_with_settlement_cannot_be_cancelled(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        user = await _make_user(session)
        supplier = await _make_kassa_supplier(session)
        article = await _enable_kassa(session, SUPPLIER_PREPAYMENT_ARTICLE_CODE)
        await session.commit()

        result = await create_payout(
            session,
            article_id=article.id,
            amount=Decimal("2000"),
            counterparty_id=supplier.id,
            actor_user_id=user.id,
        )
        prepayment = await session.get(SupplierPrepayment, result.prepayment.id)
        assert prepayment is not None
        # Гашение началось: часть зачтена накладной.
        prepayment.amount_settled = Decimal("500")
        prepayment.status = "partially_settled"
        await session.commit()

        with pytest.raises(KassaPayoutError, match="гаситься"):
            await delete_payout(
                session, transaction_id=result.transaction_id, actor_user_id=user.id
            )

        # Нетронутая предоплата снимается: и запись, и денежный факт.
        fresh = await create_payout(
            session,
            article_id=article.id,
            amount=Decimal("900"),
            counterparty_id=supplier.id,
            actor_user_id=user.id,
        )
        await delete_payout(
            session, transaction_id=fresh.transaction_id, actor_user_id=user.id
        )
        assert await session.get(SupplierPrepayment, fresh.prepayment.id) is None
        assert await session.get(CashflowTransaction, fresh.transaction_id) is None
