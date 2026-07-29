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
откатывает выдачу. Судьба проводки живёт в журнале ``IikoCashPayout`` (pending-first, кейс
owner-review при неуспехе) — раньше сбой терял изъятие молча. См. ``iiko_cash_payout_log``.
Образец — ``deposit_iiko_payout_production``.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.deposit_iiko_payout_production import (
    ADD_PAYOUT_PATH,
    PAYOUT_TYPES_PATH,
)
from app.services.iiko_cash_payout_log import PayoutRejected, post_cash_payout
from app.services.iiko_location import get_department_id
from app.services.kassa.cheque_payout_push import _build_payout_type_index
from app.services.kassa.iiko_cashshift_sync import (
    _auth_token,
    _fetch_accounts_map,
    _iiko_get,
    _iiko_post,
)

# Тип изъятия по (chiefAccount, account, cfc.code). Наличные — под «Главная касса»,
# банковская выдача (эквайринг, Фаза 2) — под «Денежные средства, эквайринг». Счёт и код
# статьи одинаковые («Текущие расчёты с сотрудниками» / 39), различается только корсчёт.
ADVANCE_CASH_PAYOUT_TYPE_KEY = ("Главная касса", "Текущие расчеты с сотрудниками", "39")
ADVANCE_BANK_PAYOUT_TYPE_KEY = (
    "Денежные средства, эквайринг",
    "Текущие расчеты с сотрудниками",
    "39",
)


def send_advance_payout(
    *,
    amount: Decimal,
    payout_date: date,
    source_id: uuid.UUID,
    is_loan: bool,
    source: str = "cash",
) -> str:
    """Синхронная отправка изъятия (исполняется в треде). Возвращает id типа проводки.

    ``LookupError`` — тип не найден (до отправки, проводки в iiko точно нет);
    :class:`PayoutRejected` — iiko ответила не ``SUCCESS``. Прочие исключения (сеть/таймаут)
    оставляют исход неизвестным — их разбирает журнал."""
    kind_label = "займа" if is_loan else "аванса"
    type_key = ADVANCE_BANK_PAYOUT_TYPE_KEY if source == "bank" else ADVANCE_CASH_PAYOUT_TYPE_KEY
    token = _auth_token()
    accounts = _fetch_accounts_map(token)
    raw_types = _iiko_get(token, PAYOUT_TYPES_PATH, {"includeDeleted": "false"})
    index = _build_payout_type_index(accounts, raw_types if isinstance(raw_types, list) else [])
    type_id = index.get(type_key)
    if type_id is None:
        raise LookupError(f"тип изъятия iiko {type_key} не найден")
    value = Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    data = _iiko_post(
        token,
        ADD_PAYOUT_PATH,
        {
            "payOutTypeId": type_id,
            "payOutDate": payout_date.isoformat(),
            "departmentSumMap": {get_department_id(): float(value)},
            "comment": f"Выдача {kind_label} сотруднику (операция {source_id})",
        },
    )
    result = data.get("result") if isinstance(data, dict) else None
    if result != "SUCCESS":
        raise PayoutRejected(f"addPayOut вернул не SUCCESS: {data}")
    return type_id


async def post_advance_payout_to_iiko(
    session: AsyncSession,
    *,
    amount: Decimal,
    payout_date: date,
    source_id: uuid.UUID,
    is_loan: bool,
    source: str = "cash",
) -> None:
    """Провести выдачу аванса/займа изъятием в iiko под журналом.

    ``source='cash'`` — наличная выдача с ТК Черникова (Главная касса);
    ``source='bank'`` — банковская выдача (эквайринг, Фаза 2, после «Выплачено»).
    """
    await post_cash_payout(
        session,
        kind="loan" if is_loan else "advance",
        source_id=source_id,
        amount=amount,
        payout_date=payout_date,
        send=lambda: send_advance_payout(
            amount=amount,
            payout_date=payout_date,
            source_id=source_id,
            is_loan=is_loan,
            source=source,
        ),
    )
