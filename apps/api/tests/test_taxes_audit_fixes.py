"""Замки на находки аудита 27.07.2026 (детерминизм, идемпотентность, полнота контура).

Каждый тест фиксирует починенный дефект: регресс при rebase на main снова подсветится.
"""

from __future__ import annotations

import calendar
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.tax import IikoRevenuePeriod, TaxPayment, TaxPayrollLedger
from app.services.taxes.engine import YEAR_CONFIGS
from app.services.taxes.enp_split import rebuild_payroll_enp_split
from app.services.taxes.obligations import is_settled, list_payable_obligations
from app.services.taxes.repository import DEFAULT_DEPARTMENT


def _payment(
    kind: str,
    amount: str,
    *,
    status: str = "paid",
    paid_on: date = date(2026, 6, 23),
    period: str | None = None,
    source: str = "bank_statement",
    quality: str = "confirmed",
    year: int = 2026,
) -> TaxPayment:
    return TaxPayment(
        id=uuid.uuid4(),
        bundle_id=uuid.uuid4(),
        paid_on=paid_on,
        kind=kind,
        amount=Decimal(amount),
        recipient="fns",
        for_year=year,
        for_period=period,
        status=status,
        source_kind=source,
        quality_status=quality,
    )


# ── enp_split не трогает банковские факты ────────────────────────────────────


async def test_rebuild_split_preserves_bank_facts(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Повторный rebuild разноса НЕ удаляет банковские факт-строки (reconstructed).

    Дефект: DELETE не фильтровал source_kind — каждый прогон уносил факты уплаты
    (временная потеря вычета, пинг-понг с bank_facts)."""
    async with async_session_factory() as session:
        session.add(
            TaxPayrollLedger(
                id=uuid.uuid4(),
                year=2026,
                month=6,
                tab_number="206",
                employee="ВОДОЛАЗОВА В.С.",
                contributions=Decimal("8571.30"),
                ndfl=Decimal("3532.00"),
            )
        )
        bank_fact = _payment(
            "contrib_employees",
            "8571.30",
            period="2026-06",
            source="bank_statement",
            quality="reconstructed",
            paid_on=date(2026, 7, 28),
        )
        session.add(bank_fact)
        await session.commit()
        fact_id = bank_fact.id

        await rebuild_payroll_enp_split(session, year=2026)
        await session.commit()

        survived = await session.get(TaxPayment, fact_id)

    assert survived is not None  # банковский факт пережил пересборку разноса


async def test_split_paid_prefers_bank_over_convention(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """«Факт» сверки не задваивается при сосуществовании банка и кассовой конвенции.

    За один месяц банковский разнос ЕНП и tax_notice-разнос оборотки описывают ОДИН
    платёж: приоритет у банка, конвенция — фолбэк, суммировать их нельзя."""
    from app.services.taxes.reconcile import _payroll_split_paid

    async with async_session_factory() as session:
        session.add(
            _payment(
                "contrib_employees",
                "8571.30",
                period="2026-06",
                source="tax_notice",
                paid_on=date(2026, 7, 28),
            )
        )
        session.add(
            _payment(
                "contrib_employees",
                "8571.30",
                period="2026-06",
                source="bank_statement",
                quality="reconstructed",
                paid_on=date(2026, 7, 28),
            )
        )
        await session.commit()

        paid = await _payroll_split_paid(session, year=2026, period="2026-06")

    assert paid == Decimal("8571.30")  # не 17 142,60


# ── is_settled: расход фактов и края допуска ─────────────────────────────────


def test_is_settled_tolerance_edges() -> None:
    planned = _payment("contrib_fixed", "14347.50", status="planned", period="q1")
    fact_ok = _payment("contrib_fixed", "14348.50")  # ровно +1.00 — гасит
    fact_far = _payment("contrib_fixed", "14348.51")  # +1.01 — уже нет
    assert is_settled(planned, [fact_ok])
    assert not is_settled(planned, [fact_far])


def test_is_settled_foreign_period_does_not_match() -> None:
    """Факт с ЧУЖИМ периодом не гасит планку даже при совпавшей сумме."""
    planned = _payment("contrib_fixed", "14347.50", status="planned", period="q1")
    fact = _payment("contrib_fixed", "14347.50", period="q2")
    assert not is_settled(planned, [fact])


def test_one_fact_settles_one_plan_only() -> None:
    """Один факт без периода гасит ОДНУ планку, а не все четыре одинаковые квартальные."""
    plans = [
        _payment("contrib_fixed", "14347.50", status="planned", period=p)
        for p in ("q1", "h1", "9m", "year")
    ]
    fact = _payment("contrib_fixed", "14347.50")
    used: set = set()
    settled = [p for p in plans if is_settled(p, [fact], used_fact_ids=used)]
    assert len(settled) == 1


# ── кросс-годовой хвост: УСН за год и 1% не исчезают 1 января ────────────────


async def test_prior_year_usn_and_extra_survive_new_year(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """В январе 2027 расчётный слой держит долг 2026: УСН за год (28.04) и 1% (01.07)."""
    async with async_session_factory() as session:
        for m in range(1, 13):
            session.add(
                IikoRevenuePeriod(
                    id=uuid.uuid4(),
                    period_start=date(2026, m, 1),
                    period_end=date(2026, m, calendar.monthrange(2026, m)[1]),
                    granularity="month",
                    department=DEFAULT_DEPARTMENT,
                    revenue_net=Decimal("4000000.00"),
                    source="iiko_olap",
                )
            )
        # Уплачены только авансы за 9 месяцев — годовая доплата и 1% остались.
        session.add(_payment("usn_advance", "674624.00", period="q1", paid_on=date(2026, 4, 20)))
        session.add(_payment("usn_advance", "478376.00", period="h1", paid_on=date(2026, 7, 28)))
        await session.commit()

        obligations = await list_payable_obligations(session, today=date(2027, 1, 15))

    by_kind = {(o.kind, o.for_period): o for o in obligations}
    usn_year = by_kind.get(("usn_advance", "year"))
    assert usn_year is not None, "годовой УСН исчез из обязательств после Нового года"
    assert usn_year.for_year == 2026
    assert usn_year.due_date == date(2027, 4, 28)
    assert usn_year.amount > 0
    extra = by_kind.get(("contrib_extra_1pct", "year"))
    assert extra is not None, "допвзнос 1% исчез из обязательств после Нового года"
    assert extra.due_date == date(2027, 7, 1)
    assert extra.amount > 0


def test_next_year_config_exists() -> None:
    """Конфиг следующего года существует — 1 января контур не гаснет молча."""
    assert 2027 in YEAR_CONFIGS
    assert YEAR_CONFIGS[2027].regime == "usn_income"


# ── налоговый слой активных платежей: прогноз не платёжный ───────────────────


async def test_aggregator_skips_projections(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Прогнозные строки официального контура не попадают в «Активные платежи»."""
    from app.models.employee import Employee
    from app.services.payments_aggregator import _tax_due_items

    async with async_session_factory() as session:
        session.add(
            Employee(
                id=uuid.uuid4(),
                full_name="Victoria Manager",
                iiko_id=f"iiko-{uuid.uuid4()}",
                is_official=True,
                official_full_name="Водолазова Виктория Сергеевна",
                official_tab_number="206",
                official_salary=Decimal("50000"),
                official_children_count=1,
                official_status="working",
                hire_date=date(2026, 5, 18),
            )
        )
        await session.commit()

        obligations = await list_payable_obligations(session, today=date(2026, 7, 15))
        items = await _tax_due_items(session)

    projections = {(o.kind, o.for_period) for o in obligations if o.is_projection}
    assert projections, "прогнозные строки должны существовать в этом сценарии"
    item_keys = {(i.extra.get("tax_kind"), i.extra.get("for_period")) for i in items}
    assert not (projections & item_keys), "прогноз просочился в окно активных платежей"
