"""Заморозка, чтение и детектор дрейфа балансового снимка.

ТРИ СОСТОЯНИЯ И ГРАНИЦА МЕЖДУ НИМИ. Черновик пересобирается сколько угодно — это рабочая
поверхность, на которой владелец видит, что подтянулось, и вносит недостающее руками.
Финальный снимок не пересобирается никогда: он подписан и перенесён в отчётность. Разница между
ними — не техническая, а смысловая: черновик отвечает «сколько получается», финал — «сколько мы
сказали».

ДЕТЕКТОР ДРЕЙФА СРАВНИВАЕТ СНИМОК С ЖИВЫМ РАСЧЁТОМ, и живой расчёт ради этого явно переключает
основные средства на пересчёт: их регистр за закрытый месяц отдаёт собственный замороженный
снимок, и сравнение снимка со снимком дало бы тождественный ноль. Ту же ошибку легко повторить
этажом выше, поэтому режим ``live`` в сборке существует ровно для этого пути.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    BalanceSnapshot,
    BalanceSnapshotFlag,
    BalanceSnapshotLine,
)
from app.services.balance import reconcile as recon_service
from app.services.balance.assembly import AssembledBalance, assemble, month_net_profit

_ZERO = Decimal("0.00")


class BalanceSnapshotError(ValueError):
    """Действие над снимком невозможно в его текущем состоянии."""


def _money(value: object) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


async def _previous_snapshot(session: AsyncSession, as_of: date) -> BalanceSnapshot | None:
    """Ближайший ФИНАЛЬНЫЙ снимок раньше даты.

    Именно финальный: сверять приращение с черновиком, который завтра пересоберут, значит
    получить сверку, меняющуюся сама собой.
    """
    return await session.scalar(
        select(BalanceSnapshot)
        .where(BalanceSnapshot.as_of < as_of, BalanceSnapshot.status == "final")
        .order_by(BalanceSnapshot.as_of.desc())
        .limit(1)
    )


@dataclass(frozen=True)
class LineDrift:
    line_code: str
    title: str
    frozen_amount: Decimal | None
    live_amount: Decimal | None

    @property
    def delta(self) -> Decimal:
        return _money((self.live_amount or _ZERO) - (self.frozen_amount or _ZERO))


async def freeze(
    session: AsyncSession,
    *,
    as_of: date,
    actor_user_id: uuid.UUID | None = None,
    note: str | None = None,
) -> BalanceSnapshot:
    """Собрать баланс на дату и заморозить его черновиком.

    Идемпотентно: повторный вызов ПЕРЕСОБИРАЕТ черновик — данные могли доехать. Финальный
    снимок не трогается никогда, попытка отклоняется.
    """
    existing = await session.scalar(select(BalanceSnapshot).where(BalanceSnapshot.as_of == as_of))
    if existing is not None and existing.status == "final":
        raise BalanceSnapshotError(
            f"Баланс на {as_of:%d.%m.%Y} финализирован — пересобрать его нельзя. "
            f"Если цифра изменилась, это видно в дрейфе."
        )

    assembled: AssembledBalance = await assemble(session, as_of=as_of)
    previous = await _previous_snapshot(session, as_of)
    month = as_of.replace(day=1)
    net_profit, profit_status, profit_missing = await month_net_profit(session, month)

    reconciliation = None
    if previous is not None:
        reconciliation = await recon_service.reconcile(
            session,
            previous=previous,
            current_equity=assembled.equity_plug,
            current_as_of=as_of,
            current_approximate=assembled.approximate_total,
            # Приблизительная часть предыдущего снимка заморожена в его флаге: брать её
            # пересчётом значило бы, что допуск прошлого месяца меняется задним числом.
            previous_approximate=await _frozen_approximate(session, previous.id),
            net_profit=net_profit,
            net_profit_status=profit_status,
        )

    if existing is None:
        # Итоги проставляются В КОНСТРУКТОРЕ, а не после ``add``: строки и флаги ссылаются на
        # ``snapshot.id``, значит шапку надо записать раньше них, а запись пустой шапки упирается
        # в NOT NULL. Заполнить сразу дешевле, чем городить отложенную запись.
        snapshot = BalanceSnapshot(
            as_of=as_of,
            period_month=month,
            asset_total=assembled.asset_total,
            liability_total=assembled.liability_total,
            equity_plug=assembled.equity_plug,
        )
        session.add(snapshot)
        await session.flush()
    else:
        snapshot = existing
        await session.execute(
            delete(BalanceSnapshotLine).where(BalanceSnapshotLine.snapshot_id == snapshot.id)
        )
        await session.execute(
            delete(BalanceSnapshotFlag).where(BalanceSnapshotFlag.snapshot_id == snapshot.id)
        )

    snapshot.period_month = month
    snapshot.status = "draft"
    # Метод хранится, а не выводится из «есть ли предыдущий»: снимок, добавленный задним
    # числом между двумя существующими, не должен менять смысл уже подписанных цифр.
    snapshot.method = "inventory" if previous is None else "rolling"
    snapshot.asset_total = assembled.asset_total
    snapshot.liability_total = assembled.liability_total
    snapshot.equity_plug = assembled.equity_plug
    snapshot.net_profit = net_profit
    snapshot.net_profit_status = profit_status
    snapshot.net_profit_missing = profit_missing
    snapshot.created_by_user_id = actor_user_id
    snapshot.note = note

    if reconciliation is not None:
        snapshot.prev_snapshot_id = previous.id if previous else None
        snapshot.recon_delta_equity = reconciliation.delta_equity
        snapshot.recon_expected = reconciliation.expected
        snapshot.recon_residual = reconciliation.residual
        snapshot.tolerance_band = reconciliation.tolerance
        snapshot.equity_dividends = reconciliation.movements.dividends
        snapshot.equity_contributions = reconciliation.movements.contributions
        snapshot.equity_returns = reconciliation.movements.returns
    else:
        snapshot.prev_snapshot_id = None
        snapshot.recon_delta_equity = None
        snapshot.recon_expected = None
        snapshot.recon_residual = None
        snapshot.tolerance_band = None

    for line in assembled.lines:
        session.add(
            BalanceSnapshotLine(
                snapshot_id=snapshot.id,
                line_code=line.code,
                title=line.title,
                block=line.block,
                group_code=line.group,
                sort_order=line.sort_order,
                sign=line.sign,
                kind=line.kind,
                amount=line.amount,
                status=line.status,
                source_kind=line.source_kind,
                source_note=line.source_note,
                detail=line.detail,
            )
        )

    flags = list(assembled.flags)
    # Объём приблизительного замораживается флагом: посчитанный в октябре, он был бы другим,
    # и подпись «сошлось в пределах неточности» задним числом осталась бы без основания.
    flags.append(_approximate_flag(assembled.approximate_total))
    # Неизвестная прибыль — сигнал ВСЕГДА, а не только когда есть с чем сверять. У первого
    # снимка сверки нет вовсе, и раньше поломка отчёта была видна только в поле таблицы: на
    # экране первый месяц выглядел безупречно, а сверить второй оказалось бы нечем.
    if net_profit is None:
        flags.append(_profit_flag(month, profit_status))
    if reconciliation is not None and not reconciliation.explained:
        flags.append(_reconciliation_flag(reconciliation))

    for flag in [item for item in flags if item is not None]:
        session.add(
            BalanceSnapshotFlag(
                snapshot_id=snapshot.id,
                code=flag.code,
                severity=flag.severity,
                title=flag.title,
                explanation=flag.explanation,
                count=flag.count,
                amount=flag.amount,
                rows=flag.rows or None,
            )
        )

    await session.flush()
    return snapshot


def _profit_flag(month: date, status: str | None):
    """Прибыли за месяц нет — сверять приращение капитала будет нечем."""
    from app.services.balance.assembly import AssembledFlag

    return AssembledFlag(
        code="net_profit_unavailable",
        severity="blocking",
        title="Прибыль за месяц не посчиталась",
        explanation=(
            f"Отчёт о прибыли за {month:%m.%Y} не отдал чистую прибыль "
            f"({status or 'без причины'}). "
            "Снимок собран, но проверить его следующим месяцем не получится: приращение капитала "
            "не с чем сравнивать. Это не «сошлось» и не «не сошлось» — это отсутствие ответа."
        ),
        count=1,
    )


def _approximate_flag(total: Decimal):
    from app.services.balance.assembly import AssembledFlag

    return AssembledFlag(
        code="approximate_total",
        severity="info",
        title="Объём приблизительно датированных величин",
        explanation=(
            "Не ошибка и не оценка ошибки: это сумма величин, чья дата события восстановлена "
            "приблизительно. Допуск сверки считается из ИЗМЕНЕНИЯ этой суммы между снимками."
        ),
        amount=total,
    )


def _reconciliation_flag(reconciliation):
    from app.services.balance.assembly import AssembledFlag

    return AssembledFlag(
        code="reconciliation_failed",
        severity="blocking",
        title="Приращение капитала не объяснено прибылью",
        explanation=recon_service.explain(reconciliation),
        amount=abs(reconciliation.residual),
    )


async def _frozen_approximate(session: AsyncSession, snapshot_id: uuid.UUID) -> Decimal:
    value = await session.scalar(
        select(BalanceSnapshotFlag.amount).where(
            BalanceSnapshotFlag.snapshot_id == snapshot_id,
            BalanceSnapshotFlag.code == "approximate_total",
        )
    )
    return _money(value)


async def finalize(
    session: AsyncSession,
    *,
    as_of: date,
    actor_user_id: uuid.UUID | None = None,
    residual_reason: str | None = None,
) -> BalanceSnapshot:
    """Подписать снимок. После этого он не пересобирается.

    Необъяснённое расхождение финализировать МОЖНО, но только с письменной причиной: запретить
    значило бы остановить закрытие месяца из-за цифры, которую владелец уже понял и принял;
    промолчать — потерять единственное объяснение, которое через полгода никто не вспомнит.
    """
    snapshot = await session.scalar(select(BalanceSnapshot).where(BalanceSnapshot.as_of == as_of))
    if snapshot is None:
        raise BalanceSnapshotError(f"Баланса на {as_of:%d.%m.%Y} нет — сначала соберите черновик")
    if snapshot.status == "final":
        raise BalanceSnapshotError(f"Баланс на {as_of:%d.%m.%Y} уже финализирован")

    unresolved = (
        snapshot.recon_residual is not None
        and snapshot.tolerance_band is not None
        and abs(_money(snapshot.recon_residual)) > _money(snapshot.tolerance_band)
    )
    if unresolved and not (residual_reason or "").strip():
        raise BalanceSnapshotError(
            "Расхождение приращения капитала не объяснено — укажите причину, "
            "с которой вы его принимаете"
        )

    snapshot.status = "final"
    snapshot.finalized_at = datetime.now(UTC)
    snapshot.finalized_by_user_id = actor_user_id
    if residual_reason:
        snapshot.residual_reason = residual_reason.strip()
    await session.flush()
    return snapshot


async def drift(session: AsyncSession, *, as_of: date) -> list[LineDrift]:
    """Сравнить замороженный снимок с тем, что получается сейчас.

    Живой расчёт берёт основные средства ПЕРЕСЧЁТОМ, а не их снимком: иначе обе стороны
    читали бы одно и то же и расхождение было бы тождественным нулём.
    """
    snapshot = await session.scalar(select(BalanceSnapshot).where(BalanceSnapshot.as_of == as_of))
    if snapshot is None:
        return []

    frozen_rows = (
        await session.scalars(
            select(BalanceSnapshotLine).where(BalanceSnapshotLine.snapshot_id == snapshot.id)
        )
    ).all()
    frozen = {row.line_code: row for row in frozen_rows}

    live = await assemble(session, as_of=as_of, live=True)
    live_by_code = {line.code: line for line in live.lines}

    out: list[LineDrift] = []
    for code in sorted(set(frozen) | set(live_by_code)):
        was = frozen.get(code)
        now = live_by_code.get(code)
        was_amount = _money(was.amount) if was is not None and was.amount is not None else None
        now_amount = _money(now.amount) if now is not None and now.amount is not None else None
        if was_amount == now_amount:
            continue
        out.append(
            LineDrift(
                line_code=code,
                title=(was.title if was is not None else now.title if now else code),
                frozen_amount=was_amount,
                live_amount=now_amount,
            )
        )
    return out
