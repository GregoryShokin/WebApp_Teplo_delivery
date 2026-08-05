"""Справочники ОПиУ (P&L) — строки отчёта и правила, откуда берётся каждая цифра.

ЗАЧЕМ ОТДЕЛЬНЫЕ ТАБЛИЦЫ, А НЕ КОНСТАНТЫ В КОДЕ. Каскад ОПиУ — не техническая деталь, а
управленческий документ владельца: строки заводятся и переразмечаются по мере того, как
контрагенты переезжают из кассы в признание, а статьи ДДС появляются новыми миграциями.
Держать это в коде значит требовать деплой на каждую правку разметки.

ЧЕГО ЗДЕСЬ НЕТ И НЕ БУДЕТ — таблицы с суммами отчёта. ОПиУ собирается проектором при каждом
запросе из первичных контуров (признание, ДДС, payroll, ОС, налоги, ревизии, зеркало iiko).
Поэтому правка задним числом — перенос периода услуги, дефинализация ведомости,
переклассификация проводки, отмена ревизии — видна немедленно, и класса «тихо устаревшая
витрина» не существует по построению. Снимок появится отдельной сущностью только вместе с
заморозкой месяца, и он будет фиксацией закрытого периода, а не кэшем.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
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
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PnlLine(Base):
    """Строка ОПиУ: 53 статьи-источника эталона + 20 расчётных + служебные приёмники."""

    __tablename__ = "pnl_line"
    __table_args__ = (
        CheckConstraint(
            "kind in ('source', 'memo', 'subtotal', 'ratio', 'stat', 'service')",
            name="ck_pnl_line_kind",
        ),
        CheckConstraint(
            "status in ('active', 'not_used', 'historic')",
            name="ck_pnl_line_status",
        ),
        CheckConstraint(
            "month_basis in ('document', 'cash', 'calendar')",
            name="ck_pnl_line_month_basis",
        ),
        CheckConstraint("sign_role in (-1, 1)", name="ck_pnl_line_sign_role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Стабильный slug — по нему живут формулы агрегатов, правила источников, снимок и фронт.
    # Переименование строки в эталоне не должно ломать разметку, поэтому ключ не название.
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # revenue / variable / direct_fixed / overhead / admin / commercial /
    # below_income / below_expense / stat / service
    block: Mapped[str] = mapped_column(String(32), nullable=False)
    # source   — цифру даёт источник, строка входит в подытог блока;
    # memo     — справочная: показывается, но в каскад НЕ входит. Такова «Выручка без учёта
    #            скидок»: выручкой отчёта служит строка со скидками, а валовая — база для
    #            налоговой модели и сверок. Сложить обе значило бы задвоить выручку;
    # subtotal — считается формулой (Маржинальный доход, Валовая прибыль, EBITDA, Чистая прибыль);
    # ratio    — рентабельность в процентах;
    # stat     — блок «Показатели для статистики» (ФОТ по группам, численность);
    # service  — служебный приёмник, в каскад не входит (см. сид: вне ОПиУ / не разнесено).
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # Декларативная формула для subtotal/ratio: {"op": "sub", "args": [...]}. Не eval —
    # четыре операции над кодами строк, разбираются собственным парсером. Хранится в базе,
    # потому что состав блока меняется вместе с разметкой статей, а не вместе с кодом.
    #
    # СОГЛАШЕНИЕ О ЗНАКАХ, без которого формулы читаются двусмысленно: подытог расходного
    # блока хранит ПОЛОЖИТЕЛЬНУЮ величину расхода, а каскад вычитает её явным ``sub``.
    # ``{"op": "sum", "block": X}`` складывает величины активных строк блока КАК ЕСТЬ, не
    # применяя ``sign_role``: тогда компенсирующая величина (излишек ревизии, возврат) сама
    # уменьшает сумму блока, а ``sign_role`` остаётся тем, чем должен быть, — признаком
    # «доход или расход» для отображения, а не множителем в арифметике. Строки ``memo`` в
    # сумму блока не входят: «Выручка без учёта скидок» справочная, сложить её с выручкой
    # со скидками значило бы задвоить продажи.
    formula: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Шаг 10 — чтобы новую строку можно было вставить между существующими без перенумерации.
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    level: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    # Как КАСКАД использует строку: +1 доход, -1 расход. Знак ВНУТРИ строки это поле не трогает:
    # расход печатается положительным, а компенсация (возврат, излишек ревизии) — отрицательной.
    sign_role: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # document — месяц из периода услуги в документе (методология: основание месяца — период,
    #            а не дата письма и не только дата платежа);
    # cash     — месяц по дате денег, документа не ждут вовсе (эквайринг, РКО, комиссии банка);
    # calendar — источник сам знает свой месяц (payroll, амортизация, зеркало iiko).
    month_basis: Mapped[str] = mapped_column(String(16), nullable=False)
    # Строка имеет смысл в разрезе Роллы/Пицца/Горячий цех/Бар. В эталоне таких всего четыре:
    # выручка, фудкост, ЗП поваров и коробки для пиццы — остальное живёт в «Общем».
    direction_aware: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Нормативный коридор шаблона «Нескучных финансов»: маржинальность 0.60–0.70, рентабельность
    # по валовой 0.12–0.25, по операционной 0.03–0.07. В справочнике, а не в коде, — это
    # управленческая настройка владельца, она меняется вместе с моделью бизнеса.
    norm_min: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), nullable=True)
    norm_max: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), nullable=True)
    # not_used — строка обязана быть в справочнике ради сходимости с таблицей владельца, но
    # никогда не считается: пять строк закрытой точки «Гагарина» и «Печатные материалы».
    # На экране это бледный бейдж «не используется», а НЕ ноль: пустое и ноль в управленческом
    # учёте разные вещи, и подменять одно другим — врать.
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default=text("'active'")
    )
    # Человеческое «откуда берётся» — в тултип строки и в шапку drill-down.
    source_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class PnlArticleRule(Base):
    """Статья ДДС → строка ОПиУ. Одно правило обслуживает и кассу, и признание.

    Почему одна таблица на два слоя: признанный расход по статье — это та же строка отчёта, что
    и оплата по ней. Разводить их значило бы завести два места, которые обязаны совпадать.

    ``in_pnl = false`` — статья НИКОГДА не расход и не доход ОПиУ: переводы между кошельками,
    выдача займов и депозитов, дивиденды, а также то, чей факт принадлежит другому слою
    (оплата поставщикам приходит фудкостом из iiko, зарплатные статьи — из ведомости).
    Именно этот флаг делает уравнение сходимости замкнутым: каждый рубль оттока получает
    ровно один ярлык, и «не разнесено» обязано быть нулём.
    """

    __tablename__ = "pnl_article_rule"
    __table_args__ = (
        CheckConstraint("sign in (-1, 1)", name="ck_pnl_article_rule_sign"),
        CheckConstraint(
            "applies_to in ('both', 'cash_only', 'accrual_only')",
            name="ck_pnl_article_rule_applies_to",
        ),
        # Строка ОПиУ обязательна ровно тогда, когда статья участвует в отчёте. Иначе
        # «участвует, но некуда» тихо превратилось бы в потерю.
        CheckConstraint(
            "(in_pnl = false and line_code is null) or (in_pnl = true and line_code is not null)",
            name="ck_pnl_article_rule_line_presence",
        ),
        Index(
            "uq_pnl_article_rule_active",
            "article_id",
            "applies_to",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    article_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dds_articles.id", ondelete="CASCADE"), nullable=False
    )
    line_code: Mapped[str | None] = mapped_column(
        ForeignKey("pnl_line.code", ondelete="RESTRICT"), nullable=True
    )
    in_pnl: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Кто владеет фактом вместо этой статьи: iiko / payroll / taxes / transfer / financing.
    # Печатается человеку в панели сходимости — чтобы «вне ОПиУ» не выглядело как «потеряли».
    owner_stream: Mapped[str | None] = mapped_column(String(24), nullable=True)
    sign: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    applies_to: Mapped[str] = mapped_column(
        String(16), nullable=False, default="both", server_default=text("'both'")
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PnlSourceRule(Base):
    """Неартикульные источники: payroll, iiko, ОС, налоги, ревизии, ручной ввод.

    ``component`` существует потому, что строки эталона бывают составными и это не наша
    выдумка, а методология: «Результаты ревизии» = результат инвентаризаций МИНУС штрафы по
    ревизиям; «Содержание торговых точек» = касса ПЛЮС закупка расходников из накладных;
    «Телекоммуникации» = Mango вручную ПЛЮС два фиксированных провайдера.

    ``sign_policy`` отдельно от ``sign``, потому что политик знака ровно три и единой быть не
    может. ``invert`` — это «(-1) *» из методологии для продуктовой ревизии: в коде недостача
    отрицательна, а в ОПиУ она положительный расход. У соседних строк — инвентаризации упаковки
    и коробок для пиццы — политика ДРУГАЯ, ``as_is``: там знак iiko сохраняется как есть.
    Перепутать их значит ошибиться на двойную величину недостачи.
    """

    __tablename__ = "pnl_source_rule"
    __table_args__ = (
        CheckConstraint(
            "stream in ('payroll', 'iiko', 'fixed_assets', 'taxes', 'inventory', "
            "'acquiring', 'manual')",
            name="ck_pnl_source_rule_stream",
        ),
        CheckConstraint(
            "sign_policy in ('as_is', 'invert', 'expense_positive')",
            name="ck_pnl_source_rule_sign_policy",
        ),
        CheckConstraint("sign in (-1, 1)", name="ck_pnl_source_rule_sign"),
        # Уникальность по хэшу селектора, а не по самому jsonb: NULL-семантика в составном
        # индексе рассыпается, а md5 текста даёт честное сравнение.
        Index(
            "uq_pnl_source_rule_active",
            "line_code",
            "stream",
            "component",
            func.md5(func.coalesce(text("selector::text"), "")),
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    line_code: Mapped[str] = mapped_column(
        ForeignKey("pnl_line.code", ondelete="CASCADE"), nullable=False
    )
    stream: Mapped[str] = mapped_column(String(24), nullable=False)
    component: Mapped[str] = mapped_column(
        String(32), nullable=False, default="main", server_default=text("'main'")
    )
    # Ключ внутри потока: роли и поля payroll, метрика зеркала iiko, корзина whitelist,
    # источник ревизии. Схема у каждого потока своя — она описана в его адаптере.
    selector: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    sign: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    sign_policy: Mapped[str] = mapped_column(
        String(16), nullable=False, default="as_is", server_default=text("'as_is'")
    )
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("100"))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PnlCashOrigin(Base):
    """Классификатор ``source_kind`` проводки: чей факт уже посчитан другим слоем.

    Почему таблица, а не список в коде: новый платёжный контур добавляет свой ``source_kind``,
    и молчаливое попадание его проводок в ОПиУ — это задвоение с payroll или депозитами.
    Тест-сторож требует, чтобы каждый ``source_kind``, встреченный в ``cashflow_transactions``,
    был здесь классифицирован; новый контур роняет тест, а не портит отчёт.

    ВАЖНО: исключение задаётся ПАРОЙ «механизм + статья», а не одним механизмом. Выплата из
    Сейфа — универсальный механизм выдачи, через него идут и зарплата, и наличные траты
    администраторов на содержание точек и питание персонала. Исключить механизм целиком значит
    выбросить из отчёта реальный операционный расход (в июле 2026 это 63 785,71 ₽ по двум
    статьям). ``article_id IS NULL`` означает «весь механизм», конкретная статья — точечное
    исключение.
    """

    __tablename__ = "pnl_cash_origin"
    __table_args__ = (
        Index(
            "uq_pnl_cash_origin_kind_article",
            "source_kind",
            func.coalesce(text("article_id::text"), "*"),
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    article_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dds_articles.id", ondelete="CASCADE"), nullable=True
    )
    # Кто владеет фактом: payroll / deposits / taxes / transfer / supplier.
    # NULL — механизм не исключается, проводка идёт в ОПиУ обычным порядком.
    owner_stream: Mapped[str | None] = mapped_column(String(24), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PnlManualEntry(Base):
    """Ручное число строки за месяц — первичка, а не правка отчёта.

    Шесть строк эталона не заполняются автоматически ничем и никогда: телефония Mango (личный
    кабинет за двойным барьером — SPA-логин и капча), комиссия агрегатора «К порогу» (сумму
    присылают менеджеры), бюджет контекстной рекламы из Biz Panel, прочие доходы, проценты по
    кредитам. Без этой таблицы такие строки остаются пустыми навсегда, и ОПиУ никогда не
    сойдётся с таблицей владельца.

    ``reason`` обязателен — по образцу отмены признания расхода: ручное число в управленческом
    отчёте без объяснения через месяц неотличимо от опечатки.
    """

    __tablename__ = "pnl_manual_entry"
    __table_args__ = (
        CheckConstraint("sign in (-1, 1)", name="ck_pnl_manual_entry_sign"),
        Index(
            "uq_pnl_manual_entry_slot",
            "period_month",
            "line_code",
            "component",
            "direction",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Всегда первое число месяца — приводится на входе, чтобы уникальность работала.
    period_month: Mapped[date] = mapped_column(Date, nullable=False)
    line_code: Mapped[str] = mapped_column(
        ForeignKey("pnl_line.code", ondelete="RESTRICT"), nullable=False
    )
    component: Mapped[str] = mapped_column(
        String(32), nullable=False, default="main", server_default=text("'main'")
    )
    # 'total' — без разреза; иначе Роллы/Пицца/Горячий цех/Бар. Заложено сразу, чтобы
    # включение разреза не требовало миграции данных.
    direction: Mapped[str] = mapped_column(
        String(16), nullable=False, default="total", server_default=text("'total'")
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    sign: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class PnlIikoFact(Base):
    """Месячный факт из iiko: выручка, фудкост, зарплата курьеров.

    Единственное место проектора, где данные ХРАНЯТСЯ. Причина не в скорости расчёта, а в
    доступности источника: iiko Server отвечает только с прод-адреса, отвечает секундами и
    ограничивает частоту. Ходить в него при каждом открытии отчёта — значит поставить
    страницу в зависимость от внешней системы.

    ``previous_amount`` хранит прошлое значение при перезаливке месяца. Выручка закрытого
    месяца меняться не должна; если изменилась — это видно, а не подменяется молча.
    """

    __tablename__ = "pnl_iiko_fact"
    __table_args__ = (
        Index(
            "uq_pnl_iiko_fact_slot",
            "period_month",
            "metric_code",
            "direction",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    period_month: Mapped[date] = mapped_column(Date, nullable=False)
    metric_code: Mapped[str] = mapped_column(String(32), nullable=False)
    # rolls / pizza / hot_shop / bar либо total.
    direction: Mapped[str] = mapped_column(
        String(16), nullable=False, default="total", server_default=text("'total'")
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    rows_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    previous_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PnlPartnerCommissionRule(Base):
    """Ставка партнёра и сохранённый OLAP-пресет, из которого берётся его выручка.

    Ставка хранится отдельно от факта: партнёров станет больше, договорный процент может
    измениться, а уже закрытый месяц обязан сохранить ставку, по которой был рассчитан.
    ``partner_key`` — нормализованное имя клиента из ``Delivery.CustomerName``; исходное имя
    остаётся в ``partner_name`` для понятного отображения владельцу.
    """

    __tablename__ = "pnl_partner_commission_rule"
    __table_args__ = (
        CheckConstraint(
            "commission_rate >= 0 and commission_rate <= 1",
            name="ck_pnl_partner_commission_rule_rate",
        ),
        Index("uq_pnl_partner_commission_rule_key", "partner_key", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    partner_key: Mapped[str] = mapped_column(String(255), nullable=False)
    partner_name: Mapped[str] = mapped_column(String(255), nullable=False)
    commission_rate: Mapped[Decimal] = mapped_column(Numeric(7, 6), nullable=False)
    source_preset_id: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class PnlPartnerCommissionFact(Base):
    """Месячный расчёт комиссии: выручка партнёра × ставка договора.

    Это детализация агрегата ``PnlIikoFact(metric_code='partner_commission')``. В ней
    намеренно снимком лежат и имя, и ставка: изменение карточки партнёра не переписывает
    объяснение уже рассчитанного месяца.
    """

    __tablename__ = "pnl_partner_commission_fact"
    __table_args__ = (
        CheckConstraint(
            "commission_rate >= 0 and commission_rate <= 1",
            name="ck_pnl_partner_commission_fact_rate",
        ),
        Index(
            "uq_pnl_partner_commission_fact_slot",
            "period_month",
            "rule_id",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    period_month: Mapped[date] = mapped_column(Date, nullable=False)
    rule_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pnl_partner_commission_rule.id", ondelete="RESTRICT"), nullable=False
    )
    partner_key: Mapped[str] = mapped_column(String(255), nullable=False)
    partner_name: Mapped[str] = mapped_column(String(255), nullable=False)
    revenue_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    commission_rate: Mapped[Decimal] = mapped_column(Numeric(7, 6), nullable=False)
    commission_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    rows_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PnlIikoGoodsFact(Base):
    """Вклад одной номенклатуры whitelist в месячную товарную строку ОПиУ.

    ``PnlIikoFact`` хранит контрольный итог корзины, а эта таблица — объяснение итога.
    Данные сохраняются в тот же момент синхронизации: повторно ходить в iiko при открытии
    вкладки нельзя, потому что Server доступен только с прод-адреса и ограничивает частоту.
    """

    __tablename__ = "pnl_iiko_goods_fact"
    __table_args__ = (
        CheckConstraint(
            "source_kind in ('inventory', 'incoming_invoice')",
            name="ck_pnl_iiko_goods_fact_source_kind",
        ),
        Index(
            "uq_pnl_iiko_goods_fact_slot",
            "period_month",
            "metric_code",
            "source_kind",
            "iiko_product_guid",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    period_month: Mapped[date] = mapped_column(Date, nullable=False)
    metric_code: Mapped[str] = mapped_column(String(32), nullable=False)
    line_code: Mapped[str] = mapped_column(
        ForeignKey("pnl_line.code", ondelete="RESTRICT"), nullable=False
    )
    source_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    iiko_product_guid: Mapped[str] = mapped_column(String(64), nullable=False)
    product_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Знак хранится ровно как в iiko. В расходное соглашение он переводится при чтении,
    # тем же правилом, которым проектор переводит контрольный итог корзины.
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    rows_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PnlIikoStockFact(Base):
    """Месячный roll-forward складского товара из двух снимков iiko.

    Таблица хранит сырой мост для ОПиУ и будущего Баланса: начальный остаток, закупки,
    конечный остаток и вычисленный фактический расход. Внутренние перемещения между
    складами взаимно погашаются, потому что снимки агрегируются по всем складам бизнеса.
    """

    __tablename__ = "pnl_iiko_stock_fact"
    __table_args__ = (
        Index(
            "uq_pnl_iiko_stock_fact_slot",
            "period_month",
            "iiko_product_guid",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    period_month: Mapped[date] = mapped_column(Date, nullable=False)
    iiko_product_guid: Mapped[str] = mapped_column(String(64), nullable=False)
    product_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    product_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    opening_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    opening_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    receipts_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    closing_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    closing_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    consumption_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    stores_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PnlWorkupReview(Base):
    """Вопрос человеку: «этот товар брали на проработку?» — и след ответа.

    ЗАЧЕМ ОЧЕРЕДЬ, А НЕ ФЛАГ У НАКЛАДНОЙ. Проработкой бывает ОДНА позиция из десяти, а
    решение принимается по товару и месяцу — ровно так его хранит ``PnlProductMonthlyDecision``.
    Флаг на документе не смог бы ответить, про какую его строку идёт речь, и повторное
    подтверждение накладной пришлось бы разбирать заново.

    ЗАЧЕМ СЛЕД АКТА (``writeoff_document_id``). Cloud ``create`` НЕ идемпотентен: каждый вызов
    плодит новый документ в боевой iiko. Единственный гейт повтора — на нашей стороне, и это
    он: пока id заполнен, второй акт не создаётся ни ретраем, ни двойным кликом.

    ``writeoff_error`` держит текст отказа iiko. Ошибка внешней системы не должна ронять
    ответ пользователю и не должна молча теряться — иначе товар останется без акта, а расход
    (он признаётся именно актом) не попадёт в прибыль вовсе.
    """

    __tablename__ = "pnl_workup_review"
    __table_args__ = (
        CheckConstraint(
            "status in ('pending', 'confirmed', 'rejected')",
            name="ck_pnl_workup_review_status",
        ),
        Index(
            "uq_pnl_workup_review_slot",
            "period_month",
            "iiko_product_guid",
            unique=True,
        ),
        Index("ix_pnl_workup_review_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    period_month: Mapped[date] = mapped_column(Date, nullable=False)
    iiko_product_guid: Mapped[str] = mapped_column(String(64), nullable=False)
    product_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Накладная, которой товар пришёл. Опциональна: связь ищется по номенклатуре, и у
    #: товара из iiko-выгрузки нашей накладной может не быть вовсе.
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("supplier_invoice.id", ondelete="SET NULL"), nullable=True
    )
    purchase_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default=text("'pending'")
    )
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: GUID акта списания в iiko. Заполнен — акт уже создан, создавать заново нельзя.
    writeoff_document_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    writeoff_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Проведён ли акт. Создание и проведение — два разных вызова, и упасть может второй:
    #: документ уже существует, но лежит в NEW и в расход не идёт. Один ``document_id`` не
    #: различает «готово» и «сделана половина», поэтому признак отдельный.
    writeoff_posted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    writeoff_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class PnlIikoWriteoffFact(Base):
    """Номенклатура проведённых актов списания за месяц.

    ЗАЧЕМ ХРАНИТЬ ПОИМЁННО, РАЗ СТРОКА ОПиУ — ОДНА СУММА. Потому что без имён невозможно
    поймать задвоение. Товар на проработку приходит накладной и признаётся расходом строкой
    «Проработка»; если управляющий потом списывает его же актом, тот же расход встаёт вторым
    разом в «Списание продукции и сырья». Пока таблица считала один итог, такое задвоение не
    было видно ни в отчёте, ни в расшифровке — оно просто увеличивало расход.

    Здесь нет ``source_kind``: у списаний источник ровно один — ``/v2/documents/writeoff``,
    только документы в статусе PROCESSED. Черновик акта списанием не является: товар ещё
    физически на месте.
    """

    __tablename__ = "pnl_iiko_writeoff_fact"
    __table_args__ = (
        Index(
            "uq_pnl_iiko_writeoff_fact_slot",
            "period_month",
            "iiko_product_guid",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    period_month: Mapped[date] = mapped_column(Date, nullable=False)
    iiko_product_guid: Mapped[str] = mapped_column(String(64), nullable=False)
    product_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    product_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Себестоимость списанного — поле ``cost`` позиции акта, ровно то, что суммирует строка.
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    #: Сколько строк актов сложилось в эту сумму: один товар списывают несколькими актами.
    rows_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PnlIikoProductObservation(Base):
    """Вся номенклатура, встреченная в товарных источниках iiko за месяц.

    Детализация ОПиУ хранит только уже размеченные товары. Этот реестр намеренно шире:
    он сохраняет и неизвестные GUID, чтобы они не исчезали из расчёта молча, а попадали
    в очередь разметки вместе с суммой и источником.
    """

    __tablename__ = "pnl_iiko_product_observation"
    __table_args__ = (
        CheckConstraint(
            "source_kind in ('inventory', 'incoming_invoice')",
            name="ck_pnl_iiko_product_observation_source_kind",
        ),
        Index(
            "uq_pnl_iiko_product_observation_slot",
            "period_month",
            "source_kind",
            "iiko_product_guid",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    period_month: Mapped[date] = mapped_column(Date, nullable=False)
    source_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    iiko_product_guid: Mapped[str] = mapped_column(String(64), nullable=False)
    product_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    product_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Для складского источника это вычисленный фактический расход (начало + приход − конец),
    # для накладных — стоимость закупки.
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    rows_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PnlProductWhitelist(Base):
    """Товар iiko → товарная строка ОПиУ.

    Отбор идёт по конкретным идентификаторам, а не по словам в названии, и это не
    перестраховка: проверка на марте 2026 показала, что поиск по слову «вакуум» даёт
    2 352 ₽ против 1 658 ₽ контрольной суммы — в выдачу попадает посторонний товар.
    """

    __tablename__ = "pnl_product_whitelist"
    __table_args__ = (
        CheckConstraint(
            "include_status in ('include', 'requires_owner_review', 'exclude', 'stocked')",
            name="ck_pnl_product_whitelist_status",
        ),
        CheckConstraint(
            "source_kind in ('inventory', 'incoming_invoice')",
            name="ck_pnl_product_whitelist_source_kind",
        ),
        Index(
            "uq_pnl_product_whitelist_guid_source",
            "iiko_product_guid",
            "source_kind",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    iiko_product_guid: Mapped[str] = mapped_column(String(64), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    line_code: Mapped[str | None] = mapped_column(
        ForeignKey("pnl_line.code", ondelete="RESTRICT"), nullable=True
    )
    # requires_owner_review — решение ещё нужно владельцу; stocked — товар учитывается
    # через складской roll-forward (начало + приходы − конец), а не как прямой расход
    # накладной. Оба статуса в прямые товарные расходы не входят.
    include_status: Mapped[str] = mapped_column(String(24), nullable=False)
    product_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    product_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class PnlProductMonthlyDecision(Base):
    """Временное решение по неизвестному товару только для одного месяца.

    ``workup`` позволяет не занижать расход открытого периода, но намеренно не становится
    whitelist-правилом. В следующем месяце того же GUID в этой таблице ещё нет, поэтому он
    снова возвращается владельцу на разметку.
    """

    __tablename__ = "pnl_product_monthly_decision"
    __table_args__ = (
        CheckConstraint(
            "source_kind in ('inventory', 'incoming_invoice')",
            name="ck_pnl_product_monthly_decision_source_kind",
        ),
        CheckConstraint(
            "decision_kind = 'workup'",
            name="ck_pnl_product_monthly_decision_kind",
        ),
        Index(
            "uq_pnl_product_monthly_decision_slot",
            "period_month",
            "iiko_product_guid",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    period_month: Mapped[date] = mapped_column(Date, nullable=False)
    iiko_product_guid: Mapped[str] = mapped_column(String(64), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    decision_kind: Mapped[str] = mapped_column(
        String(24), nullable=False, default="workup", server_default=text("'workup'")
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
