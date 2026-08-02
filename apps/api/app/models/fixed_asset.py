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
# Абсолютный пол капитального ремонта: дешевле этой суммы работы не капитализируются, какой бы
# ни вышла доля (решение владельца 2026-07-30). Нужен потому, что доля на дешёвом объекте
# срабатывает от копеек: у стула за 1 200 ₽ ремонт за 300 ₽ — это 25%, и без пола он стал бы
# «модернизацией». Граница ВКЛЮЧИТЕЛЬНАЯ, как и порог признания: ровно 5 000 ₽ капитальный
# ремонт уже возможен.
CAPITAL_REPAIR_FLOOR = Decimal("5000.00")

# Какие поля имеет смысл спрашивать при заведении карточки. Профиль хранится НА КАТЕГОРИИ:
# у рисоварки опознавательный признак — марка и модель, у производственного стола — материал
# и размеры, а марки у него обычно нет. Подробности и причина «флаг на категории, а не список
# имён во фронте» — в миграции ``0229_asset_spec_profile``.
SPEC_PROFILES = ("equipment", "furniture", "other")
# Новым куплен объект или с рук. NULL — неизвестно (карточки описи 2026).
ASSET_CONDITIONS = ("new", "used")
# О чём обращение: поломка у работающего объекта или покупка б/у. См. ``AssetConditionReport``.
CONDITION_REPORT_KINDS = ("purchase", "incident")
# Откуда у бизнеса взялся объект — правая сторона баланса для актива, за который фирма не
# платила. Подробности и цена вопроса — в миграции ``0232_asset_acquisition_source``.
ACQUISITION_SOURCES = ("purchase", "owner_contribution", "owner_loan", "donation")


class AssetCategory(Base):
    """Категория ОС — задаёт СПИ по умолчанию для карточек внутри неё."""

    __tablename__ = "asset_category"
    __table_args__ = (
        UniqueConstraint("name", name="uq_asset_category_name"),
        CheckConstraint("useful_life_months > 0", name="ck_asset_category_life_positive"),
        CheckConstraint(
            "spec_profile IN ('equipment','furniture','other')",
            name="ck_asset_category_spec_profile",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    useful_life_months: Mapped[int] = mapped_column(Integer, nullable=False)
    # Набор полей формы заведения: 'equipment' — марка и модель, 'furniture' — материал и
    # размеры, 'other' — свободная строка характеристик.
    spec_profile: Mapped[str] = mapped_column(
        String(16), nullable=False, default="other", server_default=text("'other'")
    )
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
        CheckConstraint(
            "condition IS NULL OR condition IN ('new','used')", name="ck_fixed_asset_condition"
        ),
        CheckConstraint(
            "acquisition_source IS NULL OR acquisition_source IN "
            "('purchase','owner_contribution','owner_loan','donation')",
            name="ck_fixed_asset_acquisition_source",
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
    # 'new' — куплен новым, 'used' — с рук. NULL — неизвестно: 149 карточек описи 2026 заведены
    # обходом помещений, и чем объект был в момент покупки, не знает никто. Признак нужен
    # оценке: у б/у объекта износ уже есть, а ни сумма платежа, ни срок из категории его не видят.
    condition: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # Откуда объект взялся у бизнеса. 'purchase' — куплен за деньги фирмы (правую сторону
    # баланса не двигает); остальные три означают, что актив вырос, а деньги не тратились, и
    # значит в пассиве обязана появиться встречная запись. NULL — «не указано»: так стоят
    # 149 карточек описи 2026, и разметить их может только владелец.
    acquisition_source: Mapped[str | None] = mapped_column(String(24), nullable=True)
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
    """Событие жизненного цикла объекта: ввод, перемещение, модернизация, списание, продажа.

    ВЫБЫТИЕ (``movement_type='writeoff'``) — первый и пока единственный вид движения, у
    которого есть код. Таблица заведена вместе с модулем, но до 2026-08-02 в неё никто не
    писал; заводить рядом вторую «таблицу списаний» значило бы держать два журнала одной
    жизни объекта. Поэтому списание пишется сюда, а две колонки ниже заполняются ТОЛЬКО у
    него — у перемещения и ввода их смысла нет.

    Строка выбытия — исторический документ и основание строки ОПиУ «УчОС Убыток от выбытия».
    Сумму убытка она хранит СВОЮ, а не пересчитывает от карточки: карточку правят (коррекция
    начисления, переоценка), и пересчитанный задним числом убыток тихо разошёлся бы с тем,
    что уже перенесли в отчётность.
    """

    __tablename__ = "asset_movement"
    __table_args__ = (
        CheckConstraint(
            "movement_type IN ('commission','transfer','upgrade','writeoff','sale')",
            name="ck_asset_movement_type",
        ),
        CheckConstraint("amount IS NULL OR amount >= 0", name="ck_asset_movement_amount"),
        Index("ix_asset_movement_asset", "asset_id", "occurred_on"),
        # Выбытие у объекта одно: списать дважды значит дважды показать убыток. Повторное
        # списание должно упереться в базу, а не в проверку, которую забудут при следующей
        # точке входа.
        Index(
            "uq_asset_movement_writeoff",
            "asset_id",
            unique=True,
            postgresql_where=text("movement_type = 'writeoff'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("fixed_asset.id", ondelete="CASCADE"), nullable=False
    )
    movement_type: Mapped[str] = mapped_column(String(24), nullable=False)
    occurred_on: Mapped[date] = mapped_column(Date, nullable=False)
    # Сумма модернизации (капитализируется), продажи или УБЫТКА ОТ ВЫБЫТИЯ — остаточной
    # стоимости, которая ушла с баланса в расход; для перемещения/ввода не нужна.
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ТОЛЬКО У ВЫБЫТИЯ: статус, в котором объект жил до списания. Нужен, чтобы отмена
    # ошибочного списания вернула карточку туда, где она была: «не работает» и «в работе» —
    # разные строки баланса, и восстанавливать всех подряд «в работу» значило бы чинить одну
    # ошибку другой.
    previous_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # ТОЛЬКО У ВЫБЫТИЯ: сообщение о состоянии, из которого выросло решение. Списание по
    # кнопке в карточке ссылки не имеет — там основание только в ``note``.
    condition_report_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("asset_condition_report.id", ondelete="SET NULL"), nullable=True
    )
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


class AssetBalanceSnapshot(Base):
    """Строка баланса по основным средствам, замороженная на конец закрытого месяца.

    Остаточная стоимость выводится из первоначальной, а та меняется ЗАДНИМ ЧИСЛОМ: ручная
    коррекция начисления, применённая переоценка и правка карточки — каждая зовёт
    ``recompute_residuals``, который переписывает остатки по всей истории объекта. Без снимка
    баланс за июль, уже перенесённый в отчётность, в сентябре тихо стал бы другим.

    Строк одиннадцать, а категорий десять: «Не работающее оборудование» — это статус карточки.
    Такой объект уходит в свою строку и исключается из строки своей категории, иначе считался
    бы дважды. Поэтому ключ строки текстовый: у одной из одиннадцати категории нет.
    """

    __tablename__ = "asset_balance_snapshot"
    __table_args__ = (
        UniqueConstraint("period_month", "line_name", name="uq_asset_balance_snapshot_line"),
        Index("ix_asset_balance_snapshot_period", "period_month"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    period_month: Mapped[date] = mapped_column(Date, nullable=False)
    line_name: Mapped[str] = mapped_column(String(160), nullable=False)
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("asset_category.id", ondelete="SET NULL"), nullable=True
    )
    asset_count: Mapped[int] = mapped_column(Integer, nullable=False)
    initial_cost: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    # Накоплено за всё время по эту дату — то, что вычитается из первоначальной.
    accumulated: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    residual: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    # Начислено именно в этом месяце — строка «УчОС Амортизация» в ОПиУ.
    depreciation: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AssetConditionReport(Base):
    """Сообщение менеджера о состоянии объекта и предложение модели по переоценке.

    Отдельной таблицей, а не полем в карточке: ``note`` и ``review_reason`` одиночные, второе
    сообщение затёрло бы первое. А история состояния и есть главная ценность — по ней видно,
    что техника ломается третий раз за полгода и её пора менять, а не чинить.

    Предложение модели хранится как ГИПОТЕЗА, а не факт: с уверенностью, обоснованием, именем
    модели и временем. Без этого следа нельзя ни проверить решение через полгода, ни объяснить,
    почему стоимость упала на сорок тысяч.

    Порога автоприменения НЕТ сознательно: стоимость актива меняет только человек, какой бы
    уверенной модель ни была.

    ДВА РАЗНЫХ РАЗГОВОРА В ОДНОЙ ТАБЛИЦЕ (``kind``). ``incident`` — менеджер пишет, что
    сломалось: вопрос «сколько объект теперь стоит». ``purchase`` — купили б/у: вопрос «сколько
    ему осталось работать». Второй появился потому, что цена б/у объекта износ уже содержит, а
    срок из категории — нет, и скидывать цену ещё раз значило бы посчитать износ дважды.
    """

    __tablename__ = "asset_condition_report"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','proposed','applied','dismissed','failed')",
            name="ck_asset_condition_report_status",
        ),
        CheckConstraint("kind IN ('purchase','incident')", name="ck_asset_condition_report_kind"),
        CheckConstraint(
            "proposed_useful_life_months IS NULL OR proposed_useful_life_months > 0",
            name="ck_asset_condition_report_life_positive",
        ),
        CheckConstraint(
            "proposed_cost IS NULL OR proposed_cost >= 0",
            name="ck_asset_condition_report_cost_non_negative",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_asset_condition_report_confidence",
        ),
        Index("ix_asset_condition_report_asset", "asset_id", "created_at"),
        Index("ix_asset_condition_report_status", "status"),
        # Одна необработанная запись на объект: двойное «Сохранить» не должно дать два
        # параллельных вызова модели по одному поводу.
        Index(
            "uq_asset_condition_report_pending",
            "asset_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("fixed_asset.id", ondelete="CASCADE"), nullable=False
    )
    # Дословно то, что написал менеджер: это и вход модели, и свидетельство для владельца.
    message: Mapped[str] = mapped_column(Text, nullable=False)
    # 'incident' — поломка у работающего объекта, 'purchase' — покупка б/у.
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="incident", server_default=text("'incident'")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # Стоимость на момент обращения — чтобы предложение читалось и через полгода.
    cost_before: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    proposed_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    # Сколько объекту осталось работать. Заполняется только у покупок б/у: у поломки предметом
    # разговора остаётся стоимость.
    proposed_useful_life_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Предложение не переоценить, а СПИСАТЬ: объекта физически нет (украли, утратили,
    # уничтожен). Отдельный флаг, а не «предложенная стоимость = 0», потому что это разные
    # действия. Переоценка в ноль оставляет объект на балансе и переписывает первоначальную
    # стоимость — то есть врёт о том, за сколько его купили. Выбытие убирает объект из
    # внеоборотных активов целиком и показывает остаток убытком.
    proposed_disposal: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    proposed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    reported_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
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
