"""Выдача производственного депозита в iiko — изъятие из «Главной кассы».

ТК Черникова (наш наличный счёт) = iiko «Главная касса». При немедленной выдаче
производственного депозита с ТК Черникова, помимо ДДС-списания
(``deposit_service.book_production_deposit_payout_cashflow``), создаём параллельное
изъятие в iiko по преднастроенному типу «Приложение/Выдача депозита сотруднику»
(chiefAccount=«Главная касса», account=«Депозиты сотрудников», cfc=38 — резолв по
структуре, id различается dev/prod). Для выдачи с Сейфа iiko НЕ вызывается (другой счёт).

Вызывается ПОСЛЕ commit (БД — источник истины). addPayOut необратим: ошибка iiko НЕ
откатывает выдачу, только логируется; задвоение исключено вызовом ровно один раз на
созданную выдачу. Образец — ``couriers.deposit_iiko_payout``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.services.kassa.cheque_payout_push import _build_payout_type_index
from app.services.kassa.iiko_cashshift_sync import (
    _auth_token,
    _fetch_accounts_map,
    _iiko_get,
    _iiko_post,
)

logger = logging.getLogger(__name__)

CHERNIKOVA_DEPARTMENT_ID = "d8d4a22e-3abd-4f02-b82d-7d4712f32729"
PAYOUT_TYPES_PATH = "/resto/api/v2/entities/payInOutTypes/list"
ADD_PAYOUT_PATH = "/resto/api/v2/payInOuts/addPayOut"
# Тип изъятия «Приложение/Выдача депозита сотруднику» по (chiefAccount, account, cfc.code).
PRODUCTION_PAYOUT_TYPE_KEY = ("Главная касса", "Депозиты сотрудников", "38")


def post_production_deposit_payout_to_iiko(
    *, amount: Decimal, payout_date: date, source_id: uuid.UUID
) -> None:
    """Провести выдачу производственного депозита изъятием в iiko. Ошибку логирует, не поднимает."""
    try:
        token = _auth_token()
        accounts = _fetch_accounts_map(token)
        raw_types = _iiko_get(token, PAYOUT_TYPES_PATH, {"includeDeleted": "false"})
        index = _build_payout_type_index(accounts, raw_types if isinstance(raw_types, list) else [])
        type_id = index.get(PRODUCTION_PAYOUT_TYPE_KEY)
        if type_id is None:
            logger.warning(
                "Выдача депозита %s: тип изъятия iiko %s не найден",
                source_id,
                PRODUCTION_PAYOUT_TYPE_KEY,
            )
            return
        value = Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        body = {
            "payOutTypeId": type_id,
            "payOutDate": payout_date.isoformat(),
            "departmentSumMap": {CHERNIKOVA_DEPARTMENT_ID: float(value)},
            "comment": f"Выдача депозита сотруднику (операция {source_id})",
        }
        data = _iiko_post(token, ADD_PAYOUT_PATH, body)
        result = data.get("result") if isinstance(data, dict) else None
        if result != "SUCCESS":
            logger.warning(
                "Выдача депозита %s: addPayOut вернул не SUCCESS: %s", source_id, data
            )
    except Exception as exc:  # noqa: BLE001 — iiko не должен валить уже проведённую выдачу
        logger.warning("Выдача депозита %s: изъятие в iiko не проведено: %s", source_id, exc)
