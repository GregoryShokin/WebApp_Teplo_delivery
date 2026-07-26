"""Схемы ответов модуля «Налоги».

Страница «Налоги» отвечает владельцу (не бухгалтеру) на три вопроса подряд:
сколько должны бюджету, сходится ли наш расчёт с тем, что прислала бухгалтер и что ушло
из банка, и когда ближайший срок. Схемы ниже разложены ровно по этим вопросам, а не по
таблицам БД.

Два правила, которые здесь важнее удобства:

* деньги везде ``Decimal`` — налог считается в полных рублях, взносы в копейках, и
  ``float`` уронил бы сверку на копеечных расхождениях, ради которых модуль и делался;
* ``None`` и ноль — РАЗНЫЕ вещи в сверке. ``None`` значит «источника нет» (бухгалтер не
  присылала платёжку), а ноль — «источник есть и в нём ноль». Именно нулевая платёжка по
  УСН при расчёте 478 376 ₽ и является тем расхождением, которое страница обязана поймать,
  поэтому опциональность полей ``calculated`` / ``documented`` / ``paid`` не схлопывается.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

# ── Расчёт ───────────────────────────────────────────────────────────────────


class TaxStateRead(BaseModel):
    """Состояние расчёта УСН на дату среза — результат ``compute_tax_state``.

    Нарастающим итогом с 1 января: все суммы относятся к периоду [1 января .. ``as_of``],
    а не к кварталу в отдельности. ``amount_due`` — это остаток к уплате ПОСЛЕ вычета и
    ранее уплаченных авансов, то есть та цифра, которую владелец сверяет с платёжкой.
    """

    model_config = ConfigDict(from_attributes=True)

    year: int
    as_of: date
    period_code: str

    income_ytd: Decimal
    tax_computed: Decimal

    # Вычет А — взносы за работников, только по факту уплаты (кассовый принцип).
    employees_paid: Decimal
    # Вычет Б — фиксированные взносы ИП «за себя».
    fixed_available: Decimal
    fixed_claimed: Decimal
    # Вычет В — допвзнос 1% с дохода свыше 300 000 ₽.
    extra_accrued: Decimal
    extra_available: Decimal
    extra_claimed: Decimal
    extra_prior_available: Decimal
    extra_prior_claimed: Decimal

    deduction_total: Decimal
    deduction_limit: Decimal
    deduction_applied: Decimal
    # «Сгорело» из-за лимита 50% — потеряно навсегда, не переносится никуда.
    deduction_burned: Decimal
    # «Не заявлено» — в отличие от сгоревшего, внутри года ещё можно использовать.
    deduction_unclaimed: Decimal
    # Отложено, потому что взносы «за себя» начислены, но не уплачены.
    deduction_deferred: Decimal

    advances_paid: Decimal
    amount_due: Decimal
    overpayment: Decimal

    total_burden: Decimal
    # Доля, а не проценты: 0.0512 = 5,12%. Умножает фронт, чтобы не терять точность.
    effective_rate: Decimal

    # Отпечаток входных данных — по нему ловится дрейф базы после фиксации периода.
    input_fingerprint: str
    # Готовые русские формулировки из движка: warnings — «посмотри», blocking — «цифре нельзя
    # верить, база загружена не целиком». Фронт показывает их как есть, не переписывая.
    warnings: list[str]
    blocking: list[str]


# ── Сверка ───────────────────────────────────────────────────────────────────


class ReconLineRead(BaseModel):
    """Одно обязательство в трёх независимых измерениях + вердикт.

    Сердце модуля: расчёт (наш движок), документ (платёжка бухгалтера) и факт (выписка
    банка) приходят из РАЗНЫХ источников, поэтому их расхождение — сигнал, а не ошибка
    отображения. Любое из трёх может отсутствовать (``None``).
    """

    model_config = ConfigDict(from_attributes=True)

    label: str
    tax_kind: str
    period_code: str
    due_date: date | None = None

    calculated: Decimal | None = None
    documented: Decimal | None = None
    paid: Decimal | None = None

    # 'ok' | 'doc_mismatch' | 'payment_mismatch' | 'overdue' | 'due' | 'no_data'
    verdict: str
    # 'ok' | 'info' | 'warning' | 'alert' — цвет строки на первом экране.
    severity: str
    messages: list[str] = []
    # Конкретный следующий шаг для владельца (что делать с красным/жёлтым), если есть.
    action: str | None = None
    # Сколько платить по обязательству (для кнопки «Отправить в банк»); None — платить нечего.
    payable_amount: Decimal | None = None


class ReconciliationRead(BaseModel):
    """Сверка целиком на дату среза."""

    year: int
    as_of: date
    lines: list[ReconLineRead]
    # Сколько строк требуют реакции владельца прямо сейчас (severity='alert').
    alert_count: int
    has_alerts: bool


class TaxOverviewRead(BaseModel):
    """Первый экран страницы «Налоги»: расчёт и сверка в одном ответе.

    Отдаётся одним запросом намеренно — расчёт и сверка обязаны быть на ОДНУ дату среза.
    Два раздельных запроса могли бы разъехаться по ``as_of`` и показать владельцу
    несуществующее расхождение.
    """

    as_of: date
    year: int
    period_code: str
    state: TaxStateRead
    reconciliation: ReconciliationRead
    alert_count: int
    # Расчёт нельзя показывать как окончательный: база загружена не за весь период.
    is_blocked: bool


# ── Календарь и платежи ──────────────────────────────────────────────────────


class TaxCalendarItemRead(BaseModel):
    """Обязательство в таймлайне сроков.

    В ``tax_payment`` одна колонка даты на два смысла, и это сознательно: у плановой строки
    ``paid_on`` — это СРОК уплаты (её так заполняет продвижение документа), у уплаченной —
    фактическая дата списания. Раскладываем оба смысла явно, чтобы фронт не угадывал:
    ``due_date`` заполнен только у плановых, ``paid_on`` — сырое значение колонки.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    bundle_id: uuid.UUID
    kind: str
    amount: Decimal
    recipient: str
    for_year: int
    for_period: str | None = None
    status: str

    paid_on: date
    due_date: date | None = None
    # Срок прошёл, а деньги не ушли. Считается на «сегодня», а не на дату среза.
    is_overdue: bool = False

    source_kind: str
    quality_status: str
    document_number: str | None = None
    purpose: str | None = None
    note: str | None = None


class TaxCalendarRead(BaseModel):
    """Год обязательств, отсортированный по дате: что уже уплачено и что впереди."""

    year: int
    today: date
    items: list[TaxCalendarItemRead]
    planned_total: Decimal
    paid_total: Decimal
    overdue_total: Decimal
    overdue_count: int


class TaxPaymentRead(BaseModel):
    """Строка реестра платежей в бюджет.

    Одна строка = одно НАЗНАЧЕНИЕ внутри одного перевода, а не один перевод: ЕНП уходит
    единой суммой, но содержит и НДФЛ (в вычет не идёт), и взносы за работников (идут).
    Строки одного перевода связаны общим ``bundle_id``.

    ``quality_status='reconstructed'`` означает, что разнос выведен расчётом, а не взят из
    уведомления — банк отдаёт все бюджетные платежи под одним КБК. Это обязано быть видно
    в интерфейсе, иначе реконструкция незаметно превратится в «факт».
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    bundle_id: uuid.UUID
    paid_on: date
    kind: str
    amount: Decimal
    recipient: str
    for_year: int
    for_period: str | None = None
    status: str
    source_kind: str
    quality_status: str
    document_number: str | None = None
    purpose: str | None = None
    note: str | None = None
    cashflow_transaction_id: uuid.UUID | None = None
    bank_operation_id: uuid.UUID | None = None


class TaxPaymentListRead(BaseModel):
    """Реестр платежей за год + итоги, чтобы фронт не пересуммировал Decimal сам."""

    year: int
    items: list[TaxPaymentRead]
    total: int
    paid_total: Decimal
    planned_total: Decimal
    # Суммы уплаченного по видам платежа: ключ — kind из TAX_PAYMENT_KINDS.
    totals_by_kind: dict[str, Decimal]


# ── Документы бухгалтера ─────────────────────────────────────────────────────


class TaxDocumentRead(BaseModel):
    """Входящий документ бухгалтера из почты — staging до продвижения в обязательство.

    ``recognition`` отдаётся КАК ЕСТЬ, без нормализации: там лежит результат разбора вместе
    с причинами, по которым документ ушёл в ``needs_review``. Владелец решает по этому
    сырому содержимому, поэтому подчищать его на слое API нельзя — потеряется основание
    решения. Само вложение (``content``) наружу не отдаётся.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    mailbox: str
    from_addr: str | None = None
    subject: str | None = None
    received_at: datetime | None = None

    filename: str | None = None
    mime: str | None = None
    document_type: str
    status: str
    recognition: dict[str, Any] = {}
    error: str | None = None
    tax_payment_bundle_id: uuid.UUID | None = None
    created_at: datetime | None = None
    # Есть ли сохранённый исходный файл, который можно открыть. Байты (``content``) наружу
    # по-прежнему не отдаются в списке — это лишь флаг для кнопки «Открыть».
    has_file: bool = False


class TaxDocumentListRead(BaseModel):
    """Входящие документы + разрез по статусам для бейджа «ждут проверки»."""

    items: list[TaxDocumentRead]
    # Сколько строк подходит под фильтр ВСЕГО. Может быть больше ``len(items)``: выдача
    # ограничена ``limit``, а счётчик обязан показывать правду.
    total: int
    status_counts: dict[str, int]


class TaxPromotionResultRead(BaseModel):
    """Что случилось с одним документом при продвижении в обязательство.

    ``skipped`` — это НОРМАЛЬНЫЙ исход, а не сбой: нулевая платёжка-заглушка и смешанный
    зарплатный ЕНП намеренно не продвигаются. Причина в ``reason`` — русский текст,
    пригодный к показу владельцу без перевода.
    """

    model_config = ConfigDict(from_attributes=True)

    intake_id: uuid.UUID
    tax_payment_id: uuid.UUID | None = None
    # 'created' | 'updated' | 'skipped'
    action: str
    reason: str | None = None


class TaxPromotionSummaryRead(BaseModel):
    """Итог прогона продвижения: сводные счётчики + построчная расшифровка."""

    created: int
    updated: int
    skipped: int
    results: list[TaxPromotionResultRead]


# ── Реестр источников ────────────────────────────────────────────────────────


class TaxSourceRead(BaseModel):
    """Один вход налогового контура: откуда берётся число и насколько ему можно верить.

    Все поля со значениями по умолчанию намеренно: реестр редактируется через настройки
    (``app_setting``, ключ ``taxes.sources``), и чуть иная форма сохранённого JSON не должна
    ронять страницу — реестр справочный, а не расчётный.
    """

    key: str = ""
    title: str = ""
    system: str = ""
    # 'api' | 'file_cache' | 'derived' | 'manual' | 'code_constant'
    method: str = ""
    # 'verified' | 'reconstructed' | 'unverified' | 'missing'
    confidence: str = ""
    note: str = ""
    # Чем источник должен стать. Пусто — менять не планируем.
    target: str = ""


class TaxSourcesRead(BaseModel):
    """Реестр источников + отдельно выделенные слабые звенья.

    ``weakest_links`` — это входы с доверием ``reconstructed`` или ``missing``, то есть те,
    что выведены расчётом или вводятся руками. Они вынесены отдельно, чтобы владелец видел
    границу доверия к цифре, не читая весь реестр.
    """

    version: int = 1
    sources: list[TaxSourceRead]
    weakest_links: list[TaxSourceRead]


# ── Зарплатный критерий льготы по НДС ────────────────────────────────────────


class VatMonthAccrualRead(BaseModel):
    """Начисления действующего работника за один месяц (из оборотки)."""

    month: int
    accrued: Decimal
    oklad: Decimal | None
    full_month: bool  # начислено = оклад → месяц отработан полностью


class VatWageCriterionRead(BaseModel):
    """Зарплатный критерий льготы по НДС (подп. 38 п. 3 ст. 149 НК).

    Показатель — среднемесячное начисление ДЕЙСТВУЮЩЕГО работника; сравнивается с региональным
    порогом (Росстат/ЕМИСС), который вводится вручную по годам. ``passes = None`` — порога нет.
    """

    year: int
    active_employee: str | None
    active_tab: str | None
    months: list[VatMonthAccrualRead]
    # Средняя по ПОЛНЫМ месяцам (основной показатель) и по всем месяцам с выплатами.
    indicator_full: Decimal | None
    indicator_all: Decimal | None
    threshold: Decimal | None
    passes: bool | None
    margin: Decimal | None
    messages: list[str]


class VatThresholdInput(BaseModel):
    """Ввод регионального порога за год (Росстат/ЕМИСС отстаёт — вводит человек)."""

    year: int
    amount: Decimal


# ── Черновик платёжки в банк ─────────────────────────────────────────────────


class TaxDocumentReviewInput(BaseModel):
    """Ручная отметка документа: проверен (``parsed``) или отклонён (``ignored``)."""

    status: str


class TaxBankDraftInput(BaseModel):
    """Запрос на создание черновика платёжки ЕНП в банк."""

    amount: Decimal
    purpose: str | None = None


class TaxBankDraftResultRead(BaseModel):
    """Итог создания черновика: он готов в банк-клиенте, но деньги ещё не ушли."""

    document_id: str
    status: str
    provider_ref: str | None = None


class TaxPaymentDraftInput(BaseModel):
    """Подготовить налоговый платёж в очередь «Активные платежи» (кнопка «Отправить в банк»)."""

    tax_kind: str
    amount: Decimal
    purpose: str | None = None
    for_year: int | None = None
    for_period: str | None = None
    due_date: date | None = None
    title: str | None = None


class TaxDraftSendInput(BaseModel):
    """Отправка подготовленного платежа в банк — сумму/назначение можно поправить."""

    amount: Decimal | None = None
    purpose: str | None = None


class TaxPaymentDraftRead(BaseModel):
    """Подготовленный налоговый платёж — строка очереди «Активные платежи»."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tax_kind: str
    for_year: int | None = None
    for_period: str | None = None
    title: str | None = None
    amount: Decimal
    purpose: str
    due_date: date | None = None
    kbk: str | None = None
    recipient_name: str | None = None
    status: str
    bank_provider: str
    document_id: str | None = None
    provider_ref: str | None = None
    last_error: str | None = None
    created_at: datetime | None = None
