"""Снимок баланса на дату: замороженные строки, флаги честности и сверка приращения капитала.

ПОЧЕМУ СНИМОК ХРАНИТ ЧИСЛА, А НЕ ПЕРЕСЧИТЫВАЕТ ИХ. Прошлое в этой системе законно двигается
четырьмя путями, и каждый бесследен: исключение банковской операции не датировано; первоначальная
стоимость основного средства переписывается задним числом вместе со всей историей объекта; отметка
выплаты сотруднику при снятии удаляется физически; разметка товара не имеет периода действия.
Плюс сам отчёт о прибыли — проектор без хранения: он считается заново при каждом запросе.

Пересчитываемый баланс ломает не отчётность, а КОНТРОЛЬ. Сверка сентября считает приращение
капитала как разницу с августом. Если август пересчитывается, то октябрьская правка августа
переезжает в расхождение сентября — и владелец ищет в сентябре дыру, созданную в октябре правкой
августа. Ровно то, ради чего модуль существует, перестаёт работать.

Отсюда контур такой же, как у основных средств: заморозка пишет числа, чтение отдаёт замороженное,
а отдельный детектор сравнивает снимок с живым расчётом и показывает дрейф. И жёсткое правило
оттуда же: ЖИВОЙ РАСЧЁТ НИКОГДА НЕ ЧИТАЕТ СНИМОК — иначе детектор начнёт сравнивать снимок сам с
собой, и расхождение станет тождественным нулём.

ВТОРОЕ ПРАВИЛО, БЕЗ КОТОРОГО СВЕРКА МЕРТВА. Капитал выводится как «актив минус обязательства»,
поэтому его приращение алгебраически равно приращению актива минус приращение обязательств.
Проверка настоящая ровно потому, что ОЖИДАЕМОЕ приращение считается НЕЗАВИСИМО — из прибыли и
движений по статьям капитала. Значит ни одно поле снимка не имеет права участвовать в вычислении
ожидаемой величины. Стоит однажды взять прибыль из шапки снимка «для консистентности» — и сверка
станет нулём навсегда.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class BalanceSnapshot(Base):
    """Шапка среза: итоги, метод сбора и сверка с предыдущим снимком."""

    __tablename__ = "balance_snapshot"
    __table_args__ = (
        # Два снимка на одну дату — это два «правильных» ответа на один вопрос.
        UniqueConstraint("as_of", name="uq_balance_snapshot_as_of"),
        CheckConstraint("status in ('draft', 'final')", name="ck_balance_snapshot_status"),
        CheckConstraint("method in ('inventory', 'rolling')", name="ck_balance_snapshot_method"),
        Index("ix_balance_snapshot_period", "period_month"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    period_month: Mapped[date] = mapped_column(Date, nullable=False)
    # draft пересобирается сколько угодно, final — нет.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    # ХРАНИТСЯ, а не выводится из «первый ли это снимок»: снимок, добавленный задним числом,
    # не должен менять смысл уже подписанной цифры. inventory — капитал установлен описью,
    # rolling — приращение проверяется сверкой.
    method: Mapped[str] = mapped_column(String(16), nullable=False, default="inventory")

    asset_total: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False)
    liability_total: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False)
    # Балансирующая величина, а не посчитанная. Помечена так и в строке, и здесь.
    equity_plug: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False)

    # Прибыль месяца, замороженная из отчёта. NULL допустим: у отчёта строка может быть
    # неполной, и ноль вместо «неизвестно» соврал бы в сверке.
    net_profit: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    net_profit_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    net_profit_missing: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Три движения капитала за месяц — считаются из проводок, НЕ из строк снимка.
    equity_dividends: Mapped[Decimal] = mapped_column(
        Numeric(16, 2), nullable=False, default=Decimal("0")
    )
    equity_contributions: Mapped[Decimal] = mapped_column(
        Numeric(16, 2), nullable=False, default=Decimal("0")
    )
    equity_returns: Mapped[Decimal] = mapped_column(
        Numeric(16, 2), nullable=False, default=Decimal("0")
    )

    # Сверка приращения. NULL у первого снимка — сверять не с чем, и это не ошибка.
    prev_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("balance_snapshot.id", ondelete="SET NULL"), nullable=True
    )
    recon_delta_equity: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    recon_expected: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    recon_residual: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    # Полоса допуска — это ИЗМЕНЕНИЕ приблизительных величин между двумя снимками, а не их
    # сумма. Сумма означала бы: чем хуже данные, тем шире «сошлось».
    tolerance_band: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    # Подпись расхождения человеком при финализации: почему остаток допустим.
    residual_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    finalized_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class BalanceSnapshotLine(Base):
    """Строка снимка с замороженными суммой, подписью и расшифровкой."""

    __tablename__ = "balance_snapshot_line"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "line_code", name="uq_balance_snapshot_line"),
        CheckConstraint(
            "kind in ('source', 'memo', 'subtotal')", name="ck_balance_snapshot_line_kind"
        ),
        Index("ix_balance_snapshot_line_snapshot", "snapshot_id", "sort_order"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("balance_snapshot.id", ondelete="CASCADE"), nullable=False
    )
    line_code: Mapped[str] = mapped_column(String(64), nullable=False)
    # Текст ЗАМОРОЖЕН вместе с суммой: категорию основных средств переименуют, и старый снимок
    # должен читаться так, как его подписали, а не так, как называется сегодня.
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    block: Mapped[str] = mapped_column(String(16), nullable=False)
    group_code: Mapped[str] = mapped_column(String(64), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    sign: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    # memo не входит в итог блока: величина уже посчитана внутри другой строки.
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="source")

    # NULL означает «величина неизвестна», и это не то же самое, что ноль.
    amount: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="ok")
    source_kind: Mapped[str | None] = mapped_column(String(48), nullable=True)
    source_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Расшифровка замораживается вместе с итогом: без неё сумма сходится, а состав через два
    # месяца оказывается другим — и доказать подмену нечем.
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BalanceSnapshotFlag(Base):
    """Сигнал о неполноте данных, замороженный вместе со снимком.

    Заморожен намеренно: объём приблизительных величин, посчитанный на 31 августа в октябре,
    будет другим — и подпись «сошлось в пределах неточности» задним числом окажется без
    основания. Форма повторяет сигналы готовности месяца, чтобы интерфейс рисовал их одинаково.
    """

    __tablename__ = "balance_snapshot_flag"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "code", name="uq_balance_snapshot_flag"),
        CheckConstraint(
            "severity in ('blocking', 'warning', 'info')", name="ck_balance_snapshot_flag_severity"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("balance_snapshot.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    amount: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False, default=Decimal("0"))
    rows: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BalanceManualValue(Base):
    """Значение строки, у которой источника нет, — введённое человеком.

    ``reason`` обязателен: цифра без основания в балансе неотличима от опечатки, а через месяц
    вспомнить, откуда она взялась, будет не у кого. Хранится ОТДЕЛЬНО от снимка, по дате: снимок
    пересобирается сколько угодно, а введённое человеком переживает пересборку.
    """

    __tablename__ = "balance_manual_value"
    __table_args__ = (UniqueConstraint("as_of", "line_code", name="uq_balance_manual_value_slot"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    line_code: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source_document: Mapped[str | None] = mapped_column(Text, nullable=True)
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
