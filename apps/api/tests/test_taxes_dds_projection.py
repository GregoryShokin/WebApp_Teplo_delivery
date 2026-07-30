"""Проекция налоговых фактов в проводки ДДС.

Контур, которого не хватало: платёж создан в «Налогах», ушёл в банк, вернулся выпиской —
и до сих пор оседал в «Требует разбора», потому что налоговый слой в ДДС не писал ничего.
Проверяем разнос по видам, дозревание ЕНП, идемпотентность и — главное — что автомат
никогда не перебивает разметку владельца.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    Account,
    BankOperation,
    CashflowTransaction,
    DdsArticle,
    ReconciliationCase,
    Wallet,
)
from app.models.tax import TaxBankDraft, TaxPayment
from app.services.taxes.dds_projection import (
    PAYROLL_TAX_ARTICLE_CODE,
    TAX_ARTICLE_CODE,
    project_tax_facts_to_dds,
)

FNS_INN = "7727406020"
SFR_INN = "6163013494"
OP_DATE = date(2026, 7, 29)


async def _bank_wallet(session: AsyncSession) -> Wallet:
    account = Account(
        id=uuid.uuid4(),
        bank_code="tbank",
        account_number=f"4080281{uuid.uuid4().int % 10**12:012d}",
        legal_entity="ИП Шокина К.Ю.",
        status="active",
    )
    session.add(account)
    await session.flush()
    wallet = Wallet(
        id=uuid.uuid4(),
        code=f"bank-tax-{uuid.uuid4().hex[:8]}",
        name="Тест банк (налоги)",
        type="bank",
        status="active",
        account_id=account.id,
        opening_balance=Decimal("0"),
    )
    session.add(wallet)
    await session.flush()
    return wallet


async def _articles(session: AsyncSession) -> dict[str, DdsArticle]:
    """Налоговые статьи каталога. Сид их уже несёт — переиспользуем, иначе заводим."""
    found = {}
    for code, name in ((TAX_ARTICLE_CODE, "Налоги"), (PAYROLL_TAX_ARTICLE_CODE, "Налоги с з/п")):
        article = await session.scalar(select(DdsArticle).where(DdsArticle.code == code))
        if article is None:
            article = DdsArticle(
                code=code, name=name, movement_type="outflow", activity_type="operating"
            )
            session.add(article)
            await session.flush()
        found[code] = article
    return found


async def _operation(
    session: AsyncSession,
    wallet: Wallet,
    amount: str,
    *,
    inn: str = FNS_INN,
    status: str = "needs_review",
    purpose: str = "Единый налоговый платеж",
    doc_number: str = "23570",
) -> BankOperation:
    operation = BankOperation(
        id=uuid.uuid4(),
        provider="tbank",
        provider_operation_id=f"op-{uuid.uuid4()}",
        account_id=wallet.account_id,
        operation_date=OP_DATE,
        direction="out",
        amount=Decimal(amount),
        currency="RUB",
        counterparty_name_raw="Казначейство России (ФНС России)",
        counterparty_inn_raw=inn,
        payment_purpose=purpose,
        document_number=doc_number,
        raw_payload={},
        classification_status=status,
    )
    session.add(operation)
    await session.flush()
    return operation


def _fact(
    operation: BankOperation,
    kind: str,
    amount: str,
    *,
    recipient: str = "fns",
    quality: str = "confirmed",
    bundle_id: uuid.UUID | None = None,
) -> TaxPayment:
    return TaxPayment(
        id=uuid.uuid4(),
        bundle_id=bundle_id or uuid.uuid4(),
        paid_on=operation.operation_date,
        kind=kind,
        amount=Decimal(amount),
        recipient=recipient,
        for_year=2026,
        for_period="h1",
        status="paid",
        source_kind="bank_statement",
        quality_status=quality,
        bank_operation_id=operation.id,
        cashflow_transaction_id=None,
        document_number=operation.document_number,
        purpose=operation.payment_purpose,
    )


async def _rows(session: AsyncSession, operation: BankOperation) -> list[CashflowTransaction]:
    return list(
        (
            await session.scalars(
                select(CashflowTransaction)
                .where(
                    CashflowTransaction.source_kind == "bank_operation",
                    CashflowTransaction.source_id == operation.id,
                )
                .order_by(CashflowTransaction.amount)
            )
        ).all()
    )


async def test_usn_payment_books_tax_article(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """УСН 478 376 ₽ → одна проводка «Налоги», операция размечена, факт знает свою проводку."""
    async with async_session_factory() as session:
        wallet = await _bank_wallet(session)
        articles = await _articles(session)
        operation = await _operation(session, wallet, "478376.00")
        fact = _fact(operation, "usn_advance", "478376.00")
        session.add(fact)
        await session.commit()

        report = await project_tax_facts_to_dds(session)
        await session.commit()

        assert report.projected == 1
        rows = await _rows(session, operation)
        assert len(rows) == 1
        assert rows[0].article_id == articles[TAX_ARTICLE_CODE].id
        assert rows[0].amount == Decimal("478376.00")
        assert rows[0].quality_status == "final"
        assert rows[0].counterparty_id is None
        assert rows[0].comment == "УСН"
        refreshed_op = await session.get(BankOperation, operation.id)
        assert refreshed_op is not None
        assert refreshed_op.classification_status == "classified"
        assert refreshed_op.cashflow_transaction_id == rows[0].id
        refreshed_fact = await session.get(TaxPayment, fact.id)
        assert refreshed_fact is not None
        assert refreshed_fact.cashflow_transaction_id == rows[0].id


async def test_payroll_enp_splits_into_two_rows(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Зарплатный ЕНП виден в журнале составом: НДФЛ и взносы — отдельными строками.

    Обе строки ведут в «Налоги с з/п», но владельцу нужен состав платежа на экране
    (решение 30.07.2026), поэтому доли не схлопываются в одну.
    """
    async with async_session_factory() as session:
        wallet = await _bank_wallet(session)
        articles = await _articles(session)
        operation = await _operation(session, wallet, "14902.30", doc_number="157458")
        bundle = uuid.uuid4()
        session.add_all(
            [
                _fact(operation, "ndfl", "6331.00", quality="reconstructed", bundle_id=bundle),
                _fact(
                    operation,
                    "contrib_employees",
                    "8571.30",
                    quality="reconstructed",
                    bundle_id=bundle,
                ),
            ]
        )
        await session.commit()

        report = await project_tax_facts_to_dds(session)
        await session.commit()

        assert report.projected == 1
        rows = await _rows(session, operation)
        assert len(rows) == 2
        payroll_article = articles[PAYROLL_TAX_ARTICLE_CODE].id
        assert {row.article_id for row in rows} == {payroll_article}
        assert sorted(row.amount for row in rows) == [Decimal("6331.00"), Decimal("8571.30")]
        assert {row.comment for row in rows} == {"НДФЛ", "Страховые взносы за работников"}
        # Сумма разноса равна сумме операции — деньги не задвоены и не потеряны.
        assert sum(row.amount for row in rows) == Decimal("14902.30")
        # Каждая строка факта смотрит на СВОЮ проводку, а не на якорь операции.
        facts = list(
            (
                await session.scalars(
                    select(TaxPayment).where(TaxPayment.bank_operation_id == operation.id)
                )
            ).all()
        )
        by_amount = {row.amount: row.id for row in rows}
        for fact in facts:
            assert fact.cashflow_transaction_id == by_amount[fact.amount]


async def test_injury_books_payroll_article(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Травматизм в СФР — зарплатный контур, статья «Налоги с з/п»."""
    async with async_session_factory() as session:
        wallet = await _bank_wallet(session)
        articles = await _articles(session)
        operation = await _operation(session, wallet, "100.00", inn=SFR_INN, doc_number="466945")
        session.add(_fact(operation, "contrib_injury", "100.00", recipient="sfr"))
        await session.commit()

        await project_tax_facts_to_dds(session)
        await session.commit()

        rows = await _rows(session, operation)
        assert len(rows) == 1
        assert rows[0].article_id == articles[PAYROLL_TAX_ARTICLE_CODE].id
        assert rows[0].comment == "Взносы на травматизм"


async def test_own_contributions_go_to_general_tax_article(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Взносы ИП «за себя» — в «Налоги», не в «Налоги с з/п» (решение владельца 30.07.2026)."""
    async with async_session_factory() as session:
        wallet = await _bank_wallet(session)
        articles = await _articles(session)
        operation = await _operation(session, wallet, "105628.00")
        session.add(_fact(operation, "contrib_extra_1pct", "105628.00"))
        await session.commit()

        await project_tax_facts_to_dds(session)
        await session.commit()

        rows = await _rows(session, operation)
        assert rows[0].article_id == articles[TAX_ARTICLE_CODE].id


async def test_immature_enp_takes_article_from_draft(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Оборотки ещё нет: состав неизвестен, но черновик подсказывает зарплатный контур."""
    async with async_session_factory() as session:
        wallet = await _bank_wallet(session)
        articles = await _articles(session)
        operation = await _operation(session, wallet, "14902.30", doc_number="157458")
        session.add(
            TaxBankDraft(
                id=uuid.uuid4(),
                tax_kind="enp_payroll",
                for_year=2026,
                for_period="2026-06",
                title="Зарплатный ЕНП за июнь",
                amount=Decimal("14902.30"),
                purpose="Единый налоговый платеж",
                due_date=date(2026, 7, 28),
                status="in_bank",
                bank_provider="tbank",
            )
        )
        session.add(_fact(operation, "other", "14902.30", quality="requires_review"))
        await session.commit()

        await project_tax_facts_to_dds(session)
        await session.commit()

        rows = await _rows(session, operation)
        assert len(rows) == 1
        assert rows[0].article_id == articles[PAYROLL_TAX_ARTICLE_CODE].id
        assert "состав уточнится" in (rows[0].comment or "")


async def test_immature_without_draft_falls_back_to_general_article(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ни состава, ни черновика — деньги всё равно в бюджет: родовая статья «Налоги»."""
    async with async_session_factory() as session:
        wallet = await _bank_wallet(session)
        articles = await _articles(session)
        operation = await _operation(session, wallet, "5000.00")
        session.add(_fact(operation, "other", "5000.00", quality="requires_review"))
        await session.commit()

        await project_tax_facts_to_dds(session)
        await session.commit()

        rows = await _rows(session, operation)
        assert rows[0].article_id == articles[TAX_ARTICLE_CODE].id


async def test_reprojects_after_ripening_without_doubling(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Дозревание: строка ``other`` заменена составом → разнос пересобран, расход не задвоен."""
    async with async_session_factory() as session:
        wallet = await _bank_wallet(session)
        articles = await _articles(session)
        operation = await _operation(session, wallet, "14902.30")
        immature = _fact(operation, "other", "14902.30", quality="requires_review")
        session.add(immature)
        await session.commit()

        await project_tax_facts_to_dds(session)
        await session.commit()
        assert len(await _rows(session, operation)) == 1

        # Пришла оборотка: факт-слой снёс «прочее» и разложил платёж по видам.
        await session.delete(immature)
        await session.flush()
        bundle = uuid.uuid4()
        session.add_all(
            [
                _fact(operation, "ndfl", "6331.00", quality="reconstructed", bundle_id=bundle),
                _fact(
                    operation,
                    "contrib_employees",
                    "8571.30",
                    quality="reconstructed",
                    bundle_id=bundle,
                ),
            ]
        )
        await session.commit()

        report = await project_tax_facts_to_dds(session)
        await session.commit()

        assert report.projected == 1
        rows = await _rows(session, operation)
        assert len(rows) == 2
        assert sum(row.amount for row in rows) == Decimal("14902.30")
        assert {row.article_id for row in rows} == {articles[PAYROLL_TAX_ARTICLE_CODE].id}


async def test_projection_is_idempotent(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Повторные прогоны не создают вторых проводок и не двигают уже сделанное."""
    async with async_session_factory() as session:
        wallet = await _bank_wallet(session)
        await _articles(session)
        operation = await _operation(session, wallet, "478376.00")
        session.add(_fact(operation, "usn_advance", "478376.00"))
        await session.commit()

        first = await project_tax_facts_to_dds(session)
        await session.commit()
        ids_after_first = [row.id for row in await _rows(session, operation)]

        second = await project_tax_facts_to_dds(session)
        await session.commit()
        third = await project_tax_facts_to_dds(session)
        await session.commit()

        assert first.projected == 1
        assert (second.projected, third.projected) == (0, 0)
        assert [row.id for row in await _rows(session, operation)] == ids_after_first


async def test_pending_case_is_closed(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Кейс «требует разбора» закрывается — иначе платёж висит в owner-review навсегда."""
    async with async_session_factory() as session:
        wallet = await _bank_wallet(session)
        await _articles(session)
        operation = await _operation(session, wallet, "478376.00")
        session.add(_fact(operation, "usn_advance", "478376.00"))
        session.add(
            ReconciliationCase(
                kind="unclassified_operation",
                status="pending",
                provider="tbank",
                bank_operation_id=operation.id,
                payload={},
            )
        )
        await session.commit()

        await project_tax_facts_to_dds(session)
        await session.commit()

        case = await session.scalar(
            select(ReconciliationCase).where(ReconciliationCase.bank_operation_id == operation.id)
        )
        assert case is not None
        assert case.status == "resolved"
        assert case.resolution_payload["action"] == "tax_fact_projection"


async def test_manual_owner_split_is_never_overwritten(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Разнос владельца сильнее автомата: проектор отступает и не трогает его строки."""
    async with async_session_factory() as session:
        wallet = await _bank_wallet(session)
        articles = await _articles(session)
        operation = await _operation(session, wallet, "478376.00", status="classified")
        manual = CashflowTransaction(
            wallet_id=wallet.id,
            direction="out",
            amount=Decimal("478376.00"),
            operation_date=OP_DATE,
            article_id=articles[PAYROLL_TAX_ARTICLE_CODE].id,
            source_kind="bank_operation",
            source_id=operation.id,
            quality_status="owner_review",
        )
        session.add(manual)
        await session.flush()
        operation.cashflow_transaction_id = manual.id
        session.add(_fact(operation, "usn_advance", "478376.00"))
        await session.commit()

        report = await project_tax_facts_to_dds(session)
        await session.commit()

        assert report.skipped == 1
        assert report.projected == 0
        rows = await _rows(session, operation)
        assert len(rows) == 1
        assert rows[0].id == manual.id
        assert rows[0].article_id == articles[PAYROLL_TAX_ARTICLE_CODE].id
        assert rows[0].quality_status == "owner_review"


async def test_amount_mismatch_is_skipped(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Сумма фактов разъехалась с операцией — не разносим и ничего не портим."""
    async with async_session_factory() as session:
        wallet = await _bank_wallet(session)
        await _articles(session)
        operation = await _operation(session, wallet, "478376.00")
        session.add(_fact(operation, "usn_advance", "400000.00"))
        await session.commit()

        report = await project_tax_facts_to_dds(session)
        await session.commit()

        assert report.skipped == 1
        assert await _rows(session, operation) == []
        refreshed = await session.get(BankOperation, operation.id)
        assert refreshed is not None and refreshed.classification_status == "needs_review"


async def test_missing_article_does_not_raise(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Владелец архивировал статью — операция пропускается, приём выписки не падает."""
    async with async_session_factory() as session:
        wallet = await _bank_wallet(session)
        articles = await _articles(session)
        articles[TAX_ARTICLE_CODE].is_active = False
        operation = await _operation(session, wallet, "478376.00")
        session.add(_fact(operation, "usn_advance", "478376.00"))
        await session.commit()

        report = await project_tax_facts_to_dds(session)
        await session.commit()

        assert report.skipped == 1
        assert await _rows(session, operation) == []


async def test_excluded_operation_is_left_alone(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Исключённую из ДДС операцию автомат не воскрешает."""
    async with async_session_factory() as session:
        wallet = await _bank_wallet(session)
        await _articles(session)
        operation = await _operation(session, wallet, "478376.00", status="excluded")
        session.add(_fact(operation, "usn_advance", "478376.00"))
        await session.commit()

        report = await project_tax_facts_to_dds(session)
        await session.commit()

        assert report.operations_seen == 0
        assert await _rows(session, operation) == []


async def test_disabled_flag_is_noop(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    """Аварийный выключатель гасит контур целиком, ничего не записав."""
    from app.core import config as config_module

    async with async_session_factory() as session:
        wallet = await _bank_wallet(session)
        await _articles(session)
        operation = await _operation(session, wallet, "478376.00")
        session.add(_fact(operation, "usn_advance", "478376.00"))
        await session.commit()

        settings = config_module.get_settings()
        monkeypatch.setattr(settings, "tax_dds_projection_enabled", False, raising=False)
        report = await project_tax_facts_to_dds(session)
        await session.commit()

        assert (report.operations_seen, report.projected) == (0, 0)
        assert await _rows(session, operation) == []


async def test_since_filter_limits_history(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    """Глубина истории ограничивается датой — расход закрытых месяцев не меняется задним числом."""
    from app.core import config as config_module

    async with async_session_factory() as session:
        wallet = await _bank_wallet(session)
        await _articles(session)
        operation = await _operation(session, wallet, "478376.00")
        operation.operation_date = date(2026, 3, 10)
        session.add(_fact(operation, "usn_advance", "478376.00"))
        await session.commit()

        settings = config_module.get_settings()
        monkeypatch.setattr(settings, "tax_dds_projection_since", date(2026, 7, 1), raising=False)
        report = await project_tax_facts_to_dds(session)
        await session.commit()

        assert report.operations_seen == 0
        assert await _rows(session, operation) == []


async def test_operation_with_own_prebooked_payment_is_left_alone(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Платёж в бюджет, заведённый через «Новый платёж», уже описан своей проводкой.

    Классификатор привязывает такую операцию к её prebooked-строке ДО налогового блока.
    Проектор обязан отступить — иначе один платёж лёг бы в журнал дважды: prebooked-строкой
    и строкой проектора.
    """
    async with async_session_factory() as session:
        wallet = await _bank_wallet(session)
        articles = await _articles(session)
        operation = await _operation(session, wallet, "478376.00", status="classified")
        prebooked = CashflowTransaction(
            wallet_id=wallet.id,
            direction="out",
            amount=Decimal("478376.00"),
            operation_date=OP_DATE,
            article_id=articles[TAX_ARTICLE_CODE].id,
            source_kind="counterparty_payment",
            quality_status="final",
        )
        session.add(prebooked)
        await session.flush()
        operation.cashflow_transaction_id = prebooked.id
        session.add(_fact(operation, "usn_advance", "478376.00"))
        await session.commit()

        report = await project_tax_facts_to_dds(session)
        await session.commit()

        assert report.skipped == 1
        assert report.projected == 0
        assert await _rows(session, operation) == []
        refreshed = await session.get(BankOperation, operation.id)
        assert refreshed is not None and refreshed.cashflow_transaction_id == prebooked.id


async def test_owner_row_survives_even_when_fact_points_at_it(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Замок на инвариант «человек сильнее автомата».

    Ссылку факта на строку владельца мог поставить предыдущий прогон, который перед этой
    разметкой отступил. Если считать такую строку «своей», следующий прогон снёс бы работу
    владельца через ``_clear_operation_cashflow``.
    """
    async with async_session_factory() as session:
        wallet = await _bank_wallet(session)
        articles = await _articles(session)
        operation = await _operation(session, wallet, "14902.30")
        owner_row = CashflowTransaction(
            wallet_id=wallet.id,
            direction="out",
            amount=Decimal("14902.30"),
            operation_date=OP_DATE,
            article_id=articles[TAX_ARTICLE_CODE].id,
            source_kind="bank_operation",
            source_id=operation.id,
            quality_status="owner_review",
        )
        session.add(owner_row)
        await session.flush()
        bundle = uuid.uuid4()
        facts = [
            _fact(operation, "ndfl", "6331.00", quality="reconstructed", bundle_id=bundle),
            _fact(
                operation, "contrib_employees", "8571.30", quality="reconstructed", bundle_id=bundle
            ),
        ]
        for fact in facts:
            fact.cashflow_transaction_id = owner_row.id
        session.add_all(facts)
        await session.commit()

        report = await project_tax_facts_to_dds(session)
        await session.commit()

        assert report.projected == 0
        rows = await _rows(session, operation)
        assert [row.id for row in rows] == [owner_row.id]
        assert rows[0].quality_status == "owner_review"


async def test_exclude_takes_the_tax_row_out_and_keeps_the_fact_link(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """«Исключить» на налоговой операции: расход уходит, ссылка факта остаётся валидной.

    Решение по ссылке ``TaxPayment.cashflow_transaction_id`` (30.07.2026): при мягком
    исключении её НЕ сбрасываем. Строка жива, просто помечена ``excluded``, поэтому ссылка
    никуда не «протухает» и никакого расхода за собой не тянет — исключённую проводку не
    считает ни один денежный контур. Сброс же в ``None`` дал бы ровно обратное: факты
    выглядели бы неразнесёнными, а связь пришлось бы восстанавливать заново.

    Сам автомат расход назад не вернёт: ``_operations_needing_projection`` не берёт операции
    со статусом ``excluded``, и самозалечивание молча не отменяет решение владельца.
    """
    from app.services.banking.classifier import apply_operation_action

    async with async_session_factory() as session:
        wallet = await _bank_wallet(session)
        await _articles(session)
        operation = await _operation(session, wallet, "478376.00")
        fact = _fact(operation, "usn_advance", "478376.00")
        session.add(fact)
        await session.commit()

        await project_tax_facts_to_dds(session)
        await session.commit()
        booked_id = (await _rows(session, operation))[0].id
        await session.refresh(fact)
        assert fact.cashflow_transaction_id == booked_id

        await apply_operation_action(session, operation, action="exclude")
        await session.commit()

        rows = await _rows(session, operation)
        assert [row.quality_status for row in rows] == ["excluded"]
        await session.refresh(fact)
        assert fact.cashflow_transaction_id == booked_id, "ссылка ведёт на живую строку"

        # Повторный прогон проектора не воскрешает расход по исключённой операции.
        report = await project_tax_facts_to_dds(session)
        await session.commit()
        assert report.projected == 0
        assert [row.quality_status for row in await _rows(session, operation)] == ["excluded"]
