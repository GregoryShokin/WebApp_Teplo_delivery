"""Модуль «Баланс»: черновик, заморозка, подпись и детектор дрейфа.

ПОЧЕМУ ПРЕДПРОСМОТР И СНИМОК — РАЗНЫЕ РУЧКИ. Предпросмотр отвечает «сколько получается прямо
сейчас» и ничего не пишет: на нём владелец видит, что подтянулось, и вносит недостающее.
Снимок отвечает «сколько мы сказали на эту дату» и не меняется от того, что первичку поправили
позже. Смешать их в одну ручку значило бы потерять границу между рабочей поверхностью и
подписанной цифрой.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, get_current_actor, require_permission
from app.db.session import get_session
from app.models import (
    BalanceManualValue,
    BalanceSnapshot,
    BalanceSnapshotFlag,
    BalanceSnapshotLine,
)
from app.services.balance import catalog as cat
from app.services.balance import snapshot as snapshot_service
from app.services.balance.assembly import BalanceBeforeAccountingStart, assemble

router = APIRouter()

READ = [Depends(require_permission("finance.balance.read"))]
EDIT = [Depends(require_permission("finance.balance.edit"))]


class BalanceLineRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    line_code: str
    title: str
    block: str
    group_code: str
    sort_order: int
    sign: int
    kind: str
    amount: Decimal | None
    status: str
    source_kind: str | None = None
    source_note: str | None = None
    detail: dict[str, Any] | None = None


class BalanceFlagRow(BaseModel):
    code: str
    severity: str
    title: str
    explanation: str
    count: int
    amount: Decimal
    rows: list[dict[str, Any]] | None = None


class ReconciliationRow(BaseModel):
    """Сверка приращения капитала — единственная непустая проверка в модуле."""

    delta_equity: Decimal
    expected: Decimal
    residual: Decimal
    tolerance: Decimal
    explained: bool
    net_profit: Decimal | None
    net_profit_status: str | None
    dividends: Decimal
    contributions: Decimal
    returns: Decimal


class BalanceView(BaseModel):
    as_of: date
    period_month: date
    status: str
    method: str
    asset_total: Decimal
    liability_total: Decimal
    equity_plug: Decimal
    lines: list[BalanceLineRow]
    flags: list[BalanceFlagRow]
    reconciliation: ReconciliationRow | None = None
    residual_reason: str | None = None
    finalized_at: datetime | None = None
    note: str | None = None


class FreezeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: date
    note: str | None = None


class FinalizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    residual_reason: str | None = None


class ManualValueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal
    # Обязательно и непусто: цифра без основания в балансе неотличима от опечатки.
    reason: str = Field(min_length=3, max_length=2000)
    source_document: str | None = Field(default=None, max_length=2000)


class DriftRow(BaseModel):
    line_code: str
    title: str
    frozen_amount: Decimal | None
    live_amount: Decimal | None
    delta: Decimal


def _reconciliation(row: BalanceSnapshot) -> ReconciliationRow | None:
    if row.recon_delta_equity is None:
        return None
    residual = Decimal(str(row.recon_residual or 0))
    tolerance = Decimal(str(row.tolerance_band or 0))
    return ReconciliationRow(
        delta_equity=Decimal(str(row.recon_delta_equity)),
        expected=Decimal(str(row.recon_expected or 0)),
        residual=residual,
        tolerance=tolerance,
        explained=row.net_profit is not None and abs(residual) <= tolerance,
        net_profit=Decimal(str(row.net_profit)) if row.net_profit is not None else None,
        net_profit_status=row.net_profit_status,
        dividends=Decimal(str(row.equity_dividends or 0)),
        contributions=Decimal(str(row.equity_contributions or 0)),
        returns=Decimal(str(row.equity_returns or 0)),
    )


async def _load(session: AsyncSession, as_of: date) -> BalanceView | None:
    row = await session.scalar(select(BalanceSnapshot).where(BalanceSnapshot.as_of == as_of))
    if row is None:
        return None
    lines = (
        await session.scalars(
            select(BalanceSnapshotLine)
            .where(BalanceSnapshotLine.snapshot_id == row.id)
            .order_by(BalanceSnapshotLine.sort_order)
        )
    ).all()
    flags = (
        await session.scalars(
            select(BalanceSnapshotFlag).where(BalanceSnapshotFlag.snapshot_id == row.id)
        )
    ).all()
    return BalanceView(
        as_of=row.as_of,
        period_month=row.period_month,
        status=row.status,
        method=row.method,
        asset_total=Decimal(str(row.asset_total)),
        liability_total=Decimal(str(row.liability_total)),
        equity_plug=Decimal(str(row.equity_plug)),
        lines=[BalanceLineRow.model_validate(line) for line in lines],
        flags=[
            BalanceFlagRow(
                code=flag.code,
                severity=flag.severity,
                title=flag.title,
                explanation=flag.explanation,
                count=flag.count,
                amount=Decimal(str(flag.amount)),
                rows=flag.rows,
            )
            for flag in flags
        ],
        reconciliation=_reconciliation(row),
        residual_reason=row.residual_reason,
        finalized_at=row.finalized_at,
        note=row.note,
    )


@router.get("/preview", response_model=BalanceView, dependencies=READ)
async def preview_balance(
    session: Annotated[AsyncSession, Depends(get_session)],
    as_of: date,
) -> BalanceView:
    """Сколько получается на дату ПРЯМО СЕЙЧАС. Ничего не пишет.

    Это рабочая поверхность: здесь видно, что подтянулось из регистров, чего не хватает и что
    ждёт ручного ввода. Подписанной цифрой предпросмотр не является — ею становится снимок.
    """
    try:
        assembled = await assemble(session, as_of=as_of)
    except BalanceBeforeAccountingStart as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error

    return BalanceView(
        as_of=assembled.as_of,
        period_month=assembled.as_of.replace(day=1),
        status="preview",
        method="inventory",
        asset_total=assembled.asset_total,
        liability_total=assembled.liability_total,
        equity_plug=assembled.equity_plug,
        lines=[
            BalanceLineRow(
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
            for line in assembled.lines
        ],
        flags=[
            BalanceFlagRow(
                code=flag.code,
                severity=flag.severity,
                title=flag.title,
                explanation=flag.explanation,
                count=flag.count,
                amount=flag.amount,
                rows=flag.rows or None,
            )
            for flag in assembled.flags
        ],
    )


@router.get("/snapshots", response_model=list[dict], dependencies=READ)
async def list_snapshots(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=60)] = 24,
) -> list[dict]:
    rows = (
        await session.scalars(
            select(BalanceSnapshot).order_by(BalanceSnapshot.as_of.desc()).limit(limit)
        )
    ).all()
    return [
        {
            "as_of": row.as_of,
            "status": row.status,
            "method": row.method,
            "asset_total": Decimal(str(row.asset_total)),
            "liability_total": Decimal(str(row.liability_total)),
            "equity_plug": Decimal(str(row.equity_plug)),
            "recon_residual": (
                Decimal(str(row.recon_residual)) if row.recon_residual is not None else None
            ),
            "finalized_at": row.finalized_at,
        }
        for row in rows
    ]


@router.get("/snapshots/{as_of}", response_model=BalanceView, dependencies=READ)
async def get_snapshot(
    as_of: date,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> BalanceView:
    view = await _load(session, as_of)
    if view is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Баланса на {as_of:%d.%m.%Y} нет — соберите черновик",
        )
    return view


@router.post("/snapshots", response_model=BalanceView, dependencies=EDIT)
async def freeze_snapshot(
    payload: FreezeRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> BalanceView:
    """Собрать баланс на дату и заморозить черновиком.

    Повторный вызов пересобирает черновик — данные могли доехать. Финальный снимок не трогает.
    """
    try:
        await snapshot_service.freeze(
            session, as_of=payload.as_of, actor_user_id=actor.user_id, note=payload.note
        )
    except BalanceBeforeAccountingStart as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error
    except snapshot_service.BalanceSnapshotError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    await session.commit()
    view = await _load(session, payload.as_of)
    assert view is not None
    return view


@router.post("/snapshots/{as_of}/finalize", response_model=BalanceView, dependencies=EDIT)
async def finalize_snapshot(
    as_of: date,
    payload: FinalizeRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> BalanceView:
    """Подписать снимок. После этого он не пересобирается."""
    try:
        await snapshot_service.finalize(
            session,
            as_of=as_of,
            actor_user_id=actor.user_id,
            residual_reason=payload.residual_reason,
        )
    except snapshot_service.BalanceSnapshotError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    await session.commit()
    view = await _load(session, as_of)
    assert view is not None
    return view


@router.get("/snapshots/{as_of}/drift", response_model=list[DriftRow], dependencies=READ)
async def get_drift(
    as_of: date,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[DriftRow]:
    """Разошёлся ли подписанный снимок с тем, что получается сейчас.

    Пустой список означает, что перенесённые в отчётность числа всё ещё верны.
    """
    rows = await snapshot_service.drift(session, as_of=as_of)
    return [
        DriftRow(
            line_code=row.line_code,
            title=row.title,
            frozen_amount=row.frozen_amount,
            live_amount=row.live_amount,
            delta=row.delta,
        )
        for row in rows
    ]


@router.put("/manual/{as_of}/{line_code}", response_model=dict, dependencies=EDIT)
async def set_manual_value(
    as_of: date,
    line_code: str,
    payload: ManualValueRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    """Внести величину строки, у которой нет регистра.

    Только для строк, объявленных ручными: подменить руками цифру, которую считает регистр,
    нельзя — иначе расхождение между балансом и регистром станет невидимым.
    """
    if line_code not in cat.MANUAL_CODES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Строка «{line_code}» считается из регистра — ручное значение для неё "
                f"не принимается"
            ),
        )
    existing = await session.scalar(
        select(BalanceManualValue).where(
            BalanceManualValue.as_of == as_of, BalanceManualValue.line_code == line_code
        )
    )
    if existing is None:
        existing = BalanceManualValue(as_of=as_of, line_code=line_code)
        session.add(existing)
    existing.amount = payload.amount
    existing.reason = payload.reason.strip()
    existing.source_document = payload.source_document
    existing.author_user_id = actor.user_id
    await session.commit()
    return {"as_of": as_of, "line_code": line_code, "amount": existing.amount}


@router.get("/catalog", response_model=list[dict], dependencies=READ)
async def get_catalog() -> list[dict]:
    """Справочник строк: что вообще бывает в балансе и чем закрывается.

    Нужен, чтобы пустая строка читалась как осознанная, а не как забытая.
    """
    return [
        {
            "code": line.code,
            "title": line.title,
            "block": line.block,
            "group": line.group,
            "sort_order": line.sort_order,
            "fill": line.fill,
            "note": line.note,
        }
        for line in cat.CATALOG
    ]


__all__ = ["router"]
