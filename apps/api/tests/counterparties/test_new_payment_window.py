"""Окно «Новый платёж»: свободный вывод на Сейф по статье + контекст формы.

Новый кусок (0161): via-safe черновик «просто траты» без получателя —
``counterparty_payments.create_expense_payment_draft``. Paid-переход заводит
транзит р/с→Сейф и целёвку ЦЕЛЕВОЙ статьи черновика с назначением из формы
(расширение ``_settle_draft_via_safe``); при отказе банка целёвки нет.

Контекст окна (``/dds/new-payment/context``) собирает статьи ПО ПРАВАМ:
займ-статья видна только с ``payroll.loans.issue``, свободные статьи — только
с ``finance.safe.allocate``; сотрудники несут флаг on_demand для маршрута выплат.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from cp_helpers import make_counterparty, make_invoice
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin_payout_split import _payer_wallet, _safe_wallet

from app.api.v1.routes.counterparties import DraftRead
from app.models import (
    AppSetting,
    CashflowTransaction,
    DdsArticle,
    Employee,
    EmployeePositionAssignment,
    ExpenseDraftLine,
    InvoicePaymentAllocation,
    SafeAllocation,
)
from app.services.bank_payment_status import (
    SUPPLIER_BANK_TO_SAFE_SOURCE_KIND,
    apply_payment_status,
)
from app.services.counterparty_payments import (
    DEFAULT_SUPPLIER_ARTICLE_CODE,
    CounterpartyPaymentError,
    ExpenseLineInput,
    create_expense_payment_draft,
    create_payment_draft_for_invoices,
)
from app.services.employee_payouts import DEFAULT_PAYOUT_ARTICLE_CODE
from app.services.kassa.payouts import PROTECTED_ARTICLE_CODES
from app.services.new_payment import (
    EMPLOYEE_PAYOUT_ARTICLE_CODES,
    FLOW_BY_ARTICLE_CODE,
    SUPPLIER_INVOICES_ARTICLE_CODE,
    list_new_payment_articles,
    list_new_payment_employees,
    new_payment_article_flow,
)
from app.services.payroll_admin import OKLADNIK_PAYOUT_MODES_KEY
from app.services.payroll_advance_service import ADVANCE_ARTICLE_CODE, LOAN_ARTICLE_CODE
from app.services.payroll_payout_allocation import (
    DDS_ARTICLE_ADMIN_PAYROLL,
    DDS_ARTICLE_PRODUCTION_PAYROLL,
)
from app.services.supplier_prepayments import PREPAYMENT_ARTICLE_CODE


def test_route_codes_match_source_services() -> None:
    """Коды статей-маршрутов продублированы из профильных сервисов — сверяем."""
    assert FLOW_BY_ARTICLE_CODE[ADVANCE_ARTICLE_CODE] == "employee_advance"
    assert FLOW_BY_ARTICLE_CODE[LOAN_ARTICLE_CODE] == "employee_loan"
    assert FLOW_BY_ARTICLE_CODE[PREPAYMENT_ARTICLE_CODE] == "supplier_prepayment"
    assert SUPPLIER_INVOICES_ARTICLE_CODE == DEFAULT_SUPPLIER_ARTICLE_CODE
    assert DEFAULT_PAYOUT_ARTICLE_CODE in EMPLOYEE_PAYOUT_ARTICLE_CODES
    assert set(EMPLOYEE_PAYOUT_ARTICLE_CODES) == {
        DDS_ARTICLE_ADMIN_PAYROLL,
        DDS_ARTICLE_PRODUCTION_PAYROLL,
    }


async def _free_expense_article(session: AsyncSession) -> DdsArticle:
    """Любая засеянная статья свободного вывода (не маршрут/не защищённая/не кассовая)."""
    articles = (
        await session.scalars(
            select(DdsArticle)
            .where(DdsArticle.is_active.is_(True), DdsArticle.movement_type == "outflow")
            .order_by(DdsArticle.code)
        )
    ).all()
    for article in articles:
        if new_payment_article_flow(article) == "expense":
            return article
    raise AssertionError("в сидах каталога должна быть хотя бы одна свободная статья")


async def _transfer_legs(session: AsyncSession, draft_id) -> list[CashflowTransaction]:
    return list(
        (
            await session.scalars(
                select(CashflowTransaction).where(
                    CashflowTransaction.source_kind == SUPPLIER_BANK_TO_SAFE_SOURCE_KIND,
                    CashflowTransaction.source_id == draft_id,
                )
            )
        ).all()
    )


async def _draft_reserve(session: AsyncSession, draft_id) -> SafeAllocation | None:
    return await session.scalar(
        select(SafeAllocation).where(SafeAllocation.source_draft_id == draft_id)
    )


async def test_expense_draft_targets_ip_card(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        article = await _free_expense_article(session)

        draft = await create_expense_payment_draft(
            session,
            article_id=article.id,
            amount=Decimal("2500.00"),
            purpose="Аренда помещения за июль",
        )

        assert draft.pays_via_safe is True
        assert draft.creates_prepayment is False
        assert draft.counterparty_id is None
        assert draft.target_article_id == article.id
        assert draft.target_purpose == "Аренда помещения за июль"
        assert draft.status == "created"
        # Получатель — карта ИП (реквизиты зарплатных выплат), назначение — из формы.
        payout_setting = await session.scalar(
            select(AppSetting).where(AppSetting.key == "payroll.bank_payout_requisites")
        )
        assert draft.payload["recipientName"] == payout_setting.value["recipientName"]
        assert draft.payload["paymentPurpose"] == "Аренда помещения за июль"
        # Регрессия: контрагентские схемы переваривают черновик без контрагента
        # (GET /counterparties/drafts/list гоняет все черновики через DraftRead).
        assert DraftRead.model_validate(draft).counterparty_id is None


async def test_expense_paid_books_transfer_and_targeted_allocation(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        _account, payer_wallet = await _payer_wallet(session)
        safe_wallet = await _safe_wallet(session)
        article = await _free_expense_article(session)
        draft = await create_expense_payment_draft(
            session,
            article_id=article.id,
            amount=Decimal("1200.50"),
            purpose="Хозрасходы: лампочки",
        )

        status = await apply_payment_status(session, draft=draft, raw_status="executed")
        assert status == "paid"

        legs = await _transfer_legs(session, draft.id)
        assert len(legs) == 2
        bank_leg = next(t for t in legs if t.wallet_id == payer_wallet.id)
        safe_leg = next(t for t in legs if t.wallet_id == safe_wallet.id)
        assert bank_leg.direction == "out" and bank_leg.amount == Decimal("1200.50")
        assert safe_leg.direction == "in" and safe_leg.amount == Decimal("1200.50")
        assert "Хозрасходы: лампочки" in (safe_leg.payment_purpose or "")

        # Целёвка НУЖНОЙ статьи с назначением из формы, без контрагента.
        reserve = await _draft_reserve(session, draft.id)
        assert reserve is not None
        assert reserve.article_id == article.id
        assert reserve.purpose == "Хозрасходы: лампочки"
        assert reserve.counterparty_id is None
        assert reserve.amount == Decimal("1200.50")
        assert reserve.status == "reserved"

        # Расходной проводки нет — расход случится при выплате/выдаче целёвки.
        expense_count = await session.scalar(
            select(func.count())
            .select_from(CashflowTransaction)
            .where(CashflowTransaction.source_kind == "counterparty_payment")
        )
        assert expense_count == 0


async def test_expense_paid_is_idempotent(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        await _payer_wallet(session)
        await _safe_wallet(session)
        article = await _free_expense_article(session)
        draft = await create_expense_payment_draft(
            session, article_id=article.id, amount=Decimal("300"), purpose="Повторный paid"
        )

        await apply_payment_status(session, draft=draft, raw_status="executed")
        await apply_payment_status(session, draft=draft, raw_status="executed")

        assert len(await _transfer_legs(session, draft.id)) == 2
        reserves = (
            await session.scalars(
                select(SafeAllocation).where(SafeAllocation.source_draft_id == draft.id)
            )
        ).all()
        assert len(reserves) == 1


async def test_expense_failed_creates_no_allocation(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        await _payer_wallet(session)
        await _safe_wallet(session)
        article = await _free_expense_article(session)
        draft = await create_expense_payment_draft(
            session, article_id=article.id, amount=Decimal("300"), purpose="Отказ банка"
        )

        status = await apply_payment_status(session, draft=draft, raw_status="declined")

        assert status == "failed"
        assert await _transfer_legs(session, draft.id) == []
        assert await _draft_reserve(session, draft.id) is None


async def _two_free_expense_articles(session: AsyncSession) -> tuple[DdsArticle, DdsArticle]:
    articles = (
        await session.scalars(
            select(DdsArticle)
            .where(DdsArticle.is_active.is_(True), DdsArticle.movement_type == "outflow")
            .order_by(DdsArticle.code)
        )
    ).all()
    free = [a for a in articles if new_payment_article_flow(a) == "expense"]
    assert len(free) >= 2, "в сидах должно быть ≥2 свободных статей"
    return free[0], free[1]


async def test_expense_multiline_tranche_splits_into_per_line_reserves(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Транш нескольких статей: один черновик на сумму → по целёвке на строку по статьям."""
    async with async_session_factory() as session:
        _account, payer_wallet = await _payer_wallet(session)
        safe_wallet = await _safe_wallet(session)
        a1, a2 = await _two_free_expense_articles(session)

        draft = await create_expense_payment_draft(
            session,
            lines=[
                ExpenseLineInput(article_id=a1.id, amount=Decimal("2000.00"), purpose="Аренда"),
                ExpenseLineInput(article_id=a2.id, amount=Decimal("500.00"), purpose="Реклама"),
            ],
        )
        # Один черновик на сумму строк; одиночной target-статьи у транша нет.
        assert draft.amount == Decimal("2500.00")
        assert draft.target_article_id is None
        lines = (
            await session.scalars(
                select(ExpenseDraftLine).where(ExpenseDraftLine.draft_id == draft.id)
            )
        ).all()
        assert {line.amount for line in lines} == {Decimal("2000.00"), Decimal("500.00")}

        status = await apply_payment_status(session, draft=draft, raw_status="executed")
        assert status == "paid"

        # Транзит р/с→Сейф один на всю сумму (2 ноги), но целёвки — по строке.
        legs = await _transfer_legs(session, draft.id)
        assert len(legs) == 2
        safe_in = sum(t.amount for t in legs if t.wallet_id == safe_wallet.id)
        assert safe_in == Decimal("2500.00")
        assert any(t.wallet_id == payer_wallet.id for t in legs)

        reserves = (
            await session.scalars(
                select(SafeAllocation).where(SafeAllocation.source_draft_id == draft.id)
            )
        ).all()
        assert len(reserves) == 2
        by_article = {r.article_id: r for r in reserves}
        assert by_article[a1.id].amount == Decimal("2000.00")
        assert by_article[a1.id].purpose == "Аренда"
        assert by_article[a2.id].amount == Decimal("500.00")
        assert by_article[a2.id].purpose == "Реклама"
        assert all(r.counterparty_id is None for r in reserves)
        assert all(r.source_draft_line_id is not None for r in reserves)

        # Повторный paid не плодит целёвки (идемпотентность по строке).
        await apply_payment_status(session, draft=draft, raw_status="executed")
        again = (
            await session.scalars(
                select(SafeAllocation).where(SafeAllocation.source_draft_id == draft.id)
            )
        ).all()
        assert len(again) == 2


async def test_expense_line_counterparty_tags_reserve(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Строка свободного вывода с контрагентом → целёвка Сейфа помечена им (атрибуция)."""
    async with async_session_factory() as session:
        await _payer_wallet(session)
        await _safe_wallet(session)
        article = await _free_expense_article(session)
        supplier = await make_counterparty(session, name="ООО Сервис", inn="7711111119")

        draft = await create_expense_payment_draft(
            session,
            lines=[
                ExpenseLineInput(
                    article_id=article.id,
                    amount=Decimal("500.00"),
                    purpose="Подписка",
                    counterparty_id=supplier.id,
                ),
            ],
        )
        await apply_payment_status(session, draft=draft, raw_status="executed")

        reserve = await _draft_reserve(session, draft.id)
        assert reserve is not None
        assert reserve.counterparty_id == supplier.id
        assert reserve.article_id == article.id
        assert reserve.amount == Decimal("500.00")


async def test_expense_rejects_articles_with_own_circuits(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Статьи-маршруты, защищённые и кассовые статьи свободным выводом не платятся."""
    async with async_session_factory() as session:
        prepayment = await session.scalar(
            select(DdsArticle).where(DdsArticle.code == PREPAYMENT_ARTICLE_CODE)
        )
        assert prepayment is not None
        with pytest.raises(CounterpartyPaymentError, match="собственный контур"):
            await create_expense_payment_draft(
                session, article_id=prepayment.id, amount=Decimal("100"), purpose="x"
            )

        # Расходная защищённая статья (переводы-приходы отсеклись бы другим текстом).
        protected = await session.scalar(
            select(DdsArticle).where(
                DdsArticle.code.in_(tuple(PROTECTED_ARTICLE_CODES)),
                DdsArticle.movement_type == "outflow",
                DdsArticle.is_active.is_(True),
            )
        )
        assert protected is not None, "нет засеянных расходных защищённых статей"
        with pytest.raises(CounterpartyPaymentError, match="собственный контур"):
            await create_expense_payment_draft(
                session, article_id=protected.id, amount=Decimal("100"), purpose="x"
            )

        kassa_article = await _free_expense_article(session)
        kassa_article.kassa_enabled = True
        await session.flush()
        with pytest.raises(CounterpartyPaymentError, match="собственный контур"):
            await create_expense_payment_draft(
                session, article_id=kassa_article.id, amount=Decimal("100"), purpose="x"
            )


async def test_expense_optional_purpose_defaults_to_article_name(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        article = await _free_expense_article(session)
        # Пустое назначение допустимо — подставляется имя статьи (в банк и в целёвку).
        draft = await create_expense_payment_draft(
            session, article_id=article.id, amount=Decimal("100"), purpose="   "
        )
        assert draft.target_purpose == article.name
        assert draft.payload["paymentPurpose"] == article.name
        # Нулевая/отрицательная сумма всё так же запрещена.
        with pytest.raises(CounterpartyPaymentError, match="больше нуля"):
            await create_expense_payment_draft(
                session, article_id=article.id, amount=Decimal("0"), purpose="x"
            )


async def test_context_articles_follow_permissions(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        # Только оплата накладных/предоплата: видны ровно supplier-маршруты.
        supplier_only = await list_new_payment_articles(
            session, permissions=frozenset({"invoices.normal.pay"})
        )
        flows = {item["flow"] for item in supplier_only}
        assert flows == {"supplier_prepayment", "supplier_invoices"}
        codes = {item["code"] for item in supplier_only}
        assert PREPAYMENT_ARTICLE_CODE in codes
        assert DEFAULT_SUPPLIER_ARTICLE_CODE in codes

        # Только свободный вывод: ни одной статьи-маршрута, только expense.
        expense_only = await list_new_payment_articles(
            session, permissions=frozenset({"finance.safe.allocate"})
        )
        assert expense_only, "свободные статьи должны быть в сидах"
        assert {item["flow"] for item in expense_only} == {"expense"}
        expense_codes = {item["code"] for item in expense_only}
        assert not expense_codes & set(FLOW_BY_ARTICLE_CODE)
        assert not expense_codes & PROTECTED_ARTICLE_CODES

        # Займ-статья видна только с правом займов ПОВЕРХ issue-права авансов:
        # POST /payroll/advances требует issue-право по должности до allow_loan,
        # одиночное право займов дало бы мёртвый маршрут (форма → всегда 403).
        advance_only = await list_new_payment_articles(
            session, permissions=frozenset({"payroll.advances.admin.issue"})
        )
        assert {item["flow"] for item in advance_only} == {"employee_advance"}
        loans_only = await list_new_payment_articles(
            session, permissions=frozenset({"payroll.loans.issue"})
        )
        assert loans_only == []
        advance_plus_loans = await list_new_payment_articles(
            session,
            permissions=frozenset(
                {"payroll.advances.production.issue", "payroll.loans.issue"}
            ),
        )
        assert {item["flow"] for item in advance_plus_loans} == {
            "employee_advance",
            "employee_loan",
        }
        assert LOAN_ARTICLE_CODE in {item["code"] for item in advance_plus_loans}


async def test_context_hides_kassa_enabled_free_articles(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Флаг «доступна в кассе» убирает свободную статью из окна (у неё контур кассы)."""
    async with async_session_factory() as session:
        article = await _free_expense_article(session)
        article.kassa_enabled = True
        await session.flush()
        items = await list_new_payment_articles(
            session, permissions=frozenset({"finance.safe.allocate"})
        )
        assert article.code not in {item["code"] for item in items}


async def _make_employee(
    session: AsyncSession, *, full_name: str, position: str
) -> Employee:
    employee = Employee(
        id=uuid.uuid4(),
        full_name=full_name,
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
            position=position,
            effective_from=date(2026, 1, 1),
            effective_to=None,
        )
    )
    await session.flush()
    return employee


async def test_context_employees_carry_on_demand_flag(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        okladnik = await _make_employee(
            session, full_name="Окладник Востребованный", position="Управляющий"
        )
        cook = await _make_employee(session, full_name="Повар Обычный", position="Повар")
        session.add(
            AppSetting(
                key=OKLADNIK_PAYOUT_MODES_KEY,
                value={"Управляющий": "on_demand"},
                value_type="object",
                category="Зарплата",
                display_name="Режимы выплаты окладов",
                widget_type="json",
            )
        )
        await session.commit()

        employees = {item["full_name"]: item for item in await list_new_payment_employees(session)}
        assert employees[okladnik.full_name]["on_demand"] is True
        assert employees[cook.full_name]["on_demand"] is False

        # Пользователю с одним правом выплат полный штат не раскрываем — только on_demand.
        limited = {
            item["full_name"]
            for item in await list_new_payment_employees(session, include_all=False)
        }
        assert okladnik.full_name in limited
        assert cook.full_name not in limited

        # Уволенный сотрудник в список не попадает.
        cook_row = await session.get(Employee, cook.id)
        cook_row.status = "inactive"
        await session.commit()
        names = {item["full_name"] for item in await list_new_payment_employees(session)}
        assert cook.full_name not in names


async def test_invoices_draft_amount_is_sum_of_remainders(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Сумма черновика оплаты накладных = Σ остатков выбранных (частичная оплата учтена)."""
    async with async_session_factory() as session:
        supplier = await make_counterparty(
            session,
            name="ООО Остатки",
            inn="7709998887",
            requisites={
                "bankAcnt": "40702810400000012345",
                "bankBik": "044525225",
                "recipientCorrAccountNumber": "30101810400000000225",
            },
            requisites_verified=True,
        )
        inv1 = await make_invoice(
            session, counterparty_id=supplier.id, amount="1000.00", number="51"
        )
        inv2 = await make_invoice(
            session, counterparty_id=supplier.id, amount="2000.00", number="52"
        )
        # Частичная оплата первой накладной: остаток 600.
        session.add(
            InvoicePaymentAllocation(
                invoice_id=inv1.id,
                source_kind="bank",
                bank_operation_id=None,
                amount=Decimal("400.00"),
            )
        )
        inv1.payment_status = "partially_paid"
        await session.commit()

        draft = await create_payment_draft_for_invoices(
            session, invoice_ids=[inv1.id, inv2.id], actor_user_id=None
        )
        assert draft.amount == Decimal("2600.00")
