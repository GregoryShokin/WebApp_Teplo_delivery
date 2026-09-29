from __future__ import annotations

from datetime import UTC, date
from typing import Any

from sqlalchemy import Date, cast, func
from sqlalchemy.sql.elements import ColumnElement

from app.models import DepositTransaction
from app.services.clock import MOSCOW_TZ


def effective_deposit_date(transaction: Any) -> date | None:
    """Хозяйственный день; старые записи без него относим к дню регистрации по Москве."""
    happened_on = getattr(transaction, "happened_on", None)
    if happened_on is not None:
        return happened_on
    created_at = getattr(transaction, "created_at", None)
    if created_at is None:
        return None
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return created_at.astimezone(MOSCOW_TZ).date()


def effective_deposit_date_expression() -> ColumnElement[date]:
    """SQL-эквивалент для отбора и сортировки до сериализации."""
    return func.coalesce(
        DepositTransaction.happened_on,
        cast(func.timezone("Europe/Moscow", DepositTransaction.created_at), Date),
    )
