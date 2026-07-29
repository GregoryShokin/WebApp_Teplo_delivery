"""Durable-след денежных проводок в iiko вне накладных (авансы, депозиты курьеров и штата).

Проводка ``payInOuts/addPayOut`` необратима, а её судьба раньше нигде не фиксировалась: любой
сбой терял изъятие молча, и остаток «Главной кассы» iiko расходился с нашим ДДС без единого
следа, кроме ``warning`` в логах контейнера.

Здесь — обёртка вокруг вызова: ``pending`` пишется ДО HTTP, после ответа строка переходит в
``posted``/``failed``, а неуспех поднимает видимый кейс owner-review. Авто-повтора нет
намеренно: ``addPayOut`` не идемпотентен, и повторить можно только то, про что ТОЧНО известно,
что оно не прошло (``type_not_found`` — отказ до отправки). Всё остальное — ручная сверка в
бэк-офисе iiko. Тот же выбор, что в контуре оплат накладных (см. counterparty_iiko_payment).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date
from decimal import Decimal

import anyio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import IikoCashPayout, ReconciliationCase

logger = logging.getLogger(__name__)

CASE_KIND = "iiko_cash_payout_unsettled"

# Человекочитаемые названия контуров — идут в кейс, чтобы владелец понимал, что именно
# не отразилось в iiko, не заглядывая в код.
KIND_TITLES: dict[str, str] = {
    "advance": "выдача аванса сотруднику",
    "loan": "выдача займа сотруднику",
    "deposit_production": "выдача производственного депозита",
    "courier_deposit_return": "возврат депозита курьеру",
    "courier_deposit_topup": "пополнение депозита курьера",
}
# Причины, где повтор безопасен: отказ случился ДО отправки, проводки в iiko точно нет.
RETRIABLE_REASONS = frozenset({"type_not_found"})


class PayoutRejected(RuntimeError):
    """iiko приняла запрос, но ответила не ``SUCCESS`` — проводки нет."""


async def _open_case(
    session: AsyncSession, record: IikoCashPayout, *, reason: str
) -> None:
    """Завести (или обновить) видимый кейс owner-review по незакрытой проводке."""
    existing = await session.scalar(
        select(ReconciliationCase).where(
            ReconciliationCase.kind == CASE_KIND,
            ReconciliationCase.status == "pending",
            ReconciliationCase.payload["source_id"].astext == record.source_id,
        )
    )
    payload = {
        "source_id": record.source_id,
        "payout_kind": record.kind,
        "amount": str(record.amount),
        "payout_date": record.payout_date.isoformat(),
        "reason": reason,
        "reason_code": record.reason_code,
        "retriable": record.reason_code in RETRIABLE_REASONS,
    }
    if existing is not None:
        existing.payload = payload
        return
    session.add(ReconciliationCase(kind=CASE_KIND, status="pending", payload=payload))


async def post_cash_payout(
    session: AsyncSession,
    *,
    kind: str,
    source_id: str,
    amount: Decimal,
    payout_date: date,
    send: Callable[[], str | None],
) -> IikoCashPayout | None:
    """Провести денежную операцию в iiko под журналом. Никогда не бросает: операция у нас уже
    проведена, и отказ iiko её не откатывает — он становится строкой журнала и кейсом.

    ``send`` — синхронная отправка (исполняется в треде), возвращает id типа проводки для
    аудита. Ошибки: :class:`PayoutRejected` — iiko отказала (проводки нет), любое другое
    исключение — исход НЕИЗВЕСТЕН (сеть/таймаут), и повторять нельзя.
    """
    existing = await session.scalar(
        select(IikoCashPayout).where(
            IikoCashPayout.kind == kind, IikoCashPayout.source_id == source_id
        )
    )
    if existing is not None and (
        existing.status in ("posted", "pending") or existing.reason_code == "unknown"
    ):
        # posted — уже проведено; pending — либо прямо сейчас в полёте, либо осиротело после
        # краша; unknown — отправка оборвалась, не получив ответа. Во всех трёх случаях исход
        # либо известен, либо неизвестен настолько, что второй addPayOut рискует выдать деньги
        # в учёте дважды. Разбор — ручной, по кейсу owner-review.
        return existing

    record = existing or IikoCashPayout(kind=kind, source_id=source_id)
    record.amount = Decimal(str(amount))
    record.payout_date = payout_date
    record.status = "pending"
    record.reason_code = None
    record.error = None
    if existing is None:
        session.add(record)
    try:
        await session.commit()
    except IntegrityError:
        # Гонка по (kind, source_id): другой запрос уже взял проводку — не шлём вторую.
        await session.rollback()
        return await session.scalar(
            select(IikoCashPayout).where(
                IikoCashPayout.kind == kind, IikoCashPayout.source_id == source_id
            )
        )

    reason_code: str | None = None
    error: str | None = None
    pay_out_type_id: str | None = None
    try:
        pay_out_type_id = await anyio.to_thread.run_sync(send)
    except PayoutRejected as exc:
        reason_code, error = "rejected", str(exc)[:500]
    except LookupError as exc:
        # Тип проводки в iiko не найден — до отправки, значит проводки там точно нет.
        reason_code, error = "type_not_found", str(exc)[:500]
    except Exception as exc:  # noqa: BLE001 — сеть/таймаут/лимит: исход НЕИЗВЕСТЕН
        reason_code, error = "unknown", f"{type(exc).__name__}: {exc}"[:500]

    record.pay_out_type_id = pay_out_type_id
    record.status = "posted" if reason_code is None else "failed"
    record.reason_code = reason_code
    record.error = error
    if reason_code is not None:
        title = KIND_TITLES.get(kind, kind)
        hint = (
            "повторите отправку"
            if reason_code in RETRIABLE_REASONS
            else "проверьте проводку в бэк-офисе iiko и проведите вручную, если её нет"
        )
        await _open_case(
            session,
            record,
            reason=f"{title} на {record.amount} не отражена в iiko ({error}) — {hint}",
        )
        logger.warning(
            "iiko cash payout %s/%s не проведена (%s): %s", kind, source_id, reason_code, error
        )
    await session.commit()
    return record
