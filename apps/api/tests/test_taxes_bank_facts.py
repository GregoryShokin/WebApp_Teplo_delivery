"""Факт-слой: списания в ФНС/СФР из выписки → строки ``tax_payment``.

Критичный для прода контур: без него вычет УСН пуст и налог завышен. Проверяем
слои доверия (черновик → план → платёжка → эвристика), обязательный разнос ЕНП
по оборотке, «дозревание» нераспознанных платежей и идемпотентность.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.dds import BankOperation
from app.models.tax import TaxBankDraft, TaxDocumentIntake, TaxPayment, TaxPayrollLedger
from app.services.taxes.bank_facts import sync_tax_facts_from_bank

FNS_INN = "7727406020"
SFR_INN = "6163013494"


def _operation(
    amount: str,
    op_date: date,
    *,
    inn: str = FNS_INN,
    direction: str = "out",
    purpose: str = "Единый налоговый платеж",
    doc_number: str | None = "123456",
) -> BankOperation:
    return BankOperation(
        id=uuid.uuid4(),
        provider="tbank",
        provider_operation_id=uuid.uuid4().hex,
        operation_date=op_date,
        direction=direction,
        amount=Decimal(amount),
        currency="RUB",
        counterparty_name_raw="Казначейство России (ФНС России)",
        counterparty_inn_raw=inn,
        payment_purpose=purpose,
        document_number=doc_number,
        raw_payload={},
        classification_status="pending",
    )


def _draft(
    kind: str, amount: str, *, period: str | None, due: date, title: str = ""
) -> TaxBankDraft:
    return TaxBankDraft(
        id=uuid.uuid4(),
        tax_kind=kind,
        for_year=2026,
        for_period=period,
        title=title or kind,
        amount=Decimal(amount),
        purpose="Единый налоговый платеж",
        due_date=due,
        status="in_bank",
        bank_provider="tbank",
    )


def _planned(kind: str, amount: str, due: date, period: str | None) -> TaxPayment:
    recipient = "sfr" if kind == "contrib_injury" else "fns"
    return TaxPayment(
        id=uuid.uuid4(),
        bundle_id=uuid.uuid4(),
        paid_on=due,
        kind=kind,
        amount=Decimal(amount),
        recipient=recipient,
        for_year=2026,
        for_period=period,
        status="planned",
        source_kind="tax_notice",
        quality_status="confirmed",
    )


def _intake(tax_kind: str, amount: str, due: date) -> TaxDocumentIntake:
    return TaxDocumentIntake(
        id=uuid.uuid4(),
        mailbox="corporate",
        from_addr="Бухгалтер <a@b.c>",
        attachment_sha256=(uuid.uuid4().hex + uuid.uuid4().hex),
        received_at=datetime(2026, 7, 1, tzinfo=UTC),
        filename="платёжка.docx",
        document_type="payment_order",
        status="parsed",
        recognition={
            "tax_kind": tax_kind,
            "amount": amount,
            "due_date": due.isoformat(),
        },
    )


def _ledger(month: int, contributions: str, ndfl: str = "0") -> TaxPayrollLedger:
    return TaxPayrollLedger(
        id=uuid.uuid4(),
        year=2026,
        month=month,
        tab_number="206",
        employee="ИВАНОВА И.И.",
        contributions=Decimal(contributions),
        ndfl=Decimal(ndfl),
    )


async def _facts(session: AsyncSession) -> list[TaxPayment]:
    return list(
        (
            await session.scalars(
                select(TaxPayment).where(TaxPayment.source_kind == "bank_statement")
            )
        ).all()
    )


async def test_draft_match_creates_fact_and_pays_draft(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Списание суммой нашего черновика: факт с видом/периодом черновика, черновик → paid."""
    async with async_session_factory() as session:
        draft = _draft("usn_advance", "478376.00", period="h1", due=date(2026, 7, 28))
        session.add(draft)
        session.add(_operation("478376.00", date(2026, 7, 27)))
        await session.commit()

        report = await sync_tax_facts_from_bank(session)
        await session.commit()

        facts = await _facts(session)
        assert report.bundles_created == 1
        assert report.drafts_paid == 1
        assert len(facts) == 1
        fact = facts[0]
        assert (fact.kind, fact.for_period, fact.for_year) == ("usn_advance", "h1", 2026)
        assert fact.quality_status == "confirmed"
        assert fact.paid_on == date(2026, 7, 27)
        assert fact.bank_operation_id is not None
        refreshed = await session.get(TaxBankDraft, draft.id)
        assert refreshed is not None and refreshed.status == "paid"


async def test_enp_operation_splits_by_turnover_ledger(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ЕНП 14 902,30 по платёжке бухгалтера: взносы июня из оборотки + НДФЛ остатком."""
    async with async_session_factory() as session:
        session.add(_intake("enp_payroll", "14902.30", date(2026, 7, 28)))
        session.add(_ledger(6, "8571.30", "3532"))
        session.add(_operation("14902.30", date(2026, 7, 28)))
        await session.commit()

        report = await sync_tax_facts_from_bank(session)
        await session.commit()

        facts = sorted(await _facts(session), key=lambda f: f.kind)
        assert report.bundles_created == 1
        assert [(f.kind, str(f.amount)) for f in facts] == [
            ("contrib_employees", "8571.30"),
            ("ndfl", "6331.00"),
        ]
        assert {f.for_period for f in facts} == {"2026-06"}
        assert {f.quality_status for f in facts} == {"reconstructed"}
        assert len({f.bundle_id for f in facts}) == 1  # один перевод — один bundle


async def test_enp_without_ledger_waits_then_ripens(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Без оборотки ЕНП ждёт в other/requires_review; пришла оборотка — дозрел."""
    async with async_session_factory() as session:
        session.add(_intake("enp_payroll", "14902.30", date(2026, 7, 28)))
        session.add(_operation("14902.30", date(2026, 7, 28)))
        await session.commit()

        report = await sync_tax_facts_from_bank(session)
        await session.commit()
        facts = await _facts(session)
        assert report.review_pending == 1
        assert [(f.kind, f.quality_status) for f in facts] == [("other", "requires_review")]

        session.add(_ledger(6, "8571.30", "3532"))
        await session.commit()

        report2 = await sync_tax_facts_from_bank(session)
        await session.commit()
        facts2 = sorted(await _facts(session), key=lambda f: f.kind)
        assert report2.bundles_ripened == 1
        assert [(f.kind, str(f.amount)) for f in facts2] == [
            ("contrib_employees", "8571.30"),
            ("ndfl", "6331.00"),
        ]


async def test_sfr_heuristic_and_unknown_fns_review(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """СФР без документа — травматизм по эвристике; неизвестный ФНС — на проверку."""
    async with async_session_factory() as session:
        session.add(_operation("57.14", date(2026, 6, 23), inn=SFR_INN))
        session.add(_operation("39029.00", date(2026, 6, 23)))
        await session.commit()

        report = await sync_tax_facts_from_bank(session)
        await session.commit()

        facts = {f.kind: f for f in await _facts(session)}
        assert report.bundles_created == 2
        assert facts["contrib_injury"].recipient == "sfr"
        assert facts["contrib_injury"].quality_status == "reconstructed"
        assert facts["other"].quality_status == "requires_review"
        assert facts["other"].recipient == "fns"
        assert report.review_pending == 1


async def test_planned_match_sets_kind_and_period(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Списание суммой планового обязательства: вид/период из плана (допвзнос 1%)."""
    async with async_session_factory() as session:
        session.add(_planned("contrib_extra_1pct", "105628.00", date(2026, 9, 25), "h1"))
        session.add(_operation("105628.00", date(2026, 9, 20)))
        await session.commit()

        await sync_tax_facts_from_bank(session)
        await session.commit()

        facts = await _facts(session)
        assert [(f.kind, f.for_period, f.quality_status) for f in facts] == [
            ("contrib_extra_1pct", "h1", "confirmed")
        ]


async def test_sync_is_idempotent_and_ignores_foreign_operations(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Повторный прогон не плодит фактов; чужие/входящие операции не трогаются."""
    async with async_session_factory() as session:
        session.add(_operation("57.14", date(2026, 6, 23), inn=SFR_INN))
        # Поставщик и входящая операция — не налоговый контур.
        session.add(_operation("5000.00", date(2026, 6, 23), inn="6167110428"))
        session.add(_operation("70000.00", date(2026, 6, 23), direction="in"))
        await session.commit()

        first = await sync_tax_facts_from_bank(session)
        await session.commit()
        second = await sync_tax_facts_from_bank(session)
        await session.commit()

        assert first.bundles_created == 1
        assert second.bundles_created == 0
        assert second.bundles_ripened == 0
        assert len(await _facts(session)) == 1


async def test_split_enp_feeds_deduction_with_contributions_only(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Вычет двигается ТОЛЬКО взносовой частью разнесённого ЕНП, не НДФЛ."""
    from app.services.taxes.repository import load_tax_inputs

    async with async_session_factory() as session:
        session.add(_intake("enp_payroll", "14902.30", date(2026, 7, 28)))
        session.add(_ledger(6, "8571.30", "3532"))
        session.add(_operation("14902.30", date(2026, 7, 28)))
        await session.commit()

        await sync_tax_facts_from_bank(session)
        await session.commit()

        inputs = await load_tax_inputs(session, as_of=date(2026, 8, 1))

    assert inputs.employees_paid == Decimal("8571.30")


# ── Разнос ЕНП за месяцы без оборотки (сверка с реальной выпиской 27.07.2026) ──


async def test_split_enp_uses_injury_payment_when_turnover_missing(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Нет оборотки — взносы берём из платёжки травматизма: 100 ₽ / 0,2 % → ФОТ 50 000.

    Реальный случай: оборотки за январь и февраль 2026 бухгалтер не присылала, а ЕНП
    21 783,93 ₽ уплачен. Взносы 13 595,93 ₽ (тариф МСП от 50 000) сверены с платёжкой
    копейка в копейку — без этого разноса они не попадали в вычет и УСН был завышен.
    """
    async with async_session_factory() as session:
        session.add(_intake("enp_payroll", "21783.93", date(2026, 2, 27)))
        session.add(_operation("100.00", date(2026, 1, 26), inn=SFR_INN))
        session.add(_operation("21783.93", date(2026, 2, 25)))
        await session.commit()

        await sync_tax_facts_from_bank(session)
        await session.commit()

        facts = {(f.kind, f.for_period): f.amount for f in await _facts(session)}

    assert facts[("contrib_employees", "2026-01")] == Decimal("13595.93")
    assert facts[("ndfl", "2026-01")] == Decimal("8188.00")


async def test_enp_smaller_than_contributions_is_pure_ndfl(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Платёж меньше взносов месяца — это отдельный НДФЛ, а не часть взносов.

    Платёжка «ЕНП-НДФЛ с аванса» на 1 733 ₽ при взносах 13 595,93 ₽: без проверки
    ``amount < взносы`` весь платёж уходил в взносы и завышал вычет УСН.
    """
    async with async_session_factory() as session:
        session.add(_ledger(1, "13595.93", "6500"))
        session.add(_intake("enp_payroll", "1733.00", date(2026, 2, 27)))
        session.add(_operation("1733.00", date(2026, 1, 21)))
        await session.commit()

        await sync_tax_facts_from_bank(session)
        await session.commit()

        facts = await _facts(session)

    assert [(f.kind, f.for_period, f.amount) for f in facts] == [
        ("ndfl", "2026-01", Decimal("1733.00"))
    ]


async def test_enp_period_is_accrual_month_not_due_month(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Месяц ЕНП — месяц начисления (срок 28.N+1 → N), даже если в платёжке стоит апрель.

    Дефект: подсказка периода из имени файла «ЕНП сроком до 28.04» читалась как 2026-04,
    и платежи за март и за апрель занимали ОДИН слот — вычет задваивался на одном месяце
    и терялся на другом.
    """
    async with async_session_factory() as session:
        march = _intake("enp_payroll", "20559.93", date(2026, 4, 28))
        march.recognition = {**march.recognition, "period_hint": "2026-04"}
        april = _intake("enp_payroll", "18556.93", date(2026, 5, 28))
        april.recognition = {**april.recognition, "period_hint": "2026-05"}
        session.add_all([march, april])
        session.add(_ledger(3, "13595.93", "6500"))
        session.add(_ledger(4, "13595.93", "6500"))
        session.add(_operation("20559.93", date(2026, 4, 20)))
        session.add(_operation("18556.93", date(2026, 5, 26)))
        await session.commit()

        await sync_tax_facts_from_bank(session)
        await session.commit()

        periods = sorted(
            f.for_period
            for f in await _facts(session)
            if f.kind == "contrib_employees"
        )

    assert periods == ["2026-03", "2026-04"]


async def test_kopeck_payment_does_not_match_zero_document(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Добор 0,90 ₽ не приклеивается к нулевой платёжке из-за допуска ±1 ₽.

    Бухгалтер прислала «УСН 2 кв» пустой (0 ₽) и заменила на следующий день. Допуск
    ±1 ₽ ловил её на копеечный добор к ЕНП — 0,90 ₽ уезжали в УСН.
    """
    async with async_session_factory() as session:
        session.add(_intake("usn_advance", "0", date(2026, 7, 28)))
        session.add(_operation("0.90", date(2026, 4, 27)))
        await session.commit()

        await sync_tax_facts_from_bank(session)
        await session.commit()

        facts = await _facts(session)

    assert [(f.kind, f.amount) for f in facts] == [("other", Decimal("0.90"))]


async def test_ripening_runs_after_new_operations_in_one_pass(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Основание разноса, появившееся в этом же проходе, применяется сразу.

    Травматизм за январь уплачен 26.01, а НДФЛ-ЕНП — 21.01: при дозревании ДО разбора
    новых операций платёж 21.01 оставался «нужна проверка» до следующего запуска —
    результат зависел от числа нажатий кнопки.
    """
    async with async_session_factory() as session:
        session.add(_intake("enp_payroll", "1733.00", date(2026, 2, 27)))
        session.add(_operation("1733.00", date(2026, 1, 21)))
        session.add(_operation("100.00", date(2026, 1, 26), inn=SFR_INN))
        await session.commit()

        report = await sync_tax_facts_from_bank(session)
        await session.commit()

        facts = {(f.kind, f.for_period) for f in await _facts(session)}

    assert report.review_pending == 0
    assert ("ndfl", "2026-01") in facts


async def test_kopeck_topup_is_attached_to_its_payment_order(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Добор к платёжке ЕНП относится к ней же, а не висит «прочим».

    Реальный случай: платёжка марта на 20 559,93, ушло 20 559,03, бухгалтер написала «не
    доплатили 90 копеек» — доплату сделали отдельным платежом. Такой платёж ни с чем не
    матчится по сумме и оставался неразнесённым: НДФЛ месяца не сходился на 90 копеек.
    """
    async with async_session_factory() as session:
        session.add(_intake("enp_payroll", "20559.93", date(2026, 4, 28)))
        session.add(_ledger(3, "13595.93", "6500"))
        session.add(_operation("20559.03", date(2026, 4, 20)))
        session.add(_operation("0.90", date(2026, 4, 27)))
        await session.commit()

        await sync_tax_facts_from_bank(session)
        await session.commit()

        facts = await _facts(session)

    ndfl = sorted(
        (f.amount for f in facts if f.kind == "ndfl" and f.for_period == "2026-03")
    )
    assert ndfl == [Decimal("0.90"), Decimal("6963.10")]  # вместе — 6 964,00
    assert not [f for f in facts if f.kind == "other"]


async def test_small_payment_without_matching_shortfall_stays_unallocated(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Мелкий платёж, который НЕ добивает ни одну платёжку, остаётся «прочим».

    Замок против соблазна «пристроить копейки куда-нибудь»: совпадение недостачи требуется
    точное, иначе в вычет уедут случайные платежи.
    """
    async with async_session_factory() as session:
        session.add(_intake("enp_payroll", "20559.93", date(2026, 4, 28)))
        session.add(_ledger(3, "13595.93", "6500"))
        session.add(_operation("20559.03", date(2026, 4, 20)))
        session.add(_operation("5.00", date(2026, 4, 27)))
        await session.commit()

        await sync_tax_facts_from_bank(session)
        await session.commit()

        facts = await _facts(session)

    assert [(f.kind, f.amount) for f in facts if f.kind == "other"] == [
        ("other", Decimal("5.00"))
    ]


async def test_draft_match_prefers_document_number(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Два черновика одной суммы разводятся по номеру платёжки, а не «как повезёт».

    Травматизм 100 ₽ за разные месяцы законно висит `in_bank` одновременно (уникум слота —
    по виду и периоду). Номер документа детерминирован: он выведен из `document_id`,
    закоммиченного до вызова банка, и возвращается в выписке.
    """
    from app.services.banking.tbank import _document_number

    async with async_session_factory() as session:
        target = _draft(
            "contrib_injury", "100.00", period="2026-06", due=date(2026, 7, 15), title="июнь"
        )
        target.document_id = uuid.uuid4().hex
        other = _draft(
            "contrib_injury", "100.00", period="2026-07", due=date(2026, 8, 15), title="июль"
        )
        other.document_id = uuid.uuid4().hex
        session.add_all([target, other])
        operation = _operation(
            "100.00",
            date(2026, 7, 29),
            inn=SFR_INN,
            purpose="Страховые взносы (травматизм)",
            doc_number=_document_number(target.document_id),
        )
        session.add(operation)
        await session.commit()

        await sync_tax_facts_from_bank(session)
        await session.commit()

        # Закрылся именно тот черновик, чей номер стоит в операции, хотя ближе по сроку — другой.
        assert (await session.get(TaxBankDraft, target.id)).status == "paid"
        assert (await session.get(TaxBankDraft, other.id)).status == "in_bank"


async def test_draft_match_falls_back_when_number_differs(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Платёжку подготовили у нас, а оплатили руками из банк-клиента — номер чужой.

    Замок против соблазна сделать номер жёстким фильтром: такой платёж ушёл бы в
    ``other``/``requires_review``, то есть мимо вычета УСН, и налог был бы завышен.
    """
    async with async_session_factory() as session:
        draft = _draft("usn_advance", "478376.00", period="h1", due=date(2026, 7, 28))
        draft.document_id = uuid.uuid4().hex
        session.add(draft)
        session.add(_operation("478376.00", date(2026, 7, 29), doc_number="900001"))
        await session.commit()

        await sync_tax_facts_from_bank(session)
        await session.commit()

        facts = await _facts(session)
        assert [(f.kind, f.quality_status) for f in facts] == [("usn_advance", "confirmed")]
        assert (await session.get(TaxBankDraft, draft.id)).status == "paid"


async def test_draft_match_ignores_other_bank(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Черновик уходил в Сбер — списание Т-Банка той же суммы закрывать его не вправе."""
    async with async_session_factory() as session:
        draft = _draft("usn_advance", "478376.00", period="h1", due=date(2026, 7, 28))
        draft.bank_provider = "sber"
        session.add(draft)
        session.add(_operation("478376.00", date(2026, 7, 29)))
        await session.commit()

        await sync_tax_facts_from_bank(session)
        await session.commit()

        assert (await session.get(TaxBankDraft, draft.id)).status == "in_bank"
        facts = await _facts(session)
        assert [f.kind for f in facts] == ["other"]


async def test_fact_rows_start_without_cashflow_link(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ссылку на проводку ставит проектор ДДС — факт-слой её не выдумывает.

    Замок на правку, без которой контур молча перестаёт переразносить дозревшие платежи:
    «факт без проводки» — единственный признак, по которому проектор понимает, что надо
    пересобрать разнос.
    """
    async with async_session_factory() as session:
        operation = _operation("478376.00", date(2026, 7, 29))
        operation.cashflow_transaction_id = None
        session.add(_draft("usn_advance", "478376.00", period="h1", due=date(2026, 7, 28)))
        session.add(operation)
        await session.commit()

        await sync_tax_facts_from_bank(session)
        await session.commit()

        facts = await _facts(session)
        assert facts and all(f.cashflow_transaction_id is None for f in facts)


# ── Черновик не ловит списания вечно ──────────────────────────────────────────
#
# Единственными жёсткими условиями матча были получатель и точная сумма, а выхода из
# `in_bank`, кроме удачного матча, у черновика не было вовсе. Платёжка, которую владелец
# не подтвердил в банк-клиенте (или удалил там), висела активной вечно и однажды
# перехватывала бы постороннее списание той же суммы, помечаясь оплаченной.


def _stale(draft: TaxBankDraft, *, days: int = 60) -> TaxBankDraft:
    """Черновик, отправленный в банк `days` дней назад (по умолчанию — заведомо протухший)."""
    draft.sent_to_bank_at = datetime.now(UTC) - timedelta(days=days)
    return draft


async def test_stale_draft_does_not_capture_foreign_debit(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Протухший черновик не забирает чужое списание той же суммы.

    Платёж уходит в ``other``/``requires_review`` — консервативно (мимо вычета, налог не
    занижаем) и видимо на «Платежах», а черновик остаётся `in_bank`: снять его — решение
    владельца, а не догадка синка.
    """
    async with async_session_factory() as session:
        draft = _stale(_draft("usn_advance", "478376.00", period="h1", due=date(2026, 5, 28)))
        draft.document_id = uuid.uuid4().hex
        session.add(draft)
        session.add(_operation("478376.00", date(2026, 7, 29), doc_number="900001"))
        await session.commit()

        await sync_tax_facts_from_bank(session)
        await session.commit()

        facts = await _facts(session)
        assert [(f.kind, f.quality_status) for f in facts] == [("other", "requires_review")]
        refreshed = await session.get(TaxBankDraft, draft.id)
        assert refreshed is not None
        assert refreshed.status == "in_bank"
        assert refreshed.settled_operation_id is None


async def test_stale_draft_still_matches_by_own_document_number(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Своя платёжка, оплаченная с опозданием, не теряется: номер документа работает бессрочно.

    Протухание бьёт только по послаблению «совпала сумма» — иначе запоздалый платёж уехал бы
    мимо вычета УСН, и налог оказался бы завышен.
    """
    from app.services.banking.tbank import _document_number

    async with async_session_factory() as session:
        draft = _stale(_draft("usn_advance", "478376.00", period="h1", due=date(2026, 5, 28)))
        draft.document_id = uuid.uuid4().hex
        session.add(draft)
        operation = _operation(
            "478376.00", date(2026, 7, 29), doc_number=_document_number(draft.document_id)
        )
        session.add(operation)
        await session.commit()

        await sync_tax_facts_from_bank(session)
        await session.commit()

        facts = await _facts(session)
        assert [(f.kind, f.quality_status) for f in facts] == [("usn_advance", "confirmed")]
        refreshed = await session.get(TaxBankDraft, draft.id)
        assert refreshed is not None and refreshed.status == "paid"
        assert refreshed.settled_operation_id == operation.id


async def test_cancelled_draft_never_matches(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Отменённый черновик выбывает из разбора совсем — даже по своему номеру документа.

    Отмена — это «платёж снят», и она обязана быть окончательной: иначе снятая с очереди
    строка воскресала бы как `paid` от первого подходящего списания.
    """
    from app.services.banking.tbank import _document_number

    async with async_session_factory() as session:
        draft = _draft("usn_advance", "478376.00", period="h1", due=date(2026, 7, 28))
        draft.document_id = uuid.uuid4().hex
        draft.status = "cancelled"
        session.add(draft)
        session.add(
            _operation(
                "478376.00", date(2026, 7, 29), doc_number=_document_number(draft.document_id)
            )
        )
        await session.commit()

        await sync_tax_facts_from_bank(session)
        await session.commit()

        facts = await _facts(session)
        assert [(f.kind, f.quality_status) for f in facts] == [("other", "requires_review")]
        refreshed = await session.get(TaxBankDraft, draft.id)
        assert refreshed is not None and refreshed.status == "cancelled"


async def test_failed_draft_matches_only_by_document_number(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Отказ банка после реально созданной платёжки: списание подхватывается по номеру.

    ``document_id`` коммитится ДО вызова банка, поэтому у `failed`-черновика номер платёжки
    известен и детерминирован. По сумме такой черновик не матчится: подтверждения, что
    платёжка вообще существует, у него нет.
    """
    from app.services.banking.tbank import _document_number

    async with async_session_factory() as session:
        by_number = _draft("usn_advance", "478376.00", period="h1", due=date(2026, 7, 28))
        by_number.document_id = uuid.uuid4().hex
        by_number.status = "failed"
        session.add(by_number)
        session.add(
            _operation(
                "478376.00",
                date(2026, 7, 29),
                doc_number=_document_number(by_number.document_id),
            )
        )
        await session.commit()

        await sync_tax_facts_from_bank(session)
        await session.commit()

        assert [(f.kind, f.quality_status) for f in await _facts(session)] == [
            ("usn_advance", "confirmed")
        ]
        assert (await session.get(TaxBankDraft, by_number.id)).status == "paid"


async def test_failed_draft_ignores_debit_without_its_number(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Тот же `failed`-черновик и списание с ЧУЖИМ номером — матча нет."""
    async with async_session_factory() as session:
        draft = _draft("usn_advance", "478376.00", period="h1", due=date(2026, 7, 28))
        draft.document_id = uuid.uuid4().hex
        draft.status = "failed"
        session.add(draft)
        session.add(_operation("478376.00", date(2026, 7, 29), doc_number="900001"))
        await session.commit()

        await sync_tax_facts_from_bank(session)
        await session.commit()

        assert [f.kind for f in await _facts(session)] == ["other"]
        assert (await session.get(TaxBankDraft, draft.id)).status == "failed"


async def test_settled_draft_records_closing_operation(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Закрытый черновик помнит, какое списание его оплатило.

    Ссылка — не украшение: по ней закрытый черновик выбывает из разбора, поэтому второе
    списание той же суммы забрать его уже не может.
    """
    async with async_session_factory() as session:
        draft = _draft("usn_advance", "478376.00", period="h1", due=date(2026, 7, 28))
        session.add(draft)
        first = _operation("478376.00", date(2026, 7, 27))
        session.add(first)
        await session.commit()

        await sync_tax_facts_from_bank(session)
        await session.commit()

        refreshed = await session.get(TaxBankDraft, draft.id)
        assert refreshed is not None and refreshed.status == "paid"
        assert refreshed.settled_operation_id == first.id

        # Второе списание той же суммы уже не может «переоткрыть» закрытый черновик.
        session.add(_operation("478376.00", date(2026, 7, 30)))
        await session.commit()
        await sync_tax_facts_from_bank(session)
        await session.commit()

        kinds = sorted(f.kind for f in await _facts(session))
        assert kinds == ["other", "usn_advance"]
        again = await session.get(TaxBankDraft, draft.id)
        assert again is not None and again.settled_operation_id == first.id

        # Статус можно вернуть руками (правка в БД, будущая дефинализация) — ссылка на
        # закрывшую операцию остаётся и держит инвариант «оплачен один раз»: второе
        # списание такой черновик не забирает даже в `in_bank`.
        again.status = "in_bank"
        await session.commit()
        await sync_tax_facts_from_bank(session)
        await session.commit()

        assert sorted(f.kind for f in await _facts(session)) == ["other", "usn_advance"]
        reopened = await session.get(TaxBankDraft, draft.id)
        assert reopened is not None and reopened.status == "in_bank"
