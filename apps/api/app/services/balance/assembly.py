"""Сборка баланса на дату: опрос регистров и превращение их ответов в строки.

ЧТО ЭТОТ МОДУЛЬ ДЕЛАЕТ И ЧЕГО НЕ ДЕЛАЕТ. Он ТОЛЬКО складывает: каждый регистр уже умеет отдавать
остаток на произвольную дату и сам отвечает за свою правильность. Здесь не считается ни одна
величина заново — иначе появилась бы вторая формула того же, и однажды они разошлись бы.

КАПИТАЛ ВЫВОДИТСЯ, А НЕ СЧИТАЕТСЯ. Учёт начат 01.07.2026, истории до этой даты в системе нет,
поэтому накопленную прибыль прошлых лет взять неоткуда. Капитал = актив минус обязательства, и
строка помечена так, чтобы её не приняли за посчитанную. Прямое следствие: равенство «актив =
пассив» в снимке ТОЖДЕСТВЕННО и проверкой не является — настоящая проверка живёт в сверке
приращения между двумя снимками (см. ``reconcile``).

ПУСТОТА БЫВАЕТ РАЗНАЯ, И ЭТО ВИДНО. ``None`` — величина неизвестна (регистр не смог ответить);
ноль с пометкой ``zero_confirmed`` — владелец подтвердил, что величины нет; ``manual`` — ждёт
человека. Свести всё к нулю значило бы соврать одинаково во всех трёх случаях.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    BalanceManualValue,
    BankOperation,
    BusinessOwner,
    CashflowTransaction,
    Counterparty,
    IikoProduct,
    PnlIikoStockFact,
    PnlProductMonthlyDecision,
    PnlProductWhitelist,
)
from app.services import (
    asset_balance,
    counterparty_balance_as_of,
    partner_cash_balance,
    staff_balance_as_of,
)
from app.services import wallet_balance_as_of as money
from app.services.accounting_periods import ACCOUNTING_START
from app.services.balance import catalog as cat

_ZERO = Decimal("0.00")
ZERO_DATE = date(1970, 1, 1)


def _money(value: object) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


class BalanceBeforeAccountingStart(ValueError):
    """Дата раньше начала управленческого учёта — собирать баланс не из чего."""


@dataclass
class AssembledLine:
    code: str
    title: str
    block: str
    group: str
    sort_order: int
    sign: int
    kind: str
    amount: Decimal | None
    status: str
    source_kind: str | None = None
    source_note: str | None = None
    detail: dict | None = None


@dataclass
class AssembledFlag:
    code: str
    severity: str
    title: str
    explanation: str
    count: int = 0
    amount: Decimal = _ZERO
    rows: list[dict] = field(default_factory=list)


@dataclass
class AssembledBalance:
    as_of: date
    lines: list[AssembledLine]
    flags: list[AssembledFlag]
    asset_total: Decimal
    liability_total: Decimal
    equity_plug: Decimal
    # Объём величин, чья датировка приблизительна. НЕ полоса допуска: это сумма самих
    # приблизительных величин, а не оценка ошибки. Полоса считается из ИЗМЕНЕНИЯ этой суммы
    # между двумя снимками — см. ``reconcile``.
    approximate_total: Decimal


async def month_net_profit(
    session: AsyncSession, month: date
) -> tuple[Decimal | None, str | None, dict | None]:
    """Прибыль месяца из отчёта. Считается НЕЗАВИСИМО от снимка — см. правило в reconcile.

    Отчёт не собрался — снимок всё равно замораживается: балансу нельзя запрещать закрыть месяц
    из-за поломки в соседнем модуле. Но молчать об этом тоже нельзя, поэтому неизвестная прибыль
    поднимает БЛОКИРУЮЩИЙ сигнал (см. ``_profit_flag``): без прибыли единственная настоящая
    проверка модуля не работает, и следующий месяц сверить будет не с чем.

    Ошибку записываем с текстом, а не одним именем класса: ``TypeError`` в статусе — это то, что
    я и увидел, когда позвал ``build_report`` несуществующим именем аргумента. Имя класса не
    сказало ничего, а прогон из 3051 теста и зелёный стенд не заметили поломку вовсе.
    """
    from app.services.pnl import projector

    try:
        # Позиционно и именно ``month``: у проектора нет параметра ``period_month``, и вызов с
        # чужим именем падал бы TypeError — молча, потому что ниже стоит широкий except.
        report = await projector.build_report(session, month)
    except Exception as error:  # noqa: BLE001 — отчёт может не собраться; это не повод падать
        return None, f"недоступен: {type(error).__name__}: {error}"[:200], None

    line = next(
        (row for row in getattr(report, "lines", []) if getattr(row, "code", "") == "net_profit"),
        None,
    )
    if line is None:
        return None, "строка чистой прибыли не найдена", None
    amount = getattr(line, "amount", None)
    status = getattr(line, "status", None)
    missing = getattr(line, "missing_lines", None) or getattr(report, "missing_lines", None)
    return (
        _money(amount) if amount is not None else None,
        str(status) if status is not None else None,
        {"missing": [str(item) for item in missing]} if missing else None,
    )


def _parts(rows, pick, *, name=lambda row: row.counterparty_name) -> list[dict]:
    """Состав строки: из чего сложилась сумма, по убыванию вклада.

    ИТОГ БЕЗ СОСТАВА НЕПРОВЕРЯЕМ. «Выданные авансы поставщикам 252 461,46» — цифра, с которой
    владельцу нечего делать: она либо совпадает с его ожиданием, либо нет, и во втором случае
    искать причину негде. Разложение по контрагентам превращает вопрос «откуда столько?» в
    вопрос «почему у этого столько?», на который уже можно ответить.

    Нули отброшены: контрагент с закрытыми расчётами в составе строки не значит ничего, а
    список раздувает — на проде их десятки.
    """
    out = [
        {"name": name(row), "amount": str(_money(pick(row)))}
        for row in rows
        if _money(pick(row)) != _ZERO
    ]
    out.sort(key=lambda item: Decimal(item["amount"]), reverse=True)
    return out


def _line(defn: cat.BalanceLineDef, amount: Decimal | None, status: str, **kw) -> AssembledLine:
    return AssembledLine(
        code=defn.code,
        title=defn.title,
        block=defn.block,
        group=defn.group,
        sort_order=defn.sort_order,
        sign=defn.sign,
        kind=defn.kind,
        amount=amount,
        status=status,
        source_note=kw.pop("source_note", defn.note or None),
        **kw,
    )


async def _manual_values(session: AsyncSession, as_of: date) -> dict[str, BalanceManualValue]:
    rows = (
        await session.scalars(select(BalanceManualValue).where(BalanceManualValue.as_of == as_of))
    ).all()
    return {row.line_code: row for row in rows}


async def _assets_lines(
    session: AsyncSession, as_of: date, *, live: bool = False
) -> list[AssembledLine]:
    """Основные средства: строки приходят из регистра как есть.

    За ЗАКРЫТЫЙ месяц регистр отдаёт замороженный снимок, за открытый — живой расчёт. Ключ
    строки — ИМЯ, а не идентификатор категории: у регистра две строки имеют пустую категорию
    («Не работающее оборудование» и «Без категории»), и по идентификатору они схлопнулись бы
    в одну.
    """
    if live:
        # Путь детектора дрейфа: сравнивать снимок баланса с замороженными строками основных
        # средств значило бы сравнивать снимок со снимком — расхождение стало бы тождественным
        # нулём. Ровно та ловушка, от которой сам регистр основных средств и защищается.
        lines, frozen = await asset_balance.balance_lines(session, as_of=as_of), False
    else:
        lines, frozen = await asset_balance.report_lines(session, period_month=as_of)
    out: list[AssembledLine] = []
    for index, row in enumerate(lines):
        key = cat.AssetLineKey(row.line_name)
        out.append(
            AssembledLine(
                code=key.code,
                title=row.line_name,
                block=cat.ASSET,
                group=cat.OS_GROUP,
                sort_order=cat.OS_SORT_BASE + index,
                sign=1,
                kind=cat.KIND_SOURCE,
                amount=_money(row.residual),
                status=cat.ST_OK,
                source_kind="fixed_assets",
                source_note=(
                    "Замороженный снимок закрытого месяца"
                    if frozen
                    else "Живой расчёт: месяц ещё не закрыт, цифра не окончательна"
                ),
                detail={
                    "asset_count": row.asset_count,
                    "initial_cost": str(_money(row.initial_cost)),
                    "accumulated": str(_money(row.accumulated)),
                    "frozen": frozen,
                },
            )
        )
    return out


async def _stock_line(
    session: AsyncSession, as_of: date
) -> tuple[AssembledLine, list[AssembledFlag]]:
    """Запасы: стоимостной остаток склада на конец месяца.

    ЗАПАС — ЭТО ТОВАРЫ, И ТОЛЬКО ОНИ. Снимок iiko приносит остатки по всему справочнику — 427
    позиций на 31.07.2026, — но склад состоит из товаров: продукты, упаковка, напитки, химия.
    Блюда, заготовки и модификаторы тоже имеют в iiko «остаток», однако запасом на складе он не
    является: заготовка — это уже переработанный товар, блюдо — рецепт, модификатор — добавка к
    строке чека. На июле они дают 89 682,24 ₽ поверх настоящих 1 219 472,81 ₽.

    КРИТЕРИЙ — ТИП НОМЕНКЛАТУРЫ, А НЕ СПИСОК РЕВИЗИЙ. Первая версия брала ``pnl_product_whitelist``
    (вкладка «Ревизии» в леджере «Товары iiko») и давала 291 354,87 ₽: список ведут руками, и
    в нём оказались только упаковка и напитки — 37 позиций из 257 товаров, продукты в него просто
    не завели. Складской учёт при этом ведётся по всем товарам. Ручной список отвечает на вопрос
    «что владелец отметил», а балансу нужен ответ на «что является запасом», и это разные вопросы:
    первый отстаёт ровно настолько, насколько давно его не пополняли.

    Комбо-наборы отдельного типа в iiko не имеют — приходят как ``DISH`` и отсекаются вместе с
    блюдами.

    ПОЗИЦИЯ БЕЗ КАРТОЧКИ В СПРАВОЧНИКЕ не классифицируется вовсе: тип взять неоткуда. Такую в
    сумму не берём и поднимаем сигнал — молча включить значило бы посчитать неизвестное товаром,
    молча выбросить — потерять запас, о котором никто не узнает. На проде их сейчас ноль.

    Цифра существует ТОЛЬКО на границы месяца — снимок берётся у iiko на 00:00 первого числа
    следующего месяца. Поэтому у неё две беды, и обе надо показать, а не спрятать: снимка может
    не быть вовсе (прогон не состоялся), и он может быть СВЕЖЕЕ месяца — прогон, случившийся
    20 августа, спрашивает у iiko будущую отметку времени и записывает ответ как августовский.
    Строка есть, а доверия ей нет.
    """
    defn = cat.CATALOG_BY_CODE["stock_total"]
    month = as_of.replace(day=1)

    # Прогон месяца: был ли он вообще и когда. Считается по ВСЕМУ снимку, а не по ревизуемой
    # части: свежесть — свойство прогона, а не выборки из него.
    run = (
        await session.execute(
            select(
                func.count(),
                func.max(PnlIikoStockFact.synced_at),
                func.coalesce(func.sum(PnlIikoStockFact.closing_amount), 0),
            ).where(PnlIikoStockFact.period_month == month)
        )
    ).one()
    run_count, synced_at, run_total = int(run[0] or 0), run[1], _money(run[2])

    if not run_count:
        return (
            _line(
                defn,
                None,
                cat.ST_MISSING,
                source_kind="iiko_stock",
                source_note="Складского снимка за этот месяц нет — прогон iiko не состоялся",
            ),
            [
                AssembledFlag(
                    code="stock_month_missing",
                    severity="blocking",
                    title="Нет складского остатка за месяц",
                    explanation=(
                        "Стоимостной остаток склада берётся снимком iiko на конец месяца. "
                        "Снимка нет, поэтому строка «Запасы» пуста, а не равна нулю."
                    ),
                    count=1,
                )
            ],
        )

    # Запас — это товар по типу ИЛИ то, что владелец явно поставил на складской учёт. Второе
    # слагаемое — страховка от ошибки справочника: у «Палочек» карточка типа DISH, хотя это
    # упаковка со складским правилом, и по одному типу позиция выпала бы из актива. Whitelist
    # здесь умеет только ДОБАВЛЯТЬ: списком, который ведут руками, запас не ограничивается —
    # ровно от этого модуль уходил, когда строка показывала 291 354,87 вместо 851 715,18.
    goods = select(IikoProduct.iiko_id).where(IikoProduct.type == cat.STOCK_PRODUCT_TYPE)
    stocked = select(PnlProductWhitelist.iiko_product_guid).where(
        PnlProductWhitelist.source_kind == cat.STOCKED_SOURCE,
        PnlProductWhitelist.include_status == cat.STOCKED_STATUS,
    )
    # Расходники: товар есть, склада по нему нет — расход признан при покупке.
    expensed = select(PnlProductWhitelist.iiko_product_guid).where(
        PnlProductWhitelist.source_kind == cat.EXPENSED_ON_PURCHASE_SOURCE,
        PnlProductWhitelist.include_status == cat.EXPENSED_ON_PURCHASE_STATUS,
    )
    counted = (
        await session.execute(
            select(
                PnlIikoStockFact.product_name,
                PnlIikoStockFact.closing_quantity,
                PnlIikoStockFact.closing_amount,
                IikoProduct.deleted,
            )
            .outerjoin(IikoProduct, IikoProduct.iiko_id == PnlIikoStockFact.iiko_product_guid)
            .where(
                PnlIikoStockFact.period_month == month,
                or_(
                    PnlIikoStockFact.iiko_product_guid.in_(goods),
                    PnlIikoStockFact.iiko_product_guid.in_(stocked),
                ),
                PnlIikoStockFact.iiko_product_guid.not_in(expensed),
            )
            .order_by(PnlIikoStockFact.closing_amount.desc())
        )
    ).all()
    total = _money(sum((row[2] for row in counted), _ZERO))
    fact_count = len(counted)
    # Карточка, помеченная в iiko удалённой, остатка не отменяет: вещь на полке лежит, и
    # выкидывать её из актива значило бы списать деньги в капитал за то, что справочник
    # прибрали. Но факт называем — расход по такой позиции признавать будет нечем.
    deleted_rows = [row for row in counted if row[3]]
    # Отрицательный остаток попадает в состав как есть и виден сразу: он означает, что продали
    # больше, чем оприходовали, — то есть приходной накладной нет. Прятать такую позицию
    # значило бы прятать дыру в первичке.
    positions = [
        {
            "name": row[0] or "—",
            "amount": str(_money(row[2])),
            "quantity": str(_money(row[1])),
        }
        for row in counted
    ]

    flags: list[AssembledFlag] = []

    # Снимок закрывающего остатка берётся на 00:00 первого числа СЛЕДУЮЩЕГО месяца. Если
    # синхронизация прошла раньше этого момента, iiko ответила о будущем — то есть отдала
    # состояние на «сейчас», а не на конец периода.
    boundary = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    stale = synced_at is not None and synced_at.date() < boundary
    if stale:
        flags.append(
            AssembledFlag(
                code="stock_snapshot_stale",
                severity="warning",
                title="Складской остаток снят до конца месяца",
                explanation=(
                    "Прогон iiko состоялся раньше границы месяца, поэтому в снимке лежит "
                    "остаток на день прогона. Пересобрать месяц после его окончания."
                ),
                count=1,
                amount=total,
            )
        )

    # Товар, по которому владелец ещё не решил, расходник он или запас. Считаем запасом —
    # склад по умолчанию, расходником вещь становится решением, — но говорим об этом: если
    # решение окажется «расходник», строка на эту величину завышена.
    #
    # ИСТОЧНИК РАЗМЕТКИ ЗДЕСЬ НЕ ВАЖЕН, и раньше запрос требовал накладную. Из строки «не
    # решено» владелец волен переключить источник на накладную и выбрать статью — то есть
    # ЛЮБОЕ незакрытое решение способно снять товар с баланса, а не только уже указывающее
    # на накладную. Складское «не знаю» молчало, хотя UI его штатно создаёт при смене
    # источника до выбора статьи.
    undecided = (
        await session.execute(
            select(PnlIikoStockFact.product_name, PnlIikoStockFact.closing_amount)
            .join(IikoProduct, IikoProduct.iiko_id == PnlIikoStockFact.iiko_product_guid)
            .join(
                PnlProductWhitelist,
                PnlProductWhitelist.iiko_product_guid == PnlIikoStockFact.iiko_product_guid,
            )
            .where(
                PnlIikoStockFact.period_month == month,
                IikoProduct.type == cat.STOCK_PRODUCT_TYPE,
                PnlProductWhitelist.include_status == cat.UNDECIDED_STATUS,
            )
            .order_by(PnlIikoStockFact.closing_amount.desc())
        )
    ).all()
    if undecided:
        flags.append(
            AssembledFlag(
                code="stock_expense_decision_pending",
                severity="info",
                title="Не решено, расходник это или запас",
                explanation=(
                    "По этим товарам владелец ещё не решил, признавать ли расход при покупке. "
                    "Пока они считаются запасом: складом вещь является по умолчанию, "
                    "расходником становится решением. Решите — и строка уточнится."
                ),
                count=len(undecided),
                amount=_money(sum((row[1] for row in undecided), _ZERO)),
                rows=[
                    {"product": row[0] or "—", "amount": str(_money(row[1]))} for row in undecided
                ],
            )
        )

    # Товар, вычеркнутый из товарного контура ОПиУ. СУММУ НЕ ТРОГАЕМ: статус говорит про
    # строку отчёта, а не про наличие вещи на складе, и читать «не в товарных расходах» как
    # «товара нет» баланс не вправе — это молча списало бы актив в капитал. Но у такой
    # позиции нет канала признания расхода вообще: ни складского, ни по накладной. Когда
    # товар двинется, актив уменьшится, а расхода не появится нигде.
    excluded = (
        await session.execute(
            select(PnlIikoStockFact.product_name, PnlIikoStockFact.closing_amount)
            .join(IikoProduct, IikoProduct.iiko_id == PnlIikoStockFact.iiko_product_guid)
            .join(
                PnlProductWhitelist,
                PnlProductWhitelist.iiko_product_guid == PnlIikoStockFact.iiko_product_guid,
            )
            .where(
                PnlIikoStockFact.period_month == month,
                IikoProduct.type == cat.STOCK_PRODUCT_TYPE,
                PnlProductWhitelist.include_status == cat.EXCLUDED_STATUS,
            )
            .order_by(PnlIikoStockFact.closing_amount.desc())
        )
    ).all()
    if excluded:
        flags.append(
            AssembledFlag(
                code="stock_expense_channel_missing",
                severity="warning",
                title="Товар исключён из расходов, но остался запасом",
                explanation=(
                    "По этим позициям выбрано «не учитывать в товарных расходах». В запасах "
                    "они остаются — статус говорит про строку отчёта, а не про наличие товара "
                    "на складе. Но признать их расход теперь негде: когда товар израсходуют, "
                    "актив уменьшится, а расход не появится ни в одной строке."
                ),
                count=len(excluded),
                amount=_money(sum((row[1] for row in excluded), _ZERO)),
                rows=[
                    {"product": row[0] or "—", "amount": str(_money(row[1]))} for row in excluded
                ],
            )
        )

    # Товар в проработке: временное решение на ОДИН месяц, живущее своей таблицей. Правил
    # разметки у него не остаётся вовсе (``save_goods_classification`` их удаляет), поэтому
    # по whitelist баланс не отличил бы его от обычного складского — читаем решение напрямую.
    # Расход по нему признаёт АКТ СПИСАНИЯ, а не складской контур: строка «Проработка» в ОПиУ
    # справочная (memo) и в прибыль не входит. Пока акта нет, товар честно стоит активом.
    workup = (
        await session.execute(
            select(PnlIikoStockFact.product_name, PnlIikoStockFact.closing_amount)
            .join(IikoProduct, IikoProduct.iiko_id == PnlIikoStockFact.iiko_product_guid)
            .join(
                PnlProductMonthlyDecision,
                PnlProductMonthlyDecision.iiko_product_guid == PnlIikoStockFact.iiko_product_guid,
            )
            .where(
                PnlIikoStockFact.period_month == month,
                IikoProduct.type == cat.STOCK_PRODUCT_TYPE,
                PnlProductMonthlyDecision.period_month == month,
                PnlProductMonthlyDecision.decision_kind == cat.WORKUP_DECISION,
            )
            .order_by(PnlIikoStockFact.closing_amount.desc())
        )
    ).all()
    if workup:
        flags.append(
            AssembledFlag(
                code="stock_workup_pending",
                severity="info",
                title="Товар в проработке — ждёт акта списания",
                explanation=(
                    "Решение принято на один месяц: закупка показана в ОПиУ справочной строкой "
                    "и в прибыль не входит. Расход признает акт списания — до него товар стоит "
                    "запасом. Акта не будет — расход не появится нигде."
                ),
                count=len(workup),
                amount=_money(sum((row[1] for row in workup), _ZERO)),
                rows=[{"product": row[0] or "—", "amount": str(_money(row[1]))} for row in workup],
            )
        )

    # Позиция снимка, которой нет в справочнике iiko: тип взять неоткуда, значит неизвестно,
    # товар это или блюдо. В сумму не берём и называем — иначе запас потерялся бы молча.
    unknown = (
        await session.execute(
            select(PnlIikoStockFact.product_name, PnlIikoStockFact.closing_amount)
            .outerjoin(IikoProduct, IikoProduct.iiko_id == PnlIikoStockFact.iiko_product_guid)
            .where(PnlIikoStockFact.period_month == month, IikoProduct.iiko_id.is_(None))
            .order_by(PnlIikoStockFact.closing_amount.desc())
        )
    ).all()
    if unknown:
        flags.append(
            AssembledFlag(
                code="stock_position_unclassified",
                severity="warning",
                title="Позиция склада отсутствует в справочнике iiko",
                explanation=(
                    "Тип номенклатуры взять неоткуда, поэтому непонятно, товар это или блюдо. "
                    "В запасы такая позиция не входит — величина показана, чтобы не потерялась."
                ),
                count=len(unknown),
                amount=_money(sum((row[1] for row in unknown), _ZERO)),
                rows=[{"product": row[0] or "—", "amount": str(_money(row[1]))} for row in unknown],
            )
        )

    line = _line(
        defn,
        total,
        cat.ST_APPROXIMATE if stale else cat.ST_OK,
        source_kind="iiko_stock",
        source_note=(
            "Снимок сделан ДО конца месяца — iiko вернула состояние на момент прогона, "
            "а не на конец периода"
            if stale
            else None
        ),
        # Отброшенное показываем цифрой: строка упала вчетверо, и без расшифровки это
        # выглядело бы как пропажа запасов, а не как снятие лишнего.
        detail={
            "positions": fact_count,
            "positions_in_snapshot": run_count,
            "excluded_amount": str(_money(run_total - total)),
            "deleted_positions": len(deleted_rows),
            "deleted_amount": str(_money(sum((row[2] for row in deleted_rows), _ZERO))),
            "synced_at": synced_at.isoformat() if synced_at else None,
            "parts": positions,
        },
    )
    return line, flags


async def _money_lines(
    session: AsyncSession, as_of: date
) -> tuple[list[AssembledLine], list[AssembledFlag]]:
    """Деньги: остаток каждого кошелька, разложенный по строкам баланса."""
    snapshot = await money.build_money_balance_as_of(session, as_of=as_of, include_inactive=True)
    by_line: dict[str, Decimal] = {}
    detail: dict[str, list] = {}
    unmapped: list[dict] = []

    for row in snapshot.rows:
        code = cat.WALLET_LINES.get(row.code)
        if code is None:
            code = cat.UNMAPPED_WALLET_LINE
            unmapped.append({"wallet": row.name, "code": row.code, "balance": str(row.balance)})
        by_line[code] = by_line.get(code, _ZERO) + _money(row.balance)
        detail.setdefault(code, []).append(
            {"wallet": row.name, "balance": str(_money(row.balance))}
        )

    lines: list[AssembledLine] = []
    for code, defn in cat.CATALOG_BY_CODE.items():
        if defn.group != "Денежные средства":
            continue
        if code == cat.UNMAPPED_WALLET_LINE and not unmapped:
            continue
        lines.append(
            _line(
                defn,
                by_line.get(code, _ZERO),
                cat.ST_OK,
                source_kind="wallets",
                detail={
                    "parts": [
                        {"name": item["wallet"], "amount": item["balance"]}
                        for item in detail.get(code, [])
                        if Decimal(item["balance"]) != _ZERO
                    ]
                },
            )
        )

    flags: list[AssembledFlag] = []
    if unmapped:
        flags.append(
            AssembledFlag(
                code="wallet_not_mapped",
                severity="warning",
                title="Счёт не отнесён ни к одной строке баланса",
                explanation=(
                    "Деньги показаны отдельной строкой, а не влиты в расчётный счёт: иначе "
                    "фонд или депозит попал бы в чужую строку и разошёлся со своим обязательством."
                ),
                count=len(unmapped),
                rows=unmapped,
            )
        )
    if snapshot.unmapped_bank_movement:
        flags.append(
            AssembledFlag(
                code="bank_account_without_wallet",
                severity="blocking",
                title="Движение по счёту, за которым нет кошелька",
                explanation=(
                    "Деньги в банке есть, а в балансе их нет вовсе. Расхождение уедет в капитал "
                    "и там растворится."
                ),
                count=1,
                amount=_money(snapshot.unmapped_bank_movement),
            )
        )
    if snapshot.orphan_bank_operations:
        flags.append(
            AssembledFlag(
                code="bank_operation_without_account",
                severity="warning",
                title="Операции выписки без счёта",
                explanation="Такую операцию нельзя отнести даже к чужому кошельку.",
                count=snapshot.orphan_bank_operations,
            )
        )
    return lines, flags


async def _counterparty_lines(
    session: AsyncSession, as_of: date
) -> tuple[list[AssembledLine], list[AssembledFlag], Decimal]:
    sheet = await counterparty_balance_as_of.build_balance_as_of(session, as_of=as_of)
    lines = [
        _line(
            cat.CATALOG_BY_CODE["ar_suppliers"],
            _money(sheet.receivable_total),
            cat.ST_APPROXIMATE if sheet.approximate_settlements else cat.ST_OK,
            source_kind="counterparty_balance_as_of",
            detail={"parts": _parts(sheet.rows, lambda row: row.receivable)},
        ),
        _line(
            cat.CATALOG_BY_CODE["ap_suppliers"],
            _money(sheet.payable_total),
            cat.ST_APPROXIMATE if sheet.approximate_settlements else cat.ST_OK,
            source_kind="counterparty_balance_as_of",
            detail={"parts": _parts(sheet.rows, lambda row: row.payable)},
        ),
        _line(
            cat.CATALOG_BY_CODE["ar_owners"],
            _money(sheet.owner_loans_total),
            cat.ST_OK,
            source_kind="counterparty_balance_as_of",
        ),
    ]
    partner = await partner_cash_balance.build_partner_cash_balance(session, as_of=as_of)
    lines.append(
        _line(
            cat.CATALOG_BY_CODE["ar_partner"],
            partner.outstanding,
            cat.ST_OK if partner.complete else cat.ST_APPROXIMATE,
            source_kind="partner_cash",
            detail={
                "taken": str(partner.taken),
                "commission": str(partner.commission),
                "returned": str(partner.returned),
            },
        )
    )

    flags: list[AssembledFlag] = []
    # Три разные неполноты, и каждая завышает долг СВОИМ слагаемым. Один общий сигнал
    # «цифра приблизительна» не сказал бы владельцу, что именно чинить.
    if not partner.repayments_linked and partner.taken:
        flags.append(
            AssembledFlag(
                code="partner_repayments_not_linked",
                severity="warning",
                title="Возвраты партнёра не привязаны к контрагенту",
                explanation=(
                    "Взятое считается из изъятий смены, а возвращённое — по контрагенту на "
                    "проводке внесения. Партнёр не объявлен в настройках, поэтому возвраты "
                    "не вычитаются и долг ЗАВЫШЕН. Укажите контрагента партнёра и привяжите "
                    "его к пресету внесения в «Настройках → Касса»."
                ),
                count=1,
                amount=partner.taken,
            )
        )
    if partner.open_month:
        flags.append(
            AssembledFlag(
                code="partner_commission_open_month",
                severity="info",
                title="Комиссия партнёра за текущий месяц ещё не посчитана",
                explanation=(
                    "Выручка партнёра приходит помесячно, поэтому на срез внутри месяца "
                    "комиссия текущего месяца не вычтена и долг завышен на неё. На конец "
                    "месяца цифра станет точной."
                ),
                count=1,
            )
        )
    if partner.months_without_commission:
        flags.append(
            AssembledFlag(
                code="partner_commission_missing",
                severity="warning",
                title="За завершённый месяц нет комиссии партнёра",
                explanation=(
                    "Партнёр брал наличные, а его комиссия за этот месяц не посчитана — "
                    "вычесть нечего, и долг завышен на неизвестную величину. Проверьте "
                    "выгрузку партнёрских комиссий."
                ),
                count=len(partner.months_without_commission),
                rows=[{"month": month.isoformat()} for month in partner.months_without_commission],
            )
        )
    # Бартерные гашения — второе слагаемое общего объёма недоказуемого; отдельной карточки у
    # них нет по той же причине, что и у расчётов с людьми (см. ``_approximate_flag``).
    return lines, flags, _money(sheet.approximate_settlements)


async def _staff_lines(
    session: AsyncSession, as_of: date
) -> tuple[list[AssembledLine], list[AssembledFlag], Decimal]:
    staff = await staff_balance_as_of.build_staff_balance_as_of(session, as_of=as_of)
    approximate = _money(staff.approximate_payments) + _money(staff.approximate_write_offs)
    status = cat.ST_APPROXIMATE if approximate else cat.ST_OK
    lines = [
        _line(
            cat.CATALOG_BY_CODE["ar_staff"],
            _money(staff.receivable_total),
            status,
            source_kind="staff_balance_as_of",
        ),
        _line(
            cat.CATALOG_BY_CODE["ap_staff"],
            _money(staff.salary_total),
            status,
            source_kind="staff_balance_as_of",
        ),
        _line(
            cat.CATALOG_BY_CODE["ap_staff_vacation"],
            _money(staff.vacation_total),
            cat.ST_OK,
            source_kind="staff_balance_as_of",
        ),
        _line(
            cat.CATALOG_BY_CODE["ap_deposit_staff"],
            _money(staff.production_deposit_total),
            status,
            source_kind="staff_balance_as_of",
        ),
        _line(
            cat.CATALOG_BY_CODE["ap_fund_staff"],
            _money(staff.fund_total),
            cat.ST_OK,
            source_kind="staff_balance_as_of",
        ),
        _line(
            cat.CATALOG_BY_CODE["ap_deposit_courier"],
            _money(staff.courier_deposit_total),
            cat.ST_OK,
            source_kind="courier_deposits",
        ),
    ]
    # Своего флага здесь нет намеренно: объём недоказуемого показывает ОДИН сигнал на весь
    # баланс (``_approximate_flag``), а расчёты с людьми входят в него слагаемым. Две карточки
    # с одинаковой суммой владелец читал как два разных факта и искал между ними разницу.
    return lines, [], approximate


async def _manual_lines(
    session: AsyncSession, as_of: date
) -> tuple[list[AssembledLine], AssembledFlag | None]:
    """Строки без источника: ручное значение или явная пустота."""
    manual = await _manual_values(session, as_of)
    lines: list[AssembledLine] = []
    waiting: list[dict] = []

    for code in sorted(cat.MANUAL_CODES | cat.ZERO_CODES):
        defn = cat.CATALOG_BY_CODE[code]
        entered = manual.get(code)
        if entered is not None:
            lines.append(
                _line(
                    defn,
                    _money(entered.amount),
                    cat.ST_MANUAL,
                    source_kind="manual",
                    source_note=entered.reason,
                    detail={"source_document": entered.source_document},
                )
            )
            continue
        if defn.fill in (cat.ST_ZERO_CONFIRMED, cat.ST_NOT_APPLICABLE):
            # Подтверждённый ноль: не ждёт ввода и сигнала не поднимает.
            lines.append(_line(defn, _ZERO, defn.fill, source_kind="confirmed"))
            continue
        lines.append(_line(defn, None, cat.ST_MISSING, source_kind="manual"))
        waiting.append({"line": defn.title, "code": code, "why": defn.note})

    flag = (
        AssembledFlag(
            code="manual_lines_empty",
            severity="warning",
            title="Строки ждут ручного ввода",
            explanation=(
                "У этих строк нет регистра, и величина неизвестна. Пока они пусты, капитал "
                "вобрал их в себя: он выводится как остаток."
            ),
            count=len(waiting),
            rows=waiting,
        )
        if waiting
        else None
    )
    return lines, flag


async def _tax_line(session: AsyncSession, as_of: date) -> AssembledLine:
    """Переплата по налогам: положительное сальдо ЕНС."""
    from app.services.taxes.ens_wallet import compute_ens_wallet

    wallet = await compute_ens_wallet(session, as_of=as_of)
    return _line(
        cat.CATALOG_BY_CODE["ar_tax"],
        _money(wallet.balance),
        cat.ST_OK,
        source_kind="ens_wallet",
        detail={"inflow": str(_money(wallet.inflow)), "recognized": str(_money(wallet.recognized))},
    )


def _approximate_flag(
    total: Decimal, *, staff: Decimal, settlements: Decimal
) -> AssembledFlag | None:
    """Объём величин, чья датировка не доказана, — ОДНИМ сигналом на весь баланс.

    ПОЧЕМУ ОДИН, А НЕ ТРИ. Раньше слагаемые поднимали свои карточки, и рядом с ними стояла
    карточка агрегата. Когда бартерных зачётов нет (а их в базе нет вовсе), агрегат становился
    дословной копией расчётов с людьми: та же сумма, другой заголовок. Владелец 15.08.2026
    читал это как два разных факта на 1,1 млн каждый и просил объяснить разницу, которой нет.

    ЧТО ЭТО ЗА ВЕЛИЧИНА. Не ошибка и не её оценка: столько денег в срезе держится на записях,
    которые можно изменить задним числом, не оставив следа (отметка выплаты удаляется
    физически) или чья дата восстановлена бэкфиллом. Полоса допуска сверки считается из
    ИЗМЕНЕНИЯ этой суммы между снимками, а не из неё самой.

    СЧИТАЕТСЯ ТОЛЬКО ПО ТОМУ, ЧТО В СРЕЗЕ СТОИТ. Прогоны, чей период кончился до начала учёта,
    и займы, закрытые до даты среза, сюда не идут: мерить риск деньгами, которых нет ни в одной
    строке, значит пугать числом, с которым нечего делать.
    """
    if not total:
        return None
    rows = []
    if staff:
        rows.append({"title": "Расчёты с людьми", "amount": str(staff)})
    if settlements:
        rows.append({"title": "Бартерные гашения", "amount": str(settlements)})
    return AssembledFlag(
        code="approximate_total",
        severity="info",
        title="Объём приблизительно датированных величин",
        explanation=(
            "Столько в срезе держится на записях, чью дату нельзя доказать: выплаты без "
            "проводки в леджере и займы с датой закрытия из бэкфилла. Это не ошибка и не её "
            "оценка — допуск сверки считается из ИЗМЕНЕНИЯ этой суммы между снимками."
        ),
        count=len(rows),
        amount=total,
        rows=rows,
    )


def _months_lived(year: int, as_of: date) -> int:
    """Сколько месяцев года прожито на дату среза. Месяц засчитывается только целиком.

    Помесячно, а не по дням, намеренно: взносы платятся квартальными долями, и дробление по
    дням дало бы число, которое не сходится ни с одной платёжкой. Незавершённый месяц не
    засчитывается — на 15 июля прожито шесть месяцев, а не шесть с половиной.
    """
    if as_of.year > year:
        return 12
    if as_of.year < year:
        return 0
    last_day = calendar.monthrange(as_of.year, as_of.month)[1]
    return as_of.month if as_of.day >= last_day else as_of.month - 1


def _earned_by_time(item, as_of: date) -> Decimal:
    """Какая часть ПРОГНОЗНОГО обязательства уже заработана временем на дату среза.

    ЗАЧЕМ ЭТО ВООБЩЕ НУЖНО. ``is_projection`` — метка ПРОИСХОЖДЕНИЯ строки («документов от
    бухгалтера ещё нет, платить по ней нельзя»), а не времени. Баланс читал её как «период не
    прожит» и выбрасывал строку целиком — из-за чего фиксированные взносы ИП, заработанные
    январём–июлем, не стояли в долге вовсе: 19 130 ₽ на 31.07.2026 молча уезжали в капитал.

    ГОДОВОЙ ПЛАТЁЖ ЗАРАБАТЫВАЕТСЯ ВРЕМЕНЕМ. Обязанность по фиксвзносам возникает за время
    ведения деятельности, а не в день срока уплаты: закройся ИП в августе, за сентябрь–декабрь
    платить не пришлось бы. Поэтому прожитая часть — обязательство, непрожитая — прогноз.
    Считаем от ГОДОВОЙ суммы, а не от остатка: остаток уже уменьшен уплаченным, и доля
    непрожитых месяцев должна вычитаться из полной величины года.

    МЕСЯЧНЫЙ ПРОГНОЗ ДЕЛИТЬ НЕ НА ЧТО — месяц либо прожит, либо нет.
    """
    for_year = getattr(item, "for_year", None)
    if for_year is None:
        return _ZERO
    period = getattr(item, "for_period", None) or ""

    if period == "year":
        try:
            from app.services.taxes.repository import year_config

            year_total = _money(year_config(for_year).fixed_contribution)
        except Exception:  # noqa: BLE001 — нет конфига года: в долг не берём, покажет сигнал
            return _ZERO
        months_left = 12 - _months_lived(for_year, as_of)
        unlived = _money(year_total * Decimal(months_left) / Decimal(12))
        return max(_ZERO, _money(item.amount) - unlived)

    if len(period) == 7 and period[4] == "-":
        try:
            year, month = int(period[:4]), int(period[5:])
            month_end = date(year, month, calendar.monthrange(year, month)[1])
        except ValueError:
            return _ZERO
        return _money(item.amount) if month_end <= as_of else _ZERO

    # Незнакомая разметка периода: в долг не берём. Занижение видно сигналом, выдумка — нет.
    return _ZERO


async def _tax_payable_line(
    session: AsyncSession, as_of: date
) -> tuple[AssembledLine, AssembledFlag | None]:
    """Долг перед бюджетом: недостача по наступившим срокам ПЛЮС начисленное вперёд.

    ПОЧЕМУ НЕ ОДНА НЕДОСТАЧА ЕНС. Сальдо ЕНС признаёт начисление по СРОКУ УПЛАТЫ, поэтому
    зарплатный ЕНП за июль (срок 28.08) на 31 июля в нём ещё не виден. Для платёжного календаря
    это верно — платить рано; для баланса нет: налог за июль начислен в июле и на 31 июля уже
    является обязательством. Строка показывала 20 053,42 вместо 145 595,35 — занижение в семь
    раз, и оно молча уезжало в капитал.

    ИСТОЧНИК — ФУНКЦИЯ САМОГО НАЛОГОВОГО МОДУЛЯ. ``list_payable_obligations`` наполняет и
    «Активные платежи», и строку налогов в учёте ДЗ/КЗ; её docstring прямо говорит «одна цифра
    в трёх местах по построению». Баланс стал четвёртым местом и считал по-своему — ровно то,
    из-за чего цифры разошлись.

    ПРОГНОЗ ДЕЛИТСЯ ВРЕМЕНЕМ, А НЕ ВЫБРАСЫВАЕТСЯ ЦЕЛИКОМ. Строка «Взносы ИП за себя · остаток
    года» (43 042,50 со сроком 28.12) не является ни чистым долгом, ни чистым прогнозом:
    апрель–июль на 31.07.2026 прожиты и уже заработаны (19 130,00), август–декабрь — нет
    (23 912,50). Раньше выбрасывалась вся строка, и заработанная часть молча уезжала в капитал.
    Делит ``_earned_by_time``; непрожитый хвост называет отдельный сигнал.
    """
    from app.services.taxes.ens_wallet import compute_ens_wallet
    from app.services.taxes.obligations import list_payable_obligations

    wallet = await compute_ens_wallet(session, as_of=as_of)
    try:
        obligations = await list_payable_obligations(session, today=as_of)
    except Exception as error:  # noqa: BLE001 — налоговый модуль не должен рушить баланс
        return (
            _line(
                cat.CATALOG_BY_CODE["ap_tax"],
                _money(wallet.shortfall),
                cat.ST_APPROXIMATE,
                source_kind="ens_wallet",
                source_note=(
                    f"Реестр обязательств недоступен ({type(error).__name__}) — показана только "
                    f"недостача ЕНС, начисленное вперёд не учтено"
                ),
            ),
            None,
        )

    accrued = [item for item in obligations if not getattr(item, "is_projection", False)]
    projected = [item for item in obligations if getattr(item, "is_projection", False)]

    # Прогноз делится временем: прожитую часть года бизнес уже должен, непрожитую — ещё нет.
    earned: list[tuple[object, Decimal]] = []
    unlived: list[tuple[object, Decimal]] = []
    for item in projected:
        part = _earned_by_time(item, as_of)
        if part > _ZERO:
            earned.append((item, part))
        rest = _money(item.amount) - part
        if rest > _ZERO:
            unlived.append((item, rest))

    accrued_total = _money(sum((item.amount for item in accrued), _ZERO))
    earned_total = _money(sum((part for _, part in earned), _ZERO))
    # Недостача ЕНС и начисленное вперёд НЕ пересекаются: первая живёт по наступившим срокам,
    # второе — по ненаступившим. Сложение здесь не задваивает.
    total = _money(wallet.shortfall) + accrued_total + earned_total

    line = _line(
        cat.CATALOG_BY_CODE["ap_tax"],
        total,
        cat.ST_OK,
        source_kind="tax_obligations",
        detail={
            "ens_shortfall": str(_money(wallet.shortfall)),
            "accrued_ahead": str(accrued_total),
            "earned_projection": str(earned_total),
            "items": [
                {"title": item.title, "amount": str(_money(item.amount)), "due": str(item.due_date)}
                for item in accrued
            ]
            + [
                {
                    "title": f"{item.title} · заработанная часть",
                    "amount": str(part),
                    "due": str(item.due_date),
                }
                for item, part in earned
            ],
        },
    )
    flag = (
        AssembledFlag(
            code="tax_projection_not_counted",
            severity="info",
            title="Налог за непрожитый период в долг не включён",
            explanation=(
                "Остаток налоговых начислений, время которых ещё не наступило: закройся дело "
                "завтра — платить по ним не пришлось бы. Та часть, что уже заработана временем, "
                "в долге стоит — она внутри строки «Задолженность по налогам и взносам»."
            ),
            count=len(unlived),
            amount=_money(sum((rest for _, rest in unlived), _ZERO)),
            rows=[
                {
                    "title": item.title,
                    "amount": str(rest),
                    "due": str(item.due_date),
                    "earned": str(_money(item.amount) - rest),
                }
                for item, rest in unlived
            ],
        )
        if unlived
        else None
    )
    return line, flag


async def _data_horizon(session: AsyncSession) -> date | None:
    """Дата, до которой факты в систему заведены.

    Берётся по ДЕНЕЖНЫМ регистрам — выписке и проводкам ДДС: именно они говорят, докуда книги
    доведены. Накладные для этого не годятся: их датируют вперёд по договору (на прод-копии
    лежат документы от 31.08 при фактах до 08.08), и по ним горизонт оказался бы в будущем.
    """
    return await session.scalar(
        select(
            func.greatest(
                func.coalesce(
                    select(func.max(BankOperation.operation_date)).scalar_subquery(), ZERO_DATE
                ),
                func.coalesce(
                    select(func.max(CashflowTransaction.operation_date)).scalar_subquery(),
                    ZERO_DATE,
                ),
            )
        )
    )


async def _equity_lines(
    session: AsyncSession, as_of: date, *, total: Decimal
) -> tuple[list[AssembledLine], list[AssembledFlag]]:
    """Капитал, разложенный на слагаемые, а не одной балансирующей строкой.

    ЧТО ЭТО ДАЁТ. «Капитал 3 870 985,45» не отвечает ни на «от чего вообще считается баланс»,
    ни на «где накопленная прибыль» — а это первые два вопроса владельца. После разложения
    балансирующей остаётся ТОЛЬКО одна строка: капитал на начало учёта, установленный описью.
    Всё, что случилось с 01.07.2026, названо своими слагаемыми из регистров:

        капитал = капитал на начало + вклады − возвраты − дивиденды + накопленная прибыль

    ПОБОЧНОЕ СВОЙСТВО, РАДИ КОТОРОГО ЭТО И СТОИЛО ДЕЛАТЬ. «Капитал на начало учёта» посчитан
    как остаток, значит обязан быть ОДИНАКОВЫМ во всех месяцах: он описывает момент, который
    больше не меняется. Его сдвиг между снимками — ровно то нераспределённое расхождение,
    которое ищет сверка приращения. Строка стала видимой формой той же проверки, и заметить
    дыру теперь можно, не дожидаясь второго снимка.

    ПРИБЫЛЬ СУММИРУЕТСЯ ПОМЕСЯЧНО, а не берётся одним запросом за период: отчёт о прибыли
    считает месяц, и «прибыль за два месяца» у него спросить нельзя. Месяц, чью прибыль отчёт
    не отдал, в сумму не входит и поднимает сигнал — молча пропустить его значило бы занизить
    накопленную прибыль на неизвестную величину и переложить её в балансирующую строку.
    """
    from app.services.balance.reconcile import equity_movements

    # Нижняя граница у ``equity_movements`` строгая, поэтому отступаем на день назад: движение
    # первого дня учёта обязано попасть в накопительный итог.
    movements = await equity_movements(
        session, date_from=ACCOUNTING_START - timedelta(days=1), date_to=as_of
    )

    # Учредительные вклады из реестра собственников. Деньгами они в системе не проходили —
    # иначе их принесла бы проводка ДДС, — и без реестра 5 млн молча сидели бы внутри
    # балансирующей строки. Дата вклада обязательна для попадания в срез: вклад, о котором
    # известно «когда-то», нельзя отнести ни к одному месяцу.
    founding_rows = (
        await session.execute(
            select(Counterparty.name, BusinessOwner.contribution_amount)
            .join(Counterparty, Counterparty.id == BusinessOwner.counterparty_id)
            .where(
                BusinessOwner.contribution_amount > 0,
                BusinessOwner.contributed_on.is_not(None),
                BusinessOwner.contributed_on <= as_of,
            )
            .order_by(BusinessOwner.contribution_amount.desc())
        )
    ).all()
    founding = _money(sum((row[1] for row in founding_rows), _ZERO))

    retained = _ZERO
    # Прибыль текущего месяца отдельно и с правом быть НЕИЗВЕСТНОЙ: ноль здесь прочитался бы
    # как «месяц отработали в ноль», то есть как факт вместо его отсутствия.
    current_month: Decimal | None = None
    unknown_months: list[str] = []
    month = ACCOUNTING_START
    last_month = as_of.replace(day=1)
    while month <= last_month:
        profit, status, _missing = await month_net_profit(session, month)
        if profit is None:
            unknown_months.append(f"{month:%m.%Y} — {status or 'без причины'}")
        else:
            retained += profit
            if month == last_month:
                current_month = profit
        month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)

    contributions = _money(movements.contributions) + founding
    named = contributions - movements.returns - movements.dividends + retained
    opening = _money(total - named)

    lines = [
        _line(
            cat.CATALOG_BY_CODE["equity_contributions"],
            contributions,
            cat.ST_OK,
            source_kind="owner_registry",
            detail={
                "parts": [{"name": row[0], "amount": str(_money(row[1]))} for row in founding_rows]
                + (
                    [{"name": "Поступления деньгами", "amount": str(movements.contributions)}]
                    if movements.contributions
                    else []
                )
            },
        ),
        _line(
            cat.CATALOG_BY_CODE["equity_opening"],
            opening,
            "plug",
            source_kind="derived",
        ),
        # Изъятия капитал УМЕНЬШАЮТ, поэтому уходят со знаком минус: подытог группы складывает
        # строки как есть, и положительное число здесь раздуло бы капитал вдвое от изъятого.
        _line(
            cat.CATALOG_BY_CODE["equity_returns"],
            _money(-movements.returns),
            cat.ST_OK,
            source_kind="dds_articles",
        ),
        _line(
            cat.CATALOG_BY_CODE["equity_dividends"],
            _money(-movements.dividends),
            cat.ST_OK,
            source_kind="dds_articles",
        ),
        _line(
            cat.CATALOG_BY_CODE["equity_retained"],
            _money(retained),
            cat.ST_APPROXIMATE if unknown_months else cat.ST_OK,
            source_kind="pnl",
        ),
        _line(
            cat.CATALOG_BY_CODE["equity_profit_memo"],
            _money(current_month) if current_month is not None else None,
            cat.ST_OK if current_month is not None else cat.ST_MISSING,
            source_kind="pnl",
            source_note=(
                None
                if current_month is not None
                else "Отчёт о прибыли за этот месяц величину не отдал"
            ),
        ),
    ]

    flags: list[AssembledFlag] = []
    if unknown_months:
        flags.append(
            AssembledFlag(
                code="equity_profit_month_unknown",
                severity="blocking",
                title="Прибыль части месяцев неизвестна",
                explanation=(
                    "Накопленная прибыль занижена на прибыль этих месяцев, а разница молча "
                    "ушла в строку «капитал на начало учёта» — то есть туда, где её никто "
                    "не ищет."
                ),
                count=len(unknown_months),
                rows=[{"month": item} for item in unknown_months],
            )
        )
    return lines, flags


async def assemble(session: AsyncSession, *, as_of: date, live: bool = False) -> AssembledBalance:
    """Собрать баланс на дату из регистров. Ничего не пишет.

    ``live=True`` — режим детектора дрейфа: основные средства берутся живым расчётом, а не
    замороженным снимком. Обычная сборка использует снимок, потому что закрытый месяц обязан
    отдавать то, что уже перенесено в отчётность.
    """
    if as_of < ACCOUNTING_START:
        raise BalanceBeforeAccountingStart(
            f"Управленческий учёт ведётся с {ACCOUNTING_START:%d.%m.%Y} — "
            f"баланс на {as_of:%d.%m.%Y} собрать не из чего"
        )

    lines: list[AssembledLine] = []
    flags: list[AssembledFlag] = []
    approximate = _ZERO

    lines += await _assets_lines(session, as_of, live=live)

    stock_line, stock_flags = await _stock_line(session, as_of)
    lines.append(stock_line)
    flags += stock_flags

    money_lines, money_flags = await _money_lines(session, as_of)
    lines += money_lines
    flags += money_flags

    cp_lines, cp_flags, cp_approx = await _counterparty_lines(session, as_of)
    lines += cp_lines
    flags += cp_flags
    approximate += cp_approx

    staff_lines, staff_flags, staff_approx = await _staff_lines(session, as_of)
    lines += staff_lines
    flags += staff_flags
    approximate += staff_approx

    approximate_flag = _approximate_flag(approximate, staff=staff_approx, settlements=cp_approx)
    if approximate_flag is not None:
        flags.append(approximate_flag)

    lines.append(await _tax_line(session, as_of))
    tax_payable, tax_flag = await _tax_payable_line(session, as_of)
    lines.append(tax_payable)
    if tax_flag:
        flags.append(tax_flag)

    manual_lines, manual_flag = await _manual_lines(session, as_of)
    lines += manual_lines
    if manual_flag:
        flags.append(manual_flag)

    # Итоги: memo не считаются — их величина уже сидит внутри другой строки.
    asset_total = _money(
        sum(
            (line.amount or _ZERO)
            for line in lines
            if line.block == cat.ASSET and line.kind != cat.KIND_MEMO
        )
    )
    liability_total = _money(
        sum(
            (line.amount or _ZERO)
            for line in lines
            if line.block == cat.LIABILITY and line.kind != cat.KIND_MEMO
        )
    )
    equity_plug = _money(asset_total - liability_total)

    equity_lines, equity_flags = await _equity_lines(session, as_of, total=equity_plug)
    lines += equity_lines
    flags += equity_flags

    # СРЕЗ ЗА ГОРИЗОНТОМ ДАННЫХ — не «почти готовый баланс», а смесь двух эпох, и молчать об
    # этом нельзя. Разобрано на прод-копии 09.08.2026: баланс на 31.08 при фактах до 08.08
    # показывал падение капитала на 92 844 ₽ при прибыли ОПиУ +190 338 ₽, и разница в
    # 283 182 ₽ выглядела дырой в учёте. Дыры не было: обязательства месяца в срез уже вошли
    # (зарплата начислена на полный месяц, накладные датированы 31.08, налог начислен), а
    # выручка и расходы после 8 августа не существуют вовсе — их ещё не было.
    #
    # Строку «накопленный результат до начала учёта» это бьёт сильнее всего: она балансирующая
    # и молча впитывает всю разницу, притом что обязана быть неизменной от месяца к месяцу.
    horizon = await _data_horizon(session)
    if horizon and as_of > horizon:
        gap = (as_of - horizon).days
        # Разрыв в день-два — обычное дело: выписку разбирают не каждый день, а в конце месяца
        # запросто не случается ни одной проводки. Отличить «не было движений» от «не завели»
        # по данным нельзя, поэтому сказать надо в обоих случаях, но блокировать — только когда
        # непокрытых дней столько, что месяц заведомо неполон. Граница в три дня закрывает
        # выходные и признаётся здесь как соглашение, а не как истина.
        material = gap > 3
        flags.append(
            AssembledFlag(
                code="balance_beyond_data_horizon",
                severity="blocking" if material else "info",
                title=(
                    "Срез запрошен на дату позже последних данных"
                    if material
                    else "После последней проводки прошло несколько дней"
                ),
                explanation=(
                    f"Факты в системе доведены до {horizon:%d.%m.%Y}, а баланс собран на "
                    f"{as_of:%d.%m.%Y} — {gap} дн. без данных. "
                    + (
                        "Обязательства будущего в срез уже вошли: зарплата начислена на полный "
                        "месяц, документы с поздней датой учтены. А выручки и расходов за "
                        "оставшиеся дни ещё нет — их не было. Это прогноз, а не остаток, и "
                        "сравнивать его с прошлым месяцем нельзя."
                        if material
                        else "Движений могло не быть, а могло не хватить разбора выписки — "
                        "по данным это неразличимо."
                    )
                ),
                count=gap,
            )
        )

    lines.sort(key=lambda item: item.sort_order)
    return AssembledBalance(
        as_of=as_of,
        lines=lines,
        flags=flags,
        asset_total=asset_total,
        liability_total=liability_total,
        equity_plug=equity_plug,
        approximate_total=approximate,
    )
