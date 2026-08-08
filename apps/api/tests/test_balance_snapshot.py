"""Заморозка снимка, граница «черновик — финал» и детектор дрейфа.

ГЛАВНОЕ СВОЙСТВО МОДУЛЯ проверяется первым тестом: подписанный снимок не двигается от того,
что первичку поправили задним числом. Без него сверка приращения капитала мертва — октябрьская
правка августа переехала бы в расхождение сентября, и владелец искал бы в сентябре дыру,
созданную в октябре.

Тестовая база несёт сид-кошельки с ненулевыми входящими остатками, поэтому итоги сравниваются
не с абсолютом, а сами с собой до и после.
"""

from __future__ import annotations

import sys
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

sys.path.append(str(Path(__file__).parent / "counterparties"))

from cp_helpers import make_wallet  # noqa: E402

from app.models import (  # noqa: E402
    BalanceManualValue,
    BalanceSnapshot,
    BalanceSnapshotFlag,
    BalanceSnapshotLine,
    CashflowTransaction,
)
from app.services import asset_balance  # noqa: E402
from app.services.balance import catalog as cat  # noqa: E402
from app.services.balance import snapshot as snapshot_service  # noqa: E402
from app.services.balance.assembly import assemble  # noqa: E402

JULY_END = date(2026, 7, 31)
AUGUST_END = date(2026, 8, 31)


async def _cash(
    session: AsyncSession, *, wallet_id: uuid.UUID, amount: str, on: date, direction: str = "in"
) -> None:
    session.add(
        CashflowTransaction(
            wallet_id=wallet_id,
            direction=direction,
            amount=Decimal(amount),
            operation_date=on,
            source_kind="manual",
            quality_status="ok",
        )
    )
    await session.flush()


async def _stray_wallet_with(session: AsyncSession, *, amount: str, on: date):
    """Кошелёк вне справочника баланса: своя строка, ни с чем не смешанная.

    Сид-кошельки миграций несут собственные остатки, и проверять на них «стало ровно столько»
    значило бы измерять чужие деньги.
    """
    wallet = await make_wallet(session, name="Отдельный сейф", wallet_type="cash_safe")
    await _cash(session, wallet_id=wallet.id, amount=amount, on=on)
    return wallet


async def _frozen_line(session: AsyncSession, as_of: date, code: str) -> BalanceSnapshotLine:
    snapshot = await session.scalar(select(BalanceSnapshot).where(BalanceSnapshot.as_of == as_of))
    assert snapshot is not None
    line = await session.scalar(
        select(BalanceSnapshotLine).where(
            BalanceSnapshotLine.snapshot_id == snapshot.id,
            BalanceSnapshotLine.line_code == code,
        )
    )
    assert line is not None
    return line


async def test_a_frozen_snapshot_does_not_move_when_the_past_is_edited(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Правка задним числом меняет живой расчёт и НЕ меняет подписанный снимок.

    Прошлое в этой системе двигается законно и бесследно: исключение банковской операции не
    датировано, стоимость основного средства переписывается вместе со всей историей, отметка
    выплаты при снятии удаляется физически. Если бы снимок пересчитывался, каждая такая правка
    задним числом всплывала бы в расхождении СЛЕДУЮЩЕГО месяца.
    """
    async with async_session_factory() as session:
        wallet = await _stray_wallet_with(session, amount="30000.00", on=date(2026, 7, 5))
        await session.commit()

        await snapshot_service.freeze(session, as_of=JULY_END)
        await session.commit()

        snapshot = await session.scalar(
            select(BalanceSnapshot).where(BalanceSnapshot.as_of == JULY_END)
        )
        assets_when_signed = snapshot.asset_total
        assert (await _frozen_line(session, JULY_END, cat.UNMAPPED_WALLET_LINE)).amount == Decimal(
            "30000.00"
        )

        # Правка задним числом: деньги июля, внесённые позже.
        await _cash(session, wallet_id=wallet.id, amount="5000.00", on=date(2026, 7, 10))
        await session.commit()
        session.expire_all()

        frozen = await _frozen_line(session, JULY_END, cat.UNMAPPED_WALLET_LINE)
        assert frozen.amount == Decimal("30000.00")
        snapshot = await session.scalar(
            select(BalanceSnapshot).where(BalanceSnapshot.as_of == JULY_END)
        )
        assert snapshot.asset_total == assets_when_signed

        # А живой расчёт правку видит — иначе она была бы просто потеряна.
        live = await assemble(session, as_of=JULY_END)
        assert next(
            line for line in live.lines if line.code == cat.UNMAPPED_WALLET_LINE
        ).amount == Decimal("35000.00")


async def test_drift_names_the_line_that_the_backdated_edit_moved(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Снимок не двигается — но молчать о расхождении нельзя.

    Дрейф отвечает на вопрос «остались ли перенесённые в отчётность числа верными»: пустой
    список означает «да», строка в нём называет, где именно разошлось и на сколько.
    """
    async with async_session_factory() as session:
        wallet = await _stray_wallet_with(session, amount="30000.00", on=date(2026, 7, 5))
        await session.commit()
        await snapshot_service.freeze(session, as_of=JULY_END)
        await session.commit()

        assert await snapshot_service.drift(session, as_of=JULY_END) == []

        await _cash(session, wallet_id=wallet.id, amount="5000.00", on=date(2026, 7, 10))
        await session.commit()

        rows = await snapshot_service.drift(session, as_of=JULY_END)
        moved = next(row for row in rows if row.line_code == cat.UNMAPPED_WALLET_LINE)
        assert moved.frozen_amount == Decimal("30000.00")
        assert moved.live_amount == Decimal("35000.00")
        assert moved.delta == Decimal("5000.00")


async def test_drift_recomputes_fixed_assets_instead_of_reading_their_snapshot(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Живой расчёт не имеет права читать чужой замороженный снимок.

    Регистр основных средств за закрытый месяц отдаёт собственный снимок. Возьми его детектор
    дрейфа — и он сравнил бы снимок со снимком, получив тождественный ноль: сломанная проверка,
    которая всегда зелёная. Поэтому режим ``live`` идёт мимо ``report_lines``.
    """
    touched: list[str] = []

    async def _report_lines(*args, **kwargs):
        touched.append("frozen")
        return [], False

    async def _balance_lines(*args, **kwargs):
        touched.append("live")
        return []

    monkeypatch.setattr(asset_balance, "report_lines", _report_lines)
    monkeypatch.setattr(asset_balance, "balance_lines", _balance_lines)

    async with async_session_factory() as session:
        await assemble(session, as_of=JULY_END, live=True)
        assert touched == ["live"]

        touched.clear()
        await assemble(session, as_of=JULY_END)
        assert touched == ["frozen"]


async def test_a_draft_is_reassembled_without_duplicating_its_lines(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Черновик — рабочая поверхность: пересобрать его можно, данные ещё доезжают.

    Пересборка обязана заменять строки, а не дописывать: иначе итог сойдётся, а расшифровка
    покажет каждую величину дважды.
    """
    async with async_session_factory() as session:
        wallet = await _stray_wallet_with(session, amount="30000.00", on=date(2026, 7, 5))
        await session.commit()
        await snapshot_service.freeze(session, as_of=JULY_END)
        await session.commit()

        await _cash(session, wallet_id=wallet.id, amount="5000.00", on=date(2026, 7, 10))
        await session.commit()
        await snapshot_service.freeze(session, as_of=JULY_END)
        await session.commit()

        snapshots = (
            await session.scalars(select(BalanceSnapshot).where(BalanceSnapshot.as_of == JULY_END))
        ).all()
        assert len(snapshots) == 1
        lines = (
            await session.scalars(
                select(BalanceSnapshotLine).where(
                    BalanceSnapshotLine.snapshot_id == snapshots[0].id,
                    BalanceSnapshotLine.line_code == cat.UNMAPPED_WALLET_LINE,
                )
            )
        ).all()
        assert [line.amount for line in lines] == [Decimal("35000.00")]


async def test_a_signed_snapshot_refuses_to_be_reassembled(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Финал — это «сколько мы сказали», и переписать сказанное нельзя.

    Изменившаяся цифра не пропадает: её показывает дрейф.
    """
    async with async_session_factory() as session:
        await _stray_wallet_with(session, amount="30000.00", on=date(2026, 7, 5))
        await session.commit()
        await snapshot_service.freeze(session, as_of=JULY_END)
        await snapshot_service.finalize(session, as_of=JULY_END)
        await session.commit()

        with pytest.raises(snapshot_service.BalanceSnapshotError, match="финализирован"):
            await snapshot_service.freeze(session, as_of=JULY_END)


async def test_the_first_snapshot_has_nothing_to_reconcile_against(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """У первого снимка капитал установлен описью, и сверять приращение не с чем.

    Пустая сверка здесь — честный ответ, а не пропущенная проверка: метод так и записан.
    """
    async with async_session_factory() as session:
        await snapshot_service.freeze(session, as_of=JULY_END)
        await session.commit()

        snapshot = await session.scalar(
            select(BalanceSnapshot).where(BalanceSnapshot.as_of == JULY_END)
        )
        assert snapshot.method == "inventory"
        assert snapshot.recon_delta_equity is None
        assert snapshot.recon_residual is None
        assert snapshot.prev_snapshot_id is None


async def test_the_second_snapshot_reconciles_only_against_a_signed_one(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Сверять приращение с черновиком нельзя: он пересобирается, и сверка поехала бы сама собой.

    Пока июль остаётся черновиком, август собирается описью. После подписи июля август получает
    проверку приращения.
    """
    async with async_session_factory() as session:
        await snapshot_service.freeze(session, as_of=JULY_END)
        await session.commit()

        await snapshot_service.freeze(session, as_of=AUGUST_END)
        await session.commit()
        august = await session.scalar(
            select(BalanceSnapshot).where(BalanceSnapshot.as_of == AUGUST_END)
        )
        assert august.method == "inventory"
        assert august.recon_delta_equity is None

        await snapshot_service.finalize(session, as_of=JULY_END)
        await session.commit()
        await snapshot_service.freeze(session, as_of=AUGUST_END)
        await session.commit()

        session.expire_all()
        july = await session.scalar(
            select(BalanceSnapshot).where(BalanceSnapshot.as_of == JULY_END)
        )
        august = await session.scalar(
            select(BalanceSnapshot).where(BalanceSnapshot.as_of == AUGUST_END)
        )
        assert august.method == "rolling"
        assert august.prev_snapshot_id == july.id
        assert august.recon_delta_equity == august.equity_plug - july.equity_plug


async def test_finalizing_an_unexplained_residual_demands_a_written_reason(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Расхождение можно принять — но не молча.

    Запретить подпись значило бы остановить закрытие месяца из-за цифры, которую владелец уже
    понял. Промолчать — потерять единственное объяснение, которое через полгода не вспомнит
    никто.
    """
    async with async_session_factory() as session:
        await snapshot_service.freeze(session, as_of=JULY_END)
        await snapshot_service.finalize(session, as_of=JULY_END)
        await session.commit()

        # Деньги, взявшиеся ниоткуда: капитал вырос, а прибыли и вкладов под ним нет.
        await _stray_wallet_with(session, amount="500000.00", on=date(2026, 8, 12))
        await session.commit()
        await snapshot_service.freeze(session, as_of=AUGUST_END)
        await session.commit()

        august = await session.scalar(
            select(BalanceSnapshot).where(BalanceSnapshot.as_of == AUGUST_END)
        )
        assert abs(august.recon_residual) > august.tolerance_band
        flag = await session.scalar(
            select(BalanceSnapshotFlag).where(
                BalanceSnapshotFlag.snapshot_id == august.id,
                BalanceSnapshotFlag.code == "reconciliation_failed",
            )
        )
        assert flag is not None and flag.severity == "blocking"

        with pytest.raises(snapshot_service.BalanceSnapshotError, match="укажите причину"):
            await snapshot_service.finalize(session, as_of=AUGUST_END)

        await snapshot_service.finalize(
            session, as_of=AUGUST_END, residual_reason="Внесение собственника без проводки"
        )
        await session.commit()
        session.expire_all()
        august = await session.scalar(
            select(BalanceSnapshot).where(BalanceSnapshot.as_of == AUGUST_END)
        )
        assert august.status == "final"
        assert august.residual_reason == "Внесение собственника без проводки"


async def test_the_tolerance_uses_the_frozen_approximation_of_the_previous_month(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Допуск считается от того, что было записано в прошлом снимке, а не пересчитано сейчас.

    Пересчёт означал бы, что полоса допуска прошлого месяца меняется задним числом — и подпись
    «сошлось в пределах неточности» через полгода оказалась бы без основания.
    """
    async with async_session_factory() as session:
        await snapshot_service.freeze(session, as_of=JULY_END)
        await snapshot_service.finalize(session, as_of=JULY_END)
        await session.commit()

        july = await session.scalar(
            select(BalanceSnapshot).where(BalanceSnapshot.as_of == JULY_END)
        )
        await session.execute(
            update(BalanceSnapshotFlag)
            .where(
                BalanceSnapshotFlag.snapshot_id == july.id,
                BalanceSnapshotFlag.code == "approximate_total",
            )
            .values(amount=Decimal("120000.00"))
        )
        await session.commit()

        await snapshot_service.freeze(session, as_of=AUGUST_END)
        await session.commit()

        august = await session.scalar(
            select(BalanceSnapshot).where(BalanceSnapshot.as_of == AUGUST_END)
        )
        # У августа приблизительных величин нет, значит изменение — все 120 000 плюс копейки
        # на округление. Возьми расчёт живую (нулевую) величину июля — вышла бы единица.
        assert august.tolerance_band == Decimal("120001.00")


async def test_a_hand_entered_value_survives_the_reassembly_of_the_draft(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Введённое человеком живёт отдельно от снимка и переживает его пересборку.

    Иначе каждая пересборка черновика стирала бы работу владельца, и он заводил бы цифры заново.
    """
    async with async_session_factory() as session:
        session.add(
            BalanceManualValue(
                as_of=JULY_END,
                line_code="ar_acquiring",
                amount=Decimal("64200.00"),
                reason="Выручка 31.07, зачислена 01.08",
            )
        )
        await session.commit()

        await snapshot_service.freeze(session, as_of=JULY_END)
        await session.commit()
        await snapshot_service.freeze(session, as_of=JULY_END)
        await session.commit()

        line = await _frozen_line(session, JULY_END, "ar_acquiring")
        assert line.amount == Decimal("64200.00")
        assert line.status == cat.ST_MANUAL
        assert (
            await session.scalar(
                select(BalanceManualValue).where(BalanceManualValue.line_code == "ar_acquiring")
            )
        ) is not None
