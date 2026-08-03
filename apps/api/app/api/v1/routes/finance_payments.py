"""API единой витрины «Финансы → Платежи» (FEAT-003).

Только чтение: агрегатор исходящих платежей платёжного контура из
``app.services.payments_aggregator``. Питает и страницу истории «Платежи», и
активную модалку плавающей FAB-кнопки. Мутации (отправка в банк, оплата резерва,
разбор счёта) остаются на своих специализированных эндпоинтах — здесь только
сводный список со статусами и раскладкой по корзинам.

Права: чтение — ``counterparties.read`` (тот же контур, что «Страница на оплату»).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_any_permission
from app.db.session import get_session
from app.services import payments_aggregator as agg

router = APIRouter()

# Страница «Финансы → Платежи» в навигации открыта обоим правам — бэкенд обязан принимать
# те же, иначе finance-роль видит вечное фейковое «Платежей нет» при тихом 403.
READ = (Depends(require_any_permission(("counterparties.read", "finance.counterparties.read"))),)


class PaymentRead(BaseModel):
    id: str
    source: str
    kind: str
    ref_id: uuid.UUID
    title: str
    counterparty_id: uuid.UUID | None
    counterparty_name: str | None
    amount: float
    amount_paid: float | None
    article_id: uuid.UUID | None
    article_name: str | None
    method: str
    bank_channel: str | None
    state: str
    state_label: str
    bucket: str | None
    bucket_label: str | None
    created_at: datetime
    can_edit: bool
    can_send_to_bank: bool
    can_pay: bool
    can_cancel: bool
    extra: dict[str, Any]


class BucketMeta(BaseModel):
    key: str
    label: str
    count: int


class PaymentsResponse(BaseModel):
    scope: str
    buckets: list[BucketMeta]
    items: list[PaymentRead]


def _to_read(item: agg.PaymentItem) -> PaymentRead:
    return PaymentRead(
        id=item.id,
        source=item.source,
        kind=item.kind,
        ref_id=item.ref_id,
        title=item.title,
        counterparty_id=item.counterparty_id,
        counterparty_name=item.counterparty_name,
        amount=float(item.amount),
        amount_paid=float(item.amount_paid) if item.amount_paid is not None else None,
        article_id=item.article_id,
        article_name=item.article_name,
        method=item.method,
        bank_channel=item.bank_channel,
        state=item.state,
        state_label=item.state_label,
        bucket=item.bucket,
        bucket_label=item.bucket_label,
        created_at=item.created_at,
        can_edit=item.can_edit,
        can_send_to_bank=item.can_send_to_bank,
        can_pay=item.can_pay,
        can_cancel=item.can_cancel,
        extra=item.extra,
    )


@router.get("", response_model=PaymentsResponse, dependencies=READ)
async def list_payments(
    session: Annotated[AsyncSession, Depends(get_session)],
    scope: str = Query(default="active", pattern="^(active|all)$"),
) -> PaymentsResponse:
    items = await agg.list_payments(session, scope=scope)
    counts = agg.bucket_counts(items)
    buckets = [
        BucketMeta(key=key, label=agg.BUCKET_LABELS[key], count=counts.get(key, 0))
        for key in agg.BUCKET_ORDER
    ]
    return PaymentsResponse(scope=scope, buckets=buckets, items=[_to_read(i) for i in items])
