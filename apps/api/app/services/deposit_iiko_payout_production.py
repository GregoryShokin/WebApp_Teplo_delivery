"""Выдача производственного депозита в iiko — изъятие из «Главной кассы».

ТК Черникова (наш наличный счёт) = iiko «Главная касса». При немедленной выдаче
производственного депозита с ТК Черникова, помимо ДДС-списания
(``deposit_service.book_production_deposit_payout_cashflow``), создаём параллельное
изъятие в iiko по преднастроенному типу «Приложение/Выдача депозита сотруднику»
(chiefAccount=«Главная касса», account=«Депозиты сотрудников», cfc=38 — резолв по
структуре, id различается dev/prod). Для выдачи с Сейфа iiko НЕ вызывается (другой счёт).

Вызывается ПОСЛЕ commit (БД — источник истины). addPayOut необратим: ошибка iiko НЕ
откатывает выдачу. Судьба проводки живёт в журнале ``IikoCashPayout`` (pending-first,
кейс owner-review при неуспехе) — раньше сбой терял изъятие молча. См.
``iiko_cash_payout_log``. Образец — ``couriers.deposit_iiko_payout``.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.iiko_cash_payout_log import PayoutRejected, post_cash_payout
from app.services.iiko_location import get_department_id
from app.services.kassa.cheque_payout_push import _build_payout_type_index
from app.services.kassa.iiko_cashshift_sync import (
    _auth_token,
    _fetch_accounts_map,
    _iiko_get,
    _iiko_post,
)

PAYOUT_TYPES_PATH = "/resto/api/v2/entities/payInOutTypes/list"
ADD_PAYOUT_PATH = "/resto/api/v2/payInOuts/addPayOut"
# Тип изъятия «Приложение/Выдача депозита сотруднику» по (chiefAccount, account, cfc.code).
PRODUCTION_PAYOUT_TYPE_KEY = ("Главная касса", "Депозиты сотрудников", "38")


def send_production_deposit_payout(
    *, amount: Decimal, payout_date: date, source_id: uuid.UUID
) -> str:
    """Синхронная отправка изъятия (исполняется в треде). Возвращает id типа проводки.

    ``LookupError`` — тип не найден (до отправки, проводки в iiko точно нет);
    :class:`PayoutRejected` — iiko ответила не ``SUCCESS``. Прочие исключения (сеть/таймаут)
    оставляют исход неизвестным — их разбирает журнал."""
    token = _auth_token()
    accounts = _fetch_accounts_map(token)
    raw_types = _iiko_get(token, PAYOUT_TYPES_PATH, {"includeDeleted": "false"})
    index = _build_payout_type_index(accounts, raw_types if isinstance(raw_types, list) else [])
    type_id = index.get(PRODUCTION_PAYOUT_TYPE_KEY)
    if type_id is None:
        raise LookupError(f"тип изъятия iiko {PRODUCTION_PAYOUT_TYPE_KEY} не найден")
    value = Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    data = _iiko_post(
        token,
        ADD_PAYOUT_PATH,
        {
            "payOutTypeId": type_id,
            "payOutDate": payout_date.isoformat(),
            "departmentSumMap": {get_department_id(): float(value)},
            "comment": f"Выдача депозита сотруднику (операция {source_id})",
        },
    )
    result = data.get("result") if isinstance(data, dict) else None
    if result != "SUCCESS":
        raise PayoutRejected(f"addPayOut вернул не SUCCESS: {data}")
    return type_id


async def post_production_deposit_payout_to_iiko(
    session: AsyncSession, *, amount: Decimal, payout_date: date, source_id: uuid.UUID
) -> None:
    """Провести выдачу производственного депозита изъятием в iiko под журналом."""
    await post_cash_payout(
        session,
        kind="deposit_production",
        source_id=source_id,
        amount=amount,
        payout_date=payout_date,
        send=lambda: send_production_deposit_payout(
            amount=amount, payout_date=payout_date, source_id=source_id
        ),
    )
