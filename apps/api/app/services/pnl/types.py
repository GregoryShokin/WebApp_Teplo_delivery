"""Типы проектора ОПиУ: значение строки, её компоненты и вердикт проводки.

ГЛАВНОЕ РЕШЕНИЕ ЗДЕСЬ — ``amount`` может быть ``None``, и это не то же самое, что ноль.
Ноль означает «источник ответил, движения не было». ``None`` означает «данных нет»: синк iiko
не заливал месяц, контрагент не прислал документ, источник не настроен. В управленческом
отчёте подмена одного другим — ложь, из-за которой владелец увидит убыток там, где просто
не приехала выручка.

Различие живёт в ТИПАХ, а не в рендере: если бы проектор отдавал ``Decimal("0")`` и статус
отдельным полем, любой потребитель — фронт, экспорт, будущий баланс — рано или поздно сложил
бы нули и напечатал итог. С ``None`` сложение падает или требует явного решения, что делать
с неизвестностью, и это правильно.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any


class LineStatus(StrEnum):
    """Статус строки отчёта. Порядок важен: чем ниже, тем «хуже» новость для владельца."""

    OK = "ok"
    # Источник ответил, движения за месяц не было. Единственный статус, при котором
    # печатается настоящий ноль.
    ZERO_CONFIRMED = "zero_confirmed"
    # Цифра введена руками (Mango, бюджет рекламы, проценты по кредитам).
    MANUAL = "manual"
    # Деньги ушли, документа за период ещё нет. НОРМАЛЬНОЕ состояние: контрагент присылает
    # УПД после окончания месяца. Не ошибка и не повод рисовать ноль.
    WAITING_DOCUMENT = "waiting_document"
    # Документ просрочен относительно ожидаемой даты закрытия.
    OVERDUE_DOCUMENT = "overdue_document"
    # Источник настроен, но данных за месяц нет (синк не заливал, выгрузка не пришла).
    NO_DATA = "no_data"
    # Для строки не заведено ни одного правила источника.
    NOT_CONFIGURED = "not_configured"
    # Есть подозрение на ошибку данных: два начисления по одному документу, отрицательная
    # величина там, где её быть не должно.
    NEEDS_REVIEW = "needs_review"
    # Строка не используется (закрытая точка «Гагарина», печатные материалы).
    NOT_USED = "not_used"
    # Месяц раньше начала управленческого учёта.
    BEFORE_ACCOUNTING_START = "before_accounting_start"
    # Подытог, у которого хотя бы одно слагаемое неизвестно. Сумма известных показывается
    # со знаком «≈», но выдавать её за итог нельзя.
    INCOMPLETE = "incomplete"


#: Статусы, при которых у строки есть осмысленная сумма и её можно складывать.
KNOWN_STATUSES = frozenset({LineStatus.OK, LineStatus.ZERO_CONFIRMED, LineStatus.MANUAL})


class Verdict(StrEnum):
    """Что случилось с денежной проводкой. Ровно один исход на проводку — это и замыкает
    уравнение сходимости: сумма по всем вердиктам обязана дать отток месяца."""

    INCLUDED = "included"
    # Платёж без контрагента с явным месяцем расхода вне текущего: деньги учтены здесь,
    # расход — в месяце из ``expense_month``. Вердикт держит сверку денег замкнутой.
    INCLUDED_OTHER_MONTH = "included_other_month"
    EXCLUDED_QUALITY = "excluded_quality"
    EXCLUDED_TRANSFER = "excluded_transfer"
    # Статья размечена «вне ОПиУ»: перевод, финансовая операция либо факт принадлежит
    # другому слою (товар — фудкосту, зарплата — ведомости).
    EXCLUDED_OUT_OF_PNL = "excluded_out_of_pnl"
    # Проводка рождена механизмом, чей факт уже посчитан: выплата ведомости, депозит, аванс.
    EXCLUDED_OWNED_BY_LAYER = "excluded_owned_by_layer"
    # Оплата закрывает признанный расход — сам расход уже взят начислением.
    EXCLUDED_ACCRUAL_SETTLEMENT = "excluded_accrual_settlement"
    # Контрагент ведётся признанием: его касса не расход, расход придёт документом.
    EXCLUDED_ACCRUAL_COUNTERPARTY = "excluded_accrual_counterparty"
    # Ни статьи, ни правила. Обязан быть нулём — это и есть сигнал о потере.
    UNMAPPED = "unmapped"


@dataclass(slots=True)
class Component:
    """Слагаемое строки: один источник, одна политика знака.

    Строки эталона бывают составными, и это не наша выдумка: «Результаты ревизии» —
    инвентаризации минус штрафы; «Содержание торговых точек» — касса плюс накладные;
    «Телекоммуникации» — ручной Mango плюс два фиксированных провайдера.
    """

    stream: str
    component: str
    amount: Decimal | None
    status: LineStatus
    # Сколько денег по этой паре исключено и почему — чтобы владелец в drill-down увидел,
    # что сумма не потеряна, а заменена начислением.
    excluded_amount: Decimal = Decimal("0.00")
    excluded_reason: str | None = None
    # Оплачено, документа за ПЕРИОД ещё нет. Считается по ДЗ/КЗ, а не по кассе месяца:
    # услуги платят вперёд, и июльский платёж чаще относится к августу. В расход не идёт —
    # иначе, когда документ приедет, месяц получит расход дважды.
    unrecognized_paid: Decimal = Decimal("0.00")
    # В строке сошлись наличная касса без контрагента и признание документом по одной
    # статье. Обычно это разные расходы, но проверить может только человек — пометка.
    cash_alongside_accrual: Decimal = Decimal("0.00")
    # Месяц взят по дате денег, потому что документа не будет вовсе.
    cash_proxy_amount: Decimal = Decimal("0.00")
    note: str | None = None


@dataclass(slots=True)
class LineValue:
    """Итог строки за месяц."""

    code: str
    title: str
    block: str
    kind: str
    level: int
    sort_order: int
    sign_role: int
    month_basis: str
    amount: Decimal | None
    status: LineStatus
    components: list[Component] = field(default_factory=list)
    # Для подытога: коды слагаемых, значение которых неизвестно. Именно они превращают
    # EBITDA в «неполно», а не в уверенный минус.
    missing_lines: list[str] = field(default_factory=list)
    pct_of_revenue: Decimal | None = None
    norm_min: Decimal | None = None
    norm_max: Decimal | None = None
    source_note: str | None = None
    drill_available: bool = False


@dataclass(slots=True)
class Reconciliation:
    """Уравнение замкнутости денежного слоя за месяц.

    Задвоение шумит — расходятся итоги. Потеря молчит, поэтому её ловят здесь: всё движение
    месяца обязано без остатка разложиться по вердиктам, а ``unmapped`` — быть нулём.

    ВТОРАЯ СТОРОНА РАВЕНСТВА — НЕЗАВИСИМАЯ, и это главное. Пока обе части считались одним
    циклом по одной выборке, дрейф был нулём алгебраически: сверка не могла провалиться даже
    при потерянной проводке. Сейчас контроль приходит агрегатом из базы, а ``missed_count``
    ловит то, чего сумма не увидит, — проводку, которая до разбора не дошла вовсе.
    """

    cash_out_total: Decimal = Decimal("0.00")
    cash_in_total: Decimal = Decimal("0.00")
    by_verdict: dict[str, Decimal] = field(default_factory=dict)
    unmapped: Decimal = Decimal("0.00")
    unmapped_count: int = 0
    balanced: bool = True
    # Расхождение контрольного агрегата базы с разложением по вердиктам. Ненулевое означает
    # ошибку в самом проекторе: деньги в месяце есть, а вердикта у них нет.
    drift: Decimal = Decimal("0.00")
    # Сколько проводок месяца не дошло до разбора. Отдельно от суммы: потеря нулевой проводки
    # рублями не видна, а дыру в выборке показывает.
    missed_count: int = 0


@dataclass(slots=True)
class Warning:
    """Сообщение владельцу о том, что требует его действия, а не действия разработчика."""

    code: str
    message: str
    line_code: str | None = None
    amount: Decimal | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PnlReport:
    month: date
    lines: list[LineValue] = field(default_factory=list)
    reconciliation: Reconciliation = field(default_factory=Reconciliation)
    warnings: list[Warning] = field(default_factory=list)
    quality: dict[str, Any] = field(default_factory=dict)
