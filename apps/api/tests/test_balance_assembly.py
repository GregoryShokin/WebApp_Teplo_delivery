"""Сборка баланса на дату: что попадает в строки и как выглядит пустота.

Модуль складывает ответы чужих регистров, поэтому здесь проверяется не арифметика остатков
(она покрыта тестами самих регистров), а СТЫКИ: не потерялась ли строка, не слилась ли одна
величина с другой, отличается ли «неизвестно» от «ноль» и виден ли повод для сомнения.

Тестовая база несёт сид-кошельки миграций с ненулевыми входящими остатками, поэтому итоги
здесь нигде не сравниваются с абсолютом — только с самими собой до и после.
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

sys.path.append(str(Path(__file__).parent / "counterparties"))

from cp_helpers import make_wallet  # noqa: E402

from app.models import (  # noqa: E402
    BalanceManualValue,
    CashflowTransaction,
    PnlIikoStockFact,
    PnlProductWhitelist,
)
from app.services.balance import catalog as cat  # noqa: E402
from app.services.balance.assembly import BalanceBeforeAccountingStart, assemble  # noqa: E402

JULY_END = date(2026, 7, 31)
JULY = date(2026, 7, 1)


def _line(assembled, code: str):
    return next(item for item in assembled.lines if item.code == code)


def _flag(assembled, code: str):
    return next((item for item in assembled.flags if item.code == code), None)


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


async def _stock_fact(
    session: AsyncSession,
    *,
    month: date,
    amount: str,
    synced_at: datetime,
    name: str = "Упаковка",
    stocked: bool = True,
) -> str:
    """Складской факт месяца. ``stocked`` — числится ли позиция под складским учётом.

    Снимок iiko приносит остатки по ВСЕЙ номенклатуре, а в баланс идёт только та, что
    инвентаризируется, — поэтому у хелпера два режима, и по умолчанию позиция ревизуемая.
    """
    guid = str(uuid.uuid4())
    session.add(
        PnlIikoStockFact(
            period_month=month,
            iiko_product_guid=guid,
            product_name=name,
            opening_quantity=Decimal("0"),
            opening_amount=Decimal("0"),
            receipts_amount=Decimal("0"),
            closing_quantity=Decimal("10"),
            closing_amount=Decimal(amount),
            consumption_amount=Decimal("0"),
            synced_at=synced_at,
        )
    )
    if stocked:
        await _stocked(session, guid=guid, name=name)
    await session.flush()
    return guid


async def _stocked(session: AsyncSession, *, guid: str, name: str, status: str = "stocked") -> None:
    """Позиция в списке ревизий («Товары iiko» → «Ревизии»)."""
    session.add(
        PnlProductWhitelist(
            iiko_product_guid=guid,
            source_kind=cat.STOCK_WHITELIST_SOURCE,
            include_status=status,
            product_name=name,
        )
    )
    await session.flush()


async def test_a_date_before_the_start_of_accounting_is_refused(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """До 01.07.2026 истории в системе нет — собирать баланс не из чего.

    Отказ, а не пустой баланс: пустой прочитали бы как «на эту дату у бизнеса ничего не было».
    """
    async with async_session_factory() as session:
        with pytest.raises(BalanceBeforeAccountingStart):
            await assemble(session, as_of=date(2026, 6, 30))


async def test_capital_is_the_plug_and_says_so(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Капитал выводится из актива и обязательств, поэтому «актив = пассив» ничего не проверяет.

    Тест закрепляет не арифметику (она тождественна), а ПОМЕТКУ: строка обязана читаться иначе
    прочих, иначе тождество примут за сошедшийся баланс.
    """
    async with async_session_factory() as session:
        assembled = await assemble(session, as_of=JULY_END)

        assert assembled.equity_plug == assembled.asset_total - assembled.liability_total
        equity = _line(assembled, "equity_plug")
        assert equity.status == "plug"
        assert equity.amount == assembled.equity_plug
        assert "выведен" in (equity.source_note or "").lower()


async def test_the_vacation_memo_is_shown_but_not_counted_twice(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Отпускные уже сидят внутри долга перед сотрудниками — в итог они входить не должны.

    Строка-заметка существует ради состава, и единственный способ проверить, что она не удвоила
    обязательства, — сложить итог самому, исключив memo.
    """
    async with async_session_factory() as session:
        assembled = await assemble(session, as_of=JULY_END)

        memo = _line(assembled, "ap_staff_vacation")
        assert memo.kind == cat.KIND_MEMO

        by_hand = sum(
            (item.amount or Decimal("0"))
            for item in assembled.lines
            if item.block == cat.LIABILITY and item.kind != cat.KIND_MEMO
        )
        assert assembled.liability_total == Decimal(by_hand).quantize(Decimal("0.01"))


async def test_the_three_kinds_of_emptiness_stay_apart(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Пустая строка означает три разные вещи, и свести их к нулю значило бы соврать.

    «Неизвестно» — величина есть, но её неоткуда взять; «подтверждённый ноль» — владелец
    сказал, что величины нет (кредитов у бизнеса нет); «неприменимо» — у ИП уставного капитала
    не бывает. Первая ждёт человека и поднимает сигнал, две другие — нет.
    """
    async with async_session_factory() as session:
        assembled = await assemble(session, as_of=JULY_END)

        unknown = _line(assembled, "ap_vacation_reserve")
        assert unknown.amount is None
        assert unknown.status == cat.ST_MISSING

        confirmed_zero = _line(assembled, "debt_overdraft")
        assert confirmed_zero.amount == Decimal("0.00")
        assert confirmed_zero.status == cat.ST_ZERO_CONFIRMED

        not_applicable = _line(assembled, "equity_charter")
        assert not_applicable.amount == Decimal("0.00")
        assert not_applicable.status == cat.ST_NOT_APPLICABLE

        waiting = _flag(assembled, "manual_lines_empty")
        assert waiting is not None
        codes = {row["code"] for row in waiting.rows}
        assert "ap_vacation_reserve" in codes
        # Кредитов нет — строка не приглашает к вводу и в сигнал не попадает.
        assert "debt_overdraft" not in codes
        assert "debt_leasing" not in codes
        # Займы собственникам считает регистр, значит приглашать к вводу их нельзя: иначе
        # владелец впишет то же самое руками и сумма встанет в баланс дважды.
        assert "ar_owners" not in codes


async def test_a_wallet_outside_the_map_gets_its_own_line_instead_of_the_bank_one(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Незнакомый кошелёк не вливается молча в расчётный счёт.

    Влить значило бы спрятать деньги фонда или депозита внутри чужой строки: итог сошёлся бы,
    а обязательство напротив осталось бы без своего актива, и разница никогда бы не всплыла.
    """
    async with async_session_factory() as session:
        before = await assemble(session, as_of=JULY_END)
        bank_before = _line(before, "money_bank").amount

        stray = await make_wallet(session, name="Новый счёт", wallet_type="cash_safe")
        await _cash(session, wallet_id=stray.id, amount="30000.00", on=date(2026, 7, 5))
        await session.commit()

        after = await assemble(session, as_of=JULY_END)
        assert _line(after, cat.UNMAPPED_WALLET_LINE).amount == Decimal("30000.00")
        assert _line(after, "money_bank").amount == bank_before

        flag = _flag(after, "wallet_not_mapped")
        assert flag is not None
        assert stray.code in {row["code"] for row in flag.rows}


async def test_stock_is_empty_not_zero_when_the_iiko_snapshot_never_ran(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Нет складского снимка — строка пуста и блокирует, а не показывает ноль.

    Ноль здесь стоил бы дороже всего: склад — крупнейший оборотный актив, и его отсутствие
    молча уехало бы в капитал, установленный описью.
    """
    async with async_session_factory() as session:
        assembled = await assemble(session, as_of=JULY_END)

        stock = _line(assembled, "stock_total")
        assert stock.amount is None
        assert stock.status == cat.ST_MISSING

        flag = _flag(assembled, "stock_month_missing")
        assert flag is not None
        assert flag.severity == "blocking"


async def test_only_the_inventoried_nomenclature_lands_in_stock(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Снимок iiko приносит весь справочник, а запас на складе — это малая его часть.

    Найдено владельцем 08.08.2026 на прод-данных: строка считала все 427 позиций снимка и
    показывала 1 309 155,05 ₽ вместо 291 354,87 ₽ по 39 инвентаризируемым — завышение вчетверо.
    Остаток блюда или заготовки в iiko существует, но запасом на складе не является: складской
    учёт и результаты инвентаризации есть только у товаров из списка ревизий.
    """
    august = date(2026, 8, 1)
    async with async_session_factory() as session:
        synced = datetime(2026, 9, 1, 3, 0, tzinfo=UTC)
        await _stock_fact(session, month=august, amount="90000.00", synced_at=synced, name="Кола")
        await _stock_fact(
            session, month=august, amount="10000.00", synced_at=synced, name="Контейнер"
        )
        # Блюдо: в снимке есть, складского учёта по нему не ведут.
        await _stock_fact(
            session,
            month=august,
            amount="777000.00",
            synced_at=synced,
            name="Ролл Филадельфия",
            stocked=False,
        )
        await session.commit()

        assembled = await assemble(session, as_of=date(2026, 8, 31))
        stock = _line(assembled, "stock_total")
        assert stock.amount == Decimal("100000.00")
        # Отброшенное названо цифрой: падение строки должно читаться как снятие лишнего,
        # а не как пропажа запасов.
        assert stock.detail["positions"] == 2
        assert stock.detail["positions_in_snapshot"] == 3
        assert stock.detail["excluded_amount"] == "777000.00"


async def test_an_inventoried_position_missing_from_the_snapshot_is_reported(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Позиция под складским учётом, которой в снимке нет, занижает запасы на неизвестное.

    Отличается от намеренно отброшенной номенклатуры именно этим: там мы знаем, сколько сняли,
    здесь — нет. На проде таких две («Палочки» и «Добрый лимон-лайм ж/б»).
    """
    august = date(2026, 8, 1)
    async with async_session_factory() as session:
        await _stock_fact(
            session,
            month=august,
            amount="90000.00",
            synced_at=datetime(2026, 9, 1, 3, 0, tzinfo=UTC),
            name="Кола",
        )
        await _stocked(session, guid=str(uuid.uuid4()), name="Палочки")
        await session.commit()

        assembled = await assemble(session, as_of=date(2026, 8, 31))
        assert _line(assembled, "stock_total").amount == Decimal("90000.00")

        flag = _flag(assembled, "stock_position_absent_from_snapshot")
        assert flag is not None
        assert flag.severity == "warning"
        # Вхождение, а не точное совпадение: список ревизий засеян миграцией, и остальные его
        # позиции в этом тесте тоже без фактов — сравнение множеств проверяло бы чужой сид.
        assert "Палочки" in {row["product"] for row in flag.rows}


async def test_a_position_awaiting_the_owner_is_neither_counted_nor_hidden(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Спорная позиция в сумму не входит, но и не пропадает — о ней сказано отдельно.

    Включить её молча значило бы выдать нерешённое за решённое; выбросить молча — потерять
    запас, о котором никто уже не вспомнит.
    """
    august = date(2026, 8, 1)
    async with async_session_factory() as session:
        synced = datetime(2026, 9, 1, 3, 0, tzinfo=UTC)
        await _stock_fact(session, month=august, amount="90000.00", synced_at=synced, name="Кола")
        guid = await _stock_fact(
            session,
            month=august,
            amount="50000.00",
            synced_at=synced,
            name="Спорный товар",
            stocked=False,
        )
        await _stocked(session, guid=guid, name="Спорный товар", status="requires_owner_review")
        await session.commit()

        assembled = await assemble(session, as_of=date(2026, 8, 31))
        assert _line(assembled, "stock_total").amount == Decimal("90000.00")

        flag = _flag(assembled, "stock_position_undecided")
        assert flag is not None
        assert {row["product"] for row in flag.rows} == {"Спорный товар"}


async def test_a_stock_snapshot_taken_before_month_end_is_marked_approximate(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Прогон iiko 20 августа отдаёт остаток на 20 августа, а записывается он как августовский.

    Строка есть, цифра правдоподобна — и именно поэтому её надо пометить: доверять ей нельзя.
    """
    august = date(2026, 8, 1)
    async with async_session_factory() as session:
        await _stock_fact(
            session,
            month=august,
            amount="415000.00",
            synced_at=datetime(2026, 8, 20, 9, 0, tzinfo=UTC),
        )
        await session.commit()

        assembled = await assemble(session, as_of=date(2026, 8, 31))
        stock = _line(assembled, "stock_total")
        assert stock.amount == Decimal("415000.00")
        assert stock.status == cat.ST_APPROXIMATE

        flag = _flag(assembled, "stock_snapshot_stale")
        assert flag is not None
        assert flag.severity == "warning"


async def test_a_stock_snapshot_taken_after_the_boundary_is_trusted(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Прогон 1 сентября спрашивает у iiko состояние на конец августа — это и есть остаток."""
    august = date(2026, 8, 1)
    async with async_session_factory() as session:
        await _stock_fact(
            session,
            month=august,
            amount="415000.00",
            synced_at=datetime(2026, 9, 1, 3, 0, tzinfo=UTC),
        )
        await session.commit()

        assembled = await assemble(session, as_of=date(2026, 8, 31))
        assert _line(assembled, "stock_total").status == cat.ST_OK
        assert _flag(assembled, "stock_snapshot_stale") is None


async def test_a_hand_entered_value_fills_the_line_and_clears_the_signal(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Введённая человеком величина встаёт в строку вместе со своим основанием.

    Основание хранится не для порядка: через месяц цифра без него неотличима от опечатки.
    """
    async with async_session_factory() as session:
        session.add(
            BalanceManualValue(
                as_of=JULY_END,
                line_code="ap_vacation_reserve",
                amount=Decimal("18400.00"),
                reason="Накопленные дни отпуска по расчёту от 31.07",
                source_document="Расчёт №4",
            )
        )
        await session.commit()

        assembled = await assemble(session, as_of=JULY_END)
        line = _line(assembled, "ap_vacation_reserve")
        assert line.amount == Decimal("18400.00")
        assert line.status == cat.ST_MANUAL
        assert "Накопленные дни" in (line.source_note or "")

        waiting = _flag(assembled, "manual_lines_empty")
        still_waiting = {row["code"] for row in (waiting.rows if waiting else [])}
        assert "ap_vacation_reserve" not in still_waiting


async def test_a_hand_entered_value_belongs_to_its_date_alone(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Величина, введённая на 31 июля, не переезжает в август сама собой.

    Ручные строки — это остаток НА ДАТУ, а не постоянная настройка: накопленные дни отпуска за
    месяц меняются, и молча перенесённая цифра была бы вымыслом.
    """
    async with async_session_factory() as session:
        session.add(
            BalanceManualValue(
                as_of=JULY_END,
                line_code="ap_vacation_reserve",
                amount=Decimal("18400.00"),
                reason="Расчёт накопленных дней на 31.07",
            )
        )
        await session.commit()

        august = await assemble(session, as_of=date(2026, 8, 31))
        assert _line(august, "ap_vacation_reserve").amount is None


async def test_tax_debt_counts_what_is_accrued_not_only_what_is_overdue(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Налог, начисленный за период, — обязательство на дату, даже если срок уплаты впереди.

    Владелец 09.08.2026: «налоги 168». Строка показывала 20 053,42 при 145 595,35 — потому что
    брала только недостачу ЕНС, а та признаёт начисление по СРОКУ УПЛАТЫ. Для платёжного
    календаря это верно, для баланса — нет: зарплатный ЕНП за июль со сроком 28.08 на 31 июля
    уже начислен и уже является долгом.

    Источник теперь — функция самого налогового модуля, та же, что наполняет «Активные платежи»
    и строку налогов в учёте ДЗ/КЗ. Баланс перестал быть четвёртым местом со своей формулой.
    """
    from datetime import date as _date

    from app.services.balance import assembly as assembly_module
    from app.services.taxes import obligations as obligations_module

    class _Ob:
        def __init__(self, title, amount, due, projection):
            self.title = title
            self.amount = Decimal(amount)
            self.due_date = due
            self.is_projection = projection

    async def _fake(session, *, today):  # noqa: ARG001
        return [
            _Ob("Зарплатный ЕНП за июль", "19913.93", _date(2026, 8, 28), False),
            _Ob("Допвзнос 1% · II квартал", "105628.00", _date(2026, 9, 25), False),
            _Ob("Взносы ИП «за себя» · остаток года", "43042.50", _date(2026, 12, 28), True),
        ]

    monkeypatch.setattr(obligations_module, "list_payable_obligations", _fake)

    async with async_session_factory() as session:
        line, flag = await assembly_module._tax_payable_line(session, JULY_END)

        # Недостача ЕНС на пустой базе нулевая, поэтому итог — ровно начисленное вперёд.
        assert line.amount == Decimal("125541.93")
        assert line.detail["accrued_ahead"] == "125541.93"
        # Прогноз в долг не вошёл, но и не пропал.
        assert flag is not None
        assert flag.amount == Decimal("43042.50")
        assert flag.severity == "info"


async def test_a_broken_tax_registry_does_not_break_the_balance(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Налоговый модуль упал — баланс собирается, но говорит, что цифра неполная.

    Тот же принцип, что с отчётом о прибыли: чужая поломка не имеет права ни уронить баланс,
    ни тихо превратиться в правдоподобную цифру.
    """
    from app.services.balance import assembly as assembly_module
    from app.services.taxes import obligations as obligations_module

    async def _broken(session, *, today):  # noqa: ARG001
        raise RuntimeError("реестр обязательств недоступен")

    monkeypatch.setattr(obligations_module, "list_payable_obligations", _broken)

    async with async_session_factory() as session:
        line, flag = await assembly_module._tax_payable_line(session, JULY_END)

        assert line.status == cat.ST_APPROXIMATE
        assert "не учтено" in (line.source_note or "")
        assert flag is None
