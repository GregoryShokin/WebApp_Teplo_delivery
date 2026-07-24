"""Выдача аванса/займа сотруднику в iiko — изъятие из «Главной кассы».

ТК Черникова (наш наличный счёт) = iiko «Главная касса». При выдаче наличного
аванса/займа с ТК Черникова, помимо ДДС-списания
(``payroll_advance_service.book_advance_payout_cashflow``), создаём параллельное
изъятие в iiko по преднастроенному типу «Авансы сотрудникам»
(chiefAccount=«Главная касса», account=«Текущие расчеты с сотрудниками», cfc=39 —
резолв по структуре, id различается dev/prod). Для выдачи с Сейфа iiko НЕ
вызывается (другой счёт). Банковская выдача (эквайринг) — отдельный поток (Фаза 2,
изъятие при «Выплачено», когда деньги физически выданы сотруднику из Сейфа).

Вызывается ПОСЛЕ commit (БД — источник истины). addPayOut необратим: ошибка iiko НЕ
откатывает выдачу, только логируется; задвоение исключено вызовом ровно один раз на
созданную выдачу. Образец — ``deposit_iiko_payout_production``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.services.deposit_iiko_payout_production import (
    ADD_PAYOUT_PATH,
    PAYOUT_TYPES_PATH,
)
from app.services.iiko_location import get_department_id
from app.services.kassa.cheque_payout_push import _build_payout_type_index
from app.services.kassa.iiko_cashshift_sync import (
    _auth_token,
    _fetch_accounts_map,
    _iiko_get,
    _iiko_post,
)

logger = logging.getLogger(__name__)

# Тип изъятия по (chiefAccount, account, cfc.code). Наличные — под «Главная касса»,
# банковская выдача (эквайринг, Фаза 2) — под «Денежные средства, эквайринг». Счёт и код
# статьи одинаковые («Текущие расчёты с сотрудниками» / 39), различается только корсчёт.
ADVANCE_CASH_PAYOUT_TYPE_KEY = ("Главная касса", "Текущие расчеты с сотрудниками", "39")
ADVANCE_BANK_PAYOUT_TYPE_KEY = (
    "Денежные средства, эквайринг",
    "Текущие расчеты с сотрудниками",
    "39",
)


def post_advance_payout_to_iiko(
    *,
    amount: Decimal,
    payout_date: date,
    source_id: uuid.UUID,
    is_loan: bool,
    source: str = "cash",
) -> None:
    """Провести выдачу аванса/займа изъятием в iiko. Ошибку логирует, не поднимает.

    ``source='cash'`` — наличная выдача с ТК Черникова (Главная касса);
    ``source='bank'`` — банковская выдача (эквайринг, Фаза 2, после «Выплачено»).
    """
    kind_label = "займа" if is_loan else "аванса"
    type_key = ADVANCE_BANK_PAYOUT_TYPE_KEY if source == "bank" else ADVANCE_CASH_PAYOUT_TYPE_KEY
    try:
        token = _auth_token()
        accounts = _fetch_accounts_map(token)
        raw_types = _iiko_get(token, PAYOUT_TYPES_PATH, {"includeDeleted": "false"})
        index = _build_payout_type_index(accounts, raw_types if isinstance(raw_types, list) else [])
        type_id = index.get(type_key)
        if type_id is None:
            logger.warning(
                "Выдача %s %s: тип изъятия iiko %s не найден",
                kind_label,
                source_id,
                type_key,
            )
            return
        value = Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        body = {
            "payOutTypeId": type_id,
            "payOutDate": payout_date.isoformat(),
            "departmentSumMap": {get_department_id(): float(value)},
            "comment": f"Выдача {kind_label} сотруднику (операция {source_id})",
        }
        data = _iiko_post(token, ADD_PAYOUT_PATH, body)
        result = data.get("result") if isinstance(data, dict) else None
        if result != "SUCCESS":
            logger.warning(
                "Выдача %s %s: addPayOut вернул не SUCCESS: %s", kind_label, source_id, data
            )
    except Exception as exc:  # noqa: BLE001 — iiko не должен валить уже проведённую выдачу
        logger.warning("Выдача %s %s: изъятие в iiko не проведено: %s", kind_label, source_id, exc)
