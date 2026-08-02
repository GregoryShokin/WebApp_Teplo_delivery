"""Коммунальная платёжка в учёте: из принесённой бумажки — счёт к оплате и расход месяца.

ГДЕ ЗДЕСЬ УЧЁТ. Своей механики долга у коммуналки нет, и это сознательно: разобранная платёжка
становится обычными ``SupplierInvoice``, а дальше работает канон ДЗ/КЗ. Кредиторка возникает
закрывающим документом, гасится платежом (правило 1), расход признаётся в месяце потребления
через ``sync_invoice_accrual``. Заводить параллельный реестр обязательств значило бы получить
второй источник правды о долге — ровно то, от чего контур ДЗ/КЗ уходил весь июль.

ПОЧЕМУ ДОКУМЕНТА ДВА, А БУМАЖКА ОДНА. Роли в каноне разделены жёстко: счёт — основание платежа
и в очередь оплат попадает именно он, а расход несёт закрывающий документ. Счёт расхода не
несёт никогда (``is_expense_bearing``), и это не формальность: пока начисление заводилось и на
счёте, услуга на 13 000 ₽ давала 26 000 ₽ в P&L, причём деньги сходились и увидеть удвоение
можно было только в отчёте о прибыли. Поэтому квитанция за воду порождает ПАРУ: счёт (его
платят) и закрывающий (он признаёт расход и создаёт кредиторку) — ровно как пакет «счёт + УПД»
одним файлом у поставщиков с ЭДО.

ЭЛЕКТРИЧЕСТВО: СУММЫ У ПАРЫ РАЗНЫЕ. В фактическом акте расход периода и сумма к оплате — разные
числа: из потребления с потерями вычтен ранее внесённый аванс. Поэтому закрывающий встаёт на
полный расход месяца, а счёт — на остаток к доплате. Авансовый акт наоборот: он основание
платежа, но расхода не несёт вовсе — расход придёт следующим фактическим актом, а уплаченный
аванс к тому времени будет дебиторкой, которую тот акт и зачтёт.

ДАТА ДОЛГА — КОНЕЦ ПЕРИОДА, А НЕ ДАТА БУМАЖКИ. Расчёт за июль, принесённый в сентябре, обязан
показывать долг с 31.07: услуга оказана в июле, и на 31.07 бизнес уже был должен. Дата самой
бумажки хранится в приёмке как справка — на учёт она не влияет.

ПРОПУЩЕННЫЙ МЕСЯЦ НЕ ВЫДУМЫВАЕТСЯ. Аренда начисляется по расписанию, потому что ставка известна
из договора. Здесь сумма приходит из документа и до него неизвестна, поэтому обязательство
рождается не джобой, а разбором принесённой бумажки. Отсутствие июля показывает календарь
ожиданий (``expected_periods``) — это факт, а не повод для оценки.
"""

from __future__ import annotations

import calendar
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    UTILITY_KIND_LABELS,
    SupplierInvoice,
    UtilityAccount,
)
from app.services import (
    clock,
    counterparty_matching,
    supplier_prepayments,
    supplier_service_periods,
)

UTILITY_INVOICE_SOURCE = "utility"

__all__ = [
    "UTILITY_INVOICE_SOURCE",
    "UtilityChargeError",
    "build_utility_documents",
    "expected_periods",
    "intake_external_id",
    "settle_utility_invoices_from_cash",
]


class UtilityChargeError(RuntimeError):
    """Проведение невозможно: причина написана так, чтобы её можно было показать человеку."""


def month_bounds(month: date) -> tuple[date, date]:
    first = month.replace(day=1)
    return first, first.replace(day=calendar.monthrange(first.year, first.month)[1])


# Роль документа в ключе идемпотентности. За один месяц по одному потоку их РАЗНЫХ до трёх, и
# различать их обязательно: у энергетика авансовый счёт за июнь выписывается 19 июня, а
# фактический акт за тот же июнь приходит 17 июля. Без роли в ключе они столкнулись бы на
# уникуме source+external_id — и второй документ упал бы сырой ошибкой базы вместо работы.
UTILITY_DOC_ROLES = ("advance", "due", "closing")


def intake_external_id(account_id: uuid.UUID, month: date, role: str = "due") -> str:
    """Ключ идемпотентности под уникумом ``source + external_id``.

    Ключ на МЕСЯЦ и РОЛЬ, а не на приёмку: за один месяц по одному потоку каждый документ ровно
    один, сколько бы фотографий этой квитанции ни принесли. Повторная попытка провести второй
    снимок июля упирается в существующий документ, а не создаёт второй долг — это защита именно
    от ПЕРЕСНЯТОЙ бумаги, у которой байты другие, а долг тот же, поэтому дедуп по содержимому
    файла её не ловит.

    Роли: ``advance`` — авансовый счёт (платится вперёд, расхода не несёт), ``due`` — счёт к
    оплате по факту, ``closing`` — закрывающий документ месяца.
    """
    if role not in UTILITY_DOC_ROLES:
        raise ValueError(f"неизвестная роль коммунального документа: {role}")
    return f"utility:{account_id}:{month:%Y-%m}:{role}"


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


def _validate_period(
    account: UtilityAccount,
    period_start: date,
    period_end: date,
    expense_amount: Decimal,
) -> None:
    """Отказать до создания документов, если период или сумма не годятся.

    Проверки здесь, а не в роуте: документов рождается два, и отказ на середине оставил бы
    счёт без закрывающего — то есть платёж без расхода.
    """
    if not account.is_active:
        raise UtilityChargeError("Поток отключён — включите его или выберите другой")
    if expense_amount <= 0:
        raise UtilityChargeError("Не указана сумма к возмещению")
    if period_end < period_start:
        raise UtilityChargeError("Конец периода раньше начала")
    if period_start < account.started_on:
        raise UtilityChargeError(
            f"Период начинается раньше, чем ведётся учёт по этому потоку "
            f"(с {account.started_on:%d.%m.%Y})"
        )
    if account.ended_on is not None and period_end > account.ended_on:
        raise UtilityChargeError(
            f"Период заканчивается позже, чем закрыт поток ({account.ended_on:%d.%m.%Y})"
        )


async def build_utility_documents(
    session: AsyncSession,
    account: UtilityAccount,
    *,
    period_start: date,
    period_end: date,
    expense_amount: Decimal | None,
    payable_amount: Decimal,
    actor_user_id: uuid.UUID | None = None,
    as_of: date | None = None,
) -> tuple[SupplierInvoice, SupplierInvoice | None]:
    """Создать по разобранной платёжке счёт и, если она несёт расход, закрывающий документ.

    Возвращает пару ``(счёт, закрывающий)``. Второй элемент — ``None`` у авансового акта:
    аванс это основание платежа и ничего больше, расход по нему признает будущий фактический
    акт. Не коммитит.

    ``expense_amount`` — расход периода целиком (у воды и газа совпадает с суммой к оплате, у
    фактического акта электроэнергии больше неё на зачтённый аванс). ``payable_amount`` — то,
    что реально уходит деньгами.

    Порядок вызовов повторяет аренду и важен: сначала проводим закрывающий документ (правило 4
    решает, вступает он в силу сегодня или ждёт своей даты, и гасит открытую дебиторку, если
    платили вперёд), затем заводим строку признания расхода — уже зная, справочный он или нет.
    """
    _validate_period(
        account,
        period_start,
        period_end,
        expense_amount if expense_amount is not None else payable_amount,
    )

    month = period_end.replace(day=1)
    title = invoice_title(account.kind, month)
    # Роль счёта: авансовый и фактический относятся к ОДНОМУ месяцу и одному потоку — у
    # энергетика аванс за июнь выписан 19 июня, а факт за июнь приходит 17 июля. Общий ключ
    # столкнул бы их на уникуме source+external_id, и второй документ упал бы сырой ошибкой базы.
    bill_external_id = intake_external_id(
        account.id, month, "due" if expense_amount is not None else "advance"
    )
    closing_external_id = (
        intake_external_id(account.id, month, "closing") if expense_amount is not None else None
    )

    # Занятость проверяем по тому же ключу, каким будем писать: второй снимок той же бумаги
    # упрётся сюда с понятным отказом, а не в IntegrityError. Это защита именно от ПЕРЕСНЯТОГО
    # документа — у него другие байты, и дедуп по содержимому файла его не ловит.
    if (existing := await _existing_invoice(session, bill_external_id)) is not None:
        raise UtilityChargeError(
            f"За {month:%m.%Y} по этому потоку счёт уже заведён (документ «{existing.number}»). "
            "Отзовите его, если сумма изменилась"
        )
    if (
        closing_external_id is not None
        and (existing := await _existing_invoice(session, closing_external_id)) is not None
    ):
        raise UtilityChargeError(
            f"За {month:%m.%Y} по этому потоку долг уже проведён "
            f"(документ «{existing.number}»). Отзовите его, если сумма изменилась"
        )

    bill = SupplierInvoice(
        counterparty_id=account.counterparty_id,
        source=UTILITY_INVOICE_SOURCE,
        external_id=bill_external_id,
        direction="payable",
        doc_kind="bill",
        # finance — иначе документ не увидят ни правило 1 канона, ни авто-зачёт предоплат:
        # автоматика намеренно не трогает товарный документооборот.
        operational_scope="finance",
        number=title,
        invoice_date=period_end,
        amount=payable_amount,
        dds_article_id=account.dds_article_id,
        service_period_start=period_start,
        service_period_end=period_end,
        service_period_source=UTILITY_INVOICE_SOURCE,
        service_period_status="ready",
    )
    session.add(bill)
    await session.flush()

    if expense_amount is None:
        return bill, None

    closing = SupplierInvoice(
        counterparty_id=account.counterparty_id,
        source=UTILITY_INVOICE_SOURCE,
        external_id=closing_external_id,
        direction="payable",
        doc_kind="closing",
        operational_scope="finance",
        number=title,
        # Конец периода, а не дата бумажки: долг существует с момента, когда услуга оказана.
        invoice_date=period_end,
        amount=expense_amount,
        dds_article_id=account.dds_article_id,
        service_period_start=period_start,
        service_period_end=period_end,
        service_period_source=UTILITY_INVOICE_SOURCE,
        # ready — без этого sync_invoice_accrual молча не создаст строку признания расхода.
        service_period_status="ready",
    )
    session.add(closing)
    await session.flush()

    await supplier_prepayments.apply_closing_document(
        session, closing, actor_user_id=actor_user_id, as_of=as_of
    )
    # Замещение самоакта переклеивает его денежные аллокации на наш документ, а статуса не
    # трогает: документ остался бы «неоплаченным» с живыми деньгами на нём — и отзыв, который
    # смотрит именно на статус, аннулировал бы оплаченный долг. Пересчёт закрывает окно.
    await counterparty_matching._recompute_status(session, closing)
    # Помещение известно точно — оно и есть смысл потока. Без него расход осел бы в графе
    # «без помещения», и прибыль по точке считалась бы без коммуналки.
    await supplier_service_periods.sync_invoice_accrual(
        session, closing, location_id=account.location_id
    )
    return bill, closing


async def settle_utility_invoices_from_cash(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    article_id: uuid.UUID | None,
    location_id: uuid.UUID | None,
    transaction_id: uuid.UUID,
    amount: Decimal,
    wallet_id: uuid.UUID | None = None,
    created_by_user_id: uuid.UUID | None = None,
) -> bool:
    """Наличная выплата арендодателю гасит коммунальный долг. Не коммитит.

    Почему это нужно отдельной функцией. Канон не различает канал денег, но исполняют его два
    разных механизма: банковский платёж проходит правило 1 сам, а наличная выдача из Сейфа в
    правило 1 не заходит вовсе (``safe_payout`` в ``DEDICATED_MONEY_SOURCE_KINDS`` — слепой FIFO
    снял бы адресную привязку выдачи). Аренда решает это своим
    ``settle_lease_invoice_from_cash``; у коммуналки такого не было, и деньги, выданные
    арендодателю из Сейфа — а это здесь ОСНОВНОЙ канал, — кредиторку не гасили ничем.

    Возвращает ``True``, если выдача опознана как коммунальная и деньгами распорядились.
    """
    if amount <= 0 or article_id is None:
        return False

    conditions = [
        UtilityAccount.counterparty_id == counterparty_id,
        UtilityAccount.dds_article_id == article_id,
    ]
    if location_id is not None:
        conditions.append(UtilityAccount.location_id == location_id)
    accounts = (await session.scalars(select(UtilityAccount).where(*conditions))).all()
    if not accounts:
        return False

    # Локальные импорты: модуль зовут из safe_allocations, а тот тянет пол-банка — на верхнем
    # уровне вышло бы кольцо.
    from app.models import InvoicePaymentAllocation, SupplierPrepayment, invoice_binds_settlement

    prefixes = [f"utility:{account.id}:%" for account in accounts]
    invoices = (
        await session.scalars(
            select(SupplierInvoice)
            .where(
                SupplierInvoice.source == UTILITY_INVOICE_SOURCE,
                or_(*[SupplierInvoice.external_id.like(prefix) for prefix in prefixes]),
                SupplierInvoice.payment_status.in_(("unpaid", "partially_paid")),
                # Только вступившие в силу: документ будущего месяца обязательством ещё не
                # является, и платить по нему нельзя — ровно тот дефект, на котором аренда
                # once потеряла 50 000 ₽.
                invoice_binds_settlement(),
            )
            # Счёт ПЕРВЫМ, и порядок задан явно. У пары «счёт + закрывающий» из одной
            # бумаги совпадают и дата документа (конец периода), и created_at (обе строки
            # рождаются в одной транзакции, а func.now() в PostgreSQL — время транзакции),
            # поэтому сортировка по ним двоим оставляла порядок на усмотрение базы. Выпади
            # закрывающий первым — наличные закрыли бы его напрямую, а счёт остался бы висеть
            # в очереди оплат уже оплаченного месяца.
            .order_by(
                SupplierInvoice.invoice_date,
                case((SupplierInvoice.doc_kind == "bill", 0), else_=1),
                SupplierInvoice.created_at,
            )
        )
    ).all()

    left = amount
    for invoice in invoices:
        if left <= 0:
            break
        allocated = await counterparty_matching._allocated_amount(session, invoice.id)
        outstanding = Decimal(invoice.amount) - allocated
        if outstanding <= 0:
            continue
        part = min(outstanding, left)
        session.add(
            InvoicePaymentAllocation(
                invoice_id=invoice.id,
                source_kind="cash",
                cashflow_transaction_id=transaction_id,
                amount=part,
                created_by_user_id=created_by_user_id,
            )
        )
        await session.flush()
        await counterparty_matching._recompute_status(session, invoice)
        left -= part

    if left > 0:
        # Заплатили больше, чем предъявлено, — деньги вперёд. Обычная дебиторка: пришедшая
        # позже платёжка погасится ею штатным авто-зачётом.
        session.add(
            SupplierPrepayment(
                counterparty_id=counterparty_id,
                kind=supplier_prepayments.RULE1_PREPAYMENT_KIND,
                wallet_id=wallet_id,
                amount=left,
                amount_settled=Decimal("0.00"),
                status="open",
                cashflow_transaction_id=transaction_id,
                article_id=article_id,
                note="Коммунальные услуги: оплачено вперёд",
                created_by_user_id=created_by_user_id,
            )
        )
        await session.flush()
    return True


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
