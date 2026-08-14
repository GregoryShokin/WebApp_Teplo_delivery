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
    IikoProduct,
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
    kind: str | None = "GOODS",
    expensed: str | None = None,
) -> str:
    """Складской факт месяца плюс карточка номенклатуры iiko.

    Снимок приносит остатки по ВСЕМУ справочнику, а запасом является только товар, поэтому у
    хелпера есть ``kind``: ``GOODS`` (по умолчанию), любой другой тип — или ``None``, если
    карточки в справочнике нет вовсе. ``expensed`` помечает товар расходником в разметке ОПиУ.
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
    if kind is not None:
        session.add(IikoProduct(iiko_id=guid, name=name, type=kind, unit="шт", synced_at=synced_at))
    if expensed is not None:
        session.add(
            PnlProductWhitelist(
                iiko_product_guid=guid,
                source_kind=cat.EXPENSED_ON_PURCHASE_SOURCE,
                include_status=expensed,
                product_name=name,
            )
        )
    await session.flush()
    return guid


async def test_a_date_before_the_start_of_accounting_is_refused(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """До 01.07.2026 истории в системе нет — собирать баланс не из чего.

    Отказ, а не пустой баланс: пустой прочитали бы как «на эту дату у бизнеса ничего не было».
    """
    async with async_session_factory() as session:
        with pytest.raises(BalanceBeforeAccountingStart):
            await assemble(session, as_of=date(2026, 6, 30))


async def test_only_the_opening_capital_stays_a_plug(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """«Актив = пассив» ничего не проверяет, и ровно одна строка обязана это признавать.

    Тест закрепляет не арифметику (она тождественна), а ПОМЕТКУ: балансирующая строка должна
    читаться иначе прочих, иначе тождество примут за сошедшийся баланс. И она должна быть
    ЕДИНСТВЕННОЙ: вторая означала бы, что часть капитала опять никем не объяснена.
    """
    async with async_session_factory() as session:
        assembled = await assemble(session, as_of=JULY_END)

        assert assembled.equity_plug == assembled.asset_total - assembled.liability_total
        plugs = [item for item in assembled.lines if item.status == "plug"]
        assert [item.code for item in plugs] == ["equity_opening"]


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


async def test_only_goods_land_in_stock(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Запас — это товар. Блюдо, заготовка и модификатор в нём не участвуют.

    Найдено владельцем 08.08.2026: строка считала все 427 позиций снимка (1 309 155,05 ₽) при
    настоящих 1 219 472,81 ₽ по 257 товарам. Остаток блюда или заготовки в iiko существует, но
    запасом не является: заготовка — уже переработанный товар, блюдо — рецепт, модификатор —
    добавка к строке чека. Комбо-наборы приходят как ``DISH`` и отсекаются вместе с блюдами.
    """
    august = date(2026, 8, 1)
    async with async_session_factory() as session:
        synced = datetime(2026, 9, 1, 3, 0, tzinfo=UTC)
        await _stock_fact(session, month=august, amount="90000.00", synced_at=synced, name="Кола")
        await _stock_fact(
            session, month=august, amount="10000.00", synced_at=synced, name="Креветка"
        )
        for name, kind in (
            ("Ролл Филадельфия", "DISH"),
            ("Соус спайси заготовка", "PREPARED"),
            ("Доп. васаби", "MODIFIER"),
        ):
            await _stock_fact(
                session,
                month=august,
                amount="259000.00",
                synced_at=synced,
                name=name,
                kind=kind,
            )
        await session.commit()

        assembled = await assemble(session, as_of=date(2026, 8, 31))
        stock = _line(assembled, "stock_total")
        assert stock.amount == Decimal("100000.00")
        # Отброшенное названо цифрой: падение строки должно читаться как снятие лишнего,
        # а не как пропажа запасов.
        assert stock.detail["positions"] == 2
        assert stock.detail["positions_in_snapshot"] == 5
        assert stock.detail["excluded_amount"] == "777000.00"


async def test_the_owner_curated_review_list_no_longer_decides_what_is_stock(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Товар считается запасом, даже если в ручной список ревизий его не завели.

    Первая версия брала именно этот список и давала 291 354,87 ₽ вместо 1 219 472,81 ₽: в него
    попали упаковка и напитки — 37 позиций из 257, — а продукты просто не завели. Ручной список
    отвечает на вопрос «что владелец отметил», балансу нужен ответ на «что является запасом»,
    и первый отстаёт ровно настолько, насколько давно его не пополняли.
    """
    august = date(2026, 8, 1)
    async with async_session_factory() as session:
        await _stock_fact(
            session,
            month=august,
            amount="47503.92",
            synced_at=datetime(2026, 9, 1, 3, 0, tzinfo=UTC),
            name="Креветка блок 1,8 кг",
        )
        await session.commit()

        assert _line(await assemble(session, as_of=date(2026, 8, 31)), "stock_total").amount == (
            Decimal("47503.92")
        )


async def test_a_position_without_an_iiko_card_is_named_not_swallowed(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Нет карточки в справочнике — нет и типа: непонятно, товар это или блюдо.

    В сумму такая позиция не идёт (посчитать неизвестное товаром значило бы угадать), но и не
    пропадает молча — её называет сигнал вместе с величиной.
    """
    august = date(2026, 8, 1)
    async with async_session_factory() as session:
        synced = datetime(2026, 9, 1, 3, 0, tzinfo=UTC)
        await _stock_fact(session, month=august, amount="90000.00", synced_at=synced, name="Кола")
        await _stock_fact(
            session,
            month=august,
            amount="12345.00",
            synced_at=synced,
            name="Позиция без карточки",
            kind=None,
        )
        await session.commit()

        assembled = await assemble(session, as_of=date(2026, 8, 31))
        assert _line(assembled, "stock_total").amount == Decimal("90000.00")

        flag = _flag(assembled, "stock_position_unclassified")
        assert flag is not None
        assert flag.amount == Decimal("12345.00")
        assert {row["product"] for row in flag.rows} == {"Позиция без карточки"}


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


async def test_the_month_profit_is_shown_beside_capital_but_not_added_to_it(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """«Где в балансе за июль прибыль июля?» — первый вопрос владельца, и ответ должен быть.

    Прибыль уже сидит ВНУТРИ балансирующей строки капитала: он выведен как актив минус
    обязательства, и прибыль попала в него вместе со всем остальным. Отдельным слагаемым её
    ставить нельзя — капитал удвоился бы на её величину. Поэтому заметка: величину видно,
    в итог она не идёт.
    """
    from app.services.balance import assembly as assembly_module

    async def _profit(session, month):  # noqa: ARG001
        return Decimal("255676.18"), "ok", None

    monkeypatch.setattr(assembly_module, "month_net_profit", _profit)

    async with async_session_factory() as session:
        assembled = await assemble(session, as_of=JULY_END)

        memo = _line(assembled, "equity_profit_memo")
        assert memo.amount == Decimal("255676.18")
        assert memo.kind == cat.KIND_MEMO
        # Тождество капитала не шелохнулось: заметка в него не вошла.
        assert assembled.equity_plug == assembled.asset_total - assembled.liability_total


async def test_an_unavailable_profit_is_a_dash_beside_capital_not_a_zero(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Отчёт молчит — у прибыли месяца прочерк, а не ноль.

    Ноль прочитался бы как «месяц отработали в ноль», то есть как факт вместо его отсутствия.
    """
    from app.services.balance import assembly as assembly_module

    async def _broken(session, month):  # noqa: ARG001
        return None, "недоступен: RuntimeError: отчёт сломан", None

    monkeypatch.setattr(assembly_module, "month_net_profit", _broken)

    async with async_session_factory() as session:
        memo = _line(await assemble(session, as_of=JULY_END), "equity_profit_memo")
        assert memo.amount is None
        assert memo.status == cat.ST_MISSING


async def test_a_consumable_is_not_stock_even_though_it_is_a_good(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Моющие средства, мешки для мусора и фольга — товары, но складом не являются.

    Владелец 09.08.2026: расход по ним признаётся ПО ФАКТУ ПОКУПКИ, склад по ним не ведётся.
    Одного типа номенклатуры мало: по типу они такие же ``GOODS``, как коробка и сёмга. Разметку
    бизнес уже ведёт в ОПиУ — «вспомогательные товары» и «содержание торговых точек», — и вторую
    копию заводить нельзя: один и тот же мешок для мусора оказался бы разом и расходом месяца,
    и остатком на складе.

    На проде это 22 позиции на 367 194,75 ₽ — почти треть того, что казалось запасом.
    """
    august = date(2026, 8, 1)
    async with async_session_factory() as session:
        synced = datetime(2026, 9, 1, 3, 0, tzinfo=UTC)
        await _stock_fact(
            session, month=august, amount="130032.53", synced_at=synced, name="Коробка 30х30 бренд"
        )
        await _stock_fact(
            session, month=august, amount="47503.92", synced_at=synced, name="Креветка блок"
        )
        for name in ("Моющие и чистящие средства", "Мешок для мусора 240л", "Фольга 29см"):
            await _stock_fact(
                session,
                month=august,
                amount="50000.00",
                synced_at=synced,
                name=name,
                expensed="include",
            )
        await session.commit()

        stock = _line(await assemble(session, as_of=date(2026, 8, 31)), "stock_total")
        assert stock.amount == Decimal("177536.45")
        assert {part["name"] for part in stock.detail["parts"]} == {
            "Коробка 30х30 бренд",
            "Креветка блок",
        }


async def test_an_undecided_good_stays_in_stock_and_says_so(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Не решено, расходник это или запас — считаем запасом и говорим вслух.

    Складом вещь является по умолчанию, расходником становится решением человека. Поэтому
    умолчание в пользу запаса, но с сигналом: окажется расходником — строка завышена ровно на
    эту величину. На проде так висит «диспенсер 0,7» на 562,88 ₽.
    """
    august = date(2026, 8, 1)
    async with async_session_factory() as session:
        synced = datetime(2026, 9, 1, 3, 0, tzinfo=UTC)
        await _stock_fact(session, month=august, amount="90000.00", synced_at=synced, name="Кола")
        await _stock_fact(
            session,
            month=august,
            amount="562.88",
            synced_at=synced,
            name="диспенсер 0,7",
            expensed="requires_owner_review",
        )
        await session.commit()

        assembled = await assemble(session, as_of=date(2026, 8, 31))
        assert _line(assembled, "stock_total").amount == Decimal("90562.88")

        flag = _flag(assembled, "stock_expense_decision_pending")
        assert flag is not None
        assert flag.amount == Decimal("562.88")


async def test_capital_is_decomposed_and_only_the_opening_stays_a_plug(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Капитал разложен на слагаемые; балансирующей осталась одна строка — начало учёта.

    Владелец 09.08.2026: «не хватает вкладов от собственников, то есть вообще от чего считается
    баланс, и накопленной прибыли». Одна строка «актив минус обязательства» не отвечала ни на
    один из этих вопросов.

    Проверяется тождество разложения: сумма названных слагаемых плюс остаток равна капиталу.
    Если однажды слагаемое посчитают дважды, оно сойдётся только за счёт «начала учёта» —
    и тест это поймает.
    """
    from app.services.balance import assembly as assembly_module

    async def _profit(session, month):  # noqa: ARG001
        return Decimal("100000.00"), "ok", None

    monkeypatch.setattr(assembly_module, "month_net_profit", _profit)

    async with async_session_factory() as session:
        assembled = await assemble(session, as_of=JULY_END)

        opening = _line(assembled, "equity_opening")
        retained = _line(assembled, "equity_retained")
        memo = _line(assembled, "equity_profit_memo")

        assert retained.amount == Decimal("100000.00")
        assert memo.kind == cat.KIND_MEMO
        assert opening.status == "plug"

        named = sum(
            (item.amount or Decimal("0"))
            for item in assembled.lines
            if item.group == "Капитал" and item.kind != cat.KIND_MEMO
        )
        assert Decimal(named).quantize(Decimal("0.01")) == assembled.equity_plug


async def test_a_withdrawal_lowers_capital_instead_of_raising_it(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Дивиденды и возврат вкладов уходят со знаком минус.

    Подытог группы складывает строки как есть: положительное число здесь раздуло бы капитал
    вдвое от изъятого, и тождество сошлось бы только за счёт «начала учёта» — то есть ошибка
    спряталась бы в единственной строке, которую никто не проверяет.
    """
    async with async_session_factory() as session:
        assembled = await assemble(session, as_of=JULY_END)
        assert _line(assembled, "equity_dividends").amount <= Decimal("0")
        assert _line(assembled, "equity_returns").amount <= Decimal("0")


async def test_a_month_without_profit_is_named_not_folded_into_the_opening(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Месяц без прибыли занижает накопленную — и разница молча уехала бы в «начало учёта».

    Там её никто не ищет: строка балансирующая, любое число в ней выглядит нормально. Поэтому
    сигнал блокирующий.
    """
    from app.services.balance import assembly as assembly_module

    async def _broken(session, month):  # noqa: ARG001
        return None, "недоступен: RuntimeError: отчёт сломан", None

    monkeypatch.setattr(assembly_module, "month_net_profit", _broken)

    async with async_session_factory() as session:
        assembled = await assemble(session, as_of=JULY_END)

        assert _line(assembled, "equity_retained").amount == Decimal("0.00")
        flag = _flag(assembled, "equity_profit_month_unknown")
        assert flag is not None
        assert flag.severity == "blocking"
        assert "07.2026" in flag.rows[0]["month"]


async def test_founding_contributions_come_from_the_owner_registry(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Учредительный вклад деньгами в системе не проходил — его знает только реестр.

    Владелец 09.08.2026: «вклады собственников — 5 миллионов, каждый по 2,5». В ДДС таких
    поступлений ноль: деньги внесены до начала управленческого учёта. Без реестра вся сумма
    молча сидела бы в балансирующей строке, и баланс отвечал бы «капитал такой, потому что
    актив минус обязательства» вместо «внесли столько, заработали столько».

    Дата вклада обязательна: вклад, о котором известно «когда-то», нельзя отнести ни к одному
    срезу — и по нему нельзя сказать, был ли он на 31 июля.
    """
    from cp_helpers import make_counterparty

    from app.models import BusinessOwner
    from app.services.balance import assembly as assembly_module

    async def _profit(session, month):  # noqa: ARG001
        return Decimal("0.00"), "ok", None

    monkeypatch.setattr(assembly_module, "month_net_profit", _profit)

    async with async_session_factory() as session:
        for name in ("Григорий", "Павел"):
            owner = await make_counterparty(session, name=name)
            session.add(
                BusinessOwner(
                    counterparty_id=owner.id,
                    share_percent=Decimal("50.00"),
                    started_on=date(2026, 1, 1),
                    contribution_amount=Decimal("2500000.00"),
                    contributed_on=date(2026, 1, 1),
                )
            )
        # Вклад без даты — знание без места на оси времени, в срез не входит.
        nameless = await make_counterparty(session, name="Третий")
        session.add(
            BusinessOwner(
                counterparty_id=nameless.id,
                share_percent=Decimal("1.00"),
                started_on=date(2026, 1, 1),
                contribution_amount=Decimal("999999.00"),
                contributed_on=None,
            )
        )
        await session.commit()

        assembled = await assemble(session, as_of=JULY_END)
        contributions = _line(assembled, "equity_contributions")
        assert contributions.amount == Decimal("5000000.00")
        assert {part["name"] for part in contributions.detail["parts"]} == {"Григорий", "Павел"}

        # Тождество не нарушено: вклад ушёл из балансирующей строки в свою.
        named = sum(
            (item.amount or Decimal("0"))
            for item in assembled.lines
            if item.group == "Капитал" and item.kind != cat.KIND_MEMO
        )
        assert Decimal(named).quantize(Decimal("0.01")) == assembled.equity_plug


async def test_a_contribution_made_after_the_snapshot_is_not_in_it(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Вклад, внесённый в августе, в июльском балансе не стоит."""
    from cp_helpers import make_counterparty

    from app.models import BusinessOwner

    async with async_session_factory() as session:
        owner = await make_counterparty(session, name="Поздний вкладчик")
        session.add(
            BusinessOwner(
                counterparty_id=owner.id,
                share_percent=Decimal("10.00"),
                started_on=date(2026, 1, 1),
                contribution_amount=Decimal("1000000.00"),
                contributed_on=date(2026, 8, 15),
            )
        )
        await session.commit()

        assert _line(await assemble(session, as_of=JULY_END), "equity_contributions").amount == (
            Decimal("0.00")
        )
        assert _line(
            await assemble(session, as_of=date(2026, 8, 31)), "equity_contributions"
        ).amount == Decimal("1000000.00")


async def test_a_snapshot_past_the_data_horizon_is_called_a_forecast(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Срез на дату, до которой фактов ещё нет, — прогноз, и он обязан называться прогнозом.

    Разобрано на прод-копии 09.08.2026. Баланс на 31.08 при фактах до 08.08 показывал падение
    капитала на 92 844 ₽ при прибыли ОПиУ +190 338 ₽; разница в 283 182 ₽ выглядела дырой в
    учёте. Дыры не было: обязательства месяца в срез вошли целиком (зарплата начислена на
    полный месяц, накладные датированы 31.08, налог начислен), а выручки и расходов после
    8 августа не существует. Балансирующая строка «накопленный результат до начала учёта»
    молча впитала всю разницу — притом что обязана быть неизменной от месяца к месяцу.

    Горизонт берётся по ДЕНЕЖНЫМ регистрам: накладные датируют вперёд по договору, и по ним
    горизонт оказался бы в будущем — ровно там, где проверять нечего.
    """
    async with async_session_factory() as session:
        wallet = await make_wallet(session, name="Горизонт", wallet_type="cash_safe")
        await _cash(session, wallet_id=wallet.id, amount="1000.00", on=JULY_END)
        await session.commit()

        # Срез внутри данных — сигнала нет вовсе.
        assert _flag(await assemble(session, as_of=JULY_END), "balance_beyond_data_horizon") is None

        # Пара дней без проводок — это обычная тишина, а не прогноз: говорим, но не блокируем.
        quiet = _flag(
            await assemble(session, as_of=date(2026, 8, 2)), "balance_beyond_data_horizon"
        )
        assert quiet is not None and quiet.severity == "info"

        # Три недели без фактов — уже прогноз.
        beyond = _flag(
            await assemble(session, as_of=date(2026, 8, 31)), "balance_beyond_data_horizon"
        )
        assert beyond is not None
        assert beyond.severity == "blocking"
        assert "31.07.2026" in beyond.explanation
