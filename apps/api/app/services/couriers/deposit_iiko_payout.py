"""Возврат депозита курьера в iiko — изъятие из «Главной кассы».

ТК Черникова (наш наличный счёт) = iiko «Главная касса» (один физический счёт). При
возврате депозита, помимо ДДС-списания (``deposit_service._book_deposit_return_cashflow``),
создаём параллельное изъятие в iiko по преднастроенному типу «Приложение/Возврат депозитов
курьерам» (chiefAccount=«Главная касса», account=«Возвраты депозитов курьеров», cfc=37 —
резолв по структуре, id типа различается dev/prod, имя iiko в payInOutTypes/list не отдаёт).

Вызывается ПОСЛЕ commit возврата (БД — источник истины). addPayOut необратим (нет delete):
ошибка iiko НЕ откатывает возврат, только логируется; задвоение исключено тем, что вызов
идёт ровно один раз на созданную RETURN-транзакцию. iiko-доступ — синхронные
http.client-хелперы из ``iiko_cashshift_sync`` (обход прокси/WAF), как в cheque_payout_push.
"""

from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal

from app.models import CourierDepositTransaction, CourierDepositTransactionType
from app.services.kassa.cheque_payout_push import _build_payout_type_index
from app.services.kassa.iiko_cashshift_sync import (
    _auth_token,
    _fetch_accounts_map,
    _iiko_get,
    _iiko_post,
)

logger = logging.getLogger(__name__)

# Подразделение проводки — «Foodmarket Тепло Черникова».
CHERNIKOVA_DEPARTMENT_ID = "d8d4a22e-3abd-4f02-b82d-7d4712f32729"
PAYOUT_TYPES_PATH = "/resto/api/v2/entities/payInOutTypes/list"
ADD_PAYOUT_PATH = "/resto/api/v2/payInOuts/addPayOut"
# Тип изъятия «Приложение/Возврат депозитов курьерам» по (chiefAccount, account, cfc.code).
RETURN_TYPE_KEY = ("Главная касса", "Возвраты депозитов курьеров", "37")


def post_deposit_return_to_iiko(transaction: CourierDepositTransaction) -> None:
    """Провести возврат депозита изъятием в iiko. Только RETURN; ошибку логирует, не поднимает."""
    tt = transaction.transaction_type
    tt_value = tt.value if isinstance(tt, CourierDepositTransactionType) else str(tt)
    if tt_value != CourierDepositTransactionType.RETURN.value:
        return
    try:
        token = _auth_token()
        accounts = _fetch_accounts_map(token)
        raw_types = _iiko_get(token, PAYOUT_TYPES_PATH, {"includeDeleted": "false"})
        index = _build_payout_type_index(accounts, raw_types if isinstance(raw_types, list) else [])
        type_id = index.get(RETURN_TYPE_KEY)
        if type_id is None:
            logger.warning(
                "Возврат депозита #%s: тип изъятия iiko %s не найден",
                transaction.id,
                RETURN_TYPE_KEY,
            )
            return
        amount = (Decimal(transaction.amount_cents) / Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        body = {
            "payOutTypeId": type_id,
            "payOutDate": transaction.transaction_date.isoformat(),
            "departmentSumMap": {CHERNIKOVA_DEPARTMENT_ID: float(amount)},
            "comment": f"Возврат депозита курьеру (операция #{transaction.id})",
        }
        data = _iiko_post(token, ADD_PAYOUT_PATH, body)
        result = data.get("result") if isinstance(data, dict) else None
        if result != "SUCCESS":
            logger.warning(
                "Возврат депозита #%s: addPayOut вернул не SUCCESS: %s", transaction.id, data
            )
    except Exception as exc:  # noqa: BLE001 — iiko не должен валить уже проведённый возврат
        logger.warning(
            "Возврат депозита #%s: изъятие в iiko не проведено: %s", transaction.id, exc
        )
