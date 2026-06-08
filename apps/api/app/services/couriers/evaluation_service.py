from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AppSetting,
    CourierEvaluation,
    CourierEvaluationCriterion,
    CourierEvaluationSource,
)
from app.schemas.couriers import EvaluationMonthlySummary
from app.services.couriers.common import get_courier_or_404
from app.services.couriers.permissions import ensure_admin_cashier

EDIT_WINDOW_SETTING_KEY = "couriers.evaluation.edit_window_minutes"
DEFAULT_EDIT_WINDOW_MINUTES = 30
_UNSET = object()


@dataclass(frozen=True)
class EvaluationFilters:
    courier_id: uuid.UUID | None = None
    author_id: uuid.UUID | None = None
    date_from: date | None = None
    date_to: date | None = None
    criterion_id: int | None = None


async def create_evaluation(
    session: AsyncSession,
    courier_id: uuid.UUID,
    criterion_id: int,
    evaluated_at: date | None,
    comment: str | None,
    actor_id: uuid.UUID,
    source: CourierEvaluationSource | str = CourierEvaluationSource.WEB,
) -> CourierEvaluation:
    await ensure_admin_cashier(session, actor_id)
    await get_courier_or_404(session, courier_id)
    episode_date = evaluated_at or date.today()
    _validate_evaluated_at(episode_date)
    criterion = await _get_active_criterion_or_404(session, criterion_id)
    evaluation = CourierEvaluation(
        courier_employee_id=courier_id,
        criterion_id=criterion.id,
        score_snapshot=criterion.score,
        comment=comment,
        evaluated_at=episode_date,
        source=_source_value(source),
        created_by=actor_id,
    )
    session.add(evaluation)
    await session.flush()
    return evaluation


async def update_evaluation(
    session: AsyncSession,
    evaluation_id: int,
    actor_id: uuid.UUID,
    *,
    criterion_id: int | None = None,
    evaluated_at: date | None = None,
    comment: str | None | object = _UNSET,
    now: datetime | None = None,
) -> CourierEvaluation:
    evaluation = await _get_evaluation_or_404(session, evaluation_id)
    await _ensure_can_modify(session, evaluation, actor_id, now=now)
    if criterion_id is not None:
        criterion = await _get_active_criterion_or_404(session, criterion_id)
        evaluation.criterion_id = criterion.id
        evaluation.score_snapshot = criterion.score
    if evaluated_at is not None:
        _validate_evaluated_at(evaluated_at)
        evaluation.evaluated_at = evaluated_at
    if comment is not _UNSET:
        evaluation.comment = comment
    evaluation.updated_at = now or datetime.now(UTC)
    await session.flush()
    return evaluation


async def delete_evaluation(
    session: AsyncSession,
    evaluation_id: int,
    actor_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> CourierEvaluation:
    evaluation = await _get_evaluation_or_404(session, evaluation_id)
    resolved_now = now or datetime.now(UTC)
    await _ensure_can_modify(session, evaluation, actor_id, now=resolved_now)
    evaluation.deleted_at = resolved_now
    evaluation.updated_at = resolved_now
    await session.flush()
    return evaluation


async def list_evaluations(
    session: AsyncSession,
    filters: EvaluationFilters | None = None,
) -> list[CourierEvaluation]:
    resolved = filters or EvaluationFilters()
    stmt = select(CourierEvaluation).where(CourierEvaluation.deleted_at.is_(None))
    if resolved.courier_id is not None:
        stmt = stmt.where(CourierEvaluation.courier_employee_id == resolved.courier_id)
    if resolved.author_id is not None:
        stmt = stmt.where(CourierEvaluation.created_by == resolved.author_id)
    if resolved.date_from is not None:
        stmt = stmt.where(CourierEvaluation.evaluated_at >= resolved.date_from)
    if resolved.date_to is not None:
        stmt = stmt.where(CourierEvaluation.evaluated_at <= resolved.date_to)
    if resolved.criterion_id is not None:
        stmt = stmt.where(CourierEvaluation.criterion_id == resolved.criterion_id)
    result = await session.scalars(
        stmt.order_by(CourierEvaluation.evaluated_at.desc(), CourierEvaluation.created_at.desc())
    )
    return list(result.all())


async def monthly_aggregate(
    session: AsyncSession,
    courier_id: uuid.UUID,
    month: date,
) -> dict[str, Any]:
    summary = await get_evaluations_monthly_summary(session, courier_id, month)
    return {
        "courier_employee_id": courier_id,
        "month": month.replace(day=1).strftime("%Y-%m"),
        **summary.model_dump(),
    }


async def get_evaluations_monthly_summary(
    session: AsyncSession,
    courier_id: uuid.UUID,
    month: date,
) -> EvaluationMonthlySummary:
    """score_sum, counts by sign and top-3 criteria for one courier/month."""

    month_start = month.replace(day=1)
    month_end = _next_month(month_start)
    result = await session.execute(
        select(CourierEvaluation, CourierEvaluationCriterion)
        .join(
            CourierEvaluationCriterion,
            CourierEvaluationCriterion.id == CourierEvaluation.criterion_id,
        )
        .where(
            CourierEvaluation.courier_employee_id == courier_id,
            CourierEvaluation.deleted_at.is_(None),
            CourierEvaluation.evaluated_at >= month_start,
            CourierEvaluation.evaluated_at < month_end,
        )
    )
    rows = result.all()
    score_sum = sum(evaluation.score_snapshot for evaluation, _criterion in rows)
    criterion_counts = Counter(criterion.id for _evaluation, criterion in rows)
    criterion_by_id = {criterion.id: criterion for _evaluation, criterion in rows}
    top_criteria = [
        {
            "criterion_id": criterion_id,
            "code": criterion_by_id[criterion_id].code,
            "label": criterion_by_id[criterion_id].label,
            "count": count,
        }
        for criterion_id, count in criterion_counts.most_common(3)
    ]
    positive_count = sum(1 for evaluation, _criterion in rows if evaluation.score_snapshot > 0)
    negative_count = sum(1 for evaluation, _criterion in rows if evaluation.score_snapshot < 0)
    neutral_count = sum(1 for evaluation, _criterion in rows if evaluation.score_snapshot == 0)
    return EvaluationMonthlySummary(
        score_sum=score_sum,
        positive_count=positive_count,
        negative_count=negative_count,
        neutral_count=neutral_count,
        top_criteria=top_criteria,
    )


async def get_edit_window_minutes(session: AsyncSession) -> int:
    setting = await session.scalar(
        select(AppSetting).where(AppSetting.key == EDIT_WINDOW_SETTING_KEY)
    )
    if setting is None:
        return DEFAULT_EDIT_WINDOW_MINUTES
    value = setting.value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return DEFAULT_EDIT_WINDOW_MINUTES


def evaluation_payload(evaluation: CourierEvaluation) -> dict[str, Any]:
    return {
        "id": evaluation.id,
        "courier_employee_id": evaluation.courier_employee_id,
        "criterion_id": evaluation.criterion_id,
        "score_snapshot": evaluation.score_snapshot,
        "comment": evaluation.comment,
        "evaluated_at": evaluation.evaluated_at,
        "source": _enum_value(evaluation.source),
        "created_by": evaluation.created_by,
        "created_at": evaluation.created_at,
        "updated_at": evaluation.updated_at,
        "deleted_at": evaluation.deleted_at,
    }


async def _ensure_can_modify(
    session: AsyncSession,
    evaluation: CourierEvaluation,
    actor_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> None:
    if evaluation.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Оценка не найдена",
        )
    if evaluation.created_by != actor_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Изменять оценку может только автор",
        )
    resolved_now = now or datetime.now(UTC)
    created_at = _as_aware_utc(evaluation.created_at)
    edit_window = await get_edit_window_minutes(session)
    if resolved_now > created_at + timedelta(minutes=edit_window):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Окно редактирования оценки истекло",
        )


async def _get_evaluation_or_404(session: AsyncSession, evaluation_id: int) -> CourierEvaluation:
    evaluation = await session.get(CourierEvaluation, evaluation_id)
    if evaluation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Оценка не найдена")
    return evaluation


async def _get_active_criterion_or_404(
    session: AsyncSession,
    criterion_id: int,
) -> CourierEvaluationCriterion:
    criterion = await session.get(CourierEvaluationCriterion, criterion_id)
    if criterion is None or not criterion.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Критерий не найден")
    return criterion


def _validate_evaluated_at(value: date) -> None:
    today = date.today()
    if value > today or value < today - timedelta(days=1):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Дата оценки может быть только сегодня или вчера",
        )


def _source_value(source: CourierEvaluationSource | str) -> CourierEvaluationSource:
    if isinstance(source, CourierEvaluationSource):
        return source
    return CourierEvaluationSource(source)


def _enum_value(value: Any) -> str | None:
    if value is None:
        return None
    return getattr(value, "value", value)


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _next_month(month_start: date) -> date:
    if month_start.month == 12:
        return date(month_start.year + 1, 1, 1)
    return date(month_start.year, month_start.month + 1, 1)
