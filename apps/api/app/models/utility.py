"""Коммунальные услуги помещения: вода, газ, электричество.

ЧЕМ ЭТО ОТЛИЧАЕТСЯ ОТ ОСТАЛЬНЫХ РАСХОДОВ. Счета ресурсоснабжающих организаций выставлены на
АРЕНДОДАТЕЛЯ-физлицо: договор с Водоканалом заключал он, и по документам потребитель — он.
Бизнес возмещает ему затраты (решение владельца 02.08.2026), поэтому долг у нас перед
арендодателем, а квитанция Водоканала — не первичка на нас, а подтверждение суммы.

Отсюда два следствия, которые и определили модель:

* сумма каждый месяц РАЗНАЯ (счётчик), поэтому её нельзя вывести из договора, как арендную
  ставку, — её приносит документ. Ни один существующий генератор начислений на это не годится:
  все они исходят из известной месячной суммы;
* расход по такому документу идёт в управленческий P&L, но не в налоговую базу — первички на
  ИП под ним нет. Признак ``source='utility'`` на закрывающем документе и есть эта отметка.

ЗАЧЕМ ДВЕ ТАБЛИЦЫ. ``UtilityAccount`` — постоянная настройка «за что и кому мы платим»:
помещение × ресурс → арендодатель + статья ДДС. ``UtilityIntake`` — разовое событие «принесли
бумажку за июль»: файл, распознанные поля и то, что из них получилось. Само обязательство
своей таблицы не заводит: как аренда и договор услуги, оно живёт обычным ``SupplierInvoice`` —
иначе долг не попал бы в готовые витрины ДЗ/КЗ и в признание расхода.
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
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Ресурсы, которые учитываем отдельными потоками. Раздельно, а не одной «коммуналкой», потому
# что документы по ним приходят разные и в разные сроки: вода — счётом Водоканала, свет — парой
# актов (факт и аванс), газ — вообще без бумаги, со слов арендодателя.
UTILITY_KINDS = ("water", "gas", "electricity")

UTILITY_KIND_LABELS = {
    "water": "Вода",
    "gas": "Газ",
    "electricity": "Электричество",
}

# Жизнь загруженного документа:
#   new          — файл принят, разбор ещё не делался
#   needs_review — разобрано неуверенно или вовсе не разобрано: нужны глаза и руки человека
#   ready        — поля заполнены и проверены, можно проводить
#   promoted     — проведено: создан закрывающий документ (``invoice_id``)
#   rejected     — не наш документ / дубль / брак съёмки; в учёт не идёт
UTILITY_INTAKE_STATUSES = ("new", "needs_review", "ready", "promoted", "rejected")


class UtilityAccount(Base):
    """Коммунальный поток помещения: за какой ресурс, кому платим и по какой статье.

    Одна строка = «вода на Черниковой». Смена арендодателя правит эту же строку: история долга
    живёт в документах, а не в настройке.

    Статья ДДС обязательна и по причине куда более прозаичной, чем аналитика: гард аренды
    ``_month_closed_by_landlord`` считает месяц закрытым, если у арендодателя есть чужой
    документ БЕЗ статьи. Бесстатейный коммунальный документ съел бы арендное начисление месяца.
    """

    __tablename__ = "utility_account"
    __table_args__ = (
        CheckConstraint(
            "kind in ('water', 'gas', 'electricity')", name="ck_utility_account_kind"
        ),
        CheckConstraint(
            "expected_day IS NULL OR expected_day BETWEEN 1 AND 28",
            name="ck_utility_account_expected_day",
        ),
        UniqueConstraint("location_id", "kind", name="uq_utility_account_location_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("location.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # Кому мы должны. Обычно арендодатель, но модель не запрещает и прямого поставщика: если
    # договор с ресурсником когда-нибудь переоформят на ИП, поменяется только эта ссылка.
    counterparty_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("counterparty.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    dds_article_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dds_articles.id", ondelete="RESTRICT"), nullable=False
    )
    # К какому числу следующего месяца ждём документ. Пусто — срока нет, но месяц всё равно
    # виден в календаре ожиданий: «документа за июль нет» — это факт независимо от срока.
    expected_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # С какого месяца ведём поток. Раньше этой даты календарь пропусков не строим — иначе он
    # покажет «нет документа» за все месяцы существования помещения.
    started_on: Mapped[date] = mapped_column(Date, nullable=False)
    ended_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class UtilityIntake(Base):
    """Принесённая платёжка: файл, распознанные поля и то, чем она стала в учёте.

    Обязательство создаётся не здесь, а при явном «Провести»: до этого строка — черновик,
    который ничего не двигает. Так документ, снятый криво или не за тот месяц, не попадает в
    кредиторку молча.

    Файл храним в самой строке (``content``), как это уже сделано у почтового и налогового
    интейков: пишущих томов у контейнера на проде нет, и файл, положенный на диск, не пережил
    бы ближайшую пересборку образа.

    Ручной ввод — та же строка без файла: по газу документа не приносят вовсе, сумму называет
    арендодатель. Такая запись честно отличима от подтверждённой бумагой (``has_document``).
    """

    __tablename__ = "utility_intake"
    __table_args__ = (
        CheckConstraint(
            "status in ('new', 'needs_review', 'ready', 'promoted', 'rejected')",
            name="ck_utility_intake_status",
        ),
        CheckConstraint(
            "period_end IS NULL OR period_start IS NULL OR period_end >= period_start",
            name="ck_utility_intake_period",
        ),
        CheckConstraint("amount IS NULL OR amount > 0", name="ck_utility_intake_amount"),
        # Дедуп по содержимому — только для файлов. Двух одинаковых фото одной квитанции быть
        # не должно, а вот ручных строк без файла у месяца может быть несколько (доначисление).
        Index(
            "uq_utility_intake_sha",
            "attachment_sha256",
            unique=True,
            postgresql_where=text("attachment_sha256 IS NOT NULL"),
        ),
        Index("ix_utility_intake_status", "status"),
        Index("ix_utility_intake_account_period", "account_id", "period_start"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Пусто, пока человек не сказал, за какой поток эта бумажка. Распознавание предлагает, но
    # не решает: по фото не всегда видно, чьё это помещение.
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("utility_account.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="new", server_default="new"
    )
    # Оригинал. NULL — ручной ввод: газовой квитанции не существует, сумму называет арендодатель.
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mime: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attachment_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attachment_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # Период потребления — то, ради чего всё затевалось: расход обязан лечь в месяц потребления,
    # а не в месяц оплаты.
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    document_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    document_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Сырой результат разбора: что нашли, чем и с какой уверенностью. Нужен, чтобы человек видел
    # не только цифру, но и откуда она взялась, — и чтобы можно было разбирать ошибки парсера
    # задним числом, не имея под рукой исходного фото.
    recognition: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Что получилось из этой бумажки. SET NULL: удаление документа не должно уносить историю
    # приёмки, иначе непонятно, что вообще происходило с июлем.
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("supplier_invoice.id", ondelete="SET NULL"), nullable=True, index=True
    )
    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    @property
    def has_document(self) -> bool:
        """Подтверждена ли строка бумагой. Ручной ввод — не хуже, но и не то же самое."""
        return self.content is not None
