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


# --- Чеки (оплата картой ± наличными) -----------------------------------------


class CardTransactionRead(BaseModel):
    """Кандидат card-операции для дропдауна «Оплачено с карты»."""

    model_config = ConfigDict(from_attributes=True)

    bank_operation_id: uuid.UUID
    operation_date: date
    posted_at: datetime | None = None
    amount: Decimal
    counterparty_name_raw: str | None = None
    purpose: str | None = None
    tier: int | None = None
    minutes_delta: int | None = None


class ChequeLineCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    quantity: Decimal
    price: Decimal
    iiko_product_id: uuid.UUID | None = None
    vat_percent: Decimal | None = None


class ChequeBankPartCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bank_operation_id: uuid.UUID
    amount: Decimal | None = None  # None → вся операция


class ChequeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    counterparty_id: uuid.UUID
    article_id: uuid.UUID
    issued_at: datetime
    bank_parts: list[ChequeBankPartCreate] = Field(default_factory=list)
    cash_amount: Decimal | None = None
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
    article: str | None = None
    unit: str | None = None
    quantity: float
    price: float
    sum: float
    vat_percent: float | None = None


class ChequeRead(BaseModel):
    id: uuid.UUID
    number: str | None = None
    counterparty_id: uuid.UUID
    counterparty_name: str
    issued_at: str | None = None
    amount: float
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
    pay_in: float | None = None
    pay_out: float | None = None
    cash_remain: float | None = None
    cash_diff: float | None = None  # сырое поле iiko (= 2× остаток, артефакт)
    real_cash_diff: float | None = None  # сверка ящика: положит. = недостача/неучтённое изъятие
    posted: bool
    synced_at: datetime


class KassaPayoutRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id_iiko: str
    account_name: str | None = None
    category: str
    amount: float
    comment: str | None = None


class KassaShiftDetailRead(KassaShiftRead):
    payouts: list[KassaPayoutRead] = Field(default_factory=list)


class KassaShiftSyncReport(BaseModel):
    fetched: int
    created: int
    updated: int
    payouts: int
    posted: int
    skipped: int
