from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class IikoCashShift(Base):
    """Закрытая кассовая смена iiko (`/resto/api/v2/cashshifts/list`).

    Витрина вкладки «Касса → Закрытие смены» и источник авто-проводки наличного
    контура в ДДС. Одна строка = одна смена кассы Черниковой. ``cash_remain`` —
    остаток наличных после закрытия, ``pay_out`` — суммарные изъятия (инкассация +
    ЗП курьеров + наличные Алисы); разнос изъятий по счетам-назначениям лежит в
    ``IikoCashShiftPayout``. ``posted`` — отметка, что наличный контур смены уже
    проведён в ДДС (барьер идемпотентности повторного синка).

    Смену сохраняем по наличию ``close_date``, а не по ``session_status`` — в проде
    закрытые смены приходят со статусом ``UNACCEPTED``. ``raw`` — исходный объект
    смены iiko как есть.
    """

    __tablename__ = "iiko_cash_shift"
    __table_args__ = (
        Index("uq_iiko_cash_shift_session", "iiko_session_id", unique=True),
        Index("ix_iiko_cash_shift_close_date", "close_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # GUID кассовой смены iiko — ключ upsert при повторном синке.
    iiko_session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    point_of_sale_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    manager_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    open_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    session_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    session_start_cash: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    # salesCash из iiko (завышен зачтёнными предоплатами) — храним для справки.
    sales_cash: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    # Достоверная наличная выручка из OLAP (тип оплаты «Наличные») — для расхождения/витрины.
    cash_sales: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    sales_card: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    # Вся выручка смены из OLAP (сумма по всем PayTypes.Group: нал + безнал + прочее) —
    # база порога авто-штрафа за недостачу (salesCard iiko ненадёжен, берём из OLAP).
    total_sales: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    pay_in: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    pay_out: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    cash_remain: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    cash_diff: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    posted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Итог авто-штрафа за недостачу смены: ``none`` (недостача ≤ порога / нет),
    # ``applied`` (штраф разнесён по кассирам), ``waived`` (отменён правом
    # kassa.penalty.waive), ``manual_review`` (недостача > порога, но штраф нельзя
    # создать автоматически — нет кассиров на дату / период зафиксирован / нет выручки).
    penalty_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    penalty_review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    payouts: Mapped[list[IikoCashShiftPayout]] = relationship(
        back_populates="shift", cascade="all, delete-orphan"
    )
    penalties: Mapped[list[KassaShiftPenalty]] = relationship(
        back_populates="shift", cascade="all, delete-orphan"
    )


class IikoCashShiftPayout(Base):
    """Изъятие наличных за смену (`payOutsRecords` из `/v2/cashshifts/payments/list`).

    Каждая запись — одно изъятие со счётом-назначением iiko (``account_id_iiko``),
    который задаёт ``category`` и правило проводки в ДДС:

    - ``main_cash`` — инкассация в кассу Черниковой → приход ДДС («Поступление денег
      с торг. точек», кошелёк ``tk_chernikova``);
    - ``courier_salary`` — ЗП курьеров → расход ДДС («Курьерская служба -»);
    - ``alisa`` — наличные у партнёра → дебиторка УДКЗ, движение ДДС НЕ создаётся;
    - ``unknown`` — прочий счёт назначения → движение не создаём, строку фиксируем.

    ``cashflow_transaction_id`` ссылается на созданное движение ДДС (NULL для
    ``alisa``/``unknown``).
    """

    __tablename__ = "iiko_cash_shift_payout"
    __table_args__ = (Index("ix_iiko_cash_shift_payout_shift", "shift_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shift_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("iiko_cash_shift.id", ondelete="CASCADE"),
        nullable=False,
    )
    iiko_payout_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Счёт назначения изъятия в iiko (info.accountId) — определяет category.
    account_id_iiko: Mapped[str] = mapped_column(String(128), nullable=False)
    account_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    cashflow_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("cashflow_transactions.id", ondelete="SET NULL"),
        nullable=True,
    )
    raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    shift: Mapped[IikoCashShift] = relationship(back_populates="payouts")


class KassaShiftPenalty(Base):
    """Авто-штраф кассиру за недостачу смены (недостача > порога % выручки).

    Одна строка = один штраф одному кассиру смены. Недостача делится поровну между
    всеми, кто работал кассиром (``ShiftLedgerEntry.payroll_role='administrator'``) в
    день открытия смены. Создаётся при синке смены (идемпотентно по ``shift_id`` +
    ``employee_id``) и материализуется как ``PayrollAdjustment`` (penalty) в недельной
    ведомости кассира.

    ``status='active'`` — штраф действует и его ``PayrollAdjustment`` входит в расчёт;
    ``status='waived'`` — отменён правом ``kassa.penalty.waive`` (связанный
    ``PayrollAdjustment`` удалён, ``payroll_adjustment_id`` обнулён). Запись остаётся
    для трассировки и как барьер идемпотентности (повторный синк не воскрешает штраф).
    """

    __tablename__ = "kassa_shift_penalty"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_kassa_shift_penalty_amount_positive"),
        CheckConstraint(
            "status in ('active', 'waived')",
            name="ck_kassa_shift_penalty_status",
        ),
        UniqueConstraint("shift_id", "employee_id", name="uq_kassa_shift_penalty_shift_employee"),
        Index("ix_kassa_shift_penalty_shift", "shift_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shift_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("iiko_cash_shift.id", ondelete="CASCADE"),
        nullable=False,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id", ondelete="RESTRICT"), nullable=False
    )
    payroll_adjustment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payroll_adjustment.id", ondelete="SET NULL"), nullable=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    waived_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    waived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    shift: Mapped[IikoCashShift] = relationship(back_populates="penalties")


class ChequeIikoPayout(Base):
    """Изъятие (`addPayOut`) в iiko по строке «прочих расходов» чека местного закупа.

    Не-складские расходы чека (питание/прочие затраты на персонал, содержание точек)
    дублируются в iiko финансовой проводкой-изъятием ``POST /v2/payInOuts/addPayOut``. Тип
    изъятия (``pay_out_type_id``) инкапсулирует счёт-источник (Главная касса для наличных /
    «Денежные средства, эквайринг» для карты), корсчёт расхода и статью ДДС iiko, поэтому в
    запросе передаём только тип + сумму + подразделение.

    Одна строка = одно изъятие = (чек × наша статья ДДС × источник оплаты ``cash``|``card``).
    Таблица — барьер идемпотентности: проводки iiko НЕОБРАТИМЫ через API (нет ни delete, ни
    внесения — addPayOut отвергает PAYIN), поэтому повторный прогон НЕ должен задвоить уже
    проведённое. Также аудит и основа будущей OLAP-сверки (по ``comment``). ``status``:
    ``posted`` — addPayOut вернул SUCCESS; ``failed`` — ошибка (чек при этом создан, проведение
    можно повторить, не трогая ``posted``-строки).
    """

    __tablename__ = "kassa_cheque_iiko_payout"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_cheque_iiko_payout_amount_positive"),
        CheckConstraint("source in ('cash', 'card')", name="ck_cheque_iiko_payout_source"),
        CheckConstraint("status in ('posted', 'failed')", name="ck_cheque_iiko_payout_status"),
        UniqueConstraint(
            "invoice_id",
            "dds_article_id",
            "source",
            name="uq_cheque_iiko_payout_invoice_article_source",
        ),
        Index("ix_cheque_iiko_payout_invoice", "invoice_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("supplier_invoice.id", ondelete="CASCADE"),
        nullable=False,
    )
    dds_article_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dds_articles.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Источник оплаты доли расхода: 'cash' (Главная касса) | 'card' (эквайринг).
    source: Mapped[str] = mapped_column(String(8), nullable=False)
    # id типа изъятия iiko (payInOutType) — несёт счёт-источник + корсчёт + статью ДДС.
    pay_out_type_id: Mapped[str] = mapped_column(String(128), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # Причина неуспеха: type_not_found — отказ ДО отправки (в iiko точно ничего нет, повтор
    # безопасен); unknown — обрыв на отправке, исход НЕИЗВЕСТЕН, и повтор задвоил бы изъятие.
    reason_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class KassaPayinPreset(Base):
    """Именованный пресет «Внесение в кассу»: человеческое имя → статья ДДС + контур.

    Кассир в форме внесения выбирает пресет по имени («Деньги за масло», «Приход от
    „В гостях у Алисы“»), а бэкенд подставляет статью ДДС, фиксированного контрагента и
    шаблон комментария и проводит приход на ТК Черникова (``CashflowTransaction`` с
    ``direction='in'``, ``source_kind='kassa_payin'``). Никакой статьи ДДС кассир не
    видит — только имя. Каталог курирует владелец в «Настройках → Касса».

    ``mechanism`` — контур учёта: ``income`` (голый приход по статье) — единственный в
    v1; поле-заготовка под будущие ``advance_return``/``loan_return``/``supplier_refund``
    (гашение леджеров авансов/дебиторки при возврате). ``article_id`` — только приходная
    (``inflow``) статья и не ``technical`` (переводы между счетами нельзя — повиснет
    полунога перевода). ``counterparty_id`` — необязательный фиксированный контрагент
    строки (напр. партнёр «В гостях у Алисы»: приход по «Поступление с торг. точек» с
    ``source_kind='kassa_payin'`` не задваивает авто-контур смены ``kassa_cashshift``).
    ``comment_template`` — автоподстановка в назначение (кассир может переопределить).
    ``is_active``/``sort_order`` — витрина каталога.
    """

    __tablename__ = "kassa_payin_preset"
    __table_args__ = (
        CheckConstraint(
            "mechanism in ('income')",
            name="ck_kassa_payin_preset_mechanism",
        ),
        Index("ix_kassa_payin_preset_active_sort", "is_active", "sort_order"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    mechanism: Mapped[str] = mapped_column(
        String(32), nullable=False, default="income", server_default="income"
    )
    article_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dds_articles.id", ondelete="RESTRICT"),
        nullable=False,
    )
    counterparty_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("counterparty.id", ondelete="SET NULL"),
        nullable=True,
    )
    comment_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class IikoCashPayout(Base):
    """Журнал денежных проводок в iiko, которые НЕ привязаны к накладной: выдачи авансов и
    депозитов, пополнения депозитов курьеров (``POST /v2/payInOuts/addPayOut``).

    Зачем журнал. Проводка необратима (delete в API нет), а раньше её судьба нигде не
    фиксировалась: при любом сбое (сеть, таймаут, лимит частоты, iiko недоступна) изъятие
    молча терялось, и остаток «Главной кассы» в iiko расходился с нашим ДДС — узнать об этом
    было неоткуда, кроме ``warning`` в логах контейнера, которые ротируются.

    Паттерн pending-first, как у ``IikoInvoicePaymentPush``: строка ``pending`` пишется ДО
    HTTP, после ответа переходит в ``posted``/``failed``. Краш между вызовом и финализацией
    оставляет ``pending`` — исход неизвестен, и авто-повтор по такой строке запрещён (он
    задвоил бы деньги в учёте); разбирается вручную по кейсу owner-review.

    ``kind`` + ``source_id`` — ключ идемпотентности: одна операция (выдача аванса, транзакция
    депозита) даёт ровно одну проводку.
    """

    __tablename__ = "iiko_cash_payout"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_iiko_cash_payout_amount_positive"),
        CheckConstraint(
            "status in ('pending', 'posted', 'failed')", name="ck_iiko_cash_payout_status"
        ),
        UniqueConstraint("kind", "source_id", name="uq_iiko_cash_payout_kind_source"),
        Index("ix_iiko_cash_payout_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Контур проводки: advance | loan | deposit_production | courier_deposit_return |
    # courier_deposit_topup. Вместе с source_id даёт идемпотентность.
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # id операции-источника в нашей БД. Строка, а не UUID: у транзакции депозита курьера
    # первичный ключ целочисленный, у выдач — UUID.
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    payout_date: Mapped[date] = mapped_column(Date, nullable=False)
    # id типа payInOut в iiko (несёт счёт-источник, корсчёт и статью ДДС iiko).
    pay_out_type_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # Машиночитаемая причина неуспеха: type_not_found (до отправки — точно не прошло),
    # rejected (iiko ответила не SUCCESS), unknown (исключение при отправке — исход неизвестен).
    reason_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Пост-сверка по OLAP-отчёту iiko: ``verified_at`` — когда проводка найдена в учёте,
    # ``verify_attempts`` — сколько раз искали и не нашли. Ответ addPayOut проводку не
    # доказывает, а «потерянную» выдачу иначе увидеть неоткуда.
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verify_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
