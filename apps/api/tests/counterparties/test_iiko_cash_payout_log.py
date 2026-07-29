"""Журнал денежных проводок в iiko вне накладных (авансы, депозиты).

Проводка ``addPayOut`` необратима и не идемпотентна, а раньше её судьба нигде не фиксировалась:
любой сбой терял изъятие молча, и «Главная касса» iiko расходилась с ДДС без следа. Тесты
закрепляют машину состояний журнала: pending-first, различение «точно не прошло» и «неизвестно»,
запрет повторной отправки и видимый кейс owner-review.

Сеть замокана — ``send`` подменяется функцией-заглушкой.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import IikoCashPayout, ReconciliationCase
from app.services.iiko_cash_payout_log import CASE_KIND, PayoutRejected, post_cash_payout

PAYOUT_DATE = date(2026, 7, 29)


async def _row(session: AsyncSession, kind: str, source_id: str) -> IikoCashPayout | None:
    return await session.scalar(
        select(IikoCashPayout).where(
            IikoCashPayout.kind == kind, IikoCashPayout.source_id == source_id
        )
    )


async def _cases(session: AsyncSession, source_id: str) -> list[ReconciliationCase]:
    return list(
        (
            await session.scalars(
                select(ReconciliationCase).where(
                    ReconciliationCase.kind == CASE_KIND,
                    ReconciliationCase.payload["source_id"].astext == source_id,
                )
            )
        ).all()
    )


async def _post(session: AsyncSession, source_id: str, send, *, kind: str = "advance"):
    return await post_cash_payout(
        session,
        kind=kind,
        source_id=source_id,
        amount=Decimal("1500.00"),
        payout_date=PAYOUT_DATE,
        send=send,
    )


async def test_successful_payout_is_recorded(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    source_id = str(uuid.uuid4())
    async with async_session_factory() as session:
        await _post(session, source_id, lambda: "TYPE-1")

        row = await _row(session, "advance", source_id)
        assert row is not None
        assert (row.status, row.reason_code, row.error) == ("posted", None, None)
        assert row.pay_out_type_id == "TYPE-1"
        assert await _cases(session, source_id) == []


async def test_second_call_does_not_send_again(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Проводка уже прошла — второй addPayOut выдал бы деньги в учёте дважды."""
    source_id = str(uuid.uuid4())
    calls: list[int] = []
    async with async_session_factory() as session:
        await _post(session, source_id, lambda: calls.append(1) or "TYPE-1")
        await _post(session, source_id, lambda: calls.append(1) or "TYPE-1")

    assert len(calls) == 1


async def test_unknown_outcome_blocks_retry_and_opens_case(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Обрыв на отправке: прошла проводка или нет — неизвестно. Повторять нельзя, но и молчать
    нельзя — заводим видимый кейс без предложения авто-повтора."""
    source_id = str(uuid.uuid4())
    calls: list[int] = []

    def _boom() -> str:
        calls.append(1)
        raise TimeoutError("connection reset")

    async with async_session_factory() as session:
        await _post(session, source_id, _boom)

        row = await _row(session, "advance", source_id)
        assert row is not None
        assert (row.status, row.reason_code) == ("failed", "unknown")
        cases = await _cases(session, source_id)
        assert len(cases) == 1
        assert cases[0].payload["retriable"] is False

        # Повторный вызов НЕ шлёт: исход прошлой попытки так и не выяснен.
        await _post(session, source_id, _boom)
    assert len(calls) == 1


async def test_rejected_payout_is_marked_without_retry_hint(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """iiko ответила не SUCCESS — проводки нет, но и авто-повтор не предлагаем: причина
    неизвестна, а деньги в учёте дороже лишней ручной сверки."""
    source_id = str(uuid.uuid4())

    def _rejected() -> str:
        raise PayoutRejected("addPayOut вернул не SUCCESS: {'result': 'ERROR'}")

    async with async_session_factory() as session:
        await _post(session, source_id, _rejected)

        row = await _row(session, "advance", source_id)
        assert row is not None and row.reason_code == "rejected"
        cases = await _cases(session, source_id)
        assert len(cases) == 1 and cases[0].payload["retriable"] is False


async def test_missing_payout_type_is_retriable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Тип проводки не настроен — отказ ДО отправки: в iiko точно ничего нет, повтор безопасен."""
    source_id = str(uuid.uuid4())

    def _no_type() -> str:
        raise LookupError("тип изъятия iiko ('Главная касса', 'Депозиты сотрудников') не найден")

    async with async_session_factory() as session:
        await _post(session, source_id, _no_type, kind="deposit_production")

        row = await _row(session, "deposit_production", source_id)
        assert row is not None and row.reason_code == "type_not_found"
        cases = await _cases(session, source_id)
        assert len(cases) == 1 and cases[0].payload["retriable"] is True


async def test_case_is_not_duplicated_on_repeated_failure(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    source_id = str(uuid.uuid4())

    def _no_type() -> str:
        raise LookupError("тип не найден")

    async with async_session_factory() as session:
        await _post(session, source_id, _no_type)
        await _post(session, source_id, _no_type)

        assert len(await _cases(session, source_id)) == 1


async def test_failed_payout_can_be_completed_later(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """После настройки типа повтор проходит — строка становится posted."""
    source_id = str(uuid.uuid4())

    def _no_type() -> str:
        raise LookupError("тип не найден")

    async with async_session_factory() as session:
        await _post(session, source_id, _no_type)
        await _post(session, source_id, lambda: "TYPE-9")

        row = await _row(session, "advance", source_id)
        assert row is not None
        assert (row.status, row.reason_code, row.pay_out_type_id) == ("posted", None, "TYPE-9")


@pytest.mark.parametrize(
    "kind", ["advance", "loan", "deposit_production", "courier_deposit_return"]
)
async def test_kinds_are_independent(
    async_session_factory: async_sessionmaker[AsyncSession], kind: str
) -> None:
    """Ключ идемпотентности — (контур, id операции): id транзакции курьера целочисленный и
    вполне может совпасть с чужим."""
    async with async_session_factory() as session:
        await _post(session, "42", lambda: "TYPE-1", kind=kind)
        row = await _row(session, kind, "42")
        assert row is not None and row.status == "posted"


# ── смежные страховки того же класса ────────────────────────────────────────────────────────────


async def test_reset_sync_refuses_empty_iiko_export(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Пустая выгрузка в режиме сброса уволила бы весь штат разом. Это всегда сбой выгрузки,
    а не «в iiko не осталось сотрудников» — синхронизироваться с пустотой отказываемся."""
    from app.services import iiko_sync

    monkeypatch.setattr(iiko_sync, "fetch_iiko_employee_records", lambda: [])
    async with async_session_factory() as session:
        with pytest.raises(ValueError, match="пустой список сотрудников"):
            await iiko_sync.sync_employees(session, mode="reset")


async def test_incremental_sync_tolerates_empty_export(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Инкрементальный проход отсутствующих не трогает — пустая выгрузка для него безобидна."""
    from app.services import iiko_sync

    monkeypatch.setattr(iiko_sync, "fetch_iiko_employee_records", lambda: [])
    async with async_session_factory() as session:
        result = await iiko_sync.sync_employees(session, mode="incremental")
    assert result.deactivated == 0
