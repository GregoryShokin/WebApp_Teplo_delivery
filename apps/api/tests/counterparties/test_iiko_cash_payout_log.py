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


# ── пост-сверка проводок по учёту iiko ──────────────────────────────────────────────────────────


async def _aged_row(
    session: AsyncSession,
    *,
    source_id: str,
    status: str = "posted",
    reason_code: str | None = None,
    verify_attempts: int = 0,
) -> IikoCashPayout:
    """Строка журнала старше grace-окна — иначе сверка её не возьмёт."""
    from datetime import UTC, datetime, timedelta

    from app.services.iiko_cash_payout_verify import VERIFY_GRACE

    row = IikoCashPayout(
        kind="advance",
        source_id=source_id,
        amount=Decimal("1000.00"),
        payout_date=PAYOUT_DATE,
        status=status,
        reason_code=reason_code,
        verify_attempts=verify_attempts,
    )
    session.add(row)
    await session.flush()
    row.created_at = datetime.now(UTC) - VERIFY_GRACE - timedelta(minutes=5)
    await session.commit()
    return row


def _patch_olap(monkeypatch: pytest.MonkeyPatch, comments: list[str]) -> None:
    from app.services import iiko_cash_payout_verify as verify_mod

    monkeypatch.setattr(
        verify_mod, "fetch_cash_payout_comments", lambda date_from, date_to: comments
    )


async def test_verify_confirms_payout_by_operation_id(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Комментарий проводки несёт id операции — сопоставляем точно по нему, а не по сумме."""
    from app.services.iiko_cash_payout_verify import verify_cash_payouts

    source_id = str(uuid.uuid4())
    async with async_session_factory() as session:
        row = await _aged_row(session, source_id=source_id)
        _patch_olap(
            monkeypatch,
            [f'Приложение/Авансы: "Выдача аванса сотруднику (операция {source_id})"'],
        )

        result = await verify_cash_payouts(session)

        await session.refresh(row)
        assert result["verified"] == 1
        assert row.verified_at is not None


async def test_verify_matches_courier_hash_form(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """У транзакции депозита курьера id целочисленный и уходит в комментарий с решёткой."""
    from app.services.iiko_cash_payout_verify import verify_cash_payouts

    async with async_session_factory() as session:
        row = await _aged_row(session, source_id="41")
        _patch_olap(
            monkeypatch,
            ['Приложение/Возврат депозитов: "Возврат депозита курьеру (операция #41)"'],
        )

        result = await verify_cash_payouts(session)

        await session.refresh(row)
        assert (result["verified"], row.verified_at is not None) == (1, True)


async def test_verify_promotes_unknown_outcome_when_transaction_found(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отправка оборвалась, но проводка в iiko есть → выдача отражена, строка становится posted."""
    from app.services.iiko_cash_payout_verify import verify_cash_payouts

    source_id = str(uuid.uuid4())
    async with async_session_factory() as session:
        row = await _aged_row(
            session, source_id=source_id, status="failed", reason_code="unknown"
        )
        _patch_olap(monkeypatch, [f'"Выдача аванса сотруднику (операция {source_id})"'])

        await verify_cash_payouts(session)

        await session.refresh(row)
        assert (row.status, row.reason_code, row.verified_at is not None) == ("posted", None, True)


async def test_verify_is_patient_before_opening_case(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проводка могла ещё не доехать в отчёт — одной неудачной проверки мало."""
    from app.services.iiko_cash_payout_verify import verify_cash_payouts

    source_id = str(uuid.uuid4())
    async with async_session_factory() as session:
        row = await _aged_row(session, source_id=source_id)
        _patch_olap(monkeypatch, [])

        result = await verify_cash_payouts(session)

        await session.refresh(row)
        assert (result["pending"], row.verify_attempts) == (1, 1)
        assert await _cases(session, source_id) == []


async def test_verify_opens_case_when_transaction_never_appears(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проводки нет и после порога проверок — выдача до учёта iiko не дошла. Кейс без
    авто-повтора: addPayOut необратим, лишняя проводка = выданные дважды деньги."""
    from app.services.iiko_cash_payout_verify import (
        VERIFY_ATTEMPTS_BEFORE_CASE,
        verify_cash_payouts,
    )

    source_id = str(uuid.uuid4())
    async with async_session_factory() as session:
        await _aged_row(
            session, source_id=source_id, verify_attempts=VERIFY_ATTEMPTS_BEFORE_CASE - 1
        )
        _patch_olap(monkeypatch, ['"Выдача аванса сотруднику (операция чужая-операция)"'])

        result = await verify_cash_payouts(session)

        assert result["manual"] == 1
        cases = await _cases(session, source_id)
        assert len(cases) == 1
        assert cases[0].payload["reason_code"] == "not_in_iiko_ledger"
        assert cases[0].payload["retriable"] is False


async def test_verify_skips_definite_failures(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отказ до отправки (тип не настроен) сверять нечего — проводки заведомо нет, и лишний
    поход в OLAP ей ничего не добавит."""
    from app.services.iiko_cash_payout_verify import verify_cash_payouts

    async with async_session_factory() as session:
        await _aged_row(
            session, source_id=str(uuid.uuid4()), status="failed", reason_code="type_not_found"
        )

        def _boom(date_from: object, date_to: object) -> list[str]:
            raise AssertionError("OLAP не должен вызываться на пустой выборке")

        from app.services import iiko_cash_payout_verify as verify_mod

        monkeypatch.setattr(verify_mod, "fetch_cash_payout_comments", _boom)
        result = await verify_cash_payouts(session)

    assert result == {"checked": 0, "verified": 0, "pending": 0, "manual": 0}
