"""Налоги: база дохода, факты уплаты в бюджет, закрытые периоды, ручные обязательства.

Методология (проверена по НК РФ и сверена с фактическими платежами Q1 2026):

* режим — УСН «Доходы» 6%; всё считается НАРАСТАЮЩИМ ИТОГОМ с 1 января;
* база дохода — выручка iiko со скидками (``DishDiscountSumInt``), решение владельца 23.07.2026;
  юридически база кассовая (ст. 346.17 НК) — расхождение возможно на агрегаторах с лагом;
* лимит вычета 50% (абз. 5 п. 3.1 ст. 346.21) — у ИП ЕСТЬ работники; лимит применяется
  к СОВОКУПНОСТИ вычета, срезанная часть СГОРАЕТ и никуда не переносится;
* взносы ЗА РАБОТНИКОВ уменьшают налог ТОЛЬКО ПО ФАКТУ УПЛАТЫ (подп. 1 п. 3.1) — кассовый принцип;
* взносы ИП ЗА СЕБЯ (фиксированные и допвзнос 1%) — ПО НАЧИСЛЕНИЮ, уплата не требуется
  (абз. 7 п. 3.1 в ред. 389-ФЗ + письмо ФНС от 08.04.2024 № СД-4-3/4104@);
* НДФЛ В ВЫЧЕТ НЕ ВХОДИТ НИКОГДА — это налог работника, а не расход по УСН. В ЕНП он уходит
  вперемешку со взносами, поэтому платёж обязан раскладываться на назначения;
* допвзнос 1% = (доход_YTD − 300 000) × 1%, порог вычитается ОДИН РАЗ ЗА ГОД, потолок 2026 —
  321 818 ₽; заявленное в текущем году (стр. 161 декларации) нельзя заявить повторно
  в следующем (стр. 162) — отсюда реестр израсходованного.

КЛЮЧЕВОЙ ИНВАРИАНТ: вычет НЕ создаёт дебиторку. Уплата взносов гасит то же обязательство,
что и платёж по УСН, поэтому при не-упёртом лимите ``УСН_к_уплате + зачтённые взносы = 6% × доход``.
Начислений в БД нет вообще — расчёт является чистой функцией от фактов, сторнировать нечего.
Единственный настоящий актив схемы — положительное сальдо ЕНС, оно вводится фактом из ЛК ФНС.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# ── Константы методологии ────────────────────────────────────────────────────

# Порог, свыше которого начисляется допвзнос 1% (подп. 2 п. 1 ст. 430 НК).
EXTRA_CONTRIBUTION_THRESHOLD = Decimal("300000.00")
# Доля исчисленного налога, до которой можно уменьшить его вычетом при наличии работников.
DEDUCTION_LIMIT_WITH_EMPLOYEES = Decimal("0.50")

# Назначения платежа, которые ДАЮТ вычет по факту уплаты. НДФЛ здесь отсутствует намеренно.
DEDUCTIBLE_PAID_KINDS: tuple[str, ...] = ("contrib_employees", "contrib_injury")
# Взносы ИП «за себя» — дают вычет по начислению, факт уплаты для вычета не нужен.
SELF_CONTRIBUTION_KINDS: tuple[str, ...] = ("contrib_fixed", "contrib_extra_1pct")

TAX_PAYMENT_KINDS: tuple[str, ...] = (
    "usn_advance",  # авансовый платёж / годовой налог по УСН
    "ndfl",  # НДФЛ за работников — в вычет НЕ идёт
    "contrib_employees",  # взносы за работников по единому тарифу (ОПС/ОМС/ВНиМ)
    "contrib_injury",  # взносы на травматизм в СФР — платятся ОТДЕЛЬНО, не в составе ЕНП
    "contrib_fixed",  # фиксированные взносы ИП за себя
    "contrib_extra_1pct",  # допвзнос 1% с дохода свыше 300 000
    "penalty",  # пени и штрафы
    "other",
)

REVENUE_GRANULARITIES: tuple[str, ...] = ("day", "month")

TAX_PERIOD_CODES: tuple[str, ...] = ("q1", "h1", "9m", "year")

# Откуда взялась строка платежа. Отделено от `quality_status`: источник отвечает на вопрос
# «кто её создал», качество — «насколько ей можно верить».
TAX_PAYMENT_SOURCE_KINDS: tuple[str, ...] = (
    "bank_statement",  # импорт выписки банка: сумма и дата достоверны, назначение — нет
    "tax_notice",  # уведомление об исчисленных суммах от налогового агента — эталон
    "bank_draft",  # платёж, порождённый нашим же черновиком в банке
    "manual",  # ручной ввод владельцем
)

# Насколько строке можно верить. Критично для разноса ЕНП: банк даёт ОДИН КБК
# (18201061201010000510, «Единый налоговый платеж») на НДФЛ, взносы, налог и допвзнос —
# из выписки назначение прочитать НЕЛЬЗЯ. Пока нет уведомления, разнос расчётный.
TAX_PAYMENT_QUALITY_STATUSES: tuple[str, ...] = (
    "confirmed",  # подтверждено первичным документом (уведомление, платёжка агента)
    "reconstructed",  # выведено расчётом: база ФОТ из травматизма → формула взносов → остаток НДФЛ
    "requires_review",  # расчёт не сошёлся или данных не хватило — нужен владелец
)


class IikoRevenuePeriod(Base):
    """Выручка iiko за период — БАЗА налога.

    Гранулярность двойная: ``month`` (месячный агрегат OLAP — то, что доступно из локального
    кэша) и ``day`` (подневная выгрузка с прод-контейнера). Для налога месячной гранулярности
    ДОСТАТОЧНО И ТОЧНО: все налоговые периоды (Q1, полугодие, 9 месяцев, год) заканчиваются
    на границе месяца, поэтому месячная строка никогда не пересекает границу периода частично.

    Смешивать гранулярности внутри одного месяца НЕЛЬЗЯ — будет двойной счёт. При заливке
    подневных данных месячная строка того же месяца удаляется в той же транзакции
    (см. ``app/services/taxes/revenue_sync.py``).
    """

    __tablename__ = "iiko_revenue_period"
    __table_args__ = (
        UniqueConstraint(
            "department",
            "granularity",
            "period_start",
            name="uq_iiko_revenue_period_slot",
        ),
        CheckConstraint(
            "granularity in ('day', 'month')",
            name="ck_iiko_revenue_period_granularity",
        ),
        CheckConstraint("period_end >= period_start", name="ck_iiko_revenue_period_range"),
        # Подневная строка обязана покрывать ровно один день.
        CheckConstraint(
            "granularity <> 'day' or period_end = period_start",
            name="ck_iiko_revenue_period_day_single",
        ),
        CheckConstraint("revenue_net >= 0", name="ck_iiko_revenue_period_net_non_negative"),
        CheckConstraint(
            "revenue_gross is null or revenue_gross >= 0",
            name="ck_iiko_revenue_period_gross_non_negative",
        ),
        CheckConstraint(
            "source in ('iiko_olap', 'manual')",
            name="ck_iiko_revenue_period_source",
        ),
        Index("ix_iiko_revenue_period_start", "period_start"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    granularity: Mapped[str] = mapped_column(String(8), nullable=False)
    department: Mapped[str] = mapped_column(String(120), nullable=False)

    # БАЗА НАЛОГА: выручка со скидками, iiko OLAP `DishDiscountSumInt`.
    revenue_net: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    # Справочно: выручка без скидок (`DishSumInt`) — от неё считает ОПиУ, но НЕ налог.
    revenue_gross: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    discount_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)

    source: Mapped[str] = mapped_column(String(24), nullable=False, default="iiko_olap")
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class IikoRevenueRevision(Base):
    """Журнал правок базы задним числом.

    iiko правит закрытые дни (сторно-чеки, перепроведение). Без журнала дрейф уже
    зафиксированного периода невозможно объяснить: «база за март выросла на 12 400 ₽» должно
    раскладываться до конкретной строки и её прежнего значения.
    """

    __tablename__ = "iiko_revenue_revision"
    __table_args__ = (
        CheckConstraint(
            "sync_reason in ('daily_job', 'manual_sync', 'backfill')",
            name="ck_iiko_revenue_revision_reason",
        ),
        Index("ix_iiko_revenue_revision_start", "period_start"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    granularity: Mapped[str] = mapped_column(String(8), nullable=False)
    department: Mapped[str] = mapped_column(String(120), nullable=False)
    revenue_net_old: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    revenue_net_new: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    sync_reason: Mapped[str] = mapped_column(String(32), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TaxPayment(Base):
    """ФАКТ уплаты в бюджет. Одна строка = одно НАЗНАЧЕНИЕ внутри одного перевода.

    Почему не «один платёж — одна строка»: ЕНП уходит одной суммой, но внутри содержит
    и НДФЛ (в вычет НЕ идёт), и взносы за работников (идут). Без разложения вычет посчитать
    нельзя. Строки одного перевода связаны общим ``bundle_id``.

    ``paid_on`` — дата фактического СПИСАНИЯ, а не плановая: для кассового принципа вычета
    по взносам за работников важен именно факт ухода денег.

    ``for_year`` отделён от даты: аванс, уплаченный 28.04.2027, относится к налогу за 2026 год.
    """

    __tablename__ = "tax_payment"
    __table_args__ = (
        CheckConstraint(
            "kind in ('usn_advance', 'ndfl', 'contrib_employees', 'contrib_injury', "
            "'contrib_fixed', 'contrib_extra_1pct', 'penalty', 'other')",
            name="ck_tax_payment_kind",
        ),
        CheckConstraint("recipient in ('fns', 'sfr')", name="ck_tax_payment_recipient"),
        CheckConstraint(
            "status in ('planned', 'paid', 'cancelled')", name="ck_tax_payment_status"
        ),
        CheckConstraint(
            "source_kind in ('bank_statement', 'tax_notice', 'bank_draft', 'manual')",
            name="ck_tax_payment_source_kind",
        ),
        CheckConstraint(
            "quality_status in ('confirmed', 'reconstructed', 'requires_review')",
            name="ck_tax_payment_quality_status",
        ),
        CheckConstraint("amount > 0", name="ck_tax_payment_amount_positive"),
        # НДФЛ и взносы за работников в СФР не платят; травматизм не платят в составе ЕНП.
        CheckConstraint(
            "(recipient = 'sfr' and kind in ('contrib_injury', 'penalty', 'other')) "
            "or (recipient = 'fns' and kind <> 'contrib_injury')",
            name="ck_tax_payment_recipient_kind",
        ),
        Index("ix_tax_payment_paid_on", "paid_on"),
        Index("ix_tax_payment_year_kind", "for_year", "kind"),
        Index("ix_tax_payment_bundle", "bundle_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Один банковский перевод = один bundle; строк внутри может быть несколько.
    bundle_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    paid_on: Mapped[date] = mapped_column(Date, nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    recipient: Mapped[str] = mapped_column(String(8), nullable=False)
    for_year: Mapped[int] = mapped_column(Integer, nullable=False)
    # 'q1' | 'h1' | '9m' | 'year' для УСН; 'YYYY-MM' для помесячных взносов и НДФЛ.
    for_period: Mapped[str | None] = mapped_column(String(8), nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="paid")
    # Происхождение строки и доверие к ней. Сумма перевода почти всегда достоверна
    # (она из выписки), а вот её РАЗНОС по назначениям до появления уведомления —
    # расчётный, и это обязано быть видно в интерфейсе, а не подразумеваться.
    source_kind: Mapped[str] = mapped_column(String(24), nullable=False, default="manual")
    quality_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="confirmed"
    )

    cashflow_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("cashflow_transactions.id", ondelete="SET NULL"),
        nullable=True,
    )
    bank_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bank_operations.id", ondelete="SET NULL"), nullable=True
    )

    document_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TaxPeriodClose(Base):
    """Зафиксированный расчёт отчётного периода + реестр израсходованного вычета.

    Две задачи:

    1. Заморозить цифру, поданную налоговому агенту, чтобы последующий дрейф базы (iiko правит
       закрытые дни) не переписывал историю молча — расхождение видно как дрейф.
    2. Вести реестр ЗАЯВЛЕННОГО допвзноса 1% по расчётным периодам. Письмо ФНС СД-4-3/4104@
       запрещает повторный учёт: заявленное за 2026 год нельзя заявить снова в 2027.
       ``extra_claimed_for_year`` говорит, за КАКОЙ расчётный период заявлен взнос, а ``year`` —
       в каком налоговом периоде он использован.
    """

    __tablename__ = "tax_period_close"
    __table_args__ = (
        UniqueConstraint("year", "period_code", name="uq_tax_period_close_slot"),
        CheckConstraint(
            "period_code in ('q1', 'h1', '9m', 'year')", name="ck_tax_period_close_code"
        ),
        CheckConstraint("income_ytd >= 0", name="ck_tax_period_close_income"),
        CheckConstraint("deduction_applied >= 0", name="ck_tax_period_close_deduction"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    period_code: Mapped[str] = mapped_column(String(8), nullable=False)

    # Снимок расчёта на момент фиксации.
    income_ytd: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    tax_computed: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    deduction_applied: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    deduction_burned: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    amount_due: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)

    # Сколько взносов «за себя» заявлено в этом периоде и за какой расчётный период они начислены.
    fixed_claimed: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    extra_claimed: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    extra_claimed_for_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extra_accrued: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )

    # Отпечаток входных данных — по нему ловится дрейф базы после фиксации.
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    drift_detected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    closed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    closed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TaxManualObligation(Base):
    """Ручное обязательство перед бюджетом: пени, штрафы, требования ФНС.

    Движок их не считает — они приходят требованием и вводятся руками. В расчёт УСН не входят
    (не уменьшают налог и не являются вычетом), но попадают в таймлайн сроков и в задолженность.
    """

    __tablename__ = "tax_manual_obligation"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_tax_manual_obligation_amount"),
        CheckConstraint("recipient in ('fns', 'sfr')", name="ck_tax_manual_obligation_recipient"),
        CheckConstraint(
            "status in ('open', 'paid', 'cancelled')", name="ck_tax_manual_obligation_status"
        ),
        Index("ix_tax_manual_obligation_due", "due_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    recipient: Mapped[str] = mapped_column(String(8), nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="open")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# Статусы разбора налогового документа из почты.
TAX_INTAKE_STATUSES: tuple[str, ...] = (
    "parsed",  # распознан уверенно, готов к продвижению в tax_payment
    "needs_review",  # распознан частично — нужен владелец (напр. смешанный ЕНП)
    "promoted",  # уже создана строка tax_payment / зарегистрирован факт
    "unsupported",  # тип документа не относится к платёжному контуру (приказ, договор)
    "error",  # разбор упал
    "ignored",  # владелец отклонил
)

TAX_DOCUMENT_TYPES: tuple[str, ...] = (
    "payment_order",  # платёжка (форма 0401060)
    "payroll_statement",  # платёжная ведомость (форма Т-53)
    "turnover_statement",  # сальдо-оборотная ведомость по зарплате (эталон разноса ЕНП)
    "unknown",
)


class TaxDocumentIntake(Base):
    """Staging входящих налоговых документов из почты бухгалтера.

    Зеркалит ``email_invoice_intake``: сырое вложение + результат разбора + статус, дедуп по
    SHA-256 содержимого. Разбор здесь НЕ создаёт напрямую факты налога — он складывает
    распознанное для проверки владельцем и последующего продвижения. Так распознавание по
    рукописному имени файла остаётся под human-контролем, а не уходит в расчёт молча.
    """

    __tablename__ = "tax_document_intake"
    __table_args__ = (
        UniqueConstraint("attachment_sha256", name="uq_tax_document_intake_sha"),
        CheckConstraint(
            "status in ('parsed', 'needs_review', 'promoted', 'unsupported', 'error', 'ignored')",
            name="ck_tax_document_intake_status",
        ),
        CheckConstraint(
            "document_type in ('payment_order', 'payroll_statement', "
            "'turnover_statement', 'unknown')",
            name="ck_tax_document_intake_type",
        ),
        Index("ix_tax_document_intake_status", "status"),
        Index("ix_tax_document_intake_received", "received_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    mailbox: Mapped[str] = mapped_column(String(32), nullable=False)
    from_addr: Mapped[str | None] = mapped_column(String(320), nullable=True)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    message_uid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    mime: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attachment_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    document_type: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="needs_review")
    # Разобранные поля документа (сумма, КБК, вид, срок, строки ведомости, причины review).
    recognition: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Куда продвинули: bundle платежа в tax_payment, если создали.
    tax_payment_bundle_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TaxPayrollLedger(Base):
    """Помесячная раскладка зарплаты по сотруднику из сальдо-оборотной ведомости.

    Канон для двух потребителей: (1) РАЗНОС зарплатного ЕНП — НДФЛ и взносы за работника
    берутся отсюда как эталон, а не реконструируются из травматизма; (2) МОНИТОР зарплатного
    критерия льготы по НДС — фактические месячные начисления действующего работника.

    Одна строка = один сотрудник за один месяц; источник — конкретная оборотка (``intake_id``).
    Суммы соответствуют колонкам листа 'л1'; ``None`` там, где ячейка пуста (≠ 0). Начислений
    в налоговый расчёт эта таблица НЕ создаёт — это справочные факты для разноса и монитора.
    """

    __tablename__ = "tax_payroll_ledger"
    __table_args__ = (
        # Один сотрудник × месяц = одна строка: повторная оборотка обновляет, а не плодит.
        UniqueConstraint("year", "month", "tab_number", name="uq_tax_payroll_ledger_slot"),
        CheckConstraint("month between 1 and 12", name="ck_tax_payroll_ledger_month"),
        CheckConstraint(
            "accrued is null or accrued >= 0", name="ck_tax_payroll_ledger_accrued"
        ),
        Index("ix_tax_payroll_ledger_period", "year", "month"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    tab_number: Mapped[str | None] = mapped_column(String(16), nullable=True)
    employee: Mapped[str] = mapped_column(String(200), nullable=False)

    oklad: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    days: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    accrued: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)  # начислено
    ndfl: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    advance: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    contributions: Mapped[Decimal | None] = mapped_column(  # взносы за работника (СФР)
        Numeric(14, 2), nullable=True
    )
    injury: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)  # травматизм
    deduction: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)  # вычеты
    to_pay: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)  # к выплате

    intake_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tax_document_intake.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
