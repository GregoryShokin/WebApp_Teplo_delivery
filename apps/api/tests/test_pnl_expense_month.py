"""Кассовое признание платежей без контрагента: свой месяц расхода, без «заражения статьи».

До 06.08.2026 действовало правило «по статье есть чьё-то признание → безконтрагентная касса
исключается». Оно потеряло 129 180 ₽ июля молча: признание доли коммуналки на 9 654 ₽
(перевыставление арендодателя) выбрасывало 106 281 ₽ наличных платежей за электричество,
4 000 ₽ мусорщикам и 18 899 ₽ за нейросеть. Владелец закрыл вопрос: «делай механизм
признания расходов» — платёж без контрагента и есть расход, а пересечение с признанием
подписывается пометкой, но не решается молча.

Месяц расхода: комментарии выписки («Оплата за Июнь» деньгами июля) раньше жили только в
тексте. Теперь у проводки есть ``expense_month``: деньги остаются в своём месяце с явным
вердиктом ``included_other_month`` (сверка денег замкнута), расход забирает указанный месяц.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

sys.path.append(str(Path(__file__).parent / "counterparties"))

from app.models import (
    CashflowTransaction,
    DdsArticle,
    PnlArticleRule,
    SupplierExpenseAccrual,
)
from app.services.banking.cashflow_classify import CashflowSplitLine, apply_cashflow_split
from app.services.pnl.sources import cashflow as cash_source
from app.services.pnl.types import Verdict

JULY = (date(2026, 7, 1), date(2026, 7, 31))
AUGUST = (date(2026, 8, 1), date(2026, 8, 31))
#: Июнь 2026 — период ДО начала учёта (ACCOUNTING_START = 01.07.2026). Отчёта за него нет,
#: и расход туда не «переезжает», а выбывает из периметра вовсе.
JUNE = (date(2026, 6, 1), date(2026, 6, 30))


async def _expense_article(session, *, code: str, line_code: str = "utilities_chernikova"):
    article = DdsArticle(
        code=code,
        name=f"Статья {code}",
        movement_type="outflow",
        activity_type="operating",
    )
    session.add(article)
    await session.flush()
    session.add(
        PnlArticleRule(
            article_id=article.id,
            line_code=line_code,
            in_pnl=True,
            owner_stream="cash",
            sign=1,
            applies_to="both",
            is_active=True,
        )
    )
    await session.flush()
    return article


def _cash_out(wallet_id, article_id, amount: str, day: date, **extra) -> CashflowTransaction:
    return CashflowTransaction(
        wallet_id=wallet_id,
        direction="out",
        amount=Decimal(amount),
        operation_date=day,
        article_id=article_id,
        source_kind="safe_payout",
        payment_purpose="Наличная оплата",
        quality_status="manual_override",
        **extra,
    )


def test_orphan_cash_is_expense_even_when_article_has_accruals(async_session_factory) -> None:
    """Признание доли по документу не выбрасывает чужую наличную кассу той же статьи."""
    from cp_helpers import make_counterparty, make_wallet

    async def scenario() -> None:
        async with async_session_factory() as session:
            article = await _expense_article(session, code="test_exp_month_utilities")
            wallet = await make_wallet(session, code="exp-month-w1", name="Сейф")
            landlord = await make_counterparty(
                session, name="Арендодатель-доля", inn="6155032001"
            )
            session.add(
                SupplierExpenseAccrual(
                    counterparty_id=landlord.id,
                    article_id=article.id,
                    amount=Decimal("9654.25"),
                    status="recognized",
                    service_period_start=date(2026, 7, 1),
                    service_period_end=date(2026, 7, 31),
                    recognition_month=date(2026, 7, 1),
                )
            )
            session.add(_cash_out(wallet.id, article.id, "65000.00", date(2026, 7, 19)))
            await session.commit()

            layer = await cash_source.build_cash_layer(session, *JULY)

            bucket = layer.buckets["utilities_chernikova"]
            assert bucket.amount == Decimal("65000.00")
            # Пересечение с признанием не решается молча — подписывается пометкой.
            assert bucket.cash_alongside_accrual == Decimal("65000.00")
            assert layer.by_verdict_count[Verdict.INCLUDED.value] == 1

    asyncio.run(scenario())


def test_expense_month_moves_the_expense_but_not_the_money(async_session_factory) -> None:
    """Деньги июля, расход августа: расход в августе, деньги и сверка — в июле."""
    from cp_helpers import make_wallet

    async def scenario() -> None:
        async with async_session_factory() as session:
            article = await _expense_article(session, code="test_exp_month_shift")
            wallet = await make_wallet(session, code="exp-month-w2", name="Сейф")
            session.add(
                _cash_out(
                    wallet.id,
                    article.id,
                    "30402.00",
                    date(2026, 7, 19),
                    expense_month=date(2026, 8, 1),
                )
            )
            await session.commit()

            july = await cash_source.build_cash_layer(session, *JULY)
            august = await cash_source.build_cash_layer(session, *AUGUST)

            # Июль: деньги видны и разложены (сверка замкнута), расхода в строке нет — но
            # сама строка о движении ЗНАЕТ. Без этого отчёт печатал бы «движения по статье не
            # было», хотя деньги прошли: молчание отдающего месяца было отдельной находкой
            # проверки перед выкаткой.
            assert july.by_verdict[Verdict.INCLUDED_OTHER_MONTH.value] == Decimal("30402.00")
            assert july.buckets["utilities_chernikova"].amount == Decimal("0.00")
            assert july.buckets["utilities_chernikova"].moved_out_amount == Decimal("30402.00")
            assert july.counted == 1

            # Август: расход в строке с пометкой «приехал из другого месяца денег», но в
            # денежную сверку августа проводка не входит — деньги не августовские.
            bucket = august.buckets["utilities_chernikova"]
            assert bucket.amount == Decimal("30402.00")
            assert bucket.moved_in_amount == Decimal("30402.00")
            assert bucket.moved_in_count == 1
            assert august.counted == 0
            assert august.source_count == 0

    asyncio.run(scenario())


def test_month_before_accounting_start_leaves_the_perimeter(async_session_factory) -> None:
    """Доплата за период до начала учёта: деньги в сверке, расхода нет НИГДЕ — и это верно.

    Кейс владельца 06.08.2026: 30 402 ₽ Виталию 19.07 — доплата по акту электричества за
    ИЮНЬ. Учёт ведётся с 01.07.2026, отчёта за июнь не существует, документа никто не
    выставит. Без отдельного вердикта такой платёж вечно висел бы «ждём документ».
    """
    from cp_helpers import make_counterparty, make_wallet
    from app.models import CounterpartyPayableProfile

    async def scenario() -> None:
        async with async_session_factory() as session:
            article = await _expense_article(session, code="test_exp_month_before_start")
            wallet = await make_wallet(session, code="exp-month-w5", name="Сейф")
            landlord = await make_counterparty(session, name="Арендодатель-июнь", inn="6155032004")
            profile = await session.scalar(
                select(CounterpartyPayableProfile).where(
                    CounterpartyPayableProfile.counterparty_id == landlord.id
                )
            )
            profile.service_billing_mode = "agreement"
            session.add(
                _cash_out(
                    wallet.id,
                    article.id,
                    "30402.00",
                    date(2026, 7, 19),
                    counterparty_id=landlord.id,
                    expense_month=date(2026, 6, 1),
                )
            )
            await session.commit()

            july = await cash_source.build_cash_layer(session, *JULY)

            # Деньги июля разложены — сверка замкнута.
            assert (
                july.by_verdict[Verdict.EXCLUDED_BEFORE_ACCOUNTING_START.value]
                == Decimal("30402.00")
            )
            assert july.counted == 1
            # Расхода в строке июля нет, но деньги в ней ВИДНЫ как исключённые: расшифровка
            # обязана объяснить, куда делись 30 402 ₽, иначе они выглядят пропавшими.
            bucket = july.buckets["utilities_chernikova"]
            assert bucket.amount == Decimal("0.00")
            assert bucket.excluded_amount == Decimal("30402.00")
            # И никакой тревоги «оплачено, но не признано»: ждать документ за период до
            # начала учёта не от кого.
            assert july.excluded_for_accrual == {}

            june = await cash_source.build_cash_layer(session, *JUNE)
            assert june.buckets == {}

    asyncio.run(scenario())


def test_expense_month_is_inert_on_payments_with_counterparty(async_session_factory) -> None:
    """У платежа с контрагентом месяц определяет документ — поле не действует.

    Исключение (период до начала учёта) проверяет соседний тест: там ДЗ/КЗ не работает вовсе.
    """
    from cp_helpers import make_counterparty, make_wallet
    from app.models import CounterpartyPayableProfile

    async def scenario() -> None:
        async with async_session_factory() as session:
            article = await _expense_article(session, code="test_exp_month_inert")
            wallet = await make_wallet(session, code="exp-month-w3", name="Сейф")
            supplier = await make_counterparty(session, name="Услуги-инерт", inn="6155032002")
            profile = await session.scalar(
                select(CounterpartyPayableProfile).where(
                    CounterpartyPayableProfile.counterparty_id == supplier.id
                )
            )
            profile.service_billing_mode = "per_invoice"
            session.add(
                _cash_out(
                    wallet.id,
                    article.id,
                    "5000.00",
                    date(2026, 7, 10),
                    counterparty_id=supplier.id,
                    expense_month=date(2026, 8, 1),
                )
            )
            await session.commit()

            july = await cash_source.build_cash_layer(session, *JULY)
            august = await cash_source.build_cash_layer(session, *AUGUST)

            assert (
                july.by_verdict[Verdict.EXCLUDED_ACCRUAL_COUNTERPARTY.value]
                == Decimal("5000.00")
            )
            assert "utilities_chernikova" not in august.buckets

    asyncio.run(scenario())


def test_split_writes_and_guards_expense_month(async_session_factory) -> None:
    """Разбор пишет месяц первым числом; с контрагентом — отказ, переразбор снимает месяц."""
    from cp_helpers import make_counterparty, make_wallet

    async def scenario() -> None:
        async with async_session_factory() as session:
            article = await _expense_article(session, code="test_exp_month_split")
            wallet = await make_wallet(session, code="exp-month-w4", name="Сейф")
            txn = _cash_out(wallet.id, None, "1000.00", date(2026, 7, 2))
            txn.source_kind = "manual_bank_to_safe"
            session.add(txn)
            await session.flush()

            await apply_cashflow_split(
                session,
                txn,
                splits=[
                    CashflowSplitLine(
                        article_id=article.id,
                        amount=Decimal("1000.00"),
                        expense_month=date(2026, 8, 15),
                    )
                ],
            )
            await session.commit()
            assert txn.expense_month == date(2026, 8, 1), "месяц нормализуется к первому числу"

            supplier = await make_counterparty(session, name="Гейт-месяца", inn="6155032003")
            try:
                await apply_cashflow_split(
                    session,
                    txn,
                    splits=[
                        CashflowSplitLine(
                            article_id=article.id,
                            amount=Decimal("1000.00"),
                            counterparty_id=supplier.id,
                            expense_month=date(2026, 8, 1),
                        )
                    ],
                )
            except ValueError as error:
                assert "без контрагента" in str(error)
            else:
                raise AssertionError("месяц расхода с контрагентом обязан быть отвергнут")

            # Переразбор без месяца снимает прежнюю разметку — иначе она пережила бы смену
            # статьи и тихо продолжила двигать расход.
            await apply_cashflow_split(
                session,
                txn,
                splits=[CashflowSplitLine(article_id=article.id, amount=Decimal("1000.00"))],
            )
            await session.commit()
            assert txn.expense_month is None

    asyncio.run(scenario())
