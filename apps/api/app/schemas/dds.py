from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BankOperationRead(BaseModel):
    id: uuid.UUID
    provider: str
    provider_operation_id: str
    account_id: uuid.UUID | None = None
    operation_date: date
    posted_at: datetime | None = None
    direction: str
    amount: str
    currency: str
    counterparty_name_raw: str | None = None
    counterparty_inn_raw: str | None = None
    counterparty_account_raw: str | None = None
    payment_purpose: str | None = None
    document_number: str | None = None
    classification_status: str
    cashflow_transaction_id: uuid.UUID | None = None
    transfer_group_id: uuid.UUID | None = None
    raw_payload: dict[str, Any] | None = None
    # Карт-операция (получатель — эквайер): фронт показывает мягкое предупреждение при ручной
    # привязке к накладной. Совпадает с флагом is_card строки журнала.
    is_card: bool = False


class BankOperationListRead(BaseModel):
    items: list[BankOperationRead]
    total: int


class CashflowTransactionRead(BaseModel):
    id: uuid.UUID
    wallet_id: uuid.UUID
    direction: str
    amount: str
    operation_date: date
    article_id: uuid.UUID | None = None
    counterparty_id: uuid.UUID | None = None
    transfer_group_id: uuid.UUID | None = None
    source_kind: str
    source_id: uuid.UUID | None = None
    payment_purpose: str | None = None
    comment: str | None = None
    quality_status: str


class CashflowTransactionListRead(BaseModel):
    items: list[CashflowTransactionRead]
    total: int


class DdsWalletRead(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    type: str
    currency: str
    is_internal_transfer_eligible: bool
    status: str
    account_id: uuid.UUID | None = None
    bank_code: str | None = None
    opening_balance: str
    opening_balance_date: date | None = None
    balance: str
    # Раскладка подотчётного Сейфа и Торговой кассы (целёвки); null для прочих кошельков.
    reserved_total: str | None = None
    free_total: str | None = None
    # Число активных резервов/целёвок — бейдж «N целевых» на плитке; null для прочих.
    active_allocations: int | None = None
    # Только Торговая касса: позиции «К выдаче» (целёвки в кассе + ожидающие
    # разрешения на авансы/займы) — подпись «к выдаче N» на «Деньгах сегодня».
    pending_payout_count: int | None = None


class DdsAliasCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str
    source: str | None = None


class DdsAliasRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    alias: str
    source: str | None = None


class DdsArticleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    movement_type: str
    activity_type: str
    parent_id: uuid.UUID | None = None
    is_active: bool
    kassa_enabled: bool = False
    description: str | None = None
    aliases: list[DdsAliasRead] = Field(default_factory=list)


class DdsArticleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    name: str
    movement_type: Literal["inflow", "outflow", "internal"]
    activity_type: str
    parent_id: uuid.UUID | None = None
    is_active: bool = True
    kassa_enabled: bool = False
    description: str | None = None


class DdsArticlePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str | None = None
    name: str | None = None
    movement_type: Literal["inflow", "outflow", "internal"] | None = None
    activity_type: str | None = None
    parent_id: uuid.UUID | None = None
    is_active: bool | None = None
    kassa_enabled: bool | None = None
    description: str | None = None


class DdsCounterpartyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    inn: str | None = None
    type: str
    status: str
    aliases: list[DdsAliasRead] = Field(default_factory=list)


class DdsCounterpartyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    inn: str | None = None
    type: Literal["legal_entity", "individual", "bank", "tax_authority"] = "legal_entity"
    status: str = "active"


class DdsCounterpartyPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    inn: str | None = None
    type: Literal["legal_entity", "individual", "bank", "tax_authority"] | None = None
    status: str | None = None


class ClassificationRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    priority: int
    is_active: bool
    provider: str | None = None
    direction: str | None = None
    counterparty_inn_match: str | None = None
    counterparty_name_pattern: str | None = None
    purpose_pattern: str | None = None
    amount_min: str | None = None
    amount_max: str | None = None
    action: str
    article_id: uuid.UUID | None = None
    counterparty_id: uuid.UUID | None = None
    comment: str | None = None


class ClassificationRuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    priority: int = 100
    is_active: bool = True
    provider: Literal["sber", "tbank"] | None = None
    direction: Literal["in", "out"] | None = None
    counterparty_inn_match: str | None = None
    counterparty_name_pattern: str | None = None
    purpose_pattern: str | None = None
    amount_min: str | None = None
    amount_max: str | None = None
    action: Literal["set_article", "mark_internal_transfer", "exclude"]
    article_id: uuid.UUID | None = None
    counterparty_id: uuid.UUID | None = None
    comment: str | None = None


class ClassificationRulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    priority: int | None = None
    is_active: bool | None = None
    provider: Literal["sber", "tbank"] | None = None
    direction: Literal["in", "out"] | None = None
    counterparty_inn_match: str | None = None
    counterparty_name_pattern: str | None = None
    purpose_pattern: str | None = None
    amount_min: str | None = None
    amount_max: str | None = None
    action: Literal["set_article", "mark_internal_transfer", "exclude"] | None = None
    article_id: uuid.UUID | None = None
    counterparty_id: uuid.UUID | None = None
    comment: str | None = None


class CredentialRead(BaseModel):
    id: uuid.UUID
    provider: str
    credential_kind: str
    is_active: bool
    expires_at: datetime | None
    metadata: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class CredentialCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["sber", "tbank"]
    credential_kind: Literal[
        "access_token",
        "client_secret",
        "bearer_token",
        "mtls_cert_path",
        "mtls_key_path",
    ]
    value: str
    expires_at: datetime | None = None
    metadata: dict[str, Any] | None = None


class BankSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date_from: date = Field(alias="date_from")
    date_to: date = Field(alias="date_to")


class BankSyncQueuedRead(BaseModel):
    job_id: uuid.UUID
    status: str
    queued_at: datetime


class OwnerReviewCaseRead(BaseModel):
    id: uuid.UUID
    kind: str
    status: str
    provider: str | None = None
    bank_operation_id: uuid.UUID | None = None
    payload: dict[str, Any]
    created_at: datetime
    operation: BankOperationRead | None = None


class OwnerReviewListRead(BaseModel):
    items: list[OwnerReviewCaseRead]
    total: int


class OwnerReviewClassifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    article_id: uuid.UUID | None = None
    counterparty_id: uuid.UUID | None = None
    action: Literal["set_article", "mark_internal_transfer", "exclude"]
    remember_as_rule: bool = False


class OwnerReviewActionRead(BaseModel):
    case_id: uuid.UUID
    status: str
    bank_operation_id: uuid.UUID | None = None
    classification_status: str | None = None
    rule_id: uuid.UUID | None = None
    # «Повторить отправку»: текст ошибки iiko, если синхронный add_payment не прошёл (кейс остался).
    iiko_payment_push_error: str | None = None


class OperationSplitItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    article_id: uuid.UUID
    amount: Decimal = Field(gt=0)
    comment: str | None = None
    # Для статьи «Оплата поставщикам»: конкретная неоплаченная накладная, которую гасит эта
    # сумма (привязка операции → InvoicePaymentAllocation). Несколько строк → несколько накладных.
    invoice_id: uuid.UUID | None = None
    # Для зарплатных статей (EMPLOYEE_PAYOUT_ARTICLE_CODES): сотрудник-получатель этой выплаты.
    # Заводит EmployeePayout(«выплачено») → расчёт ЗП вычтет сумму из «к выдаче». На не-зарплатной
    # статье запрещён (валидируется в apply_operation_split).
    employee_id: uuid.UUID | None = None


class OperationClassifyRequest(BaseModel):
    """Multi-article classification of a single bank operation.

    ``action="split"`` spreads the operation across one or more articles (each with
    its own amount; amounts must sum to the operation amount). The other actions
    mirror the legacy owner-review flow.
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "split", "mark_internal_transfer", "exclude", "mark_safe_topup", "employee_advance"
    ] = "split"
    splits: list[OperationSplitItem] = Field(default_factory=list)
    counterparty_id: uuid.UUID | None = None
    # Параметры для action='employee_advance' (разбор операции как аванс/заём сотруднику).
    advance_kind: Literal["advance", "loan"] | None = None
    advance_installment_amount: Decimal | None = None
    advance_recovery_start_date: date | None = None
    advance_override_ceiling: bool = False
    # Создать нового контрагента из распознанных данных операции (если его нет в реестре): имя
    # и ИНН берутся из выписки. Имеет приоритет над counterparty_id (при заданном имени).
    new_counterparty_name: str | None = None
    new_counterparty_inn: str | None = None
    remember_as_rule: bool = False
    # Оператор явно подтвердил ручную привязку карт-оплаты к накладной (получатель в банке —
    # эквайер, не поставщик). Пропускает карт-шум в guard привязки. Требует права оплаты
    # накладной (invoices.{normal|barter}.pay) и запрещает «запомнить как правило».
    allow_card: bool = False


class TransactionClassifyRequest(BaseModel):
    """Ручная разметка проводки ДДС (статья/контрагент) — для ручных проводок без bank-операции."""

    model_config = ConfigDict(extra="forbid")

    article_id: uuid.UUID | None = None
    counterparty_id: uuid.UUID | None = None


class CashflowSplitItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    article_id: uuid.UUID
    amount: Decimal = Field(gt=0)
    comment: str | None = None
    # Для статьи «перевод между счетами»: счёт-получатель — заводит встречную ногу перевода
    # (наличному получателю) + TransferGroup. Допустим только у строки с транзитной статьёй.
    transfer_wallet_id: uuid.UUID | None = None
    # Для зарплатной статьи: сотрудник-получатель (заводит EmployeePayout «выплачено» → учёт
    # в ведомости). На не-зарплатной статье запрещён (валидируется в apply_cashflow_split).
    employee_id: uuid.UUID | None = None


class CashflowClassifyRequest(BaseModel):
    """Полный разбор РУЧНОЙ проводки ДДС (без bank-операции), сохраняющий баланс кошелька.

    ``action="split"`` разносит проводку по нескольким статьям (Σ сумм = сумма проводки); строка
    с транзитной статьёй и ``transfer_wallet_id`` дополнительно заводит перевод между счетами.
    ``exclude`` — мягко исключить проводку из ДДС и из баланса.
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["split", "exclude"] = "split"
    splits: list[CashflowSplitItem] = Field(default_factory=list)
    counterparty_id: uuid.UUID | None = None


class CashflowClassifyRead(BaseModel):
    transaction_id: uuid.UUID
    cashflow_transaction_ids: list[uuid.UUID] = Field(default_factory=list)


class OperationClassifyRead(BaseModel):
    bank_operation_id: uuid.UUID
    classification_status: str
    cashflow_transaction_ids: list[uuid.UUID] = Field(default_factory=list)
    rule_id: uuid.UUID | None = None


class SafeAllocationCreate(BaseModel):
    """Создание резерва на счёте «Сейф» (намерение потратить под статью/получателя)."""

    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(gt=0)
    article_id: uuid.UUID
    counterparty_id: uuid.UUID | None = None
    purpose: str | None = None
    # Сразу оплатить полностью — прямая трата свободных средств (без отдельного шага «Оплачено»).
    pay_full: bool = False


class SafeAllocationPayRequest(BaseModel):
    """Оплата резерва — полная или частичная (``amount`` ≤ остатка резерва)."""

    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(gt=0)


class AllocationMoveRequest(BaseModel):
    """Перемещение резерва между Сейфом и Кассой (весь непогашенный остаток)."""

    model_config = ConfigDict(extra="forbid")

    to_location: Literal["safe", "kassa"]


class InternalTransferCreate(BaseModel):
    """Внутренний перевод денег между наличными счетами (Сейф↔Касса).

    ``mode='plain'`` — просто перемещение ``amount``. ``mode='targeted'`` — перемещение
    Σ строк + целевой резерв на счёте-получателе по каждой строке (появляются в «Платежах»).
    Счёт-получатель — только наличный (Сейф/Касса); на банковские переводить нельзя.
    """

    model_config = ConfigDict(extra="forbid")

    source_wallet_id: uuid.UUID
    dest_wallet_id: uuid.UUID
    mode: Literal["plain", "targeted"] = "plain"
    amount: Decimal | None = Field(default=None, gt=0)
    purpose: str | None = Field(default=None, max_length=210)
    lines: list[NewPaymentExpenseLineIn] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _require_amount_or_lines(self) -> InternalTransferCreate:
        if self.mode == "plain" and self.amount is None:
            raise ValueError("Для обычного перевода укажите сумму")
        if self.mode == "targeted" and not self.lines:
            raise ValueError("Для целевого перевода добавьте хотя бы одну цель")
        return self


class InternalTransferRead(BaseModel):
    transfer_id: uuid.UUID
    amount: float
    reserves: int


class NewPaymentTransferCreate(BaseModel):
    """Внутренний перевод из «Нового платежа» (обычный, без резерва).

    Источник — «Счёт списания» окна; получатель — только наличный (Сейф/Касса).
    Наличный источник → мгновенный перевод; банковский → черновик-пополнение Сейфа
    (банк→Касса напрямую нельзя — только банк→Сейф).
    """

    model_config = ConfigDict(extra="forbid")

    source_wallet_id: uuid.UUID
    dest_wallet_id: uuid.UUID
    amount: Decimal = Field(gt=0)
    purpose: str | None = Field(default=None, max_length=210)


class NewPaymentTransferRead(BaseModel):
    # kind='transfer' — наличный перевод проведён; kind='draft' — создан банк-черновик.
    kind: str
    amount: float
    draft_id: uuid.UUID | None = None


class SafeAllocationRead(BaseModel):
    id: uuid.UUID
    wallet_id: uuid.UUID
    amount: str
    amount_paid: str
    outstanding: str
    article_id: uuid.UUID | None = None
    counterparty_id: uuid.UUID | None = None
    # Имя контрагента-получателя (кому предназначены деньги) — для строки резерва.
    counterparty_name: str | None = None
    purpose: str | None = None
    # Происхождение авто-резерва: черновик выплаты на карту ИП (неофициальный поставщик).
    source_draft_id: uuid.UUID | None = None
    status: str
    # Где живёт целёвка: 'safe' — на карте «Сейф», 'kassa' — передана в Торговую кассу.
    location: str
    created_at: datetime


class SafeCashWithdrawRequest(BaseModel):
    """Снятие наличных с карты «Сейф» → касса Черникова (перевод между счетами)."""

    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(gt=0)


class SafeReconcileRequest(BaseModel):
    """Сверка: ввод реального остатка карты. ``apply_adjustment`` — выровнять учётный остаток."""

    model_config = ConfigDict(extra="forbid")

    actual_balance: Decimal = Field(ge=0)
    apply_adjustment: bool = False


class SafeReconcileRead(BaseModel):
    accounted: str  # учётный остаток Сейфа
    actual: str  # реальный остаток карты (введён вручную)
    delta: str  # реальный − учётный (>0 излишек, <0 недостача)
    adjusted: bool  # проведена ли корректирующая проводка


class NewPaymentArticleCounterpartyRead(BaseModel):
    """Контрагент, закреплённый за статьёй, и обязательный маршрут его оплаты."""

    counterparty_id: uuid.UUID
    name: str
    inn: str | None = None
    relationship: Literal["official", "informal", "barter"]
    has_requisites: bool
    requisites_verified: bool


class NewPaymentArticleRead(BaseModel):
    """Статья селекта окна «Новый платёж» + её маршрут (какие поля достраивать)."""

    id: uuid.UUID
    code: str
    name: str
    flow: Literal[
        "expense",
        "income",
        "employee_payout",
        "employee_advance",
        "employee_loan",
        "supplier_prepayment",
        "supplier_invoices",
        "internal_transfer",
    ]
    # Вид деятельности статьи — для леджер-фильтра палитры (operating/financing/investing).
    activity: str | None = None
    # Закреплённые за статьёй контрагенты — «кому платим» для свободного вывода.
    counterparties: list[NewPaymentArticleCounterpartyRead] = Field(default_factory=list)


class NewPaymentWalletRead(BaseModel):
    """Счёт списания; ``bank_code`` — банк счёта (у наличных нет).

    ``kind``: ``bank`` — платёж уходит банковским черновиком; ``cash`` — оплата
    наличными без черновика (резерв). ``location`` для наличных: ``safe`` (Сейф) /
    ``kassa`` (Торговая касса)."""

    id: uuid.UUID
    code: str
    name: str
    bank_code: str | None = None
    kind: str = "bank"
    location: str | None = None


class NewPaymentEmployeeRead(BaseModel):
    """Сотрудник для маршрутов выплат/авансов; ``on_demand`` — режим «по востребованию»."""

    id: uuid.UUID
    full_name: str
    position: str | None = None
    on_demand: bool


class PayoutAttributionEmployeeRead(BaseModel):
    """Сотрудник для привязки выплаты при разборе операции журнала ДДС.

    ``on_demand`` — окладник «по востребованию» (выплата гасит долг собственника, а не
    ведомость). ``status`` — active/dismissing/dismissed (для пометки «уволен» в UI)."""

    id: uuid.UUID
    full_name: str
    position: str | None = None
    on_demand: bool
    status: str


class NewPaymentContextRead(BaseModel):
    articles: list[NewPaymentArticleRead]
    wallets: list[NewPaymentWalletRead]
    employees: list[NewPaymentEmployeeRead]


class NewPaymentExpenseLineIn(BaseModel):
    """Строка свободного расхода: статья, сумма, назначение и получатель."""

    model_config = ConfigDict(extra="forbid")

    article_id: uuid.UUID
    amount: Decimal = Field(gt=0)
    # Назначение строки уходит в банк и в целёвку Сейфа; пусто → имя статьи.
    purpose: str = Field(default="", max_length=210)
    # Необязательная атрибуция: кому платим (для статей с привязанными контрагентами).
    counterparty_id: uuid.UUID | None = None


class NewPaymentExpenseDraftCreate(BaseModel):
    """Свободный расход: прямой платёж или банковский черновик на карту ИП.

    Одним траншем можно вывести несколько статей — ``lines`` (статья, сумма, назначение);
    разбивка помнится и при выплате с Сейфа разносится по статьям. Одиночные
    ``article_id``/``amount``/``purpose`` — обратная совместимость (транш из одной строки).
    """

    model_config = ConfigDict(extra="forbid")

    lines: list[NewPaymentExpenseLineIn] | None = Field(default=None, min_length=1)
    article_id: uuid.UUID | None = None
    amount: Decimal | None = Field(default=None, gt=0)
    purpose: str | None = Field(default=None, max_length=210)
    # Банк-плательщик черновика: bank_draft (Т-Банк, по умолчанию) или bank_draft_sber (Сбер).
    # Сбер доступен только для свободного расхода — прочие маршруты остаются в Т-Банке.
    channel: Literal["bank_draft", "bank_draft_sber"] = "bank_draft"
    # Явное подтверждение fallback-маршрута: официальный контрагент без реквизитов
    # оплачивается через карту ИП → Сейф. Если реквизиты есть, флаг их не обходит.
    allow_official_via_safe: bool = False

    @model_validator(mode="after")
    def _require_lines_or_single(self) -> NewPaymentExpenseDraftCreate:
        if self.lines:
            return self
        if self.article_id is None or self.amount is None or not (self.purpose or "").strip():
            raise ValueError(
                "Укажите платежи транша (lines) или одиночный article_id/amount/purpose"
            )
        return self

    def normalized_lines(self) -> list[NewPaymentExpenseLineIn]:
        """Строки транша (одиночные поля → транш из одной строки)."""
        if self.lines:
            return self.lines
        return [
            NewPaymentExpenseLineIn(
                article_id=self.article_id,  # type: ignore[arg-type]
                amount=self.amount,  # type: ignore[arg-type]
                purpose=self.purpose or "",
            )
        ]


class NewPaymentExpenseDraftRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    amount: float
    status: str
    provider_ref: str | None = None
    last_error: str | None = None
    created_at: datetime


class NewPaymentExpenseCashCreate(BaseModel):
    """Свободный вывод НАЛИЧНЫМИ: сразу резерв(ы) на Сейфе/в Кассе, без банковского
    черновика (деньги уже на счёте). По одному резерву на строку — как у оплаченного
    транша, только без транзита с р/с."""

    model_config = ConfigDict(extra="forbid")

    # Счёт-источник: кошелёк Сейфа (cash_safe) или Торговой кассы (store_cash).
    wallet_id: uuid.UUID
    lines: list[NewPaymentExpenseLineIn] = Field(min_length=1)
    # «Создать платёж»: сразу оплатить каждый резерв целиком (out-проводка, деньги ушли).
    # Требует дополнительно права finance.safe.confirm_paid — как pay_full ручного резерва.
    pay_now: bool = False


class NewPaymentExpenseCashRead(BaseModel):
    created: int
    total: float
    location: str
    paid: bool = False


class NewPaymentIncomeCreate(BaseModel):
    """Наличное поступление из окна «Новый платёж»: одна in-проводка на строку
    (статья+сумма+назначение) на Сейф или в Кассу. Банковские приходы вручную
    запрещены — их приносит выписка."""

    model_config = ConfigDict(extra="forbid")

    # Счёт зачисления: кошелёк Сейфа (cash_safe) или Торговой кассы (store_cash).
    wallet_id: uuid.UUID
    lines: list[NewPaymentExpenseLineIn] = Field(min_length=1)


class NewPaymentIncomeRead(BaseModel):
    created: int
    total: float
    location: str


class JournalRow(BaseModel):
    """One line of the unified DDS journal — a classified cashflow movement
    (``kind="cashflow"``) or an unclassified bank operation (``kind="operation"``)."""

    kind: Literal["cashflow", "operation"]
    id: uuid.UUID
    bank_operation_id: uuid.UUID | None = None
    status: str  # "classified" | "needs_review"
    operation_date: date
    occurred_at: datetime  # business day + real time-of-day (Moscow), drives sort order
    direction: str
    amount: str
    article_id: uuid.UUID | None = None
    counterparty_id: uuid.UUID | None = None
    wallet_id: uuid.UUID | None = None
    provider: str | None = None
    payment_purpose: str | None = None
    counterparty_name_raw: str | None = None
    counterparty_inn_raw: str | None = None
    # Карт-операция (получатель в банке — эквайер): фронт показывает мягкое предупреждение при
    # ручной привязке к накладной. Для проводок (kind="cashflow") всегда False.
    is_card: bool = False


class JournalListRead(BaseModel):
    items: list[JournalRow]
    total: int
    marked_total: int
    unmarked_total: int
    transfer_total: int = 0
