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


def is_settled(planned: TaxPayment, paid_rows: list[TaxPayment]) -> bool:
    """Обязательство закрыто фактом уплаты того же вида и периода.

    Факт без периода (реконструкция из выписки не знает, за какой период платёж) —
    матчится по совпадению суммы. Иначе оплаченное обязательство висит «просрочкой».
    """
    for fact in paid_rows:
        if fact.kind != planned.kind:
            continue
        if fact.for_period is not None:
            if fact.for_period == planned.for_period:
                return True
            continue
        if abs(fact.amount - planned.amount) <= TOLERANCE:
            return True
    return False


@dataclass(frozen=True)
class PayableObligation:
    """Одно «надо платить» для окна активных платежей."""

    kind: str
    for_year: int | None
    for_period: str | None
    title: str
    amount: Decimal
    due_date: date | None
    # Травматизм идёт в СФР по своим реквизитам — банковский контур ЕНП его не отправит.
    sendable_via_enp: bool


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

    # 1) Документный слой — платёжки бухгалтера, продвинутые в обязательства.
    for row in planned:
        if is_settled(row, paid):
            continue
        result.append(
            PayableObligation(
                kind=row.kind,
                for_year=row.for_year,
                for_period=row.for_period,
                title=_title(row.kind, row.for_period),
                amount=row.amount,
                due_date=row.paid_on,  # у плановой строки в paid_on лежит СРОК уплаты
                sendable_via_enp=row.kind != "contrib_injury",
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
                sendable_via_enp=line.tax_kind != "contrib_injury",
            )
        )
        seen.add((line.tax_kind, line.period_code))

    return result
