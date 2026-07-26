"""Сверка трёх слоёв: расчёт vs документ vs факт.

Ядро — воспроизведение реальной ситуации 22.07.2026: бухгалтер прислала нулевую платёжку
по УСН при расчёте 478 376. Сверка обязана поймать это как ALERT автоматически.

Данные строятся из настоящих величин 2026 года (выручка iiko, платежи из выписки).
"""

from __future__ import annotations

import calendar
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.tax import (
    IikoRevenuePeriod,
    TaxDocumentIntake,
    TaxPayment,
    TaxPayrollLedger,
)
from app.services.taxes.enp_split import rebuild_payroll_enp_split
from app.services.taxes.reconcile import ReconLine, build_reconciliation
from app.services.taxes.repository import DEFAULT_DEPARTMENT

# Выручка со скидками, iiko, помесячно (сверено с платёжками).
REVENUE_2026 = {
    1: Decimal("4095915.11"),
    2: Decimal("3833597.82"),
    3: Decimal("4006534.78"),
    4: Decimal("3326144.31"),
    5: Decimal("4013871.62"),
    6: Decimal("3222736.00"),  # выведен из платёжки на 1% (полугодие 22 498 800)
}


async def _seed_revenue(session: AsyncSession, upto_month: int) -> None:
    for m in range(1, upto_month + 1):
        session.add(
            IikoRevenuePeriod(
                id=uuid.uuid4(),
                period_start=date(2026, m, 1),
                period_end=date(2026, m, calendar.monthrange(2026, m)[1]),
                granularity="month",
                department=DEFAULT_DEPARTMENT,
                revenue_net=REVENUE_2026[m],
                source="iiko_olap",
            )
        )
    await session.flush()


def _payment_order_intake(
    *, tax_kind: str, period: str, amount: str, due: date, received: datetime, filename: str
) -> TaxDocumentIntake:
    return TaxDocumentIntake(
        id=uuid.uuid4(),
        mailbox="corporate",
        from_addr="Бухгалтер <askad02@mail.ru>",
        attachment_sha256=(uuid.uuid4().hex + uuid.uuid4().hex),  # 64-символьный уникальный
        received_at=received,
        filename=filename,
        document_type="payment_order",
        status="parsed",
        recognition={
            "tax_kind": tax_kind,
            "period_hint": period,
            "amount": amount,
            "due_date": due.isoformat(),
        },
    )


def _usn_paid(amount: str, period: str, paid_on: date, doc: str) -> TaxPayment:
    return TaxPayment(
        id=uuid.uuid4(),
        bundle_id=uuid.uuid4(),
        paid_on=paid_on,
        kind="usn_advance",
        amount=Decimal(amount),
        recipient="fns",
        for_year=2026,
        for_period=period,
        status="paid",
        document_number=doc,
        source_kind="bank_statement",
        quality_status="confirmed",
    )


def _tax_payment(kind: str, amount: str, paid_on: date, recipient: str = "fns") -> TaxPayment:
    return TaxPayment(
        id=uuid.uuid4(),
        bundle_id=uuid.uuid4(),
        paid_on=paid_on,
        kind=kind,
        amount=Decimal(amount),
        recipient=recipient,
        for_year=2026,
        status="paid",
        source_kind="bank_statement",
        quality_status="reconstructed",
    )


async def _seed_deductions(session: AsyncSession) -> None:
    """Взносы, дающие вычет, — чтобы расчёт движка совпал с реальными платёжками.

    Q1 вычет = 41 539 (фикс 14 347,50 + взносы 26 891,50 + травма 300);
    H1 добавляет взносы Q2 (39 029) и допвзнос 116 360 → H1 вычет 196 928.
    """
    session.add(_tax_payment("contrib_fixed", "14347.50", date(2026, 1, 21)))
    session.add(_tax_payment("contrib_employees", "26891.50", date(2026, 3, 26)))
    session.add(_tax_payment("contrib_injury", "300", date(2026, 3, 26), recipient="sfr"))
    session.add(_tax_payment("contrib_employees", "39029.00", date(2026, 6, 23)))
    session.add(_tax_payment("contrib_extra_1pct", "116360", date(2026, 6, 9)))
    await session.flush()


def _line(recon, tax_kind, period_code):
    return next(
        ln for ln in recon.lines if ln.tax_kind == tax_kind and ln.period_code == period_code
    )


async def test_zero_payment_order_is_caught_as_alert(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """РЕАЛЬНАЯ ситуация 22.07: нулевая платёжка УСН при расчёте 478 376 → ALERT."""
    async with async_session_factory() as session:
        await _seed_revenue(session, 6)
        await _seed_deductions(session)
        # Аванс Q1 уплачен (674 624) — чтобы полугодовой расчёт дал именно 478 376.
        session.add(
            _usn_paid("674624", "q1", date(2026, 4, 20), "287")
        )
        # Нулевая платёжка-заглушка на полугодие.
        session.add(
            _payment_order_intake(
                tax_kind="usn_advance",
                period="h1",
                amount="0",
                due=date(2026, 7, 28),
                received=datetime(2026, 7, 22, tzinfo=UTC),
                filename="УСН 2 кв до 28.07.docx",
            )
        )
        await session.commit()

        recon = await build_reconciliation(session, as_of=date(2026, 7, 23))

    line = _line(recon, "usn_advance", "h1")
    assert line.calculated == Decimal("478376")
    assert line.documented == Decimal("0")
    assert line.verdict == "doc_mismatch"
    assert line.severity == "alert"
    assert recon.has_alerts
    assert any("расходится с расчётом" in m for m in line.messages)
    # Красному — конкретный следующий шаг для владельца.
    # Действие — короткий императив, причина уехала в action_why (дизайн-ревизия 27.07).
    assert line.action is not None and "Не платите" in line.action
    assert line.action_why is not None and "нулевую" in line.action_why


async def test_corrected_payment_order_reconciles_ok(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Исправленная платёжка 478 376, уплачено — сверка тихая."""
    async with async_session_factory() as session:
        await _seed_revenue(session, 6)
        await _seed_deductions(session)
        session.add(_usn_paid("674624", "q1", date(2026, 4, 20), "287"))
        # Нулевая, затем исправленная — сверка берёт САМУЮ СВЕЖУЮ.
        session.add(
            _payment_order_intake(
                tax_kind="usn_advance", period="h1", amount="0",
                due=date(2026, 7, 28), received=datetime(2026, 7, 22, tzinfo=UTC),
                filename="УСН 2 кв до 28.07.docx",
            )
        )
        session.add(
            _payment_order_intake(
                tax_kind="usn_advance", period="h1", amount="478376",
                due=date(2026, 7, 28), received=datetime(2026, 7, 23, tzinfo=UTC),
                filename="УСН 2 кв до 28.07.docx",
            )
        )
        session.add(_usn_paid("478376", "h1", date(2026, 7, 24), "301"))
        await session.commit()

        recon = await build_reconciliation(session, as_of=date(2026, 7, 25))

    line = _line(recon, "usn_advance", "h1")
    assert line.documented == Decimal("478376")  # свежая победила нулевую
    assert line.paid == Decimal("478376")
    assert line.verdict == "ok"
    assert line.severity == "ok"
    assert not recon.has_alerts


async def test_overdue_when_unpaid_past_due(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Документ есть, срок прошёл, не оплачено → просрочка (alert)."""
    async with async_session_factory() as session:
        await _seed_revenue(session, 6)
        await _seed_deductions(session)
        session.add(_usn_paid("674624", "q1", date(2026, 4, 20), "287"))
        session.add(
            _payment_order_intake(
                tax_kind="usn_advance", period="h1", amount="478376",
                due=date(2026, 7, 28), received=datetime(2026, 7, 23, tzinfo=UTC),
                filename="УСН 2 кв до 28.07.docx",
            )
        )
        await session.commit()

        recon = await build_reconciliation(session, as_of=date(2026, 8, 1))

    line = _line(recon, "usn_advance", "h1")
    assert line.paid is None
    assert line.verdict == "overdue"
    assert line.severity == "alert"


async def test_q1_reconciles_against_actual_payment(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Q1: расчёт 674 624, уплачено 674 624 — сверка ok."""
    async with async_session_factory() as session:
        await _seed_revenue(session, 3)
        await _seed_deductions(session)
        session.add(_usn_paid("674624", "q1", date(2026, 4, 20), "287"))
        await session.commit()

        recon = await build_reconciliation(session, as_of=date(2026, 4, 30))

    line = _line(recon, "usn_advance", "q1")
    assert line.calculated == Decimal("674624")
    assert line.paid == Decimal("674624")
    assert line.verdict == "ok"


async def test_extra_1pct_increment_is_not_a_false_alert(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Платёжка на 1% ПРИРОСТНАЯ — сверять её с накопленным начислением нельзя.

    Реальные данные: начислено нарастающим итогом 221 988, уплачено 116 360 (09.06),
    платёжка на остаток 105 628 (срок 25.09). 116 360 + 105 628 = 221 988 — расхождения НЕТ,
    и сверка обязана молчать, а не поднимать тревогу.
    """
    async with async_session_factory() as session:
        await _seed_revenue(session, 6)
        session.add(_tax_payment("contrib_extra_1pct", "116360", date(2026, 6, 9)))
        session.add(
            _payment_order_intake(
                tax_kind="contrib_extra_1pct", period="h1", amount="105628",
                due=date(2026, 9, 25), received=datetime(2026, 7, 22, tzinfo=UTC),
                filename="1% за 2 кв 2026.docx",
            )
        )
        await session.commit()

        recon = await build_reconciliation(session, as_of=date(2026, 7, 23))

    line = _line(recon, "contrib_extra_1pct", "year")
    assert line.calculated == Decimal("221988.00")
    assert line.documented == Decimal("105628")
    assert line.paid == Decimal("116360")
    assert line.verdict == "due"  # остаток к доплате, не расхождение
    assert line.severity != "alert"
    assert not recon.has_alerts


async def test_extra_1pct_real_shortfall_still_alerts(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Поправка на приростность не должна глушить НАСТОЯЩУЮ недоплату.

    Если платёжка выписана на 50 000 при остатке 105 628 — это реальное расхождение,
    и тревога обязана сработать.
    """
    async with async_session_factory() as session:
        await _seed_revenue(session, 6)
        session.add(_tax_payment("contrib_extra_1pct", "116360", date(2026, 6, 9)))
        session.add(
            _payment_order_intake(
                tax_kind="contrib_extra_1pct", period="h1", amount="50000",
                due=date(2026, 9, 25), received=datetime(2026, 7, 22, tzinfo=UTC),
                filename="1% за 2 кв 2026.docx",
            )
        )
        await session.commit()

        recon = await build_reconciliation(session, as_of=date(2026, 7, 23))

    line = _line(recon, "contrib_extra_1pct", "year")
    assert line.verdict == "doc_mismatch"
    assert line.severity == "alert"
    assert any("остатком к доплате" in m for m in line.messages)


async def test_extra_1pct_matches_payment_order(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Допвзнос 1%: расчёт нарастающим итогом сходится с платёжкой бухгалтера."""
    async with async_session_factory() as session:
        await _seed_revenue(session, 6)
        # Платёжка на 1% за полугодие: 221 988 нарастающим итогом.
        session.add(
            _payment_order_intake(
                tax_kind="contrib_extra_1pct", period="h1", amount="221988",
                due=date(2026, 9, 25), received=datetime(2026, 7, 22, tzinfo=UTC),
                filename="1% за 2 кв 2026.docx",
            )
        )
        await session.commit()

        recon = await build_reconciliation(session, as_of=date(2026, 6, 30))

    line = _line(recon, "contrib_extra_1pct", "year")
    # (22 498 800 − 300 000) × 1% = 221 988.
    assert line.calculated == Decimal("221988.00")
    assert line.documented == Decimal("221988")
    assert line.verdict in ("due", "ok")  # срок 25.09 ещё не наступил → due
    assert line.severity != "alert"


async def test_payroll_enp_split_line_shows_breakdown(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Зарплатный ЕНП: строка сверки показывает разнос НДФЛ + взносы из оборотки.

    Платёжка ЕНП (14 902,30) с помесячным начислением НЕ совпадает — НДФЛ платится «окнами»,
    поэтому она справочная, а строка сверяет начислено ↔ разнос (взносы в вычет, НДФЛ нет).
    """
    async with async_session_factory() as session:
        session.add(
            TaxPayrollLedger(
                id=uuid.uuid4(),
                year=2026,
                month=6,
                tab_number="206",
                employee="ИВАНОВА И.И.",
                contributions=Decimal("8571.30"),
                ndfl=Decimal("3532.00"),
            )
        )
        await session.flush()
        await rebuild_payroll_enp_split(session, year=2026)
        session.add(
            _payment_order_intake(
                tax_kind="enp_payroll", period="2026-06", amount="14902.30",
                due=date(2026, 7, 28), received=datetime(2026, 7, 22, tzinfo=UTC),
                filename="ЕНП до 28.07.docx",
            )
        )
        await session.commit()

        recon = await build_reconciliation(session, as_of=date(2026, 7, 31))

    line = _line(recon, "enp_payroll", "2026-06")
    assert line.calculated == Decimal("12103.30")  # 8571.30 + 3532.00 начислено
    assert line.paid == Decimal("12103.30")  # разнос уплачен к сроку 28.07 ≤ среза
    assert line.documented is None  # платёжка ушла в справку, не в колонку «Документ»
    assert line.verdict == "ok"
    assert line.severity == "ok"
    assert any("в вычет УСН" in m for m in line.messages)
    # Платёжка показана справочно с пояснением про «окна» НДФЛ, а не как расхождение.
    assert any("Справочно" in m and "окнами" in m for m in line.messages)
    assert not recon.has_alerts


# ── календарь: оплаченное обязательство закрыто, а не «просрочено» ─────────────


def _planned(
    kind: str, amount: str, due: date, period: str | None, recipient: str = "fns"
) -> TaxPayment:
    """Плановое обязательство из продвинутой платёжки (в `paid_on` лежит СРОК уплаты)."""
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


async def test_calendar_settles_planned_twin_by_period(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """РЕАЛЬНЫЙ случай 26.07: УСН за Q1 уплачен 20.04, но плановая строка из платёжки
    висела «просрочкой» на 674 624. Факт того же периода закрывает обязательство."""
    from app.api.v1.routes.taxes import get_calendar

    async with async_session_factory() as session:
        session.add(_planned("usn_advance", "674624", date(2026, 4, 28), "q1"))
        session.add(_usn_paid("674624", "q1", date(2026, 4, 20), "287"))
        await session.commit()

        result = await get_calendar(session, year=2026)

    assert result.overdue_count == 0
    assert [item.status for item in result.items] == ["paid"]  # плановый близнец скрыт


async def test_calendar_settles_periodless_fact_by_amount(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Факт без периода (реконструкция из выписки) закрывает обязательство по сумме;
    обязательство без факта остаётся в календаре."""
    from app.api.v1.routes.taxes import get_calendar

    async with async_session_factory() as session:
        # Взносы ИП: платёжка на ¼ года + факт уплаты без for_period той же суммой.
        session.add(_planned("contrib_fixed", "14347.50", date(2026, 12, 28), "q1"))
        session.add(_tax_payment("contrib_fixed", "14347.50", date(2026, 1, 21)))
        # Травматизм за июль: платёжка есть, факта уплаты нет — обязательство живо.
        session.add(
            _planned("contrib_injury", "100", date(2026, 8, 15), "2026-07", recipient="sfr")
        )
        await session.commit()

        result = await get_calendar(session, year=2026)

    by_status = {(item.status, str(item.kind)) for item in result.items}
    assert ("paid", "contrib_fixed") in by_status
    assert ("planned", "contrib_injury") in by_status  # не закрыт — факта нет
    assert ("planned", "contrib_fixed") not in by_status  # закрыт фактом по сумме


async def test_recon_line_shows_active_draft_status(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Строка сверки знает, что платёж уже в работе: кнопка «Отправить в банк» гаснет.

    Синхронизация с окном «Активные платежи» (решение владельца 26.07.2026): отправленный
    черновик по виду+периоду переводит строку в состояние draft_status='in_bank'.
    """
    from app.models.tax import TaxBankDraft

    async with async_session_factory() as session:
        await _seed_revenue(session, 6)
        await _seed_deductions(session)
        session.add(_usn_paid("674624", "q1", date(2026, 4, 20), "287"))
        session.add(
            _payment_order_intake(
                tax_kind="usn_advance", period="h1", amount="478376",
                due=date(2026, 7, 28), received=datetime(2026, 7, 23, tzinfo=UTC),
                filename="УСН 2 кв до 28.07.docx",
            )
        )
        session.add(
            TaxBankDraft(
                tax_kind="usn_advance",
                for_year=2026,
                for_period="h1",
                amount=Decimal("478376"),
                purpose="Единый налоговый платеж",
                status="in_bank",
            )
        )
        await session.commit()

        recon = await build_reconciliation(session, as_of=date(2026, 7, 25))

    line = _line(recon, "usn_advance", "h1")
    assert line.draft_status == "in_bank"
    # А у строки без черновика статуса нет.
    assert _line(recon, "usn_advance", "q1").draft_status is None


async def test_payments_registry_shows_only_bank_facts(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Журнал «Платежи» — только реальные списания: без планов и без разноса из оборотки.

    Раньше всё было свалено вместе, и страница показывала «оплачено» с будущими датами
    (разнос по сроку) рядом с дублями планов (решение владельца 26.07.2026)."""
    from app.api.v1.routes.taxes import list_payments

    async with async_session_factory() as session:
        session.add(_usn_paid("674624", "q1", date(2026, 4, 20), "287"))  # факт из банка
        session.add(_planned("usn_advance", "478376", date(2026, 7, 28), "h1"))  # план
        # Разнос из оборотки: «paid» по конвенции, дата = срок в будущем.
        razn = _tax_payment("contrib_employees", "8571.30", date(2026, 7, 28))
        razn.source_kind = "tax_notice"
        razn.quality_status = "confirmed"
        session.add(razn)
        await session.commit()

        result = await list_payments(session, year=2026, kind=None)

    kinds = [(item.kind, item.source_kind) for item in result.items]
    assert kinds == [("usn_advance", "bank_statement")]  # только факт
    assert result.paid_total == Decimal("674624.00")


async def test_calendar_presents_future_split_as_planned(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Разнос из оборотки с ненаступившим сроком в календаре — ПЛАН, а не «оплачено завтра»."""
    from datetime import timedelta

    from app.api.v1.routes.taxes import get_calendar

    async with async_session_factory() as session:
        future_due = date.today() + timedelta(days=20)
        past_due = date.today() - timedelta(days=20)
        for due, amount in ((future_due, "8571.30"), (past_due, "11842.20")):
            razn = _tax_payment("contrib_employees", amount, due)
            razn.source_kind = "tax_notice"
            razn.quality_status = "confirmed"
            razn.for_year = 2026
            session.add(razn)
        await session.commit()

        result = await get_calendar(session, year=2026)

    by_amount = {str(i.amount): i for i in result.items}
    assert by_amount["8571.30"].status == "planned"  # срок впереди — план
    assert by_amount["8571.30"].due_date == future_due
    assert by_amount["8571.30"].is_overdue is False
    assert by_amount["11842.20"].status == "paid"  # срок прошёл — уплачено по конвенции
    assert result.overdue_count == 0


# ── Зачёт расчётной переплаты ЕНС в вердикте сверки ─────────────────────────────
# Решение владельца 26.07.2026: платёжка меньше начисления при достаточной переплате
# на ЕНС — не недоимка, а трата переплаты. Красный запрет платить в этом случае врёт.


def _alert_line(*, documented: str, expected: str) -> ReconLine:
    return ReconLine(
        label="УСН, II квартал",
        tax_kind="usn_advance",
        period_code="h1",
        due_date=date(2026, 7, 28),
        calculated=Decimal(expected),
        documented=Decimal(documented),
        paid=None,
        verdict="doc_mismatch",
        severity="alert",
        messages=["Документ расходится с расчётом."],
        action="Платить по этому документу нельзя.",
        payable_amount=None,
    )


def test_ens_offset_softens_covered_gap() -> None:
    """Разница покрыта переплатой целиком — alert снимается, платить по документу можно."""
    from app.services.taxes.reconcile import _offset_by_ens

    line = _offset_by_ens(
        _alert_line(documented="463376", expected="478376"),
        expected=Decimal("478376"),
        wallet_balance=Decimal("20000"),
    )
    assert line.severity == "warning"
    assert line.payable_amount == Decimal("463376")
    assert any("покрыта расчётной переплатой" in m for m in line.messages)
    assert "переплаты ЕНС" in (line.action or "")
    assert "спишутся" in (line.action_why or "")


def test_ens_offset_partial_coverage_keeps_alert() -> None:
    """Переплата меньше разницы — alert остаётся, но владелец видит частичное покрытие."""
    from app.services.taxes.reconcile import _offset_by_ens

    line = _offset_by_ens(
        _alert_line(documented="463376", expected="478376"),
        expected=Decimal("478376"),
        wallet_balance=Decimal("5000"),
    )
    assert line.severity == "alert"
    assert line.payable_amount is None
    assert any("частично" in m for m in line.messages)


def test_ens_offset_never_excuses_zero_document() -> None:
    """Нулевая платёжка — сломанный документ: переплата её не оправдывает."""
    from app.services.taxes.reconcile import _offset_by_ens

    line = _offset_by_ens(
        _alert_line(documented="0", expected="478376"),
        expected=Decimal("478376"),
        wallet_balance=Decimal("1000000"),
    )
    assert line.severity == "alert"
    assert line.payable_amount is None


def test_ens_offset_leaves_other_verdicts_alone() -> None:
    """Не-alert строки зачёт не трогает (безопасная переплата и так жёлтая)."""
    from dataclasses import replace as dc_replace

    from app.services.taxes.reconcile import _offset_by_ens

    warning_line = dc_replace(
        _alert_line(documented="478433", expected="478376"), severity="warning"
    )
    line = _offset_by_ens(
        warning_line, expected=Decimal("478376"), wallet_balance=Decimal("99999")
    )
    assert line == warning_line


async def test_doc_mismatch_names_unallocated_payments_as_cause(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """«Документ меньше расчёта» называет причину: неразнесённые платежи в ФНС.

    Вопрос владельца 27.07.2026 («почему сходиться перестало?»): платёжка бухгалтера
    меньше нашего расчёта, потому что январско-февральские ЕНП не разнесены (нет
    обороток) и их взносная часть не в вычете. Сверка обязана говорить это сама.
    """
    async with async_session_factory() as session:
        await _seed_revenue(session, 6)
        # Платёжка бухгалтера меньше расчёта (вычет неполный) → doc_mismatch.
        session.add(
            _payment_order_intake(
                tax_kind="usn_advance",
                period="h1",
                amount="478376",
                due=date(2026, 7, 28),
                received=datetime(2026, 7, 22, tzinfo=UTC),
                filename="УСН 2 кв до 28.07.docx",
            )
        )
        # Неразнесённый платёж в ФНС (нет оборотки месяца) — вероятная причина.
        session.add(
            TaxPayment(
                id=uuid.uuid4(),
                bundle_id=uuid.uuid4(),
                paid_on=date(2026, 2, 25),
                kind="other",
                amount=Decimal("21783.93"),
                recipient="fns",
                for_year=2026,
                for_period="2026-01",
                status="paid",
                source_kind="bank_statement",
                quality_status="requires_review",
            )
        )
        await session.commit()

        recon = await build_reconciliation(session, as_of=date(2026, 7, 15))

    usn = next(
        line
        for line in recon.lines
        if line.tax_kind == "usn_advance"
        and line.verdict == "doc_mismatch"
        and line.documented is not None
        and line.calculated is not None
        and line.documented < line.calculated
    )
    assert any("неразнесённые платежи" in m for m in usn.messages)
    # fmt_money ставит неразрывный пробел между разрядами.
    assert any("21\u00a0783,93" in m for m in usn.messages)
