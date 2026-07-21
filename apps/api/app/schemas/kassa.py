from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

# Узкие read-схемы справочников модуля «Касса». Дают кассиру ровно то, что нужно для
# выбора статьи ДДС / счёта / контрагента при создании чека, не открывая полный ДДС
# (finance.cashflow/wallets/counterparties кассиру не положены — см. permissions.py).


class KassaDdsArticleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    movement_type: str
    activity_type: str


class KassaAccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    type: str


class KassaCounterpartyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    inn: str | None = None
    type: str


class KassaConfigRead(BaseModel):
    """Фиче-флаги модуля «Касса» для UI (доступны кассиру по kassa.refs.read)."""

    # Показывать ли блок ручного ввода суммы чека в выборе оплаты (страховка на случай
    # задержки банка). По умолчанию выключено — карт-операции приходят вебхуком.
    manual_pending_cheque_enabled: bool


# --- Чеки (оплата картой ± наличными) -----------------------------------------


class CardTransactionRead(BaseModel):
    """Кандидат card-операции для дропдауна «Оплачено с карты»."""

    model_config = ConfigDict(from_attributes=True)

    bank_operation_id: uuid.UUID
    operation_date: date
    posted_at: datetime | None = None
    purchased_at: datetime | None = None  # момент покупки (authorizationDate)
    amount: Decimal
    counterparty_name_raw: str | None = None
    purpose: str | None = None
    tier: int | None = None
    minutes_delta: int | None = None
    # Возврат(ы) по этой покупке, уже пришедшие в выписку (refundIn, тот же rrn).
    # UI подсвечивает: «по покупке есть возврат — отметьте позиции».
    refund_amount: Decimal | None = None
    refund_count: int = 0


class ChequeLineCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    quantity: Decimal
    unit: str | None = None  # ед. изм.: шт / кг / л / порц
    price: Decimal
    # Сумма строки, введённая на кассе. Приоритетна над quantity*price: цена на фронте
    # пересчитывается из суммы как round(сумма/кол-во), и построчное qty*price копит
    # расхождение с оплатой на копейки.
    amount: Decimal | None = None
    dds_article_id: uuid.UUID | None = None  # статья ДДС позиции (своя у каждой строки)
    iiko_product_id: uuid.UUID | None = None
    vat_percent: Decimal | None = None
    # Позиция возвращена в магазин: остаётся в чеке (gross-сверка копейка в копейку),
    # но не проводится — чек проводится net, разница = ожидаемый возврат от банка.
    is_return: bool = False


class ChequeBankPartCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bank_operation_id: uuid.UUID
    amount: Decimal | None = None  # None → вся операция


class ChequeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    counterparty_id: uuid.UUID
    # Статья на уровне чека — опциональный фолбэк для позиций без своей статьи
    # (для местного закупа статья ставится в каждой строке).
    article_id: uuid.UUID | None = None
    issued_at: datetime
    bank_parts: list[ChequeBankPartCreate] = Field(default_factory=list)
    cash_amount: Decimal | None = None
    # Ручной ввод суммы чека, когда банк ещё не передал card-операцию (выходные/задержка
    # webhook). Взаимоисключающе с ``bank_parts``. Чек «ожидает подтверждения банком».
    pending_card_amount: Decimal | None = None
    track_nomenclature: bool = False
    lines: list[ChequeLineCreate] = Field(default_factory=list)
    number: str | None = None
    store_guid: str | None = None
    comment: str | None = None


class ChequeAllocationRead(BaseModel):
    id: uuid.UUID
    source_kind: str
    bank_operation_id: uuid.UUID | None = None
    cashflow_transaction_id: uuid.UUID | None = None
    amount: float


class ChequeLineRead(BaseModel):
    id: uuid.UUID
    name: str
    article: str | None = None  # артикул товара
    unit: str | None = None
    quantity: float
    price: float
    sum: float
    vat_percent: float | None = None
    dds_article_id: uuid.UUID | None = None
    dds_article_name: str | None = None
    is_return: bool = False  # позиция возвращена (красная строка, не проведена)


class ChequeRead(BaseModel):
    id: uuid.UUID
    number: str | None = None
    counterparty_id: uuid.UUID
    counterparty_name: str
    issued_at: str | None = None
    amount: float  # проведено (net, без возвращённых позиций)
    returned_total: float = 0.0  # сумма возвращённых позиций (gross = amount + returned_total)
    payment_status: str
    article_id: uuid.UUID | None = None
    article_name: str | None = None
    allocations: list[ChequeAllocationRead] = Field(default_factory=list)
    lines: list[ChequeLineRead] = Field(default_factory=list)


# --- Закрытие смены (витрина iiko) --------------------------------------------


class KassaShiftRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    iiko_session_id: str
    session_number: int | None = None
    point_of_sale_id: str | None = None
    open_date: datetime | None = None
    close_date: datetime | None = None
    session_status: str | None = None
    session_start_cash: float | None = None
    sales_cash: float | None = None  # iiko salesCash (завышен предоплатами) — справочно
    cash_sales: float | None = None  # достоверная наличная выручка из OLAP (тип «Наличные»)
    sales_card: float | None = None
    total_sales: float | None = None  # вся выручка смены из OLAP (база порога авто-штрафа)
    pay_in: float | None = None
    pay_out: float | None = None
    cash_remain: float | None = None
    cash_diff: float | None = None  # сырое поле iiko (= 2× остаток, артефакт)
    real_cash_diff: float | None = None  # сверка ящика: положит. = недостача/неучтённое изъятие
    posted: bool
    # Итог авто-штрафа: none / applied / waived / manual_review (см. iiko_cashshift_sync).
    penalty_status: str | None = None
    synced_at: datetime


class KassaPayoutRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id_iiko: str
    account_name: str | None = None
    category: str
    amount: float
    comment: str | None = None


class KassaShiftPenaltyRead(BaseModel):
    """Один авто-штраф кассиру за недостачу смены."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    employee_full_name: str
    amount: float
    status: str  # active | waived
    waived_at: datetime | None = None


class KassaShiftDetailRead(KassaShiftRead):
    payouts: list[KassaPayoutRead] = Field(default_factory=list)
    penalty_review_reason: str | None = None
    shortage_threshold_pct: float | None = None  # порог, % выручки
    shortage_threshold_amount: float | None = None  # порог в рублях
    shortage_pct_of_revenue: float | None = None  # фактическая недостача, % выручки
    penalties: list[KassaShiftPenaltyRead] = Field(default_factory=list)


class KassaShiftSyncReport(BaseModel):
    fetched: int
    created: int
    updated: int
    payouts: int
    posted: int
    penalized: int
    skipped: int


# --- Кассовый журнал и «Выплата из кассы» (ТК Черникова) -------------------------


class KassaJournalItemRead(BaseModel):
    """Строка журнала — движение ДДС по ТК Черникова (системное или ручное)."""

    id: uuid.UUID
    operation_date: date
    direction: str  # in | out
    amount: float
    article_id: uuid.UUID | None = None
    article_name: str | None = None
    purpose: str | None = None
    comment: str | None = None
    source_kind: str
    # Сотрудник (аванс/заём) или поставщик (предоплата) — подпись строки.
    counterparty_label: str | None = None
    # Получатели для предзаполнения формы правки.
    employee_id: uuid.UUID | None = None
    counterparty_id: uuid.UUID | None = None
    # Маршрут кассовой выплаты (None — запись другого контура).
    kassa_flow: str | None = None
    # Своя сегодняшняя кассовая запись — доступны «Изменить»/«Удалить».
    editable: bool
    created_at: datetime


class KassaJournalRead(BaseModel):
    wallet_name: str
    balance: float
    period_in: float
    period_out: float
    # Шапка «в кассе N ₽ · из них целевые M ₽» + бейдж вкладки «К выдаче».
    targets_total: float
    pending_count: int
    items: list[KassaJournalItemRead] = Field(default_factory=list)


# --- Вкладка «К выдаче»: целёвки в кассе + разрешения на авансы/займы ------------


class KassaPayrollEmployeeRead(BaseModel):
    """Сотрудник зарплатной ведомости внутри кассового резерва."""

    employee_id: uuid.UUID
    employee_name: str
    accrued: float
    paid: float
    remaining: float
    payment_status: str
    payable: bool


class KassaTargetRead(BaseModel):
    """Целёвка, переданная в кассу: выдаётся наличными по статье с контрагентом."""

    id: uuid.UUID
    article_id: uuid.UUID | None = None
    article_name: str | None = None
    counterparty_id: uuid.UUID | None = None
    counterparty_name: str | None = None
    # Происхождение (например, накладные закупа) — из назначения резерва.
    purpose: str | None = None
    amount: float
    amount_paid: float
    outstanding: float
    # Авто-целёвка оплаченного банковского черновика закупа («из банковской выплаты»).
    from_bank_payout: bool
    # Пул зарплатной ведомости: раскрывается в кассе и выдаётся выбранным сотрудникам.
    is_payroll: bool
    run_id: uuid.UUID | None = None
    payroll_employees: list[KassaPayrollEmployeeRead] = Field(default_factory=list)
    created_at: datetime


class KassaAdvancePermissionRead(BaseModel):
    """Ожидающее разрешение на аванс/заём через кассу (выдаёт админ, вся сумма)."""

    id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str
    kind: str  # advance | loan
    amount: float
    comment: str | None = None
    created_by_label: str | None = None
    created_at: datetime


class KassaFreelancerRead(BaseModel):
    """Внештатник с непогашенным: одна строка (имя + Σ неоплаченных смен открытого периода)."""

    employee_id: uuid.UUID
    name: str
    unpaid_total: float
    shift_count: int


class KassaFreelancerShiftRead(BaseModel):
    """Смена внештатника открытого периода (для модалки): единица = явка."""

    attendance_entry_id: uuid.UUID
    work_date: date
    hours: float
    amount: float
    paid: bool


class KassaFreelancerShiftsRead(BaseModel):
    """Детализация смен внештатника для модалки выдачи."""

    shifts: list[KassaFreelancerShiftRead] = Field(default_factory=list)


class KassaPendingRead(BaseModel):
    """Состав вкладки «К выдаче» (он же — read-only диалог на «Деньгах сегодня»)."""

    wallet_name: str
    balance: float
    targets: list[KassaTargetRead] = Field(default_factory=list)
    permissions: list[KassaAdvancePermissionRead] = Field(default_factory=list)
    freelancers: list[KassaFreelancerRead] = Field(default_factory=list)
    targets_total: float
    pending_count: int


class KassaTargetPayoutRequest(BaseModel):
    """«Выдано»: сумма выдачи целёвки (частичная допустима, больше остатка — нет)."""

    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(gt=0)


class KassaPayrollPayoutRequest(BaseModel):
    """Распределить кассовый резерв между выбранными сотрудниками ведомости."""

    model_config = ConfigDict(extra="forbid")

    employee_ids: list[uuid.UUID] = Field(min_length=1)
    boundary_id: uuid.UUID | None = None


class KassaFreelancerShiftPayoutRequest(BaseModel):
    """«Выплатить»: мультивыбор смен (явок) внештатника — каждая выдаётся целиком."""

    model_config = ConfigDict(extra="forbid")

    attendance_entry_ids: list[uuid.UUID] = Field(min_length=1)


class KassaFreelancerSyncReport(BaseModel):
    """Итог «Синхронизировать смены»: обновлённый список внештатников с непогашенным."""

    freelancers: list[KassaFreelancerRead] = Field(default_factory=list)


class KassaPayoutArticleRead(BaseModel):
    """Статья, разрешённая для выплаты из кассы, с маршрутом формы."""

    id: uuid.UUID
    code: str
    name: str
    flow: str  # expense | employee_advance | employee_loan | supplier_prepayment


class KassaPayoutEmployeeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str
    position: str | None = None


class KassaPayoutContextRead(BaseModel):
    """Строка контекста формы: «Счёт: … · в кассе N ₽»."""

    wallet_name: str
    balance: float


class KassaPayoutCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    article_id: uuid.UUID
    amount: Decimal = Field(gt=0)
    comment: str | None = None
    employee_id: uuid.UUID | None = None  # обязателен для статей аванса/займа
    counterparty_id: uuid.UUID | None = None  # обязателен для предоплаты поставщику


class KassaPayoutResultRead(BaseModel):
    kind: str
    transaction_id: uuid.UUID | None = None
    advance_id: uuid.UUID | None = None
    prepayment_id: uuid.UUID | None = None


# --- «Внесение в кассу»: пресеты и приход по пресету -----------------------------


class KassaPayinPresetOptionRead(BaseModel):
    """Пресет внесения для формы кассира: имя + шаблон комментария (без статьи ДДС)."""

    id: uuid.UUID
    name: str
    comment_template: str | None = None


class KassaPayinCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset_id: uuid.UUID
    amount: Decimal = Field(gt=0)
    comment: str | None = None


class KassaPayinUpdate(BaseModel):
    """Правка своего сегодняшнего внесения: сумма и комментарий (статья пресета неизменна)."""

    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(gt=0)
    comment: str | None = None


class KassaPayinResultRead(BaseModel):
    transaction_id: uuid.UUID | None = None
    preset_id: uuid.UUID | None = None


class KassaPayinPresetArticleRead(BaseModel):
    """Приходная статья ДДС, доступная для привязки к пресету (каталог в Настройках)."""

    id: uuid.UUID
    code: str
    name: str
    activity_type: str


class KassaPayinPresetCounterpartyRead(BaseModel):
    """Активный контрагент для привязки к пресету (каталог в Настройках)."""

    id: uuid.UUID
    name: str


class KassaPayinPresetRead(BaseModel):
    """Строка каталога пресетов внесения (Настройки → Касса)."""

    id: uuid.UUID
    name: str
    mechanism: str
    article_id: uuid.UUID
    article_name: str
    counterparty_id: uuid.UUID | None = None
    counterparty_name: str | None = None
    comment_template: str | None = None
    is_active: bool
    sort_order: int


class KassaPayinPresetWrite(BaseModel):
    """Создание/правка пресета внесения (владелец, из Настроек)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    article_id: uuid.UUID
    counterparty_id: uuid.UUID | None = None
    comment_template: str | None = None
    is_active: bool = True
    sort_order: int = 0
