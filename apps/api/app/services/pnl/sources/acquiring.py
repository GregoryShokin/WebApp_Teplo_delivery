"""Комиссия за эквайринг: та её часть, которую банк удержал ДО зачисления.

ПОЧЕМУ ЭТУ СТРОКУ НЕЛЬЗЯ СОБРАТЬ ИЗ ДДС. Отдельной расходной проводкой приходит только
терминальный эквайринг — за июль 2026 это 6 668,93 ₽. Комиссия по картам онлайн и по приёму
платежей/СБП удерживается банком из выручки, и на счёт падает УЖЕ ЧИСТАЯ сумма: движения
денег по ней не существует, а величина названа текстом в назначении платежа. Всего за июль
это 48 907,38 ₽ — в семь с лишним раз больше, чем видно по статье ДДС. Отдельного поля
комиссии Сбер не отдаёт (``/v2/statement/transactions`` возвращает только ``paymentPurpose``),
публичного API по эквайринг-отчётам у СберБизнеса тоже нет — выписка это единственный
источник.

ЗАДВОЕНИЯ С КАССОЙ НЕТ ПО ПОСТРОЕНИЮ. Зачисления эквайринга размечены статьёй «Поступление
денег с торг. точек», у неё ``in_pnl = false``; расходные проводки терминалов идут по статье
«Эквайринг» и попадают в ту же строку кассовым компонентом. Здесь разбираются ТОЛЬКО
приходы, там — только расходы.

И С ВЫРУЧКОЙ ТОЖЕ НЕТ: выручка берётся из iiko по продажам, то есть БРУТТО. Удержанная
комиссия в ней сидит и обязана появиться расходом, иначе прибыль завышена на всю её величину.

ТРИ ФОРМАТА НАЗНАЧЕНИЯ, И ВТОРОЙ — САМЫЙ КОВАРНЫЙ:

1. ``Комиссия 198.27 (в т.ч. НДС 35.76). Возврат покупки 0.00/0.00.``
2. ``Комиссия 242.57 (в т.ч. НДС 43.74) и 16.20 (НДС не обл). Возврат покупки 0.00/0.00.``
   — ДВЕ части, и вторая ненулевая в реальных данных. Взяв только первую, теряем деньги молча.
3. ``Комиссия 4.90. Возврат покупки 0.00/0.00.НДС не облагается.`` — без скобок вовсе.

Плюс отдельный формат канала приёма платежей:
``За дату 30.06.2026, удержано комиссии за прием платежей  721.12 руб. НДС не облагается.``

«Возврат покупки 0.00/0.00» стоит сразу за суммой и состоит из таких же чисел — поэтому
дробная часть в регулярке ограничена двумя знаками, иначе точка конца предложения в
``Комиссия 4.90.`` утаскивала бы за собой следующее число.

МЕСЯЦ БЕРЁТСЯ ПО ДАТЕ ПРОВОДКИ. Зачисление приходит следующим днём, так что весь ряд
систематически сдвинут на сутки: комиссия за 30 июня попадает в июль, за 31 июля — в август.
Канал приёма платежей называет свою дату («За дату 30.06.2026»), карточный — нет, и свести их
к одной базе можно было бы только для одного из двух. Смешивать основания хуже, чем сдвинуть
оба одинаково: сдвиг на сутки виден и объясним, разнобой — нет.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CashflowTransaction

#: Число с необязательной дробной частью РОВНО до двух знаков. Ограничение принципиально:
#: без него точка в конце предложения читается как десятичный разделитель.
_NUMBER = r"\d[\d\s ]*(?:[.,]\d{1,2})?"

CARD_COMMISSION = re.compile(
    rf"Комисси[яи]\s+({_NUMBER})"
    rf"(?:\s*\(в\s*т\.?\s*ч\.?\s*НДС\s*{_NUMBER}\))?"
    rf"(?:\s*и\s+({_NUMBER})\s*\(НДС\s*не\s*обл[^)]*\))?",
    re.IGNORECASE,
)
PAYMENTS_COMMISSION = re.compile(
    rf"удержано\s+комиссии\s+за\s+при[её]м\s+платежей\s+({_NUMBER})\s*руб",
    re.IGNORECASE,
)

CHANNEL_CARD = "card"
CHANNEL_PAYMENTS = "payments"

CHANNEL_TITLE = {
    CHANNEL_CARD: "Карты онлайн (удержано из зачисления)",
    CHANNEL_PAYMENTS: "Приём платежей и СБП (удержано из зачисления)",
}


def _money(raw: str) -> Decimal:
    return Decimal(raw.replace(" ", "").replace(" ", "").replace(",", "."))


def parse_commission(purpose: str | None) -> tuple[str, Decimal] | None:
    """Канал и удержанная комиссия из назначения платежа. ``None`` — комиссии в тексте нет."""
    if not purpose:
        return None
    match = PAYMENTS_COMMISSION.search(purpose)
    if match:
        return CHANNEL_PAYMENTS, _money(match.group(1))
    match = CARD_COMMISSION.search(purpose)
    if match:
        total = _money(match.group(1))
        # Вторая часть — комиссия, не облагаемая НДС. В реальных выписках она бывает
        # ненулевой (16.20, 33.00), и пропустить её значит занизить строку молча.
        if match.group(2):
            total += _money(match.group(2))
        return CHANNEL_CARD, total
    return None


@dataclass(slots=True)
class AcquiringMonth:
    by_channel: dict[str, Decimal] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    total: Decimal = Decimal("0.00")
    #: Зачисления, где комиссию распознать не удалось. Инвариант — пусто: банк не менял
    #: формулировку. Непустой список означает, что формат поменялся и строка занижена.
    unparsed: int = 0


async def build_acquiring_month(
    session: AsyncSession, month_start: date, month_end: date
) -> AcquiringMonth:
    """Комиссия, удержанная из зачислений месяца."""
    rows = (
        (
            await session.execute(
                select(CashflowTransaction).where(
                    CashflowTransaction.direction == "in",
                    CashflowTransaction.operation_date >= month_start,
                    CashflowTransaction.operation_date <= month_end,
                    CashflowTransaction.payment_purpose.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )

    result = AcquiringMonth()
    for transaction in rows:
        purpose = transaction.payment_purpose or ""
        # Отбираем по маркеру канала, а не по слову «комиссия»: оно встречается в назначениях
        # платежей поставщикам, и без маркера сюда попала бы чужая комиссия.
        looks_acquiring = (
            "операциям эквайринга" in purpose.lower()
            or "комиссии за прием платежей" in purpose.lower()
            or "комиссии за приём платежей" in purpose.lower()
        )
        if not looks_acquiring:
            continue
        parsed = parse_commission(purpose)
        if parsed is None:
            result.unparsed += 1
            continue
        channel, amount = parsed
        if amount <= 0:
            continue
        result.by_channel[channel] = result.by_channel.get(channel, Decimal("0.00")) + amount
        result.counts[channel] = result.counts.get(channel, 0) + 1
        result.total += amount
    return result
