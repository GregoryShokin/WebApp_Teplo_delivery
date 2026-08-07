"""Периоды оказания услуг, ДЗ/КЗ и признание расходов поставщиков.

ДДС отвечает только за факт движения денег. Этот модуль ведёт начисление независимо:
оплата до конца периода остаётся дебиторкой, а после конца периода расход признаётся в P&L;
неоплаченный признанный расход образует кредиторку.
"""

from __future__ import annotations

import calendar
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CounterpartyPayableProfile,
    ExpenseDraftLine,
    SupplierExpenseAccrual,
    SupplierInvoice,
    SupplierServicePeriodChange,
)

# Признание идёт по московской дате: джоба запускается в 00:05 МСК, а UTC-дата в этот
# момент ещё вчерашняя — по ней расход признавался бы на сутки позже срока.
MOSCOW_TZ = ZoneInfo("Europe/Moscow")


class ServicePeriodError(ValueError):
    pass


def validate_period(start: date | None, end: date | None) -> tuple[date, date]:
    if start is None or end is None:
        raise ServicePeriodError("Укажите начало и окончание периода оказания услуги")
    if end < start:
        raise ServicePeriodError("Окончание периода не может быть раньше начала")
    return start, end


def recognition_month(period_end: date) -> date:
    return date(period_end.year, period_end.month, 1)


def effective_period_status(stored_status: str, *, required: bool) -> str:
    """Честный статус для выдачи: у контрагента с обязательным периодом ``not_required`` —
    это «ещё не заполнен» (накладная не из почты), а не «не нужен». Показываем ``missing``,
    чтобы оператор видел, почему счёт нельзя отправить в банк."""
    if required and stored_status == "not_required":
        return "missing"
    return stored_status


def is_expense_bearing(invoice: SupplierInvoice) -> bool:
    """Может ли документ вообще нести расход — независимо от того, заполнен ли период.

    Два случая, когда не может:
      • Счёт (doc_kind='bill') расходом не является — это основание для платежа, и по канону
        он не долг. Расход приносит закрывающий документ либо самоакт. Пока начисление
        заводилось и на счёте, период, указанный при оплате, признавал расход, а пришедший
        следом УПД признавал его второй раз: 13 000 ₽ услуги давали 26 000 ₽ в P&L. Деньги
        при этом сходились (ДЗ по счёту гасил тот же УПД), поэтому увидеть это можно было
        только в отчёте о прибыли.
      • Информационный документ расхода не создаёт: месяц уже признан начислением по
        договору, и вторая строка P&L удвоила бы расход.
    """
    # Аннулированный документ (payment_status='void') расхода не несёт тем более: он и как
    # документ больше не существует. Без этой проверки период, проставленный аннулированной
    # накладной уже ПОСЛЕ отмены (роут её не запрещает), заводил начисление заново — и
    # cancel_invoice_accrual, отработавший при отмене, снять его уже не мог.
    return (
        invoice.doc_kind != "bill"
        and not invoice.informational
        and invoice.payment_status != "void"
    )


# Подтип услугового контрагента, у которого сумма месяца известна только из документа
# (``CounterpartyPayableProfile.service_billing_mode``). Канон владельца от 01.08.2026,
# режим 3 «Счёт + УПД»: Манго Телеком, ЭкоЦентр.
PER_INVOICE_BILLING_MODE = "per_invoice"


async def _period_from_document_date(
    session: AsyncSession, invoice: SupplierInvoice
) -> tuple[date, date] | None:
    """Период для закрывающего режима «счёт + УПД», у которого явного периода нет вовсе.

    ЗАЧЕМ. У режима 3 расход признаётся ТОЛЬКО документом: платежи такого контрагента
    исключаются из кассового расхода с формулировкой «расход придёт документом». Документ
    приходил — и расхода не приносил: период у него пуст (``not_required``), а начисление
    заводится строго при ``ready``. Ловушка захлопывалась с двух сторон, и расход исчезал
    молча. За июль 2026 так потерялись 11 695,54 ₽ Манго: строка «Телекоммуникации»
    показывала 3 230 ₽ вместо 14 925,54 ₽.

    ПОЧЕМУ МЕСЯЦ ДАТЫ ДОКУМЕНТА — ЧЕСТНЫЙ ОТВЕТ. Акт за услуги месяца датируется его
    последним днём: на проде все четыре таких документа (Манго ×2 за 31.07, ЭкоЦентр ×2 за
    31.08) — ровно последний день своего месяца. Это не догадка о периоде, а прочтение даты,
    которую поставил сам контрагент.

    ПОЛЕЙ НАКЛАДНОЙ НЕ ТРОГАЕМ. Период живёт только в начислении: документ остаётся честно
    «без периода», и как только человек впишет период руками, тот победит — эта функция
    вернёт ``None``, и дальше пойдёт обычная ветка ``ready``.

    ОГРАНИЧИТЕЛЬ ОБЯЗАТЕЛЕН, каждое условие оплачено данными:
    * ``operational_scope='finance'`` — иначе начисления получат 142 товарные накладные июля
      на 1,53 млн ₽, а их себестоимость уже приходит фудкостом из зеркала iiko: двойной счёт;
    * ``direction='payable'`` — исходящий бартер расхода не несёт;
    * ``service_billing_mode='per_invoice'`` — у режимов «договор» и «счёт за период» расход
      признаёт свой механизм, а у НЕразмеченного контрагента платёж из кассы не исключается,
      и начисление задвоило бы его (по июлю это 22 074,65 ₽: Билинский и Яндекс.Еда);
    * ``invoice_date >= ACCOUNTING_START`` — июньский расход уже сидит во входящих остатках
      на 01.07, а отчёта за июнь не существует вовсе.
    """
    if invoice.doc_kind != "closing" or invoice.direction != "payable":
        return None
    if invoice.operational_scope != "finance":
        return None
    # Явный период — всегда сильнее выведенного, в том числе наполовину заполненный: он
    # означает, что за документ уже взялся человек.
    if invoice.service_period_start is not None or invoice.service_period_end is not None:
        return None
    if invoice.invoice_date is None:
        return None

    from app.services import accounting_periods

    if invoice.invoice_date < accounting_periods.ACCOUNTING_START:
        return None
    mode = await session.scalar(
        select(CounterpartyPayableProfile.service_billing_mode).where(
            CounterpartyPayableProfile.counterparty_id == invoice.counterparty_id
        )
    )
    if mode != PER_INVOICE_BILLING_MODE:
        return None
    start = invoice.invoice_date.replace(day=1)
    end = start.replace(day=calendar.monthrange(start.year, start.month)[1])
    return start, end


async def sync_invoice_accrual(
    session: AsyncSession,
    invoice: SupplierInvoice,
    *,
    location_id: uuid.UUID | None = None,
) -> SupplierExpenseAccrual | None:
    """Создать/обновить/отменить запись P&L для документа с подтверждённым периодом.

    Отменяет намеренно: признак «расхода не несёт» появляется у документа ПОЗЖЕ, чем его
    начисление. ``informational`` выставляет ``apply_closing_document``, а он во всех четырёх
    дверях приёма (почта ×3, СБИС) вызывается после этой функции; будущий закрывающий получает
    флаг только в день активации, через месяц после приёма. Пока функция на такой документ
    просто ничего не делала, уже созданное начисление оставалось жить: акт ИП Наумченко по
    договору 3 000 ₽/мес давал в июне 6 000 ₽ расхода. Отмена задним числом делает порядок
    вызовов неважным — какой бы дверью документ ни пришёл, лишнее начисление снимется.
    Не коммитит."""
    existing = await session.scalar(
        select(SupplierExpenseAccrual).where(SupplierExpenseAccrual.invoice_id == invoice.id)
    )
    if not is_expense_bearing(invoice):
        return await _cancel_accrual(session, existing)
    if (
        invoice.service_period_status == "ready"
        and invoice.service_period_start is not None
        and invoice.service_period_end is not None
    ):
        period = (invoice.service_period_start, invoice.service_period_end)
    else:
        # Периода в документе нет. У режима «счёт + УПД» его даёт дата документа — иначе
        # расход не признает никто (см. ``_period_from_document_date``).
        period = await _period_from_document_date(session, invoice)
    if period is None:
        # Периода нет — начисление создать не из чего, но и отменять существующее не за что:
        # «период ещё не заполнен» не то же самое, что «расхода не будет».
        return existing

    start, end = validate_period(*period)
    if existing is None:
        existing = SupplierExpenseAccrual(
            counterparty_id=invoice.counterparty_id,
            invoice_id=invoice.id,
            payment_draft_id=invoice.draft_id,
            article_id=invoice.dds_article_id,
            location_id=location_id,
            amount=invoice.amount,
            service_period_start=start,
            service_period_end=end,
            status="scheduled",
        )
        session.add(existing)
    else:
        existing.counterparty_id = invoice.counterparty_id
        existing.payment_draft_id = invoice.draft_id
        existing.article_id = invoice.dds_article_id
        # Локацию перезаписываем только когда её ЗНАЕТ вызывающий: обычный синк документа
        # её не передаёт, и обнулять уже проставленную было бы потерей аналитики.
        if location_id is not None:
            existing.location_id = location_id
        # Сумму и период признанного расхода не двигаем молча: он уже в P&L закрытого
        # месяца. Привязки (контрагент/черновик/статья) обновлять безопасно — они не
        # меняют величину расхода. Корректировка признанной суммы — только осознанно.
        if existing.status != "recognized":
            existing.amount = invoice.amount
            existing.service_period_start = start
            existing.service_period_end = end
    await session.flush()
    # ОПОЗДАВШИЙ ДОКУМЕНТ ПРИЗНАЁТСЯ СРАЗУ, А НЕ СЛЕДУЮЩЕЙ НОЧЬЮ. Ночная джоба права ровно в
    # одном: пока период идёт, признавать нечего. Но УПД за июль приходит в августе, и до этой
    # строки он ждал ближайших 00:05 — до суток. Владелец видел строку «признание после
    # 31.07.2026» третьего августа и читал её как поломку: дата наступила, а расхода нет
    # (УПД «Назад в будущее» на 83 092 ₽, 03.08.2026). Условие признания у джобы и здесь одно и
    # то же (``recognize_due_expenses``), включая отказ дописывать расход в закрытый месяц, —
    # разница только в моменте вызова, поэтому расхождения двух путей быть не может.
    if existing.status == "scheduled" and end < datetime.now(MOSCOW_TZ).date():
        await recognize_due_expenses(session, invoice_ids=[invoice.id], commit=False)
    return existing


async def _recognized_by_other_mechanism(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    start: date,
    end: date,
    article_id: uuid.UUID | None,
) -> bool:
    """Признает ли расход ЭТОГО периода другой механизм — целиком, а не частично.

    Два режима разметки карточки: договор услуги (сумма месяца из договора) и «счёт за период»
    (помесячные самоакты из дебиторки платежа). Оба начисляют сами и по своему расписанию, а
    строка платежа начислила бы ту же услугу ещё раз — целиком и последним месяцем периода.

    ГЛУШИМ ТОЛЬКО ПРИ ПОЛНОМ ПОКРЫТИИ, и это не педантизм. Отказ здесь означает, что расход
    признает КТО-ТО ДРУГОЙ; если он признает лишь часть периода, остальное не признает никто,
    и деньги молча исчезнут из P&L — потеря расхода хуже, чем его задвоение, потому что
    задвоение видно в отчёте, а пропажа нет:

    * договор проверяем на КАЖДЫЙ месяц периода, а не на его концы. Договор, действовавший
      только в апреле, глушил платёж за апрель-июнь целиком, а начислял один апрель;
    * режим «счёт за период» признаёт расход самоактами ИЗ ДЕБИТОРКИ платежа — значит нужна
      сама дебиторка с готовым периодом. Без неё (платёж прошёл мимо предоплаты) самоактов не
      будет вовсе, и глушить строку не за что.
    """
    # Локальные импорты: оба модуля зависят от этого, на верхнем уровне вышел бы цикл.
    from app.models import SupplierPrepayment
    from app.services.service_agreement_accruals import covered_by_agreement
    from app.services.subscription_accruals import (
        BILLING_MODE_FIXED_TARIFF,
        OPEN_PREPAYMENT_STATUSES,
        add_months,
    )

    month = start.replace(day=1)
    last_month = end.replace(day=1)
    while month <= last_month:
        if not await covered_by_agreement(
            session, counterparty_id, on=month, article_id=article_id
        ):
            break
        month = add_months(month, 1)
    else:
        return True

    mode = await session.scalar(
        select(CounterpartyPayableProfile.service_billing_mode).where(
            CounterpartyPayableProfile.counterparty_id == counterparty_id
        )
    )
    if mode != BILLING_MODE_FIXED_TARIFF:
        return False
    # Есть ли дебиторка, из которой пойдут самоакты. Ищем по контрагенту и пересечению
    # периодов: связь «платёж ↔ предоплата» рвётся штатно, когда выписку разбирают раньше
    # отметки «оплачено».
    covering_prepayment = await session.scalar(
        select(SupplierPrepayment.id).where(
            SupplierPrepayment.counterparty_id == counterparty_id,
            SupplierPrepayment.status.in_(OPEN_PREPAYMENT_STATUSES),
            SupplierPrepayment.service_period_status == "ready",
            SupplierPrepayment.service_period_start.is_not(None),
            SupplierPrepayment.service_period_start <= end,
            func.coalesce(
                SupplierPrepayment.service_period_end, SupplierPrepayment.service_period_start
            )
            >= start,
        )
    )
    return covering_prepayment is not None


async def expense_line_accrual_covering(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    start: date,
    end: date,
    article_id: uuid.UUID | None = None,
) -> SupplierExpenseAccrual | None:
    """Живое начисление по СТРОКЕ ПЛАТЕЖА, пересекающее этот период.

    Начисление по строке — четвёртый механизм признания расхода, и до 02.08.2026 остальные три
    его не видели: договор услуги и самоакты из предоплаты проверяли только ``SupplierInvoice``,
    а строка платежа документом не является. Итог — двойной расход по любому платежу, который
    оформили в «Новом платеже» с периодом:

    * Синапсис, режим «счёт за период»: строка начислила 13 000 ₽ за июль, а ночная джоба
      завела самоакт на те же деньги — 26 000 ₽ в июле;
    * ИП Наумченко, договор 3 000 ₽/мес: строка на 9 000 ₽ за квартал плюс три начисления по
      договору — 18 000 ₽.

    Ищем по КОНТРАГЕНТУ и пересечению периодов, а не по черновику платежа: связь через черновик
    рвётся штатно (если выписка пришла раньше отметки «оплачено», предоплата садится на проводку
    банка и ссылки на черновик у неё уже нет). Статью сверяем как везде в контуре — документ или
    начисление без статьи считаем относящимся к любой.
    """
    conditions = [
        SupplierExpenseAccrual.counterparty_id == counterparty_id,
        SupplierExpenseAccrual.expense_draft_line_id.is_not(None),
        SupplierExpenseAccrual.status != "cancelled",
        SupplierExpenseAccrual.service_period_start <= end,
        SupplierExpenseAccrual.service_period_end >= start,
    ]
    if article_id is not None:
        conditions.append(
            or_(
                SupplierExpenseAccrual.article_id.is_(None),
                SupplierExpenseAccrual.article_id == article_id,
            )
        )
    return await session.scalar(select(SupplierExpenseAccrual).where(*conditions))


async def expense_line_accruals_covering(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    start: date,
    end: date,
    article_id: uuid.UUID | None = None,
) -> list[SupplierExpenseAccrual]:
    """ВСЕ живые начисления по строкам платежа, пересекающие период.

    Ручное признание должно снять их все: гард в ``ensure_month_accrual`` блокирует месяц при
    любом оставшемся, и отмена одного из двух (основной платёж и доплата за тот же месяц)
    молча уменьшала расход — разовое снято, помесячное не создано.
    """
    conditions = [
        SupplierExpenseAccrual.counterparty_id == counterparty_id,
        SupplierExpenseAccrual.expense_draft_line_id.is_not(None),
        SupplierExpenseAccrual.status != "cancelled",
        SupplierExpenseAccrual.service_period_start <= end,
        SupplierExpenseAccrual.service_period_end >= start,
    ]
    if article_id is not None:
        conditions.append(
            or_(
                SupplierExpenseAccrual.article_id.is_(None),
                SupplierExpenseAccrual.article_id == article_id,
            )
        )
    rows = await session.scalars(select(SupplierExpenseAccrual).where(*conditions))
    return list(rows.all())


async def sync_expense_line_accrual(
    session: AsyncSession,
    line: ExpenseDraftLine,
) -> SupplierExpenseAccrual | None:
    """Создать начисление по строке ручного платежа, если у неё указан получатель и период.

    ``auto_recognize_monthly`` — исключение, и оно закрывает двойной счёт. Расход по такому
    платежу признаётся помесячно самоактами из его дебиторки (``subscription_accruals``), а
    строка признала бы его ещё раз, целиком и последним месяцем периода: платёж 9 000 ₽ за
    апрель-июнь давал 9 000 со строки ПЛЮС три самоакта по 3 000 — 18 000 ₽ расхода в реестре
    признания, где дедупликации нет. Деньги при этом сходились, врала только прибыль, поэтому
    заметить это можно было лишь глазами.

    Договор услуги и режим «счёт за период» — то же самое исключение по той же причине: там
    расход месяца считает не платёж, а договор или помесячный самоакт. Разница лишь в том, что
    галочку ``auto_recognize_monthly`` человек ставит руками, а эти два режима включаются
    разметкой карточки, и строка платежа о них не знала вовсе.
    """
    if (
        line.counterparty_id is None
        or line.service_period_start is None
        or line.service_period_end is None
        or line.auto_recognize_monthly
    ):
        return None
    start, end = validate_period(line.service_period_start, line.service_period_end)
    if await _recognized_by_other_mechanism(
        session,
        counterparty_id=line.counterparty_id,
        start=start,
        end=end,
        article_id=line.article_id,
    ):
        return None
    existing = await session.scalar(
        select(SupplierExpenseAccrual).where(
            SupplierExpenseAccrual.expense_draft_line_id == line.id
        )
    )
    if existing is None:
        existing = SupplierExpenseAccrual(
            counterparty_id=line.counterparty_id,
            expense_draft_line_id=line.id,
            payment_draft_id=line.draft_id,
            article_id=line.article_id,
            location_id=line.location_id,
            amount=line.amount,
            service_period_start=start,
            service_period_end=end,
            status="scheduled",
        )
        session.add(existing)
    else:
        existing.counterparty_id = line.counterparty_id
        existing.payment_draft_id = line.draft_id
        existing.article_id = line.article_id
        existing.location_id = line.location_id
        # Признанный расход не двигаем молча (см. sync_invoice_accrual).
        if existing.status != "recognized":
            existing.amount = line.amount
            existing.service_period_start = start
            existing.service_period_end = end
    await session.flush()
    return existing


async def recognize_due_expenses(
    session: AsyncSession,
    *,
    as_of: date | None = None,
    commit: bool = True,
    invoice_ids: Sequence[uuid.UUID] | None = None,
) -> int:
    """Признать расходы после завершения дня окончания услуги.

    ``service_period_end < as_of`` намеренно строго: весь последний день услуга ещё
    оказывается, признание выполняется первым запуском следующего дня.

    ``invoice_ids`` ограничивает признание своими документами. Нужен ручному признанию
    платежа: без ограничения нажатие кнопки по одному контрагенту утаскивало бы в P&L все
    созревшие начисления системы — корректно по сути, но для человека это неожиданный
    побочный эффект действия, которое он адресовал одному платежу.
    """
    cutoff = as_of or datetime.now(MOSCOW_TZ).date()
    conditions = [
        SupplierExpenseAccrual.status == "scheduled",
        SupplierExpenseAccrual.service_period_end < cutoff,
    ]
    if invoice_ids is not None:
        if not invoice_ids:
            return 0
        conditions.append(SupplierExpenseAccrual.invoice_id.in_(invoice_ids))
    rows = list(
        (
            await session.scalars(
                select(SupplierExpenseAccrual).where(*conditions).with_for_update(skip_locked=True)
            )
        ).all()
    )
    # Признание в ЗАКРЫТЫЙ месяц не идёт: цифра того месяца уже ушла в отчётность. Такой
    # расход остаётся scheduled и ждёт, пока человек либо откроет период, либо перенесёт
    # период услуги. Молча дописать его в закрытый месяц — худшее из возможного: отчёт
    # изменится сам, и никто не узнает.
    from app.services import accounting_periods

    closed = await accounting_periods.closed_months(session)
    now = datetime.now(UTC)
    recognized = 0
    for row in rows:
        month = recognition_month(row.service_period_end)
        # Закрытым считается не только месяц признания: расход раскладывается по ВСЕМ месяцам
        # периода, и акт за июль-сентябрь, признанный в сентябре, дописал бы 12 000 ₽ в июль.
        if closed.intersection(
            accounting_periods.months_between(row.service_period_start, row.service_period_end)
        ):
            continue
        row.status = "recognized"
        row.recognition_month = month
        row.recognized_at = now
        recognized += 1
    if commit:
        await session.commit()
    else:
        await session.flush()
    return recognized


async def change_accrual_period(
    session: AsyncSession,
    *,
    accrual: SupplierExpenseAccrual,
    start: date,
    end: date,
    actor_user_id: uuid.UUID | None,
    reason: str | None = None,
) -> SupplierExpenseAccrual:
    """Перенести период и записать полный аудит. Проверку права делает API-слой."""
    start, end = validate_period(start, end)
    # Закрытый месяц: и те, ИЗ которых расход уносят, и те, В которые приносят. Проверяем
    # ВЕСЬ период, а не месяц признания: отчёт раскладывает сумму по всем месяцам периода,
    # и правка 01.07–31.07 → 01.06–31.07 добавила бы расход в закрытый июнь, пройдя гард по
    # recognition_month (июль) незамеченной.
    from app.services import accounting_periods

    await accounting_periods.assert_period_open(
        session,
        accrual.service_period_start,
        accrual.service_period_end,
        action="перенос периода",
    )
    await accounting_periods.assert_period_open(session, start, end, action="перенос периода")
    old_start = accrual.service_period_start
    old_end = accrual.service_period_end
    old_month = accrual.recognition_month
    # Признанный расход, чей НОВЫЙ период ещё не закончился, признанным быть не может:
    # услуга ещё оказывается. Возвращаем в scheduled — ночная джоба признает его заново,
    # когда период реально завершится (та же строгая граница end < today, что и у неё).
    today = datetime.now(MOSCOW_TZ).date()
    revert_to_scheduled = accrual.status == "recognized" and end >= today
    new_month = (
        recognition_month(end)
        if accrual.status == "recognized" and not revert_to_scheduled
        else None
    )
    session.add(
        SupplierServicePeriodChange(
            accrual_id=accrual.id,
            old_period_start=old_start,
            old_period_end=old_end,
            new_period_start=start,
            new_period_end=end,
            old_recognition_month=old_month,
            new_recognition_month=new_month,
            actor_user_id=actor_user_id,
            reason=(reason or "").strip() or None,
        )
    )
    accrual.service_period_start = start
    accrual.service_period_end = end
    if revert_to_scheduled:
        accrual.status = "scheduled"
        accrual.recognition_month = None
        accrual.recognized_at = None
    elif accrual.status == "recognized":
        accrual.recognition_month = new_month

    if accrual.invoice_id is not None:
        invoice = await session.get(SupplierInvoice, accrual.invoice_id)
        if invoice is not None:
            invoice.service_period_start = start
            invoice.service_period_end = end
            invoice.service_period_source = "corrected"
            invoice.service_period_status = "ready"
            # Период двигает и дату вступления документа в силу — вторая дверь правки обязана
            # пересматривать активацию так же, как первая, иначе баланс на дату разойдётся с
            # витринами именно там, где оператор только что навёл порядок.
            await _resync_activation_after_period_change(session, invoice)
    if accrual.expense_draft_line_id is not None:
        line = await session.get(ExpenseDraftLine, accrual.expense_draft_line_id)
        if line is not None:
            line.service_period_start = start
            line.service_period_end = end
            line.service_period_source = "corrected"
    # ВТОРАЯ ДВЕРЬ ПРАВКИ ПЕРИОДА ОБЯЗАНА ПРИЗНАВАТЬ СРАЗУ, КАК И ПЕРВАЯ. У ``sync_invoice_accrual``
    # догоняющий вызов есть (см. конец функции), здесь его не было: человек исправлял период на
    # уже прошедший, начисление оставалось ``scheduled``, и расход не появлялся в отчёте до
    # ближайших 00:05. Он видел, что дата прошла, а строки нет, и читал это как поломку.
    # Условие то же самое, поэтому разойтись две двери не могут.
    # Ограничение по документу обязательно: без него правка одного периода утащила бы в отчёт
    # все созревшие начисления системы. У начисления по строке платежа фильтра нет — его
    # по-прежнему признаёт ночная джоба.
    if (
        accrual.status == "scheduled"
        and accrual.invoice_id is not None
        and end < datetime.now(MOSCOW_TZ).date()
    ):
        await recognize_due_expenses(session, invoice_ids=[accrual.invoice_id], commit=False)
    await session.commit()
    await session.refresh(accrual)
    return accrual


async def set_invoice_service_period(
    session: AsyncSession,
    *,
    invoice: SupplierInvoice,
    start: date,
    end: date,
    actor_user_id: uuid.UUID | None,
    reason: str | None = None,
) -> SupplierExpenseAccrual:
    """Проставить период у накладной, у которой начисления ещё нет.

    Это единственный вход, разрывающий chicken-and-egg периодов: ``sync_invoice_accrual``
    заводит начисление только при ``status='ready'``, а перенести период у существующего
    начисления умеет ``change_accrual_period`` — но начисления нет, пока период не проставлен.
    Здесь период пишется прямо в накладную, статус переводится в ``ready``, и начисление
    рождается тем же ``sync_invoice_accrual``. Если начисление уже есть (накладная из почты
    или повторная правка) — отдаём штатному ``change_accrual_period`` ради полного аудита.
    """
    start, end = validate_period(start, end)
    existing = await session.scalar(
        select(SupplierExpenseAccrual).where(SupplierExpenseAccrual.invoice_id == invoice.id)
    )
    if existing is not None:
        return await change_accrual_period(
            session,
            accrual=existing,
            start=start,
            end=end,
            actor_user_id=actor_user_id,
            reason=reason,
        )

    # Первичная установка — не «перенос», поэтому в журнал SupplierServicePeriodChange
    # (у него old_* обязательны) не пишем: факт ручного ввода несёт source='manual' на
    # накладной, а последующие правки идут через change_accrual_period с полным аудитом.
    invoice.service_period_start = start
    invoice.service_period_end = end
    invoice.service_period_source = "manual"
    invoice.service_period_status = "ready"
    accrual = await sync_invoice_accrual(session, invoice)
    await _resync_activation_after_period_change(session, invoice)
    await session.commit()
    if accrual is not None:
        await session.refresh(accrual)
    return accrual


async def _resync_activation_after_period_change(
    session: AsyncSession, invoice: SupplierInvoice
) -> None:
    """Пересмотреть вступление документа в силу после правки периода услуги. Без коммита.

    Период двигает дату вступления закрывающего в силу (правило 4 считает по поздней из даты
    документа и конца периода), поэтому правка периода может как ОТЛОЖИТЬ уже действующий
    документ, так и наоборот. Пока этого пересмотра не было, оператор, проставивший период
    задним числом, получал документ, который по всем витринам действует, а по балансу на дату —
    ещё нет: два источника правды расходились без пути назад.

    Отложить — значит вернуть авансам зачёты этого документа: он больше не основание.
    Разбудить — провести штатным ``apply_closing_document``, той же дверью, что и джоба."""
    if invoice.doc_kind != "closing" or invoice.payment_status == "void":
        return
    from app.services import supplier_prepayments as prepayments

    effective = prepayments._closing_effective_date(invoice)
    today = datetime.now(MOSCOW_TZ).date()
    should_wait = effective is not None and effective > today
    if should_wait and invoice.activation_status == "active":
        await prepayments.release_invoice_prepayment_allocations(session, invoice)
        invoice.activation_status = "pending"
    elif not should_wait and invoice.activation_status == "pending":
        await prepayments.apply_closing_document(session, invoice)


async def _cancel_accrual(
    session: AsyncSession, accrual: SupplierExpenseAccrual | None
) -> SupplierExpenseAccrual | None:
    """Перевести начисление в ``cancelled``. Идемпотентно, без коммита.

    ЗАКРЫТЫЙ МЕСЯЦ НЕ ТРОГАЕМ. Отмена — единственный путь, которым признанный расход
    уменьшается АВТОМАТИЧЕСКИ, без участия человека: настоящий УПД, пришедший в августе за
    июль, помечает документ информационным или замещает самоакт, и начисление снимается.
    Если июль уже закрыт, его цифра ушла в отчётность — снять её молча нельзя, а спросить
    некого: это фоновый путь. Оставляем начисление как есть; расхождение с пришедшим
    документом видно в сверке, и человек решит его, открыв период.
    """
    if accrual is None or accrual.status == "cancelled":
        return accrual
    if accrual.status == "recognized":
        from app.services import accounting_periods

        closed = await accounting_periods.closed_months(session)
        if closed.intersection(
            accounting_periods.months_between(
                accrual.service_period_start, accrual.service_period_end
            )
        ):
            return accrual
    accrual.status = "cancelled"
    await session.flush()
    return accrual


async def cancel_invoice_accrual(
    session: AsyncSession, invoice_id: uuid.UUID
) -> SupplierExpenseAccrual | None:
    """Отменить начисление аннулированной накладной. Не коммитит.

    Начисление рождается при ``ready`` независимо от оплаты, а аннулирование накладной его
    не трогало — джоба признания превращала расход по несуществующей накладной в фантом в
    P&L. Здесь начисление переводится в ``cancelled`` (джоба его пропускает, отчёт исключает).
    Если расход уже признан — аннулирование накладной сторнирует его: накладной нет, значит и
    расхода нет.
    """
    accrual = await session.scalar(
        select(SupplierExpenseAccrual).where(SupplierExpenseAccrual.invoice_id == invoice_id)
    )
    return await _cancel_accrual(session, accrual)


def money(value: Decimal | int | float | None) -> Decimal:
    # ROUND_HALF_UP — как во ВСЁМ платёжном контуре (_money в counterparty_payments и др.).
    # Дефолтный HALF_EVEN дал бы иное округление на пол-копейке и расхождение балансов.
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
