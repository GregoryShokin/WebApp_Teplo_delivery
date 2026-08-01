"""Абонентские платежи: помесячное признание расхода без закрывающего документа.

ЗАДАЧА. Часть поставщиков работает по абонентской плате помесячно, а закрывающие документы
присылает не всегда: Наумченко не присылает вовсе, Микроэль присылает, но за май пропустила.
Платят им нередко вперёд за несколько месяцев — 9 000 ₽ за апрель-июнь одним переводом. Такие
деньги висели дебиторкой целиком до конца всего периода, а расход не признавался ни в одном
месяце: на 01.08.2026 так стояло 311 969 ₽ у десяти контрагентов.

РЕШЕНИЕ — ТО ЖЕ, ЧТО У АРЕНДЫ. Раз в месяц по такому платежу заводится внутренний закрывающий
документ на долю периода: 9 000 за три месяца → 3 000 в апреле, 3 000 в мае, 3 000 в июне. Он
гасит дебиторку и признаёт расход в своём месяце. Ровно этим механизмом уже живёт аренда
(``lease_accruals``): арендодатель документов не выставляет, но долг известен и считается сам.
Никакой новой сущности здесь нет — тот же ``SupplierInvoice``, поэтому все витрины (ДЗ/КЗ,
признание расходов, сверка) видят его без единой правки.

ЧЕМ ОН ОТЛИЧАЕТСЯ ОТ НАСТОЯЩЕГО ДОКУМЕНТА. ``source='self_billed'`` — контрагент его не
выставлял и не подтверждал. Расход в управленческом P&L он даёт, а в налоговую базу УСН идти
не может: без первички инспекция такой расход снимет. Признак нужен именно полем, а не
догадкой по косвенным данным.

ЗАМЕЩЕНИЕ НАСТОЯЩИМ УПД. Если документ за тот же период всё-таки приходит, внутренний
аннулируется (``supersede_self_billed``), а дебиторку гасит настоящий. Без этого расход
задвоился бы: один раз по самоакту, второй — по УПД, и P&L месяца вырос бы вдвое.

ОКРУГЛЕНИЕ. Доли считаются как ``сумма / N`` вниз до копейки, а последний месяц забирает
остаток: 10 000 / 3 = 3 333,33 + 3 333,33 + 3 333,34. Иначе на длинных периодах копейки
накапливались бы и дебиторка не закрывалась бы до нуля.
"""

from __future__ import annotations

import calendar
import uuid
from datetime import date
from decimal import ROUND_DOWN, Decimal
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CounterpartyPayableProfile,
    InvoicePaymentAllocation,
    SupplierInvoice,
    SupplierPrepayment,
)
from app.services import supplier_service_periods as periods

SELF_BILLED_SOURCE = "self_billed"
# Режим «счёт за период»: расход признаётся по окончании периода, УПД не ждём (канон владельца).
BILLING_MODE_FIXED_TARIFF = "fixed_tariff"
# Предоплаты, которые ещё держат дебиторку и потому подлежат помесячному признанию.
OPEN_PREPAYMENT_STATUSES = ("open", "partially_settled")


def month_bounds(month: date) -> tuple[date, date]:
    first = month.replace(day=1)
    return first, first.replace(day=calendar.monthrange(first.year, first.month)[1])


def add_months(month: date, count: int) -> date:
    total = month.month - 1 + count
    return date(month.year + total // 12, total % 12 + 1, 1)


def self_billed_external_id(prepayment_id: uuid.UUID, month: date) -> str:
    """Ключ идемпотентности под существующим уникумом ``source + external_id``.

    Повторный прогон джобы (и ручной пересбор) ничего не задваивает — ровно как у аренды.
    """
    return f"self:{prepayment_id}:{month:%Y-%m}"


def superseded_external_id(external_id: str | None, winner_id: uuid.UUID) -> str:
    """Ключ аннулированного самоакта: освобождает исходный под новое признание.

    Уникум ``source + external_id`` один на все самоакты, поэтому пока ключ занят void-строкой,
    месяц нельзя признать заново — а он снова становится непризнанным, если настоящий документ
    убрали. Хвост с id победившего документа заодно показывает в базе, чем именно замещали.
    """
    return f"{external_id or 'self'}:superseded:{winner_id}"


def monthly_shares(amount: Decimal, months: int) -> list[Decimal]:
    """Разбить сумму на ``months`` долей; остаток от округления забирает последняя.

    Вниз, а не арифметически: при делении 10 000 / 3 три доли по 3 333,33 дают недобор
    в копейку, и его отдаём последнему месяцу. Округление вверх дало бы перебор — дебиторка
    ушла бы в минус на копейку, а это уже расхождение баланса.
    """
    if months < 1:
        raise ValueError("Период должен покрывать хотя бы один месяц")
    total = periods.money(amount)
    base = (total / months).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    shares = [base] * (months - 1)
    shares.append(total - base * (months - 1))
    return shares


def covered_months(prepayment: SupplierPrepayment) -> list[date]:
    """Месяцы, которые покрывает платёж: от начала периода, по одному на каждый месяц.

    Число месяцев берём из ``service_period_months``, а если его нет — считаем по самому
    периоду. Раньше пустое поле означало «один месяц», и квартальная лицензия с периодом
    01.07–30.09 признавала всю сумму июлем: 36 000 ₽ падали в один месяц вместо 12 000 в три.
    Поле заполняется только из окна «Новый платёж», а период приходит ещё и из счёта, из ЭДО
    и из ручной разметки — там оно пустое всегда.
    """
    start = prepayment.service_period_start
    if start is None:
        return []
    count = prepayment.service_period_months
    if not count:
        end = prepayment.service_period_end
        count = (
            (end.year - start.year) * 12 + (end.month - start.month) + 1 if end is not None else 1
        )
    first = start.replace(day=1)
    return [add_months(first, index) for index in range(max(count, 1))]


def covers_month(
    period_start: date | None, period_end: date | None, invoice_date: date | None, month: date
) -> Any:
    """Условие «документ относится к этому месяцу» — с откатом на дату документа.

    Период услуги проставлен далеко не у всех: на проде 01.08.2026 он был у 4 закрывающих
    документов из 221. Сравнивать только периоды — значит для остальных 217 не увидеть
    первичку вовсе: самоакт встал бы поверх настоящего УПД, и расход задвоился бы молча.

    Поэтому у документа без периода за период считаем месяц его даты: закрывающие выставляют
    концом месяца, за который они выданы. Это слабее явного периода, но неизмеримо лучше,
    чем ничего.
    """
    first, last = month_bounds(month)
    with_period = and_(
        period_start.isnot(None),
        period_end.isnot(None),
        period_start <= last,
        period_end >= first,
    )
    by_date = and_(
        or_(period_start.is_(None), period_end.is_(None)),
        invoice_date.isnot(None),
        invoice_date >= first,
        invoice_date <= last,
    )
    return or_(with_period, by_date)


async def _recognized_by_billing_mode(
    session: AsyncSession, prepayment: SupplierPrepayment
) -> bool:
    """Признаётся ли платёж помесячно по режиму контрагента, без явной галочки.

    Режим «счёт за период» (канон владельца от 01.08.2026, режим 2): лицензия с фиксированной
    платой и указанным в счёте периодом — Синапсис, АЙКО, Лемма, ДоксИнБокс. Расход признаётся
    по окончании периода, УПД по ним не ждут: месяц оплачен, 31-го обязательство исполнено по
    определению. Без этой ветки такие платежи вечно висели в «ждём документ» и краснели
    просрочкой за документ, которого никто не выставит.
    """
    if prepayment.service_period_status != "ready" or prepayment.service_period_start is None:
        return False
    mode = await session.scalar(
        select(CounterpartyPayableProfile.service_billing_mode).where(
            CounterpartyPayableProfile.counterparty_id == prepayment.counterparty_id
        )
    )
    return mode == BILLING_MODE_FIXED_TARIFF


async def _real_closing_exists(
    session: AsyncSession, prepayment: SupplierPrepayment, month: date
) -> bool:
    """Есть ли за этот месяц закрывающий документ, который уже признал расход.

    Если есть — самоакт не нужен: расход подтверждён, и второй документ на тот же период
    удвоил бы и расход, и гашение дебиторки.

    Начисление по договору услуги (``svc:``) считается наравне с первичкой: это тот же расход
    месяца, просто посчитанный нами. Иначе контрагент с договором И абонентским платежом
    получал бы двойное признание — по одному разу с каждой машины.

    Самоакты других предоплат (``self:``) НЕ блокируют: два платежа одному контрагенту за один
    месяц — законная история (две услуги, доплата), и каждый признаёт свою долю.
    """
    found = await session.scalar(
        select(SupplierInvoice.id).where(
            SupplierInvoice.counterparty_id == prepayment.counterparty_id,
            SupplierInvoice.direction == "payable",
            SupplierInvoice.doc_kind == "closing",
            SupplierInvoice.payment_status != "void",
            or_(
                SupplierInvoice.source != SELF_BILLED_SOURCE,
                SupplierInvoice.external_id.like("svc:%"),
            ),
            # Сверяем статью: у контрагента бывает несколько услуг (у Манго за 30.06 сразу два
            # акта — 6 108,69 и 5 250,00), и документ по одной не должен глушить признание по
            # другой. Документ без статьи считаем относящимся к любой — иначе он не закроет
            # ничего. Тот же фильтр стоит в начислении по договору.
            or_(
                SupplierInvoice.dds_article_id.is_(None),
                SupplierInvoice.dds_article_id == prepayment.article_id,
            ),
            covers_month(
                SupplierInvoice.service_period_start,
                SupplierInvoice.service_period_end,
                SupplierInvoice.invoice_date,
                month,
            ),
        )
    )
    return found is not None


async def ensure_month_accrual(
    session: AsyncSession,
    prepayment: SupplierPrepayment,
    month: date,
    *,
    as_of: date,
) -> SupplierInvoice | None:
    """Завести внутренний закрывающий документ за месяц. Идемпотентно. Не коммитит.

    ``None`` возвращается, когда признавать нечего: помесячный режим выключен, месяц ещё не
    закончился, самоакт уже есть, настоящий документ пришёл или дебиторка исчерпана.
    """
    if not prepayment.auto_recognize_monthly and not await _recognized_by_billing_mode(
        session, prepayment
    ):
        return None
    if prepayment.status not in OPEN_PREPAYMENT_STATUSES:
        return None
    months = covered_months(prepayment)
    if month.replace(day=1) not in months:
        return None

    _first, last = month_bounds(month)
    # Строго после окончания месяца: весь последний день услуга ещё оказывается. Та же
    # граница, что у recognize_due_expenses, — иначе расход признавался бы на день раньше.
    if last >= as_of:
        return None

    external_id = self_billed_external_id(prepayment.id, month)
    existing = await session.scalar(
        select(SupplierInvoice).where(
            SupplierInvoice.source == SELF_BILLED_SOURCE,
            SupplierInvoice.external_id == external_id,
        )
    )
    if existing is not None:
        return None
    if await _real_closing_exists(session, prepayment, month):
        return None

    index = months.index(month.replace(day=1))
    share = monthly_shares(periods.money(prepayment.amount), len(months))[index]
    remaining = periods.money(prepayment.amount) - periods.money(prepayment.amount_settled)
    if remaining <= 0:
        return None
    # Дебиторка могла частично уйти на настоящий УПД за другой месяц — тогда признаём только
    # то, что осталось: документ не имеет права гасить больше, чем есть.
    share = min(share, remaining)

    period_start, period_end = month_bounds(month)
    invoice = SupplierInvoice(
        counterparty_id=prepayment.counterparty_id,
        source=SELF_BILLED_SOURCE,
        external_id=external_id,
        direction="payable",
        doc_kind="closing",
        # finance — иначе ни правило 1 канона, ни авто-зачёт предоплат этот документ не увидят.
        operational_scope="finance",
        number=f"Без документа {month:%m.%Y}",
        invoice_date=period_end,
        amount=share,
        dds_article_id=prepayment.article_id,
        service_period_start=period_start,
        service_period_end=period_end,
        service_period_source="self_billed",
        # ready — без этого sync_invoice_accrual молча не создаст строку признания расхода.
        service_period_status="ready",
    )
    session.add(invoice)
    await session.flush()

    # Гасим ИМЕННО ту дебиторку, из которой документ родился, а не FIFO по всем открытым:
    # у контрагента может висеть несколько платежей за разные периоды, и FIFO закрыл бы
    # чужой — расход месяца сошёлся бы, а связь «эти деньги за этот период» потерялась.
    session.add(
        InvoicePaymentAllocation(
            invoice_id=invoice.id,
            prepayment_id=prepayment.id,
            amount=share,
            source_kind="prepayment",
        )
    )
    prepayment.amount_settled = periods.money(prepayment.amount_settled) + share
    prepayment.status = (
        "settled"
        if periods.money(prepayment.amount_settled) >= periods.money(prepayment.amount)
        else "partially_settled"
    )
    invoice.payment_status = "paid"
    await periods.sync_invoice_accrual(session, invoice)
    await session.flush()
    return invoice


async def accrue_due_months(
    session: AsyncSession, *, as_of: date | None = None, commit: bool = True
) -> list[SupplierInvoice]:
    """Признать все истёкшие месяцы по абонентским платежам. Идемпотентно."""
    today = as_of or date.today()
    # Кроме платежей с явной галочкой «признавать помесячно» берём режим «счёт за период»:
    # у таких контрагентов (Синапсис, АЙКО, Лемма, ДоксИнБокс) лицензия оплачена за конкретный
    # месяц, и 31-го обязательство исполнено по определению — УПД по ним не ждут вовсе.
    prepayments = list(
        (
            await session.scalars(
                select(SupplierPrepayment)
                .outerjoin(
                    CounterpartyPayableProfile,
                    CounterpartyPayableProfile.counterparty_id
                    == SupplierPrepayment.counterparty_id,
                )
                .where(
                    or_(
                        SupplierPrepayment.auto_recognize_monthly.is_(True),
                        and_(
                            CounterpartyPayableProfile.service_billing_mode
                            == BILLING_MODE_FIXED_TARIFF,
                            SupplierPrepayment.service_period_status == "ready",
                            SupplierPrepayment.service_period_start.is_not(None),
                        ),
                    ),
                    SupplierPrepayment.status.in_(OPEN_PREPAYMENT_STATUSES),
                )
            )
        ).all()
    )
    created: list[SupplierInvoice] = []
    for prepayment in prepayments:
        for month in covered_months(prepayment):
            invoice = await ensure_month_accrual(session, prepayment, month, as_of=today)
            if invoice is not None:
                created.append(invoice)
    if commit:
        await session.commit()
    else:
        await session.flush()
    return created


class RecognitionRefused(ValueError):
    """Признать расход по этому платежу нельзя — с объяснением, почему именно."""


# Платежи, которые помесячно не признают никогда: ``goods`` гасится накладной, ``deposit`` —
# возвратом. Календарные доли к ним не применимы в принципе.
NON_RECOGNIZABLE_KINDS = ("goods", "deposit")


def refusal_reason(
    prepayment: SupplierPrepayment, *, documents_expected: bool, covered_by_agreement: bool
) -> str | None:
    """Почему признать расход по этому платежу нельзя — или ``None``, если можно.

    Одно правило на два потребителя: сервис отказывает по нему, а витрина по нему же решает,
    показывать ли кнопку. Пока правило жило только в сервисе, кнопка стояла на строках, где
    ответ всегда был 409 — примерно на половине вкладки «ждём документ».

    ``prepaid_bill`` — ДЗ по оплаченному счёту. По канону её гасит закрывающий УПД (правило 2),
    поэтому признавать её руками нельзя, ПОКА документ ожидается. Там, где документов не будет
    (разовые работы, договор informal), запрет превращался бы в тупик: оплаченный счёт от
    такого контрагента не стал бы расходом никогда.
    """
    if prepayment.kind in NON_RECOGNIZABLE_KINDS:
        return "Такой платёж закрывается накладной или возвратом, а не признанием по месяцам"
    if prepayment.kind == "prepaid_bill" and documents_expected:
        return "Это оплата счёта — её закроет закрывающий документ, а не признание по месяцам"
    if prepayment.status not in OPEN_PREPAYMENT_STATUSES:
        return "Платёж уже закрыт — признавать нечего"
    if covered_by_agreement:
        return "По этому контрагенту действует договор услуги — расход начисляется автоматически"
    return None


def months_between(start: date, end: date) -> int:
    """Сколько календарных месяцев покрывает период, включая начальный и конечный."""
    return (end.year - start.year) * 12 + (end.month - start.month) + 1


async def _self_billed_exists(session: AsyncSession, prepayment: SupplierPrepayment) -> bool:
    found = await session.scalar(
        select(SupplierInvoice.id).where(
            SupplierInvoice.source == SELF_BILLED_SOURCE,
            SupplierInvoice.external_id.like(f"self:{prepayment.id}:%"),
            SupplierInvoice.payment_status != "void",
        )
    )
    return found is not None


async def _draft_line_accrual(
    session: AsyncSession, prepayment: SupplierPrepayment, *, start: date, end: date
) -> Any | None:
    """Разовое начисление по строке платежа, покрывающее тот же период.

    Оно есть, когда платёж оформили в «Новом платеже» с периодом, но без помесячного режима:
    ``sync_expense_line_accrual`` завела расход на ВСЮ сумму последним месяцем периода. Включить
    поверх помесячное признание — значит получить 9 000 (строка) + 3×3 000 (самоакты) = 18 000 ₽
    расхода по одному платежу. Ровно этот дефект уже был на проде у Наумченко.

    Ищем по КОНТРАГЕНТУ и пересечению периодов, а не по черновику платежа. Связь через
    черновик рвётся штатно: если выписка приходит раньше отметки «оплачено», операцию разбирает
    классификатор, и предоплата садится на проводку ``bank_operation`` — ссылки на черновик у
    неё уже нет, гард молчал, а расход задваивался.
    """
    from app.models import SupplierExpenseAccrual

    return await session.scalar(
        select(SupplierExpenseAccrual).where(
            SupplierExpenseAccrual.counterparty_id == prepayment.counterparty_id,
            SupplierExpenseAccrual.expense_draft_line_id.is_not(None),
            SupplierExpenseAccrual.status != "cancelled",
            SupplierExpenseAccrual.service_period_start <= end,
            SupplierExpenseAccrual.service_period_end >= start,
        )
    )


async def recognize_prepayment_period(
    session: AsyncSession,
    prepayment: SupplierPrepayment,
    *,
    start: date,
    end: date,
    as_of: date,
    article_id: uuid.UUID | None = None,
) -> list[SupplierInvoice]:
    """Признать расход по зависшему платежу за указанный период. Коммитит.

    Это единственный способ вытащить деньги из состояния «заплатили, а расходом не стало»
    руками: документа не будет, а месяцы всё равно надо закрыть. Дальше работает штатный
    механизм абонентских платежей — самоакты по долям периода, гашение дебиторки и признание
    в P&L; ночную джобу не ждём, иначе владелец, только что указавший период, увидит результат
    лишь завтра.

    Отказы — там, где расход задвоился бы или деньги закрылись бы не тем механизмом. Все три
    пути к двойному счёту закрыты явно: договор услуги (начисляет ночная джоба), уже созданные
    самоакты (повторный период добавил бы месяцы поверх) и разовое начисление по строке платежа.
    """
    # Локальный импорт: service_agreement_accruals импортирует этот модуль, и на уровне файла
    # получился бы цикл.
    from app.services.counterparty_settlement_ledger import documents_not_expected
    from app.services.service_agreement_accruals import covered_by_agreement

    start, end = periods.validate_period(start, end)
    # Договор проверяем по ПЕРЕСЕЧЕНИЮ с периодом, а не по его концу: договор, закрытый в мае,
    # начислил апрель и май, и признание за апрель-сентябрь легло бы поверх них — в P&L
    # апреля 3 000 по договору плюс 2 000 самоактом.
    agreement_covers = await covered_by_agreement(
        session, prepayment.counterparty_id, on=start, article_id=prepayment.article_id
    ) or await covered_by_agreement(
        session, prepayment.counterparty_id, on=end, article_id=prepayment.article_id
    )
    refusal = refusal_reason(
        prepayment,
        documents_expected=not await documents_not_expected(
            session, prepayment.counterparty_id, today=as_of
        ),
        covered_by_agreement=agreement_covers,
    )
    if refusal is not None:
        raise RecognitionRefused(refusal)
    if prepayment.article_id is None:
        # Статья нужна не для красоты: она уходит в самоакт как разрез расхода и участвует в
        # проверке «а не пришёл ли настоящий документ по этой же услуге». Платежи из выписки
        # часто не размечены, поэтому статью принимаем прямо здесь, а не отправляем человека
        # размечать проводку в другом разделе.
        if article_id is None:
            raise RecognitionRefused(
                "У платежа не указана статья ДДС — расход некуда отнести. Выберите статью"
            )
        prepayment.article_id = article_id
        # Статью дозаполнили — договор по ней мог и не проверяться выше (там статьи ещё не было).
        if await covered_by_agreement(
            session, prepayment.counterparty_id, on=start, article_id=prepayment.article_id
        ) or await covered_by_agreement(
            session, prepayment.counterparty_id, on=end, article_id=prepayment.article_id
        ):
            raise RecognitionRefused(
                "По этому контрагенту действует договор услуги — расход начисляется автоматически"
            )
    if await _self_billed_exists(session, prepayment):
        raise RecognitionRefused(
            "Расход по этому платежу уже признаётся помесячно. Изменить период можно на строке "
            "признания"
        )
    line_accrual = await _draft_line_accrual(session, prepayment, start=start, end=end)
    if line_accrual is not None:
        if line_accrual.status == "recognized":
            raise RecognitionRefused(
                "Расход по этому платежу уже признан целиком в "
                f"{line_accrual.recognition_month:%m.%Y} — признавать его ещё раз нельзя"
            )
        # Разовое начисление заменяем помесячным: иначе расход сложится дважды.
        line_accrual.status = "cancelled"

    prepayment.service_period_start = start
    prepayment.service_period_end = end
    prepayment.service_period_months = months_between(start, end)
    prepayment.service_period_status = "ready"
    prepayment.auto_recognize_monthly = True
    await session.flush()

    created: list[SupplierInvoice] = []
    for month in covered_months(prepayment):
        invoice = await ensure_month_accrual(session, prepayment, month, as_of=as_of)
        if invoice is not None:
            created.append(invoice)
    # Признаём сразу: закончившиеся месяцы должны попасть в P&L тем же действием, а не ночью.
    # Только свои документы — чужие созревшие начисления признает ночная джоба, это не дело
    # кнопки, нажатой по конкретному платежу.
    await periods.recognize_due_expenses(
        session, as_of=as_of, commit=False, invoice_ids=[invoice.id for invoice in created]
    )
    await session.commit()
    return created


async def _allocated_total(session: AsyncSession, invoice_id: uuid.UUID) -> Decimal:
    """Сколько уже разнесено на документ — чтобы переклейка оплаты не превысила его сумму."""
    total = await session.scalar(
        select(func.coalesce(func.sum(InvoicePaymentAllocation.amount), 0)).where(
            InvoicePaymentAllocation.invoice_id == invoice_id
        )
    )
    return periods.money(total or Decimal("0.00"))


async def supersede_self_billed(
    session: AsyncSession, invoice: SupplierInvoice
) -> list[SupplierInvoice]:
    """Аннулировать самоакты за период пришедшего НАСТОЯЩЕГО закрывающего документа.

    Иначе месяц оказался бы закрыт дважды: самоактом и УПД, — и расход в P&L, и гашение
    дебиторки удвоились бы. Возвращённая дебиторка тут же доступна настоящему документу:
    его собственное гашение идёт следом обычным путём (``apply_closing_document``).

    Пришедший УПД сильнее самоакта всегда: первичка есть только у него, а самоакт — наша
    временная замена на случай, когда документа нет.
    """
    if invoice.source == SELF_BILLED_SOURCE or invoice.doc_kind != "closing":
        return []
    # Период у пришедшего документа есть далеко не всегда (на проде — у 4 из 221). Без отката
    # на дату документа настоящий УПД не снимал бы наше признание, и месяц оказался бы закрыт
    # дважды — ровно то, ради чего эта функция и написана.
    if invoice.service_period_start is not None and invoice.service_period_end is not None:
        overlap = and_(
            SupplierInvoice.service_period_start <= invoice.service_period_end,
            SupplierInvoice.service_period_end >= invoice.service_period_start,
        )
    elif invoice.invoice_date is not None:
        first, last = month_bounds(invoice.invoice_date)
        overlap = and_(
            SupplierInvoice.service_period_start <= last,
            SupplierInvoice.service_period_end >= first,
        )
    else:
        return []

    victims = list(
        (
            await session.scalars(
                select(SupplierInvoice).where(
                    SupplierInvoice.counterparty_id == invoice.counterparty_id,
                    SupplierInvoice.source == SELF_BILLED_SOURCE,
                    SupplierInvoice.payment_status != "void",
                    overlap,
                )
            )
        ).all()
    )
    for victim in victims:
        allocations = list(
            (
                await session.scalars(
                    select(InvoicePaymentAllocation).where(
                        InvoicePaymentAllocation.invoice_id == victim.id
                    )
                )
            ).all()
        )
        for allocation in allocations:
            if allocation.prepayment_id is not None:
                prepayment = await session.get(SupplierPrepayment, allocation.prepayment_id)
                if prepayment is not None:
                    prepayment.amount_settled = max(
                        periods.money(prepayment.amount_settled) - periods.money(allocation.amount),
                        Decimal("0.00"),
                    )
                    prepayment.status = (
                        "open"
                        if periods.money(prepayment.amount_settled) <= 0
                        else "partially_settled"
                    )
                await session.delete(allocation)
                continue
            # Денежная аллокация: наше начисление УЖЕ ОПЛАЧЕНО живыми деньгами (постоплата по
            # договору услуги). Удалить её нельзя — платёж останется ничьим: предоплаты у него
            # нет, аллокации нет, и настоящий документ повиснет неоплаченным долгом при том, что
            # деньги контрагент получил. Переклеиваем оплату на победивший документ — деньги те
            # же, документ другой.
            paid = periods.money(allocation.amount)
            room = periods.money(invoice.amount) - await _allocated_total(session, invoice.id)
            keep = min(paid, max(room, Decimal("0.00")))
            if keep > 0:
                allocation.invoice_id = invoice.id
                allocation.amount = keep
                await session.flush()
            else:
                await session.delete(allocation)
            # Излишек (настоящий документ оказался дешевле нашей оценки) возвращаем дебиторкой:
            # эти деньги контрагенту переплачены и должны быть видны как его долг перед нами.
            excess = paid - keep
            if excess > 0:
                session.add(
                    SupplierPrepayment(
                        counterparty_id=victim.counterparty_id,
                        kind="subscription",
                        amount=excess,
                        amount_settled=Decimal("0.00"),
                        status="open",
                    )
                )
        victim.payment_status = "void"
        # Освобождаем ключ идемпотентности: он уникален (uq_supplier_invoice_source_external),
        # и пока его держит аннулированный документ, месяц не признать заново НИКОГДА. Это не
        # теория: убрали ошибочный УПД — и месяц молча остался бы без расхода вовсе.
        victim.external_id = superseded_external_id(victim.external_id, invoice.id)
        await periods.cancel_invoice_accrual(session, victim.id)
    await session.flush()
    return victims
