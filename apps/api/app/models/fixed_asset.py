"""Учёт основных средств: карточка, категории (СПИ), движения, амортизация.

Методология владельца (см. решения 2026-05-24/25, 2026-07-19 и 2026-07-30):
* порог признания ОС — 10 000 ₽ включительно; применяется к покупкам, а не к первичной
  загрузке реестра инвентаризации 2026 (та ставится на баланс целиком);
* амортизация — ЕДИНЫЙ линейный помесячный метод для всех категорий, СПИ задаётся категорией и
  может переопределяться в карточке;
* старт амортизации — с месяца ввода в эксплуатацию (объект «куплен в резерв» начинает позже);
* первоначальная стоимость: для объектов ИНВЕНТАРИЗАЦИИ — РЫНОЧНАЯ оценка на дату инвентаризации
  (единая база, историю платежей для них не восстанавливаем), для новых покупок — фактическая
  сумма платежа ДДС (``valuation_basis``);
* ремонт vs модернизация — по доле от первоначальной стоимости: <15% расход периода, >15%
  капитализация, ровно 15% уходит владельцу на решение;
* продажа ОС не даёт финрезультата в P&L — это перевод внеоборотного актива в деньги.
"""

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
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Порог признания основным средством: дешевле — расход периода, не карточка. Граница
# ВКЛЮЧИТЕЛЬНАЯ: ровно 10 000 ₽ — уже основное средство (решение владельца 2026-07-30).
# Значение по умолчанию: рабочий источник правды — настройка
# ``fixed_assets.capitalization_threshold_rub``, её владелец правит на странице «Настройки».
# Константа отвечает только за случай, когда настройки нет (тесты, пустая база).
FIXED_ASSET_THRESHOLD = Decimal("10000.00")
# Доля от первоначальной стоимости, разделяющая ремонт и модернизацию.
UPGRADE_SHARE_THRESHOLD = Decimal("0.15")


class AssetCategory(Base):
    """Категория ОС — задаёт СПИ по умолчанию для карточек внутри неё."""

    __tablename__ = "asset_category"
    __table_args__ = (
        UniqueConstraint("name", name="uq_asset_category_name"),
        CheckConstraint("useful_life_months > 0", name="ck_asset_category_life_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    useful_life_months: Mapped[int] = mapped_column(Integer, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class FixedAsset(Base):
    """Карточка основного средства.

    ``initial_cost`` — база амортизации и знаменатель правила 15%. При модернизации она растёт
    (капитализация), при ремонте — нет.
    """

    __tablename__ = "fixed_asset"
    __table_args__ = (
        CheckConstraint("initial_cost >= 0", name="ck_fixed_asset_cost_non_negative"),
        CheckConstraint(
            "useful_life_months IS NULL OR useful_life_months > 0",
            name="ck_fixed_asset_life_positive",
        ),
        CheckConstraint(
            "status IN ('in_use','in_storage','not_working','disposed','sold')",
            name="ck_fixed_asset_status",
        ),
        CheckConstraint(
            "valuation_basis IN ('market','payment')", name="ck_fixed_asset_valuation_basis"
        ),
        CheckConstraint(
            "review_status IN ('ok','requires_owner_review')", name="ck_fixed_asset_review_status"
        ),
        Index("ix_fixed_asset_status", "status"),
        Index("ix_fixed_asset_review", "review_status"),
        Index("ix_fixed_asset_category", "category_id"),
        Index("ix_fixed_asset_location", "location_id"),
        # Частичный: карточка без номера допустима, две карточки с одним номером — нет.
        # Генератор номеров читает максимум и прибавляет единицу, а такая схема не защищает
        # от гонки — индекс превращает её в честную ошибку вставки вместо тихого дубля.
        Index(
            "uq_fixed_asset_inventory_number",
            "inventory_number",
            unique=True,
            postgresql_where=text("inventory_number IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Опознание объекта при следующем обходе: в реестре инвентаризации заполнено у 76 позиций.
    brand_model: Mapped[str | None] = mapped_column(String(160), nullable=True)
    inventory_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("asset_category.id", ondelete="SET NULL"), nullable=True
    )
    initial_cost: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    # 'market'  — рыночная оценка на дату инвентаризации (legacy-объекты);
    # 'payment' — фактическая сумма платежа ДДС (покупки через контур «Покупка ОС»).
    valuation_basis: Mapped[str] = mapped_column(String(16), nullable=False, default="payment")
    valued_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Дата ввода в эксплуатацию: с ЭТОГО месяца начинает начисляться амортизация. NULL —
    # объект куплен в резерв и ещё не введён, амортизация не идёт.
    commissioned_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Переопределяет СПИ категории, если задан.
    useful_life_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="in_use")
    # Текст источника («Склад (гараж)») — сохраняется как есть, чтобы не терять формулировки
    # старых описей; ссылка на реестр помещений идёт рядом и служит осью аналитики «где»
    # (та же, по которой размечены проводки ДДС).
    location: Mapped[str | None] = mapped_column(String(160), nullable=True)
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("location.id", ondelete="SET NULL"), nullable=True
    )
    # След до описи и фотографий: «Черникова №33», «Склад №12». Без него после заливки нельзя
    # доказать, откуда взялась цифра в балансе.
    source_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Групповые legacy-строки (qty>1), нулевая стоимость, неясная принадлежность — владельцу.
    review_status: Mapped[str] = mapped_column(String(24), nullable=False, default="ok")
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AssetMovement(Base):
    """Событие жизненного цикла объекта: ввод, перемещение, модернизация, списание, продажа."""

    __tablename__ = "asset_movement"
    __table_args__ = (
        CheckConstraint(
            "movement_type IN ('commission','transfer','upgrade','writeoff','sale')",
            name="ck_asset_movement_type",
        ),
        CheckConstraint("amount IS NULL OR amount >= 0", name="ck_asset_movement_amount"),
        Index("ix_asset_movement_asset", "asset_id", "occurred_on"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("fixed_asset.id", ondelete="CASCADE"), nullable=False
    )
    movement_type: Mapped[str] = mapped_column(String(24), nullable=False)
    occurred_on: Mapped[date] = mapped_column(Date, nullable=False)
    # Сумма модернизации (капитализируется) или продажи; для перемещения/ввода не нужна.
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AssetCashflowLink(Base):
    """Связь объекта ОС с денежным фактом ДДС.

    Отдельной таблицей, а не колонкой в проводке: один платёж может купить НЕСКОЛЬКО объектов
    (три одинаковых стеллажа), и наоборот — объект может обрастать расходами (ремонт, потом
    модернизация). ``kind`` отвечает на вопрос владельца «что именно ремонтировалось»: без этой
    связи не собрать историю объекта и не применить правило 15%.
    """

    __tablename__ = "asset_cashflow_link"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('purchase','repair','upgrade','sale')", name="ck_asset_cashflow_link_kind"
        ),
        CheckConstraint("amount >= 0", name="ck_asset_cashflow_link_amount"),
        UniqueConstraint(
            "asset_id", "cashflow_transaction_id", "kind", name="uq_asset_cashflow_link"
        ),
        Index("ix_asset_cashflow_link_tx", "cashflow_transaction_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("fixed_asset.id", ondelete="CASCADE"), nullable=False
    )
    cashflow_transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cashflow_transactions.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DepreciationEntry(Base):
    """Помесячное начисление амортизации по объекту.

    Одна строка на объект и месяц (``period_month`` — первое число месяца). Идемпотентность
    закрытия месяца держится уникальным индексом: повторный прогон не задваивает начисление.

    Коррекция — ПРАВКА строки, а не сторнирующая проводка: вторую строку за месяц не пускает
    уникальный индекс, а отрицательную сумму — ограничение. Ломать их дороже, чем пользы:
    именно они делают безопасным повторный прогон ночного закрытия месяца. После правки
    ``residual_after`` всех последующих месяцев объекта пересчитывается
    (``correct_depreciation``) — иначе хранимый остаток превращается во враньё.
    """

    __tablename__ = "depreciation_entry"
    __table_args__ = (
        UniqueConstraint("asset_id", "period_month", name="uq_depreciation_asset_month"),
        CheckConstraint("amount >= 0", name="ck_depreciation_amount_non_negative"),
        Index("ix_depreciation_period", "period_month"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("fixed_asset.id", ondelete="CASCADE"), nullable=False
    )
    period_month: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    # Остаточная стоимость ПОСЛЕ этого начисления — чтобы баланс не пересчитывал всю историю.
    residual_after: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    # Сумму поправил человек. Без этого признака нельзя отличить ошибку расчёта от осознанной
    # правки владельца — а значит нельзя безопасно перезапустить закрытие месяца.
    is_manual: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    corrected_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    corrected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
