"""Платёжные обязательства модуля «Налоги» для окна «Активные платежи».

Решение владельца 26.07.2026: обязательства со статусом «К уплате» появляются в окне
активных платежей САМИ, без клика на «Налогах» — окно отвечает на вопрос «что вообще
надо платить», а не «что я уже решил платить». Отправка в банк остаётся ручной:
строка виртуальная, платёжный черновик (``TaxBankDraft``) создаётся только в момент
«В банк».

Два слоя обязательств, без задвоения между ними:

1. **Документный** — плановые ``TaxPayment`` из продвинутых платёжек бухгалтера
   (УСН, допвзнос 1%, травматизм, взносы ИП), не закрытые фактом уплаты;
2. **Расчётный** — строки сверки с ``payable_amount`` (зарплатный ЕНП, у которого
   платёжки не продвигаются; УСН/1% до прихода платёжки).

Здесь же живёт правило «оплаченное обязательство закрыто»: плановая строка гасится
фактом уплаты того же вида и периода (факт без периода — по совпадению суммы).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tax import TaxPayment
from app.services.taxes.reconcile import build_reconciliation

TOLERANCE = Decimal("1")

KIND_TITLES: dict[str, str] = {
    "usn_advance": "УСН, авансовый платёж",
    "usn_year": "УСН за год",
    "contrib_extra_1pct": "Допвзнос 1%",
    "contrib_fixed": "Взносы ИП «за себя»",
    "contrib_injury": "Взносы на травматизм (0,2%)",
    "contrib_employees": "Взносы за работников",
    "ndfl": "НДФЛ за работников",
    "enp_payroll": "Зарплатный ЕНП (НДФЛ + взносы)",
}

# Владелец думает кварталами: «полугодие» — язык деклараций, платёж — доплата за квартал.
PERIOD_TITLES: dict[str, str] = {
    "q1": "I квартал",
    "h1": "II квартал",
    "9m": "III квартал",
    "year": "год",
}


def _same_injury_window(paid_on: date | None, due: date | None) -> bool:
    """Платёж травматизма относится к той же месячной платёжке, что и плановая строка.

    В плановой строке ``paid_on`` — это СРОК: 15 число месяца, следующего за месяцем
    начисления (125-ФЗ, ст. 22). Платят обычно раньше, внутри самого месяца начисления,
    поэтому окно отсчитываем ОТ СРОКА назад: [1 число месяца начисления .. сам срок].
    Без этого январский платёж 26.01 не закрывал платёжку со сроком 15.02 — обязательство
    висело неоплаченным (найдено на проде 27.07.2026).
    """
    if paid_on is None or due is None:
        return False
    accrual_month = due.month - 1 or 12
    accrual_year = due.year - (1 if due.month == 1 else 0)
    window_start = date(accrual_year, accrual_month, 1)
    return window_start <= paid_on <= due


def is_settled(
    planned: TaxPayment,
    paid_rows: list[TaxPayment],
    *,
    used_fact_ids: set | None = None,
) -> bool:
    """Обязательство закрыто фактом уплаты того же вида и периода.

    Факт без периода (реконструкция из выписки не знает, за какой период платёж) —
    матчится по совпадению суммы. Иначе оплаченное обязательство висит «просрочкой».

    ``used_fact_ids`` — «расход» фактов: один факт гасит ОДНУ плановую строку. Без него
    единственный факт без периода закрывал бы все четыре одинаковые квартальные планки
    фикс-взносов разом (находка аудита 27.07.2026). Передавайте один set на весь проход.
    """
    for fact in paid_rows:
        if used_fact_ids is not None and fact.id in used_fact_ids:
            continue
        if fact.kind != planned.kind:
            continue
        if planned.kind == "contrib_injury":
            # Травматизм платится КАЖДЫЙ месяц, и период у строк разнобойный: у платёжек
            # он месячный ('2026-07'), у банковских фактов чаще пуст или 'year'. Сравнивать
            # периоды тут бессмысленно — соответствие задаёт ОКНО уплаты; вне окна платёж
            # чужой (январский «гасил» июльскую платёжку — находка владельца 27.07.2026).
            if _same_injury_window(fact.paid_on, planned.paid_on):
                if used_fact_ids is not None:
                    used_fact_ids.add(fact.id)
                return True
            continue
        if fact.for_period is not None:
            if fact.for_period == planned.for_period:
                if used_fact_ids is not None:
                    used_fact_ids.add(fact.id)
                return True
            continue
        if abs(fact.amount - planned.amount) <= TOLERANCE:
            if used_fact_ids is not None:
                used_fact_ids.add(fact.id)
            return True
    return False


@dataclass(frozen=True)
class PayableObligation:
    """Одно «надо платить» для окна активных платежей.

    Реквизиты получателя выбираются по виду (``requisites_for_kind``): всё идёт ЕНПом в ФНС,
    травматизм — отдельной платёжкой в СФР со своим КБК.

    ``is_projection`` — строка из прогнозного слоя официального контура (стандартный месяц
    по карточкам сотрудников, документов ещё нет): в долге УДКЗ она видна, но платить по
    ней нельзя — платим по платёжкам бухгалтера, поэтому в окно «Активные платежи» и под
    кнопку «В банк» прогноз не попадает.
    """

    kind: str
    for_year: int | None
    for_period: str | None
    title: str
    amount: Decimal
    due_date: date | None
    is_projection: bool = False


def _title(kind: str, period: str | None) -> str:
    base = KIND_TITLES.get(kind, kind)
    if not period:
        return base
    return f"{base} · {PERIOD_TITLES.get(period, period)}"


async def list_payable_obligations(
    session: AsyncSession, *, today: date
) -> list[PayableObligation]:
    """Собрать все «к уплате»: документный слой + расчётный, без задвоения."""
    rows = (
        await session.scalars(
            select(TaxPayment).where(TaxPayment.status.in_(("planned", "paid")))
        )
    ).all()
    planned = [r for r in rows if r.status == "planned"]
    paid = [r for r in rows if r.status == "paid"]

    result: list[PayableObligation] = []
    seen: set[tuple[str, str | None]] = set()
    used_facts: set = set()  # один факт гасит одну плановую строку

    # 1) Документный слой — платёжки бухгалтера, продвинутые в обязательства.
    for row in planned:
        if is_settled(row, paid, used_fact_ids=used_facts):
            continue
        result.append(
            PayableObligation(
                kind=row.kind,
                for_year=row.for_year,
                for_period=row.for_period,
                title=_title(row.kind, row.for_period),
                amount=row.amount,
                due_date=row.paid_on,  # у плановой строки в paid_on лежит СРОК уплаты
            )
        )
        seen.add((row.kind, row.for_period))

    # 2) Расчётный слой — строки сверки, ждущие уплаты. Дедуп с документным слоем: по виду
    # и периоду; допвзнос 1% — по одному виду (сверка кодирует его периодом 'year', платёжка
    # приходит на прирост квартала — сравнивать периоды напрямую нельзя).
    recon = await build_reconciliation(session, as_of=today)
    has_extra_planned = any(k == "contrib_extra_1pct" for k, _ in seen)
    for line in recon.lines:
        if line.payable_amount is None or line.payable_amount <= 0:
            continue
        if line.tax_kind == "contrib_extra_1pct":
            if has_extra_planned:
                continue
        elif (line.tax_kind, line.period_code) in seen:
            continue
        result.append(
            PayableObligation(
                kind=line.tax_kind,
                for_year=today.year,
                for_period=line.period_code,
                title=line.label,
                amount=Decimal(line.payable_amount),
                due_date=line.due_date,
            )
        )
        seen.add((line.tax_kind, line.period_code))

    # 3) Прогнозный слой официального контура (решение владельца 26.07.2026): сотрудник
    # отработал месяц — долг появился, не дожидаясь оборотки. Строится строго как
    # ДОПОЛНЕНИЕ: только полные месяцы БЕЗ оборотки (по остальным работают сверка и
    # документный слой). Плюс годовой остаток фиксированных взносов ИП — сумма года
    # известна заранее (ст. 430 НК). Прогнозные строки не платёжные (is_projection).
    result.extend(await _projection_layer(session, today=today, seen=seen))

    # 4) Кросс-годовой хвост (находка аудита 27.07.2026): сверка живёт годом среза, и
    # с 1 января годовой УСН прошлого года (крупнейший платёж, срок 28.04) и допвзнос 1%
    # (срок 01.07) выпадали из расчётного слоя, пока бухгалтер не пришлёт платёжку.
    # Считаем остатки прошлого года и держим их в долге, пока не погашены фактами.
    result.extend(await _prior_year_tail(session, today=today, seen=seen))

    return result


async def _projection_layer(
    session: AsyncSession, *, today: date, seen: set[tuple[str, str | None]]
) -> list[PayableObligation]:
    from app.services.taxes.official_payroll import (
        fixed_contribution_remainder,
        injury_due,
        months_without_turnover,
        official_month_accrual,
        paid_enp_month_covered,
        payroll_enp_projection_due,
    )
    from app.services.taxes.reconcile import _MONTHS_RU_GENITIVE
    from app.services.taxes.repository import year_config

    try:
        cfg = year_config(today.year)
    except Exception:  # noqa: BLE001 - нет конфига года — прогнозный слой просто молчит
        return []
    if cfg.regime != "usn_income":
        return []

    items: list[PayableObligation] = []
    year = today.year

    for month in await months_without_turnover(session, year=year, today=today):
        accrual = await official_month_accrual(session, year=year, month=month, cfg=cfg)
        if accrual is None:
            continue
        period = f"{year}-{month:02d}"
        month_name = _MONTHS_RU_GENITIVE[month - 1]
        if (
            accrual.enp_total > 0
            and ("enp_payroll", period) not in seen
            and not await paid_enp_month_covered(session, year=year, month=month)
        ):
            items.append(
                PayableObligation(
                    kind="enp_payroll",
                    for_year=year,
                    for_period=period,
                    title=f"Зарплатный ЕНП за {month_name} · прогноз",
                    amount=accrual.enp_total,
                    due_date=payroll_enp_projection_due(year, month),
                    is_projection=True,
                )
            )
            seen.add(("enp_payroll", period))
        if (
            accrual.injury > 0
            and ("contrib_injury", period) not in seen
            and not await _injury_month_covered(session, year=year, month=month)
        ):
            items.append(
                PayableObligation(
                    kind="contrib_injury",
                    for_year=year,
                    for_period=period,
                    title=f"Взносы на травматизм (0,2%) за {month_name} · прогноз",
                    amount=accrual.injury,
                    due_date=injury_due(year, month),
                    is_projection=True,
                )
            )
            seen.add(("contrib_injury", period))

    remainder = await fixed_contribution_remainder(session, year=year, cfg=cfg)
    if remainder > 0 and ("contrib_fixed", "year") not in seen:
        from app.services.taxes.engine import fixed_contribution_due

        items.append(
            PayableObligation(
                kind="contrib_fixed",
                for_year=year,
                for_period="year",
                title="Взносы ИП «за себя» · остаток года",
                amount=remainder,
                due_date=fixed_contribution_due(year),
                is_projection=True,
            )
        )
        seen.add(("contrib_fixed", "year"))
    return items


async def _injury_month_covered(session: AsyncSession, *, year: int, month: int) -> bool:
    from app.services.taxes.official_payroll import _paid_injury_month_covered

    return await _paid_injury_month_covered(session, year=year, month=month)


async def _prior_year_tail(
    session: AsyncSession,
    *,
    today: date,
    seen: set[tuple[str, str | None]],
) -> list[PayableObligation]:
    """Незакрытые обязательства ПРОШЛОГО года: УСН за год (28.04) и допвзнос 1% (01.07).

    Остаток = начислено за прошлый год − уплачено фактами − незакрытые плановые платёжки
    того же вида за прошлый год (они уже показаны документным слоем — не задваиваем).
    Обязательство самогасящееся: пришёл факт уплаты → остаток стал нулём → строка ушла.
    """
    from app.services.taxes.engine import TaxComputationError, compute_tax_state
    from app.services.taxes.ens_wallet import usn_due_date
    from app.services.taxes.repository import load_tax_inputs, year_config

    prior = today.year - 1
    try:
        cfg = year_config(prior)
    except Exception:  # noqa: BLE001 - прошлый год не настроен — хвоста нет
        return []
    if cfg.regime != "usn_income":
        return []
    try:
        inputs = await load_tax_inputs(session, as_of=date(prior, 12, 31))
        state = compute_tax_state(inputs)
    except TaxComputationError:
        return []

    paid_rows = (
        await session.scalars(
            select(TaxPayment).where(
                TaxPayment.for_year == prior,
                TaxPayment.status.in_(("planned", "paid")),
                TaxPayment.kind.in_(("usn_advance", "contrib_extra_1pct")),
            )
        )
    ).all()

    # Гашение плановых прошлого года — фактами прошлого года.
    prior_paid = [r for r in paid_rows if r.status == "paid"]

    def _open_planned(kind: str) -> Decimal:
        return sum(
            (
                r.amount
                for r in paid_rows
                if r.kind == kind and r.status == "planned" and not is_settled(r, prior_paid)
            ),
            Decimal("0"),
        )

    items: list[PayableObligation] = []

    usn_paid = sum(
        (r.amount for r in prior_paid if r.kind == "usn_advance"), Decimal("0")
    )
    usn_left = (
        state.tax_computed - state.deduction_applied - usn_paid - _open_planned("usn_advance")
    )
    if usn_left > 0 and ("usn_advance", "year") not in seen:
        items.append(
            PayableObligation(
                kind="usn_advance",
                for_year=prior,
                for_period="year",
                title=f"УСН за {prior} год · доплата",
                amount=usn_left,
                due_date=usn_due_date(prior, "year"),
            )
        )
        seen.add(("usn_advance", "year"))

    extra_paid = sum(
        (r.amount for r in prior_paid if r.kind == "contrib_extra_1pct"), Decimal("0")
    )
    extra_left = state.extra_accrued - extra_paid - _open_planned("contrib_extra_1pct")
    has_extra_planned = any(k == "contrib_extra_1pct" for k, _ in seen)
    if extra_left > 0 and not has_extra_planned:
        items.append(
            PayableObligation(
                kind="contrib_extra_1pct",
                for_year=prior,
                for_period="year",
                title=f"Допвзнос 1% за {prior} год · остаток",
                amount=extra_left,
                due_date=date(today.year, 7, 1),
            )
        )
        seen.add(("contrib_extra_1pct", "year"))
    return items
