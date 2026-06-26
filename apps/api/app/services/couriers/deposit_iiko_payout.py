"""Денежные операции курьерского депозита в iiko — синхронизация «Главной кассы».

ТК Черникова (наш наличный счёт) = iiko «Главная касса» (один физический счёт), поэтому
ДДС-движение депозита дублируется параллельной проводкой iiko, чтобы остатки не разъехались:

* ВОЗВРАТ (RETURN) — изъятие (addPayOut) по типу «Приложение/Возврат депозитов курьерам»
  (chiefAccount=«Главная касса», account=«Возвраты депозитов курьеров», cfc=37). Касса ↓.
* ПОПОЛНЕНИЕ (TOP_UP) — внесение по PAYIN-типу «Приложение/Пополнение депозитов курьерам»
  (chiefAccount=«Главная касса», account=«Пополнение депозитов курьерам», cfc=40). Касса ↑ —
  синхронно приходу в ДДС (``deposit_service._book_deposit_topup_cashflow``).

В этом iiko Server API нет отдельного ``addPayIn`` (404) — единственный метод записи
payInOut'ов это ``addPayOut`` (405 на GET). Направление берётся из самого типа, поэтому
внесение проводим тем же ``addPayOut``, передавая в ``payOutTypeId`` id PAYIN-типа.

Резолв типа — по структуре (имена счетов + код статьи), id типа различается dev/prod, имя
iiko в payInOutTypes/list не отдаёт. Вызываются ПОСЛЕ commit операции (БД — источник истины).
Проводки iiko необратимы (нет delete): ошибка iiko НЕ откатывает операцию, только логируется;
задвоение исключено тем, что вызов идёт ровно один раз на созданную транзакцию. iiko-доступ —
синхронные http.client-хелперы из ``iiko_cashshift_sync`` (обход прокси/WAF).
"""

from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

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
# Единственный метод записи payInOut'ов — addPayOut (отдельного addPayIn в этом API нет);
# направление берётся из самого типа, поэтому им же проводим и внесение (PAYIN-тип).
ADD_PAYOUT_PATH = "/resto/api/v2/payInOuts/addPayOut"
# Тип изъятия «Приложение/Возврат депозитов курьерам» по (chiefAccount, account, cfc.code).
RETURN_TYPE_KEY = ("Главная касса", "Возвраты депозитов курьеров", "37")
# Тип внесения «Приложение/Пополнение депозитов курьерам» (PAYIN) по той же структуре.
TOPUP_TYPE_KEY = ("Главная касса", "Пополнение депозитов курьерам", "40")


def _build_payin_type_index(
    accounts: dict[str, str], types: list[dict[str, Any]]
) -> dict[tuple[str, str, str], str]:
    """Индекс (chiefAccount-имя, account-имя, cfc.code) → payInTypeId по PAYIN-типам.

    Зеркало ``_build_payout_type_index`` для внесений (addPayOut/индекс PAYOUT их отсекает).
    """
    index: dict[tuple[str, str, str], str] = {}
    for entry in types:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("transactionType") or "").upper() != "PAYIN":
            continue
        chief = accounts.get(str(entry.get("chiefAccount") or ""))
        account = accounts.get(str(entry.get("account") or ""))
        cfc = entry.get("cashFlowCategory")
        code = str(cfc.get("code")) if isinstance(cfc, dict) and cfc.get("code") is not None else ""
        type_id = entry.get("id")
        if chief and account and code and type_id:
            index[(chief, account, code)] = str(type_id)
    return index


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


def post_deposit_topup_to_iiko(transaction: CourierDepositTransaction) -> None:
    """Провести пополнение депозита внесением (PAYIN) в iiko «Главную кассу».

    ВНИМАНИЕ: НЕ вызывается. Этот iiko Server Resto API не умеет внесение: addPayIn=404,
    addPayOut отвергает PAYIN-тип (409 «Expected PAYOUT transaction type, but was: PAYIN»,
    проверено 2026-06-26 на типе «Пополнение депозитов курьерам»). Функция оставлена как
    основа на случай, если найдётся рабочий метод внесения (iikoTransport/Cloud/иной).
    Сейчас рост Главной кассы по пополнениям проводится вручную в бэк-офисе iiko.

    Только TOP_UP; ошибку логирует, не поднимает (операция депозита уже зафиксирована).
    """
    tt = transaction.transaction_type
    tt_value = tt.value if isinstance(tt, CourierDepositTransactionType) else str(tt)
    if tt_value != CourierDepositTransactionType.TOP_UP.value:
        return
    try:
        token = _auth_token()
        accounts = _fetch_accounts_map(token)
        raw_types = _iiko_get(token, PAYOUT_TYPES_PATH, {"includeDeleted": "false"})
        index = _build_payin_type_index(accounts, raw_types if isinstance(raw_types, list) else [])
        type_id = index.get(TOPUP_TYPE_KEY)
        if type_id is None:
            logger.warning(
                "Пополнение депозита #%s: тип внесения iiko %s не найден",
                transaction.id,
                TOPUP_TYPE_KEY,
            )
            return
        amount = (Decimal(transaction.amount_cents) / Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        body = {
            "payOutTypeId": type_id,
            "payOutDate": transaction.transaction_date.isoformat(),
            "departmentSumMap": {CHERNIKOVA_DEPARTMENT_ID: float(amount)},
            "comment": f"Пополнение депозита курьера (операция #{transaction.id})",
        }
        data = _iiko_post(token, ADD_PAYOUT_PATH, body)
        result = data.get("result") if isinstance(data, dict) else None
        if result != "SUCCESS":
            logger.warning(
                "Пополнение депозита #%s: addPayOut(PAYIN) вернул не SUCCESS: %s",
                transaction.id,
                data,
            )
    except Exception as exc:  # noqa: BLE001 — iiko не должен валить уже проведённое пополнение
        logger.warning(
            "Пополнение депозита #%s: внесение в iiko не проведено: %s", transaction.id, exc
        )
