"""Проведение коммунальной платёжки: из принесённой бумажки — в долг перед арендодателем.

ГДЕ ЗДЕСЬ УЧЁТ. Своей механики долга у коммуналки нет, и это сознательно: проведённая платёжка
становится обычным закрывающим ``SupplierInvoice``, а дальше работает канон ДЗ/КЗ. Кредиторка
возникает документом, гасится платежом (правило 1), расход признаётся в месяце потребления
через ``sync_invoice_accrual``. Заводить параллельный реестр обязательств значило бы получить
второй источник правды о долге — ровно то, от чего контур ДЗ/КЗ уходил весь июль.

ЧЕМ КОММУНАЛКА ОТЛИЧАЕТСЯ ОТ АРЕНДЫ, КОТОРАЯ УСТРОЕНА ТАК ЖЕ. Аренда начисляется по расписанию:
ставка известна из договора. Здесь сумма приходит из документа и до него неизвестна, поэтому
обязательство рождается не джобой, а действием человека — и только после того, как он увидел
сумму. Пропущенный месяц при таком порядке не выдумывается системой, а показывается календарём
ожиданий (``expected_periods``): «за июль документа нет» — это факт, а не повод для оценки.

ДАТА ДОЛГА — КОНЕЦ ПЕРИОДА, А НЕ ДАТА БУМАЖКИ. Расчёт за июль, принесённый в сентябре, обязан
показывать долг с 31.07: услуга оказана в июле, и на 31.07 бизнес уже был должен. Дата самого
документа хранится в приёмке как справка — на учёт она не влияет.
"""

from __future__ import annotations

import calendar
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    UTILITY_KIND_LABELS,
    SupplierInvoice,
    UtilityAccount,
    UtilityIntake,
)
from app.services import clock, supplier_prepayments, supplier_service_periods

UTILITY_INVOICE_SOURCE = "utility"

__all__ = [
    "UTILITY_INVOICE_SOURCE",
    "UtilityChargeError",
    "expected_periods",
    "intake_external_id",
    "promote_intake",
    "revoke_intake",
]


class UtilityChargeError(RuntimeError):
    """Проведение невозможно: причина написана так, чтобы её можно было показать человеку."""


def month_bounds(month: date) -> tuple[date, date]:
    first = month.replace(day=1)
    return first, first.replace(day=calendar.monthrange(first.year, first.month)[1])


def intake_external_id(account_id: uuid.UUID, month: date) -> str:
    """Ключ идемпотентности под уникумом ``source + external_id``.

    Ключ на МЕСЯЦ, а не на приёмку: за один месяц по одному потоку долг ровно один, сколько бы
    фотографий этой квитанции ни принесли. Повторная попытка провести второй снимок июля должна
    упереться в существующий документ, а не создать второй долг.
    """
    return f"utility:{account_id}:{month:%Y-%m}"


def superseded_external_id(external_id: str, invoice_id: uuid.UUID) -> str:
    """Ключ отозванного документа: освобождает месяц под повторное проведение.

    Тот же приём, что у самоактов. Уникум ``source + external_id`` один на все документы, и пока
    ключ занят аннулированной строкой, месяц нельзя провести заново — а именно это и требуется
    после ошибки в сумме: отозвать и провести правильно. Хвост с id показывает в базе, что это
    не действующий документ, а его след.
    """
    return f"{external_id}:revoked:{invoice_id}"


def invoice_title(kind: str, month: date) -> str:
    """Заголовок документа в реестрах.

    Пишем «возмещение», а не «акт»: акта у нас нет и не будет, счёт ресурсника выставлен на
    арендодателя. Человек, увидевший строку в кредиторке, должен понимать её природу без
    открывания карточки.
    """
    label = UTILITY_KIND_LABELS.get(kind, kind)
    return f"Возмещение: {label.lower()}, {month:%m.%Y}"


async def _existing_invoice(
    session: AsyncSession, external_id: str
) -> SupplierInvoice | None:
    return await session.scalar(
        select(SupplierInvoice).where(
            SupplierInvoice.source == UTILITY_INVOICE_SOURCE,
            SupplierInvoice.external_id == external_id,
        )
    )


def _validate_ready(intake: UtilityIntake, account: UtilityAccount | None) -> tuple[date, date]:
    if intake.status == "promoted":
        raise UtilityChargeError("Эта платёжка уже проведена")
    if intake.status == "rejected":
        raise UtilityChargeError("Платёжка отклонена — сначала верните её в работу")
    if account is None:
        raise UtilityChargeError("Не выбран коммунальный поток: помещение и вид услуги")
    if not account.is_active:
        raise UtilityChargeError("Поток отключён — включите его или выберите другой")
    if intake.amount is None or Decimal(intake.amount) <= 0:
        raise UtilityChargeError("Не указана сумма к возмещению")
    if intake.period_start is None or intake.period_end is None:
        raise UtilityChargeError("Не указан период потребления — без него расход некуда отнести")
    start, end = intake.period_start, intake.period_end
    if end < start:
        raise UtilityChargeError("Конец периода раньше начала")
    if start < account.started_on:
        raise UtilityChargeError(
            f"Период начинается раньше, чем ведётся учёт по этому потоку "
            f"(с {account.started_on:%d.%m.%Y})"
        )
    if account.ended_on is not None and end > account.ended_on:
        raise UtilityChargeError(
            f"Период заканчивается позже, чем закрыт поток ({account.ended_on:%d.%m.%Y})"
        )
    return start, end


async def promote_intake(
    session: AsyncSession,
    intake: UtilityIntake,
    *,
    actor_user_id: uuid.UUID | None = None,
    as_of: date | None = None,
) -> SupplierInvoice:
    """Провести проверенную платёжку: создать долг перед арендодателем. Не коммитит.

    Порядок вызовов важен и повторяет аренду: сначала проводим документ (правило 4 решает,
    вступает он в силу сегодня или ждёт своей даты, и гасит открытую дебиторку, если платили
    вперёд), затем заводим строку признания расхода — уже зная, справочный документ или нет.
    """
    account = (
        await session.get(UtilityAccount, intake.account_id)
        if intake.account_id is not None
        else None
    )
    start, end = _validate_ready(intake, account)
    assert account is not None  # _validate_ready уже отказал бы

    month = end.replace(day=1)
    external_id = intake_external_id(account.id, month)
    if (existing := await _existing_invoice(session, external_id)) is not None:
        raise UtilityChargeError(
            f"За {month:%m.%Y} по этому потоку долг уже проведён (документ «{existing.number}»). "
            "Отзовите его, если сумма изменилась"
        )

    invoice = SupplierInvoice(
        counterparty_id=account.counterparty_id,
        source=UTILITY_INVOICE_SOURCE,
        external_id=external_id,
        direction="payable",
        doc_kind="closing",
        # finance — иначе документ не увидят ни правило 1 канона, ни авто-зачёт предоплат:
        # автоматика намеренно не трогает товарный документооборот.
        operational_scope="finance",
        number=invoice_title(account.kind, month),
        # Конец периода, а не дата бумажки: долг существует с момента, когда услуга оказана.
        invoice_date=end,
        amount=Decimal(intake.amount),
        dds_article_id=account.dds_article_id,
        service_period_start=start,
        service_period_end=end,
        service_period_source=UTILITY_INVOICE_SOURCE,
        # ready — без этого sync_invoice_accrual молча не создаст строку признания расхода.
        service_period_status="ready",
    )
    session.add(invoice)
    await session.flush()

    await supplier_prepayments.apply_closing_document(
        session, invoice, actor_user_id=actor_user_id, as_of=as_of
    )
    # Помещение известно точно — оно и есть смысл потока. Без него расход осел бы в графе
    # «без помещения», и прибыль по точке считалась бы без коммуналки.
    await supplier_service_periods.sync_invoice_accrual(
        session, invoice, location_id=account.location_id
    )

    intake.invoice_id = invoice.id
    intake.status = "promoted"
    await session.flush()
    return invoice


async def revoke_intake(
    session: AsyncSession,
    intake: UtilityIntake,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> SupplierInvoice | None:
    """Отозвать проведённую платёжку: снять долг и освободить месяц. Не коммитит.

    Нужен ровно для одного, зато частого случая: в сумме ошиблись. Пока месяц занят ключом
    идемпотентности, правильную сумму провести нельзя, а править сумму на месте — значит
    молча переписать уже признанный расход.

    Отзываем только НЕОПЛАЧЕННЫЙ документ. Если деньги по нему уже разнесены, отзыв оставил бы
    платёж висеть на аннулированном долге; такой случай правится через обычную карточку
    документа, где видно и оплату.
    """
    if intake.status != "promoted" or intake.invoice_id is None:
        raise UtilityChargeError("Эта платёжка не проведена — отзывать нечего")
    invoice = await session.get(SupplierInvoice, intake.invoice_id)
    if invoice is None:
        # Документ удалили мимо нас: приёмку всё равно возвращаем в работу, иначе месяц
        # окажется заперт навсегда.
        intake.status = "ready"
        intake.invoice_id = None
        await session.flush()
        return None
    if invoice.payment_status not in ("unpaid", "void"):
        raise UtilityChargeError(
            "По этому долгу уже прошла оплата — отозвать нельзя. Исправьте документ в карточке "
            "контрагента"
        )

    invoice.payment_status = "void"
    if invoice.external_id:
        invoice.external_id = superseded_external_id(invoice.external_id, invoice.id)
    # Начисление снимаем явно: is_expense_bearing уже вернёт False для void-документа, но
    # полагаться на это молча нельзя — расход должен исчезнуть из месяца здесь и сейчас.
    await supplier_service_periods.cancel_invoice_accrual(session, invoice.id)
    intake.status = "ready"
    intake.invoice_id = None
    await session.flush()
    return invoice


async def expected_periods(
    session: AsyncSession,
    *,
    account: UtilityAccount,
    months_back: int = 6,
    as_of: date | None = None,
) -> list[dict[str, object]]:
    """Календарь ожиданий по потоку: месяц за месяцем, с документом и без.

    Смысл ровно один — сделать видимой ПРОПАЖУ. Существующие витрины показывают то, что в
    системе есть: платежи, документы, начисления. Месяц, за который бумажку не принесли, не
    оставляет следа нигде, и обнаружить его можно было только вспомнив. Поэтому строка месяца
    существует всегда, а состояние у неё — производное от того, что нашлось.
    """
    today = as_of or clock.moscow_today()
    first_of_current = today.replace(day=1)

    months: list[date] = []
    for step in range(months_back, 0, -1):
        year, index = first_of_current.year, first_of_current.month - step
        while index <= 0:
            index += 12
            year -= 1
        month = date(year, index, 1)
        if month < account.started_on.replace(day=1):
            continue
        if account.ended_on is not None and month > account.ended_on:
            continue
        months.append(month)

    rows: list[dict[str, object]] = []
    for month in months:
        invoice = await _existing_invoice(session, intake_external_id(account.id, month))
        _, last_day = month_bounds(month)
        if invoice is not None:
            state = "provided"
        elif account.expected_day is not None:
            due = min(account.expected_day, calendar.monthrange(*_next_month(month))[1])
            deadline = date(*_next_month(month), due)
            state = "overdue" if today > deadline else "waiting"
        else:
            state = "waiting" if today <= last_day else "missing"
        rows.append(
            {
                "month": month,
                "state": state,
                "invoice_id": invoice.id if invoice is not None else None,
                "amount": invoice.amount if invoice is not None else None,
            }
        )
    return rows


def _next_month(month: date) -> tuple[int, int]:
    return (month.year + 1, 1) if month.month == 12 else (month.year, month.month + 1)
