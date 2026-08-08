"""Сверка приращения капитала — единственная настоящая проверка в этом модуле.

ПОЧЕМУ «АКТИВ = ПАССИВ» НИЧЕГО НЕ ПРОВЕРЯЕТ. Капитал выводится как «актив минус обязательства»,
значит равенство выполняется по построению, в любом месяце, при любых ошибках в данных. Владелец
увидит «сходится» и поверит — это опаснее отсутствующей проверки.

ЧТО ПРОВЕРЯЕТСЯ НА САМОМ ДЕЛЕ. Со второго снимка появляется величина, которую можно получить
ДВУМЯ независимыми путями:

    приращение капитала  =  прибыль за период  −  дивиденды  +  вклады собственников

Левая часть приходит из балансов (разница двух замороженных капиталов), правая — из отчёта о
прибыли и движений по статьям капитала. Пути не пересекаются, поэтому расхождение означает
настоящую дыру: либо в прибыль не попал расход, либо в балансе есть движение мимо прибыли.

ЖЕЛЕЗНОЕ ПРАВИЛО, БЕЗ КОТОРОГО ВСЁ ЭТО МЕРТВО: **ни одно поле снимка не участвует в вычислении
правой части.** Стоит однажды взять прибыль из шапки снимка «чтобы сходилось» — и сверка станет
тождественным нулём навсегда. Такое уже случалось в отчёте о прибыли, где обе части считались
одним циклом и дрейф был равен нулю алгебраически.

ПОЛОСА ДОПУСКА — ЭТО ИЗМЕНЕНИЕ НЕТОЧНОСТИ, А НЕ ЕЁ ОБЪЁМ. Соблазн взять сумму приблизительных
величин обоих снимков и объявить её допуском ведёт к «чем хуже данные, тем шире сошлось»: один
бартерный зачёт на 400 000 ₽ дал бы право на расхождение в 400 000 ₽. Допуск считается как
модуль ИЗМЕНЕНИЯ приблизительной части между снимками плюс копейки на округление — то есть
отвечает на вопрос «насколько могла сдвинуться датировка за этот месяц», а не «сколько у нас
вообще неточного».
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BalanceSnapshot, CashflowTransaction, DdsArticle

_ZERO = Decimal("0.00")
# Копейки на округление: суммы складываются из сотен строк, каждая округлена до копейки.
_ROUNDING_ALLOWANCE = Decimal("1.00")

# Статьи движения капитала. Дивиденды и возврат вкладов уменьшают капитал, поступления
# увеличивают. Коды взяты из каталога статей ДДС; все три имеют activity_type='financing'.
DIVIDEND_CODES = ("dividendy",)
CONTRIBUTION_CODES = ("postuplenie_deneg_ot_sobstvennikov",)
RETURN_CODES = ("vozvrat_deneg_sobstvennikam",)


def _money(value: object) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class EquityMovements:
    """Движения капитала за период — считаются из проводок, НЕ из снимка."""

    dividends: Decimal
    contributions: Decimal
    returns: Decimal


@dataclass(frozen=True)
class Reconciliation:
    """Итог сверки. ``residual`` — то, что не объяснено ничем."""

    delta_equity: Decimal
    expected: Decimal
    residual: Decimal
    tolerance: Decimal
    within_tolerance: bool
    net_profit: Decimal | None
    net_profit_status: str | None
    movements: EquityMovements

    @property
    def explained(self) -> bool:
        """Сошлось ли. При неизвестной прибыли ответа нет — и это не «сошлось»."""
        return self.net_profit is not None and self.within_tolerance


async def equity_movements(
    session: AsyncSession, *, date_from: date, date_to: date
) -> EquityMovements:
    """Движения по статьям капитала за период (включительно обе даты)."""

    async def _total(codes: tuple[str, ...]) -> Decimal:
        value = await session.scalar(
            select(func.coalesce(func.sum(CashflowTransaction.amount), 0))
            .join(DdsArticle, DdsArticle.id == CashflowTransaction.article_id)
            .where(
                DdsArticle.code.in_(codes),
                CashflowTransaction.operation_date > date_from,
                CashflowTransaction.operation_date <= date_to,
                CashflowTransaction.quality_status != "excluded",
            )
        )
        return _money(value)

    return EquityMovements(
        dividends=await _total(DIVIDEND_CODES),
        contributions=await _total(CONTRIBUTION_CODES),
        returns=await _total(RETURN_CODES),
    )


async def reconcile(
    session: AsyncSession,
    *,
    previous: BalanceSnapshot,
    current_equity: Decimal,
    current_as_of: date,
    current_approximate: Decimal,
    previous_approximate: Decimal,
    net_profit: Decimal | None,
    net_profit_status: str | None,
) -> Reconciliation:
    """Сверить приращение капитала с независимо посчитанной правой частью.

    ``current_equity`` — капитал СОБИРАЕМОГО снимка (он ещё не записан, поэтому передаётся
    значением). ``previous`` — уже замороженный снимок: его капитал берётся как есть, а не
    пересчитывается, иначе ретро-правка прошлого молча переехала бы в расхождение текущего
    месяца.
    """
    delta = _money(current_equity) - _money(previous.equity_plug)
    movements = await equity_movements(session, date_from=previous.as_of, date_to=current_as_of)

    # Правая часть: прибыль минус то, что из капитала вынули, плюс то, что внесли.
    # Ни одно слагаемое не читается из снимка.
    expected = (
        (_money(net_profit) if net_profit is not None else _ZERO)
        - movements.dividends
        - movements.returns
        + movements.contributions
    )
    residual = _money(delta - expected)

    # Допуск — изменение неточности, а не её объём.
    tolerance = _money(
        abs(_money(current_approximate) - _money(previous_approximate)) + _ROUNDING_ALLOWANCE
    )

    return Reconciliation(
        delta_equity=_money(delta),
        expected=_money(expected),
        residual=residual,
        tolerance=tolerance,
        within_tolerance=abs(residual) <= tolerance,
        net_profit=_money(net_profit) if net_profit is not None else None,
        net_profit_status=net_profit_status,
        movements=movements,
    )


def explain(recon: Reconciliation) -> str:
    """Человеческая расшифровка результата — она же попадает в отчёт владельцу."""
    if recon.net_profit is None:
        return (
            "Прибыль за период неизвестна, поэтому сверить приращение капитала не с чем. "
            "Это не «сошлось» и не «не сошлось» — это отсутствие ответа."
        )
    if recon.within_tolerance:
        return (
            f"Капитал изменился на {recon.delta_equity}, ожидалось {recon.expected}. "
            f"Расхождение {recon.residual} — в пределах сдвига датировок ({recon.tolerance})."
        )
    return (
        f"Капитал изменился на {recon.delta_equity}, а по прибыли и движениям капитала "
        f"ожидалось {recon.expected}. Необъяснённое расхождение {recon.residual} при допуске "
        f"{recon.tolerance}: либо в прибыль не попал расход, либо в балансе есть движение "
        f"мимо прибыли."
    )
