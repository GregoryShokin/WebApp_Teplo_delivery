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

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BalanceManualValue, PnlIikoStockFact
from app.services import asset_balance, counterparty_balance_as_of, staff_balance_as_of
from app.services import wallet_balance_as_of as money
from app.services.accounting_periods import ACCOUNTING_START
from app.services.balance import catalog as cat

_ZERO = Decimal("0.00")


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
) -> tuple[AssembledLine, AssembledFlag | None]:
    """Запасы: стоимостной остаток склада на конец месяца.

    Цифра существует ТОЛЬКО на границы месяца — снимок берётся у iiko на 00:00 первого числа
    следующего месяца. Поэтому у неё две беды, и обе надо показать, а не спрятать: снимка может
    не быть вовсе (прогон не состоялся), и он может быть СВЕЖЕЕ месяца — прогон, случившийся
    20 августа, спрашивает у iiko будущую отметку времени и записывает ответ как августовский.
    Строка есть, а доверия ей нет.
    """
    defn = cat.CATALOG_BY_CODE["stock_total"]
    month = as_of.replace(day=1)
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(PnlIikoStockFact.closing_amount), 0),
                func.count(),
                func.max(PnlIikoStockFact.synced_at),
            ).where(PnlIikoStockFact.period_month == month)
        )
    ).one()
    total, fact_count, synced_at = _money(row[0]), int(row[1] or 0), row[2]

    if not fact_count:
        return (
            _line(
                defn,
                None,
                cat.ST_MISSING,
                source_kind="iiko_stock",
                source_note="Складского снимка за этот месяц нет — прогон iiko не состоялся",
            ),
            AssembledFlag(
                code="stock_month_missing",
                severity="blocking",
                title="Нет складского остатка за месяц",
                explanation=(
                    "Стоимостной остаток склада берётся снимком iiko на конец месяца. Снимка "
                    "нет, поэтому строка «Запасы» пуста, а не равна нулю."
                ),
                count=1,
            ),
        )

    # Снимок закрывающего остатка берётся на 00:00 первого числа СЛЕДУЮЩЕГО месяца. Если
    # синхронизация прошла раньше этого момента, iiko ответила о будущем — то есть отдала
    # состояние на «сейчас», а не на конец месяца.
    boundary = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    stale = synced_at is not None and synced_at.date() < boundary
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
        detail={"positions": fact_count, "synced_at": synced_at.isoformat() if synced_at else None},
    )
    flag = (
        AssembledFlag(
            code="stock_snapshot_stale",
            severity="warning",
            title="Складской остаток снят до конца месяца",
            explanation=(
                "Прогон iiko состоялся раньше границы месяца, поэтому в снимке лежит остаток "
                "на день прогона. Пересобрать месяц после его окончания."
            ),
            count=1,
            amount=total,
        )
        if stale
        else None
    )
    return line, flag


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
                detail={"wallets": detail.get(code, [])},
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
        ),
        _line(
            cat.CATALOG_BY_CODE["ap_suppliers"],
            _money(sheet.payable_total),
            cat.ST_APPROXIMATE if sheet.approximate_settlements else cat.ST_OK,
            source_kind="counterparty_balance_as_of",
        ),
    ]
    flags: list[AssembledFlag] = []
    if sheet.approximate_settlements:
        flags.append(
            AssembledFlag(
                code="settlements_approximate",
                severity="info",
                title="Часть гашений датирована приблизительно",
                explanation=(
                    "У бартерных зачётов даты события нет — берётся дата записи. Само по себе "
                    "это не ошибка, но сдвиг на день переносит сумму в соседний месяц."
                ),
                amount=_money(sheet.approximate_settlements),
            )
        )
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
    flags: list[AssembledFlag] = []
    if approximate:
        flags.append(
            AssembledFlag(
                code="staff_approximate",
                severity="info",
                title="Часть расчётов с людьми восстановлена приблизительно",
                explanation=(
                    "Выплаты без проводки в леджере и займы, чья дата закрытия получена "
                    "бэкфиллом. Величина показана, но её датировка не доказана."
                ),
                amount=approximate,
            )
        )
    return lines, flags, approximate


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


async def _tax_payable_line(session: AsyncSession, as_of: date) -> AssembledLine:
    """Долг перед бюджетом: недостача по ЕНС."""
    from app.services.taxes.ens_wallet import compute_ens_wallet

    wallet = await compute_ens_wallet(session, as_of=as_of)
    return _line(
        cat.CATALOG_BY_CODE["ap_tax"],
        _money(wallet.shortfall),
        cat.ST_OK,
        source_kind="ens_wallet",
    )


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

    stock_line, stock_flag = await _stock_line(session, as_of)
    lines.append(stock_line)
    if stock_flag:
        flags.append(stock_flag)

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

    lines.append(await _tax_line(session, as_of))
    lines.append(await _tax_payable_line(session, as_of))

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

    equity_defn = cat.CATALOG_BY_CODE["equity_plug"]
    lines.append(
        _line(
            equity_defn,
            equity_plug,
            "plug",
            source_kind="derived",
            source_note=(
                "Выведен как актив минус обязательства, а не посчитан: истории до начала учёта "
                "в системе нет. Равенство «актив = пассив» поэтому тождественно и проверкой "
                "не является — проверяет сверка приращения со следующим снимком."
            ),
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
