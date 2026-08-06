"""Тревогу гасит только признание, за которым НЕ стоят свои деньги.

Сверка «оплачено, но расход не признан» вычитала из непризнанной кассы ВСЁ признание пары
«контрагент × строка». Но признание, порождённое платежом со следом в ДЗ/КЗ, в эту кассу не
входит вовсе (``ledger_known``) — и, вычитаясь, тратилось дважды. Та же ошибка, что когда-то
была со щитом по контрагенту, только на шаг глубже.

Цена на данных июля 2026: у Станислава Юрьевича вода за ИЮНЬ (9 879 ₽ наличными, документа
нет) гасилась актом за ИЮЛЬ на 9 654,25 ₽, у которого свой платёж есть. Разрыв выходил
224,75 ₽ — ниже порога существенности, и отчёт молчал.

Хуже молчания было то, что отчёт САМ СОВЕТОВАЛ привязать этот платёж к контрагенту.
Владелец выполнял совет, строка коммуналки падала на 9 879 ₽, а список предупреждений не
менялся ни на букву: совет отчёта тихо понижал расход. Это и проверяется здесь — до и после
привязки, одними и теми же данными.
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
    CounterpartyPayableProfile,
    DdsArticle,
    InvoicePaymentAllocation,
    PnlArticleRule,
    SupplierExpenseAccrual,
)
from app.services.pnl import projector
from app.services.pnl.sources import cashflow as cash_source

MONTH_START = date(2026, 7, 1)
MONTH_END = date(2026, 7, 31)
LINE = "utilities_chernikova"


async def _service_counterparty(session, *, name: str, inn: str):
    from cp_helpers import make_counterparty

    counterparty = await make_counterparty(session, name=name, inn=inn)
    profile = await session.scalar(
        select(CounterpartyPayableProfile).where(
            CounterpartyPayableProfile.counterparty_id == counterparty.id
        )
    )
    profile.service_billing_mode = "agreement"
    await session.flush()
    return counterparty


async def _article(session, *, code: str) -> DdsArticle:
    article = DdsArticle(
        code=code, name=f"Статья {code}", movement_type="outflow", activity_type="operating"
    )
    session.add(article)
    await session.flush()
    session.add(
        PnlArticleRule(
            article_id=article.id,
            line_code=LINE,
            in_pnl=True,
            owner_stream="cash",
            sign=1,
            applies_to="both",
            is_active=True,
        )
    )
    await session.flush()
    return article


def _line_amount(report) -> Decimal | None:
    """Сумма строки коммуналки в собранном отчёте."""
    for line in report.lines:
        if line.code == LINE:
            return line.amount
    return None


def _payment(wallet_id, article_id, counterparty_id, amount: str, day: int):
    return CashflowTransaction(
        wallet_id=wallet_id,
        direction="out",
        amount=Decimal(amount),
        operation_date=date(2026, 7, day),
        article_id=article_id,
        counterparty_id=counterparty_id,
        source_kind="safe_payout",
        payment_purpose="Оплата коммунальных услуг",
        quality_status="manual_override",
    )


def test_recognition_backed_by_its_own_money_does_not_silence_another_payment(
    async_session_factory,
) -> None:
    """Акт июля со своим платежом не гасит тревогу по июньской воде без документа."""
    from cp_helpers import make_invoice, make_wallet

    async def scenario() -> None:
        async with async_session_factory() as session:
            landlord = await _service_counterparty(
                session, name="Станислав Юрьевич", inn="6155037001"
            )
            article = await _article(session, code="test_backed_utilities")
            wallet = await make_wallet(session, code="backed-w1", name="Сейф")

            # Июльская вода: платёж со следом в ДЗ/КЗ + признание по нему же.
            july_water = _payment(wallet.id, article.id, landlord.id, "9654.25", day=31)
            session.add(july_water)
            await session.flush()
            bill = await make_invoice(
                session,
                counterparty_id=landlord.id,
                amount="9654.25",
                doc_kind="bill",
                number="ВОДА-07",
                payment_status="paid",
                invoice_date=date(2026, 7, 31),
            )
            session.add(
                InvoicePaymentAllocation(
                    invoice_id=bill.id,
                    source_kind="cash",
                    cashflow_transaction_id=july_water.id,
                    amount=Decimal("9654.25"),
                )
            )
            session.add(
                SupplierExpenseAccrual(
                    counterparty_id=landlord.id,
                    article_id=article.id,
                    amount=Decimal("9654.25"),
                    status="recognized",
                    service_period_start=date(2026, 7, 1),
                    service_period_end=date(2026, 7, 31),
                    recognition_month=MONTH_START,
                )
            )

            # Июньская вода: наличные без контрагента — отчёт просит его привязать.
            june_water = _payment(wallet.id, article.id, None, "9879.00", day=8)
            session.add(june_water)
            await session.commit()

            before_amount = _line_amount(
                await projector.build_report(session, MONTH_START)
            )

            # Владелец выполняет совет отчёта.
            june_water.counterparty_id = landlord.id
            await session.commit()

            after = await projector.build_report(session, MONTH_START)
            after_amount = _line_amount(after)

            # Расход строки упал ровно на привязанный платёж — он ушёл в контур ДЗ/КЗ.
            assert before_amount is not None and after_amount is not None
            assert before_amount - after_amount == Decimal("9879.00")

            # И это падение обязано быть НАЗВАННЫМ: занижение в этом модуле не бывает молчаливым.
            codes = {warning.code for warning in after.warnings}
            assert "excluded_without_accrual" in codes, (
                "совет отчёта понизил расход на 9 879 ₽ и не сказал об этом ни слова"
            )
            alarm = next(w for w in after.warnings if w.code == "excluded_without_accrual")
            assert "9 879" in alarm.message.replace(" ", " "), (
                f"тревога назвала не ту сумму: {alarm.message}"
            )

    asyncio.run(scenario())


def test_recognition_without_own_money_still_silences_the_alarm(async_session_factory) -> None:
    """Обратная сторона: обычное признание своей же кассы тревогу гасит, как и раньше.

    Иначе правка превратила бы каждый нормальный «оплатили — получили документ» в красную
    строку, а красное перестают читать.
    """
    from cp_helpers import make_wallet

    async def scenario() -> None:
        async with async_session_factory() as session:
            supplier = await _service_counterparty(session, name="Обычный", inn="6155037002")
            article = await _article(session, code="test_backed_plain")
            wallet = await make_wallet(session, code="backed-w2", name="Сейф")
            session.add(_payment(wallet.id, article.id, supplier.id, "12000.00", day=10))
            session.add(
                SupplierExpenseAccrual(
                    counterparty_id=supplier.id,
                    article_id=article.id,
                    amount=Decimal("12000.00"),
                    status="recognized",
                    service_period_start=date(2026, 7, 1),
                    service_period_end=date(2026, 7, 31),
                    recognition_month=MONTH_START,
                )
            )
            await session.commit()

            layer = await cash_source.build_cash_layer(session, MONTH_START, MONTH_END)
            assert layer.excluded_for_accrual == {(supplier.id, LINE): Decimal("12000.00")}

            report = await projector.build_report(session, MONTH_START)
            assert "excluded_without_accrual" not in {w.code for w in report.warnings}

    asyncio.run(scenario())
