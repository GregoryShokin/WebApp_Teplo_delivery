"""Расшифровка строки ОПиУ: из чего сложилась цифра.

ЗАЧЕМ ЭТО ЕСТЬ. Каскад отвечает «сколько», но владелец принимает решение по «из-за кого».
Строка «Телекоммуникации» на 13 230 ₽ не говорит ничего; она же, раскрытая до «Микроэл
3 230 ₽ признано» и «Манго 10 000 ₽ оплачено, УПД нет», говорит всё — включая то, что
делать дальше.

ПОКАЗЫВАЕМ ДВА ВОПРОСА, А НЕ ВСЁ ПОДРЯД. Первый — «из чего сложилось это число». Второй —
«я заплатил 68 000 ₽ за рекламу, где они?»: ответ «платёж есть, расход по нему признаёт УПД»
обязан быть здесь же, иначе владелец ищет пропажу руками и находит недоверие к отчёту.

А вот третьего вопроса — «какие ещё деньги проходили по этим статьям» — никто не задаёт.
Оплата уже признанного расхода, перевод между счетами, выплата из Сейфа, посчитанная
зарплатным модулем: число строки они не меняют и не изменят. В июльской строке «Сайт и
приложение» такой платёж (80 455 ₽ от 08.07, закрывающий счёт за ИЮНЬ) стоял таблицей рядом
с признанием — владелец спросил ровно это, «расход показывается верный, но зачем эта
информация?». Теперь всё такое свёрнуто в одну итоговую строку внизу: видно, что ничего не
спрятано, и не приходится читать чужой месяц.

РАСШИФРОВКА НЕ СЧИТАЕТ ЗАНОВО. Все слои поднимаются теми же функциями, что и отчёт:
``cashflow.explain_line`` переиспользует ``_classify``, признание берёт ``details`` из того
же прохода, зарплата — ``by_employee``, собранный вместе с итогами. Своя выборка «почти по
тем же правилам» разошлась бы с отчётом ровно там, где уже ошибались: подстановка статьи из
карточки контрагента, деление периода услуги по месяцам, оклад «по востребованию».
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Counterparty, DdsArticle, Employee, PnlArticleRule, PnlLine
from app.models.inventory import InventoryAudit, InventoryAuditItem
from app.models.pnl import PnlIikoFact
from app.services.pnl import projector
from app.services.pnl.sources import cashflow as cash_source
from app.services.pnl.sources import deposits as deposits_source
from app.services.pnl.sources import inventory as inventory_source
from app.services.pnl.sources import manual as manual_source
from app.services.pnl.sources import payroll as payroll_source
from app.services.pnl.sources import recognition as recognition_source
from app.services.pnl.sources import waiting as waiting_source

#: Единственный вердикт исключённой кассы, который показывается таблицей.
#:
#: Все остальные исключения объединяет одно: они НЕ МЕНЯЮТ и НЕ ИЗМЕНЯТ число строки. Оплата
#: уже признанного расхода закрывает документ, который свой месяц давно получил; перевод
#: между счетами не расход вовсе; выплата из Сейфа посчитана зарплатным модулем. Показывать
#: их построчно — значит отвечать на вопрос, которого никто не задавал: владелец 03.08.2026
#: увидел в июльской строке «Сайт и приложение» платёж 80 455 ₽ от 08.07, закрывающий счёт за
#: ИЮНЬ, и спросил ровно это — «расход показывается верный, но зачем эта информация?».
#:
#: «Оплачено, документа нет» остаётся, потому что это единственное исключение, после которого
#: строка ЕЩЁ ВЫРАСТЕТ: документ приедет — сумма встанет в свой месяц. Им же и ловятся ошибки
#: разметки контрагентов.
WAITING_VERDICT = "excluded_accrual_counterparty"
WAITING_TITLE = "Оплачено, расход берётся из документа"

#: Чем подтверждён признанный расход — словами, которые не звучат упрёком там, где всё
#: работает как задумано. Прежняя подпись «без первички (самоакт/расчёт)» стояла у Синапсиса,
#: и владелец справедливо спросил, при чём тут отсутствие первички, если счета приходят:
#: счета действительно приходят, но признание в режиме «счёт за период» строится самоактом по
#: окончании месяца и закрывающего документа не ждёт вовсе. Пробел остался ровно один —
#: ``awaiting_document``.
ORIGIN_LABEL = {
    recognition_source.ORIGIN_DOCUMENT: "документ от контрагента",
    recognition_source.ORIGIN_LEASE: "начислено по договору аренды",
    recognition_source.ORIGIN_UTILITY: "по расчёту арендодателя",
    recognition_source.ORIGIN_BY_TARIFF: "начислено по тарифу — закрывающих не ждём",
    recognition_source.ORIGIN_AWAITING_DOCUMENT: "закрывающего документа ещё нет",
    recognition_source.ORIGIN_PAYMENT_LINE: "начислено по строке платежа, документа нет",
}

#: Направления зеркала iiko. ``total`` не показываем строкой — это и есть итог группы.
DIRECTION_TITLE = {
    "rolls": "Роллы",
    "pizza": "Пицца",
    "hot_shop": "Горячий цех",
    "bar": "Бар",
    "unmapped": "Не отнесено к направлению",
}


@dataclass(slots=True)
class DrillRow:
    """Одна составляющая строки: контрагент, сотрудник, направление, документ."""

    title: str
    subtitle: str | None
    row_date: date | None
    amount: Decimal
    #: ``included`` — в сумме строки; ``waiting`` — деньги ушли, документа нет;
    #: ``excluded`` — учтено в другом месте; ``info`` — справочно, в сумму не входит.
    kind: str


@dataclass(slots=True)
class DrillGroup:
    stream: str
    title: str
    amount: Decimal
    rows: list[DrillRow] = field(default_factory=list)
    note: str | None = None
    #: Входит ли сумма группы в итог строки. У «ждём документ» — нет.
    counts_in_total: bool = True


@dataclass(slots=True)
class DrillAside:
    """Сумма, прошедшая по строке, но к её числу не относящаяся, — с объяснением почему."""

    amount: Decimal
    count: int
    reason: str


@dataclass(slots=True)
class DrillResult:
    line_code: str
    line_title: str
    month: date
    groups: list[DrillGroup] = field(default_factory=list)
    #: Сумма групп, входящих в итог. Фронт сверяет её со строкой отчёта: разойдётся —
    #: значит расшифровка чего-то не показала, и это надо видеть, а не прятать.
    total: Decimal = Decimal("0.00")
    #: Слой, который расшифровать не удалось (налоги, амортизация), — честной пометкой.
    undecomposed: list[str] = field(default_factory=list)
    #: То, что прошло по строке в этом месяце, но в её число не входит и не войдёт. Таблицей
    #: не показываем — по одной строке итога на причину, чтобы было видно, что ничего не
    #: спрятано, и при этом не приходилось читать чужой период.
    asides: list[DrillAside] = field(default_factory=list)


async def _names(session: AsyncSession, model, ids: set[uuid.UUID], column) -> dict:
    if not ids:
        return {}
    rows = await session.execute(select(model.id, column).where(model.id.in_(ids)))
    return {row[0]: row[1] for row in rows}


async def build_drill(session: AsyncSession, line_code: str, month: date) -> DrillResult | None:
    """Расшифровка строки за месяц. ``None`` — такой строки в справочнике нет."""
    month_start, month_end = projector.month_bounds(month)
    line = (
        await session.execute(select(PnlLine).where(PnlLine.code == line_code))
    ).scalar_one_or_none()
    if line is None:
        return None

    result = DrillResult(line_code=line_code, line_title=line.title, month=month_start)
    sign_roles = {
        row[0]: row[1] for row in (await session.execute(select(PnlLine.code, PnlLine.sign_role)))
    }

    await _recognition_group(session, result, line_code, month_start, month_end)
    await _cash_group(session, result, line_code, month_start, month_end, sign_roles)
    await _waiting_group(session, result, line_code, month_start, month_end)
    await _payroll_group(session, result, line_code, month_start, month_end)
    await _releases_group(session, result, line_code, month_start, month_end)
    await _iiko_group(session, result, line_code, month_start)
    await _inventory_group(session, result, line_code, month_start, month_end)
    await _manual_group(session, result, line_code, month_start)

    result.total = sum(
        (group.amount for group in result.groups if group.counts_in_total), Decimal("0.00")
    )
    if not result.groups:
        result.undecomposed.append(
            "Источник этой строки не раскладывается по составляющим: величина приходит "
            "из смежного модуля одним числом"
        )
    return result


async def _article_line_map(session: AsyncSession) -> dict[uuid.UUID, str]:
    rows = await session.execute(
        select(PnlArticleRule.article_id, PnlArticleRule.line_code).where(
            PnlArticleRule.is_active.is_(True), PnlArticleRule.in_pnl.is_(True)
        )
    )
    return {article_id: code for article_id, code in rows}


async def _recognition_group(
    session: AsyncSession,
    result: DrillResult,
    line_code: str,
    month_start: date,
    month_end: date,
) -> None:
    """Признанный расход: по одной строке на начисление, с периодом услуги."""
    layer = await recognition_source.build_recognition_layer(session, month_start, month_end)
    article_lines = await _article_line_map(session)
    details = [
        detail
        for detail in layer.details
        if article_lines.get(detail.article_id) == line_code  # type: ignore[arg-type]
    ]
    if not details:
        return

    names = await _names(
        session,
        Counterparty,
        {detail.counterparty_id for detail in details if detail.counterparty_id},
        Counterparty.name,
    )
    group = DrillGroup(
        stream="recognition",
        title="Признанный расход",
        amount=sum((detail.amount for detail in details), Decimal("0.00")),
        note="Расход месяца по документу или начислению — независимо от даты платежа",
    )
    for detail in sorted(details, key=lambda item: -item.amount):
        period = _period_label(detail.service_period_start, detail.service_period_end)
        origin = ORIGIN_LABEL.get(detail.origin, detail.origin)
        group.rows.append(
            DrillRow(
                title=names.get(detail.counterparty_id) or "Без контрагента",
                subtitle=f"{period} · {origin}" if period else origin,
                row_date=detail.service_period_start,
                amount=detail.amount,
                kind="included",
            )
        )
    result.groups.append(group)


async def _cash_group(
    session: AsyncSession,
    result: DrillResult,
    line_code: str,
    month_start: date,
    month_end: date,
    sign_roles: dict[str, int],
) -> None:
    """Денежный слой: проводки, которые формируют число, и те, что его ещё изменят.

    Разделение на группы, а не колонку-признак: складывать в глазах читателя расход с
    неотнесённым платежом нельзя, а видеть их рядом — нужно.

    Всё остальное — оплата уже признанного расхода, перевод, чужой слой — сворачивается в
    одну итоговую строку внизу. Оно не меняет число ни сейчас, ни потом, и таблицей только
    отвлекает от того, ради чего расшифровку открыли.
    """
    details = await cash_source.explain_line(
        session, month_start, month_end, line_code, sign_roles=sign_roles
    )
    if not details:
        return

    names = await _names(
        session,
        Counterparty,
        {detail.counterparty_id for detail in details if detail.counterparty_id},
        Counterparty.name,
    )
    articles = await _names(
        session,
        DdsArticle,
        {detail.article_id for detail in details if detail.article_id},
        DdsArticle.name,
    )

    included = [detail for detail in details if detail.verdict == "included"]
    aside = [detail for detail in details if detail.verdict != "included"]

    if included:
        group = DrillGroup(
            stream="cashflow",
            title="Оплачено деньгами",
            amount=sum((detail.contribution for detail in included), Decimal("0.00")),
        )
        for detail in included:
            group.rows.append(_cash_row(detail, names, articles, kind="included"))
        result.groups.append(group)

    # Вся исключённая касса месяца уходит в свёрнутую строку — включая ту, что раньше
    # показывалась как «ждём документ». Месяц платежа ничего не говорит о том, за какой
    # период платили, поэтому ожидание теперь строится из ДЗ/КЗ — см. ``_waiting_group``.
    if aside:
        result.asides.append(
            DrillAside(
                amount=sum((detail.amount for detail in aside), Decimal("0.00")),
                count=len(aside),
                reason=(
                    "прошло по этим статьям в этом месяце, но к строке не относится: оплата "
                    "документов других периодов, переводы, суммы, посчитанные другими модулями"
                ),
            )
        )


async def _waiting_group(
    session: AsyncSession,
    result: DrillResult,
    line_code: str,
    month_start: date,
    month_end: date,
) -> None:
    """Чего строка ещё ждёт ЗА ЭТОТ МЕСЯЦ — по периоду услуги из ДЗ/КЗ.

    Дата платежа здесь справочная и часто лежит в прошлом месяце: реклама за июль оплачена
    29.06. Ровно поэтому группа и не строится из кассы июля.
    """
    recognition = await recognition_source.build_recognition_layer(session, month_start, month_end)
    layer = await waiting_source.build_waiting_layer(
        session,
        month_start,
        month_end,
        recognized_counterparties={
            detail.counterparty_id
            for detail in recognition.details
            if detail.counterparty_id is not None
        },
    )
    article_lines = await _article_line_map(session)
    items = [
        item
        for item in layer.items
        if article_lines.get(item.article_id) == line_code  # type: ignore[arg-type]
    ]
    if not items:
        return

    names = await _names(
        session,
        Counterparty,
        {item.counterparty_id for item in items if item.counterparty_id},
        Counterparty.name,
    )
    group = DrillGroup(
        stream="waiting",
        title=WAITING_TITLE,
        amount=sum((item.amount for item in items), Decimal("0.00")),
        counts_in_total=False,
        note=(
            "Деньги ушли, но расход по ним признаёт документ. Пока документа нет, сумма в "
            "отчёт не идёт — иначе месяц получил бы расход дважды"
        ),
    )
    for item in sorted(items, key=lambda entry: -entry.amount):
        group.rows.append(
            DrillRow(
                title=names.get(item.counterparty_id) or "Без контрагента",
                subtitle=(
                    f"период {_period_label(item.period_start, item.period_end)}"
                    if item.period_known
                    else "период не указан — месяц взят по дате платежа"
                ),
                row_date=item.paid_on,
                amount=item.amount,
                kind="waiting",
            )
        )
    result.groups.append(group)


def _cash_row(
    detail: cash_source.CashDetail,
    names: dict,
    articles: dict,
    *,
    kind: str,
) -> DrillRow:
    article = articles.get(detail.article_id)
    purpose = (detail.payment_purpose or "").strip()
    if len(purpose) > 90:
        purpose = purpose[:90].rstrip() + "…"
    subtitle = " · ".join(part for part in (article, purpose) if part) or None
    return DrillRow(
        title=names.get(detail.counterparty_id) or "Без контрагента",
        subtitle=subtitle,
        row_date=detail.operation_date,
        amount=detail.contribution if kind == "included" else detail.amount,
        kind=kind,
    )


async def _payroll_group(
    session: AsyncSession,
    result: DrillResult,
    line_code: str,
    month_start: date,
    month_end: date,
) -> None:
    """Зарплатные строки — по сотрудникам. Отрицательная строка = удержанный штраф."""
    if line_code not in {
        "cook_payroll",
        "administrator_payroll",
        "admin_payroll",
        "bonuses",
        "accumulation_fund",
        "audit_results",
    }:
        return
    month = await payroll_source.build_payroll_month(session, month_start, month_end)
    entries = {
        employee_id: amount
        for (code, employee_id), amount in month.by_employee.items()
        if code == line_code and amount != 0
    }
    if not entries:
        return

    names = await _names(session, Employee, set(entries), Employee.full_name)
    group = DrillGroup(
        stream="payroll",
        title="Начислено сотрудникам",
        amount=sum(entries.values(), Decimal("0.00")),
        note="Начисления месяца без НДФЛ — он учтён строкой «Налоги с ЗП»",
    )
    for employee_id, amount in sorted(entries.items(), key=lambda item: -item[1]):
        group.rows.append(
            DrillRow(
                title=names.get(employee_id) or "Сотрудник удалён",
                subtitle="удержание" if amount < 0 else None,
                row_date=None,
                amount=amount,
                kind="included",
            )
        )
    result.groups.append(group)


async def _releases_group(
    session: AsyncSession,
    result: DrillResult,
    line_code: str,
    month_start: date,
    month_end: date,
) -> None:
    """Списания депозитов и накопительного фонда — пофамильно.

    Именно здесь расшифровка окупается целиком: строка «Накопительный фонд» с минусом
    объясняется одной таблицей — кто, за какой год и сколько. Без неё отрицательный расход в
    блоке общепроизводственных выглядит ошибкой отчёта.
    """
    if line_code not in {"unclaimed_deposits_writeoff", "accumulation_fund"}:
        return
    releases = await deposits_source.build_release_month(
        session, month_start, month_end, horizon_start=projector.ACCOUNTING_START
    )
    if line_code == "accumulation_fund" and releases.fund_forfeited_out_of_horizon:
        years = ", ".join(
            str(year) for year in sorted(releases.fund_forfeited_out_of_horizon_years)
        )
        result.asides.append(
            DrillAside(
                amount=releases.fund_forfeited_out_of_horizon,
                count=releases.fund_forfeited_out_of_horizon_count,
                reason=(
                    f"списано фондов за {years} — эти начисления делались до начала "
                    "управленческого учёта, поэтому их отмена расход месяца не уменьшает"
                ),
            )
        )
    entries = (
        releases.deposit_entries
        if line_code == "unclaimed_deposits_writeoff"
        else releases.fund_entries
    )
    if not entries:
        return

    names = await _names(
        session,
        Employee,
        {entry.employee_id for entry in entries if entry.employee_id},
        Employee.full_name,
    )
    deposits = line_code == "unclaimed_deposits_writeoff"
    group = DrillGroup(
        stream="payroll",
        title="Списано с уволенных" if deposits else "Списание фонда уволенных",
        amount=sum(
            ((entry.amount if deposits else -entry.amount) for entry in entries),
            Decimal("0.00"),
        ),
        note=(
            "Депозит удерживался из суммы к выдаче и расходом не был — списание даёт доход"
            if deposits
            else "Фонд был начислен расходом ранее; списание отменяет его в этой же строке"
        ),
    )
    for entry in sorted(entries, key=lambda item: -item.amount):
        group.rows.append(
            DrillRow(
                title=names.get(entry.employee_id) or "Сотрудник удалён",
                subtitle=(f"фонд {entry.period_year} года" if entry.period_year else None),
                row_date=entry.happened_on,
                amount=entry.amount if deposits else -entry.amount,
                kind="included",
            )
        )
    result.groups.append(group)


async def _iiko_group(
    session: AsyncSession, result: DrillResult, line_code: str, month_start: date
) -> None:
    """Зеркало iiko — по направлениям. Строка ``total`` не дублируется: она и есть итог."""
    metric = projector.IIKO_LINE_METRIC.get(line_code)
    if metric is None:
        return
    rows = (
        await session.execute(
            select(PnlIikoFact).where(
                PnlIikoFact.period_month == month_start,
                PnlIikoFact.metric_code == metric,
            )
        )
    ).scalars()
    # Тот же разворот знака, что и в отчёте: расшифровка, показывающая сырой знак зеркала,
    # объясняла бы строку числами, которые ей противоречат.
    sign = -1 if metric in projector.INVERTED_IIKO_METRICS else 1
    facts = {row.direction: row.amount * sign for row in rows}
    total = facts.pop("total", None)
    if total is None and not facts:
        return

    group = DrillGroup(
        stream="iiko",
        title="Из iiko",
        amount=total if total is not None else sum(facts.values(), Decimal("0.00")),
        note="Зеркало сохранённых OLAP-пресетов, наполняется ночной джобой",
    )
    for direction, amount in sorted(facts.items(), key=lambda item: -item[1]):
        group.rows.append(
            DrillRow(
                title=DIRECTION_TITLE.get(direction, direction),
                subtitle=None,
                row_date=None,
                amount=amount,
                kind="included",
            )
        )
    if not group.rows:
        group.rows.append(
            DrillRow(
                title="Итог месяца",
                subtitle="разреза по направлениям у этой метрики нет",
                row_date=None,
                amount=group.amount,
                kind="included",
            )
        )
    result.groups.append(group)


async def _inventory_group(
    session: AsyncSession,
    result: DrillResult,
    line_code: str,
    month_start: date,
    month_end: date,
) -> None:
    """«Результаты ревизии» — по одной строке на проведённую ревизию.

    Знак инвертирован так же, как в источнике: недостача положительна (расход), излишек
    отрицателен. Показать здесь сырой знак базы значило бы объяснять строку числами, которые
    ей противоречат.
    """
    if line_code != "audit_results":
        return
    audits = (
        (
            await session.execute(
                select(InventoryAudit).where(
                    InventoryAudit.status == inventory_source.APPLIED_STATUS,
                    InventoryAudit.business_date >= month_start,
                    InventoryAudit.business_date <= month_end,
                )
            )
        )
        .scalars()
        .all()
    )
    if not audits:
        return

    group = DrillGroup(
        stream="inventory",
        title="Проведённые ревизии",
        amount=Decimal("0.00"),
        note="Недостача — положительная величина расхода, излишек уменьшает его",
    )
    totals = {
        audit_id: value
        for audit_id, value in (
            await session.execute(
                select(
                    InventoryAuditItem.audit_id,
                    func.coalesce(func.sum(InventoryAuditItem.amount), 0),
                )
                .where(InventoryAuditItem.audit_id.in_([audit.id for audit in audits]))
                .group_by(InventoryAuditItem.audit_id)
            )
        ).all()
    }
    for audit in sorted(audits, key=lambda item: item.business_date):
        amount = Decimal(totals.get(audit.id) or 0) * Decimal("-1")
        group.amount += amount
        group.rows.append(
            DrillRow(
                title=f"Ревизия {audit.business_date.strftime('%d.%m.%Y')}",
                subtitle="недостача" if amount > 0 else "излишек",
                row_date=audit.business_date,
                amount=amount,
                kind="included",
            )
        )
    result.groups.append(group)


async def _manual_group(
    session: AsyncSession, result: DrillResult, line_code: str, month_start: date
) -> None:
    manual = await manual_source.build_manual_layer(session, month_start)
    entries = manual.get(line_code)
    if not entries:
        return
    group = DrillGroup(
        stream="manual",
        title="Ручной ввод",
        amount=manual_source.manual_total(entries),
        note="Введено человеком: автоматического источника у этой величины нет",
    )
    for entry in entries:
        group.rows.append(
            DrillRow(
                title=entry.reason or "Без пояснения",
                subtitle=None,
                row_date=None,
                amount=entry.amount * entry.sign,
                kind="included",
            )
        )
    result.groups.append(group)


def _period_label(start: date | None, end: date | None) -> str | None:
    if start is None or end is None:
        return None
    if start == end:
        return start.strftime("%d.%m.%Y")
    return f"{start.strftime('%d.%m')} — {end.strftime('%d.%m.%Y')}"
