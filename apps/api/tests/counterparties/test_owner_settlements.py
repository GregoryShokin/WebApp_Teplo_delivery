"""Расчёты с собственниками на «Странице ДЗ/КЗ»: входящий остаток плюс выданный займ.

СЦЕНАРИЙ — НАСТОЯЩИЙ (владелец, 02.08.2026). На 01.07.2026 собственники должны бизнесу:
Григорий 1 020 000 ₽, Павел 200 000 ₽. Четырнадцатого июля Павлу выдан займ ещё на 30 000 ₽
банковским переводом, и в проводке не был указан контрагент — механизма собственников тогда не
существовало. После указания контрагента у Павла обязано стать 230 000 ₽, у Григория —
1 020 000 ₽, и обе суммы обязаны быть видны на «Странице ДЗ/КЗ».

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ ПО СУЩЕСТВУ:

* направление долга. В наших книгах это ДЕБИТОРКА: деньги ушли из бизнеса, и должны нам. Знак
  здесь легко перепутать — владелец называет тот же факт «кредиторской задолженностью перед
  бизнесом», глядя со стороны собственника, — а перепутанный знак меняет баланс на два долга;
* займ становится долгом САМ, простым указанием контрагента: правило 1 канона превращает
  свободный платёж в дебиторку. Никакой отдельной кнопки «оформить займ» для этого не нужно;
* входящий остаток НЕ создаёт проводки: деньги ушли до внедрения системы, и вторая проводка
  задвоила бы расход в ДДС.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, timedelta
from decimal import Decimal

from cp_helpers import make_counterparty
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    BusinessOwner,
    CashflowTransaction,
    CounterpartyRole,
    DdsArticle,
    SupplierPrepayment,
    Wallet,
)
from app.services import clock, supplier_prepayments
from app.services.counterparty_settlement_ledger import build_ledger, list_gaps
from app.services.owner_analytics import DIVIDENDS_ARTICLE_CODE, OWNER_ROLE

HEADERS = {"X-User-Role": "admin"}

# Числа владельца на 01.07.2026.
OPENING = {"Григорий": Decimal("1020000.00"), "Павел": Decimal("200000.00")}
LOAN_AMOUNT = Decimal("30000.00")


def _run(coro):
    return asyncio.run(coro)


async def _owner(session: AsyncSession, name: str, opening: Decimal) -> uuid.UUID:
    person = await make_counterparty(
        session, name=name, inn=None, cp_type="individual", relationship="informal"
    )
    session.add(CounterpartyRole(counterparty_id=person.id, role=OWNER_ROLE))
    session.add(
        BusinessOwner(
            counterparty_id=person.id,
            share_percent=Decimal("50"),
            started_on=date(2026, 1, 1),
        )
    )
    await session.flush()
    await supplier_prepayments.create_opening_prepayment(
        session,
        counterparty_id=person.id,
        amount=opening,
        kind="owner_loan",
        note="Входящий остаток на 01.07.2026",
    )
    return person.id


def _seed(factory: async_sessionmaker[AsyncSession]) -> dict[str, str]:
    async def go() -> dict[str, str]:
        async with factory() as session:
            ids = {name: await _owner(session, name, amount) for name, amount in OPENING.items()}
            wallet = Wallet(code="owner_loan_bank", name="Т-Банк", type="bank_account")
            article = DdsArticle(
                code="owner_loan_issue",
                name="Выдача кредитов и займов",
                movement_type="outflow",
                activity_type="investing",
                # Как на проде после 0249: статья займа обязана называть собственника.
                owner_required=True,
            )
            session.add_all([wallet, article])
            await session.flush()
            # Ровно та проводка, что лежит на проде: банковская выписка, контрагент не указан.
            txn = CashflowTransaction(
                wallet_id=wallet.id,
                direction="out",
                amount=LOAN_AMOUNT,
                operation_date=date(2026, 7, 14),
                article_id=article.id,
                source_kind="bank_operation",
                payment_purpose="Перевод собственных средств на карту ИП! З Павел",
                quality_status="owner_review",
            )
            session.add(txn)
            await session.commit()
            return {
                "pavel": str(ids["Павел"]),
                "grigoriy": str(ids["Григорий"]),
                "txn": str(txn.id),
                "article": str(article.id),
            }

    return _run(go())


def _receivable(client: TestClient, counterparty_id: str) -> Decimal:
    response = client.get("/api/v1/accounting/suppliers/balances", headers=HEADERS)
    assert response.status_code == 200, response.text
    for item in response.json()["items"]:
        if item["counterparty_id"] == counterparty_id:
            return Decimal(str(item["receivable"]))
    return Decimal("0")


def test_owner_debt_shows_up_and_grows_with_the_loan(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Входящий остаток виден сразу; указание контрагента на займе добавляет 30 000 Павлу."""
    seeded = _seed(async_session_factory)

    assert _receivable(client, seeded["grigoriy"]) == Decimal("1020000.00")
    assert _receivable(client, seeded["pavel"]) == Decimal("200000.00")

    response = client.patch(
        f"/api/v1/dds/transactions/{seeded['txn']}",
        json={"article_id": seeded["article"], "counterparty_id": seeded["pavel"]},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text

    # 200 000 входящего остатка + 30 000 июльского займа. Долг вырос сам: правило 1 превращает
    # свободный платёж контрагенту в дебиторку, отдельной кнопки «оформить займ» не нужно.
    assert _receivable(client, seeded["pavel"]) == Decimal("230000.00")
    assert _receivable(client, seeded["grigoriy"]) == Decimal("1020000.00")


def test_owner_settlements_never_wait_for_a_document(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Расчёты с собственником не стоят в очереди признания расходов.

    Долг собственника — не услуга: документа по нему не будет никогда, расходом он не станет ни
    при каких условиях, и «признать за период» система такой строке всё равно не даст. А очередь
    по умолчанию ЖДЁТ документ, и на проде 03.08.2026 три строки собственников (1 020 000 +
    200 000 входящих остатков и 30 000 июльского займа) дали 1,25 млн ₽ из 1,4 млн ₽ плитки
    «Ждём документ»: экран читался как долг перед поставщиками, которого нет.

    Проверяется и то, что долг НЕ ПОТЕРЯЛСЯ: из ДЗ он никуда не делся, ушёл только из очереди.
    """
    seeded = _seed(async_session_factory)
    response = client.patch(
        f"/api/v1/dds/transactions/{seeded['txn']}",
        json={"article_id": seeded["article"], "counterparty_id": seeded["pavel"]},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text

    queue = client.get("/api/v1/accounting/suppliers", headers=HEADERS)
    assert queue.status_code == 200, queue.text
    payload = queue.json()
    owners = {seeded["pavel"], seeded["grigoriy"]}
    stuck = [item for item in payload["items"] if item["counterparty_id"] in owners]
    assert stuck == [], "расчёты с собственником стоят в очереди признания расходов"
    assert payload["waiting_document"]["amount"] == 0
    assert payload["needs_period"]["amount"] == 0

    # Деньги при этом на месте: 230 000 у Павла и 1 020 000 у Григория — в дебиторке.
    assert _receivable(client, seeded["pavel"]) == Decimal("230000.00")
    assert _receivable(client, seeded["grigoriy"]) == Decimal("1020000.00")


def test_opening_balance_does_not_create_a_cashflow(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Входящий остаток — не платёж. Проводки под него нет и быть не должно.

    Деньги ушли до внедрения системы. Заведи под остаток проводку — и расход в ДДС задвоится:
    один раз исторически, второй раз сегодняшним числом.
    """
    seeded = _seed(async_session_factory)

    async def check() -> tuple[int, bool]:
        async with async_session_factory() as session:
            rows = (
                await session.scalars(
                    select(CashflowTransaction).where(
                        CashflowTransaction.counterparty_id == uuid.UUID(seeded["grigoriy"])
                    )
                )
            ).all()
            prepayment = await session.scalar(
                select(supplier_prepayments.SupplierPrepayment).where(
                    supplier_prepayments.SupplierPrepayment.counterparty_id
                    == uuid.UUID(seeded["grigoriy"])
                )
            )
            return len(rows), bool(prepayment and prepayment.opening)

    transactions, is_opening = _run(check())
    assert transactions == 0, "входящий остаток не двигает деньги"
    assert is_opening, "остаток обязан быть помечен входящим — иначе сверка сочтёт его платежом"


DIVIDENDS = Decimal("50000.00")
REPAIR = Decimal("12000.00")


def test_owner_settlements_are_not_gaps_in_the_ledger(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Сверка с контрагентом и сводка разрывов не ждут документ от собственника.

    НАХОДКА 25.09.2026 НА КОПИИ ПРОДА. Очередь признания строки собственников пропускала, а
    сверка — нет: в карточке Григория входящий остаток 1 020 000 ₽ краснел «документа нет · 15 дн.,
    срок 10.09», у Павла так же 200 000 ₽ и июльский заём 30 000 ₽ («46 дн.»), и все три стояли
    в сводке разрывов — 1,25 млн из 1,42 млн ₽. А выданные 19.08 из Сейфа дивиденды по 50 000 ₽
    своей предоплаты не имеют вовсе: очередь их не видит, а сверка «ждала документ» до 10.10 и
    покраснела бы с 11.10.

    Проверяется признак, а не только эти строки:

    * все три вида — заём, входящий остаток, дивиденды без предоплаты — без срока и без разрыва;
    * отбор по СТАТЬЕ: ремонт, оплаченный тому же Григорию, ждёт документ как у всех и остаётся
      в сводке. Отбор по контрагенту спрятал бы настоящую услугу вместе с займом;
    * очередь и сверка отвечают одинаково на одних и тех же деньгах;
    * меняется только ожидание — бегущий остаток прежний: долг собственника живёт в ДЗ;
    * кроме дивидендов: решение владельца 25.09.2026 — это выплата, а не долг, и остаток
      собственника они не двигают (иначе сверка Григория 1 070 000 ₽ против 1 020 000 ₽ в
      «Остатках»).
    """
    seeded = _seed(async_session_factory)
    response = client.patch(
        f"/api/v1/dds/transactions/{seeded['txn']}",
        json={"article_id": seeded["article"], "counterparty_id": seeded["pavel"]},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    grigoriy = uuid.UUID(seeded["grigoriy"])
    pavel = uuid.UUID(seeded["pavel"])

    async def add_payments() -> uuid.UUID:
        async with async_session_factory() as session:
            wallet = await session.scalar(select(Wallet).where(Wallet.code == "owner_loan_bank"))
            assert wallet is not None
            # Статья из каталога (0114/0247) — по её коду сверка узнаёт выплату дивидендов.
            dividends = await session.scalar(
                select(DdsArticle).where(DdsArticle.code == DIVIDENDS_ARTICLE_CODE)
            )
            assert dividends is not None and dividends.owner_required
            repair = DdsArticle(
                code="owner_side_repair",
                name="Ремонт",
                movement_type="outflow",
                activity_type="operating",
            )
            session.add(repair)
            await session.flush()
            # Как на проде: выдача дивидендов из Сейфа — проводка без предоплаты.
            for owner in (grigoriy, pavel):
                session.add(
                    CashflowTransaction(
                        wallet_id=wallet.id,
                        counterparty_id=owner,
                        direction="out",
                        amount=DIVIDENDS,
                        operation_date=date(2026, 8, 19),
                        article_id=dividends.id,
                        source_kind="manual",
                        quality_status="auto",
                    )
                )
            # Собственник как подрядчик: обычная услуга, документ по ней ждут.
            repair_tx = CashflowTransaction(
                wallet_id=wallet.id,
                counterparty_id=grigoriy,
                direction="out",
                amount=REPAIR,
                operation_date=date(2026, 8, 5),
                article_id=repair.id,
                source_kind="manual",
                quality_status="auto",
            )
            session.add(repair_tx)
            await session.flush()
            repair_prepayment = SupplierPrepayment(
                counterparty_id=grigoriy,
                kind="subscription",
                wallet_id=wallet.id,
                article_id=repair.id,
                amount=REPAIR,
                amount_settled=Decimal("0"),
                status="open",
                cashflow_transaction_id=repair_tx.id,
                service_period_status="missing",
            )
            session.add(repair_prepayment)
            await session.commit()
            return repair_prepayment.id

    repair_prepayment_id = _run(add_payments())

    # День, когда любой платёж из сида без документа уже просрочен: и заём 14.07, и дивиденды
    # 19.08, и входящие остатки, заведённые сегодня. Строкам собственника это не должно быть видно.
    far_today = clock.moscow_today() + timedelta(days=120)

    async def ledgers_and_gaps():
        async with async_session_factory() as session:
            return (
                await build_ledger(session, grigoriy, today=far_today),
                await build_ledger(session, pavel, today=far_today),
                await list_gaps(session, today=far_today),
            )

    grigoriy_ledger, pavel_ledger, gaps = _run(ledgers_and_gaps())

    owner_rows = [
        row
        for row in (*grigoriy_ledger.rows, *pavel_ledger.rows)
        if row.kind in ("payment", "payout") and row.amount != REPAIR
    ]
    # Григорий: остаток + дивиденды; Павел: остаток + заём + дивиденды.
    assert sorted(row.amount for row in owner_rows) == sorted(
        [OPENING["Григорий"], DIVIDENDS, OPENING["Павел"], LOAN_AMOUNT, DIVIDENDS]
    )
    for row in owner_rows:
        assert row.owner_settlement, row.title
        assert row.status == "ok", row.title
        assert row.expected_by is None, row.title
        assert row.days_overdue == 0, row.title
        assert row.uncovered == Decimal("0"), row.title
    assert pavel_ledger.overdue_amount == Decimal("0")
    assert all(not month.has_overdue and month.gap == 0 for month in pavel_ledger.months)

    # Ремонт у того же Григория ждёт документ как обычная услуга.
    (repair_row,) = [row for row in grigoriy_ledger.rows if row.amount == REPAIR]
    assert not repair_row.owner_settlement
    assert repair_row.status == "overdue"
    assert grigoriy_ledger.overdue_amount == REPAIR
    assert [(gap.counterparty_id, gap.amount) for gap in gaps] == [(grigoriy, REPAIR)]

    # Остаток — долг собственника: ожидание документа его не касается. Дивиденды в него не
    # входят — решение владельца 25.09.2026: это выплата, а не долг (строка ``payout``).
    assert grigoriy_ledger.closing_balance == OPENING["Григорий"] + REPAIR
    assert pavel_ledger.closing_balance == OPENING["Павел"] + LOAN_AMOUNT
    payouts = [row for row in (*grigoriy_ledger.rows, *pavel_ledger.rows) if row.kind == "payout"]
    assert [row.amount for row in payouts] == [DIVIDENDS, DIVIDENDS]
    assert all(not row.binds for row in payouts)

    # Очередь признания отвечает так же: из денег собственников в ней только ремонт.
    queue = client.get("/api/v1/accounting/suppliers", headers=HEADERS)
    assert queue.status_code == 200, queue.text
    owners = {seeded["pavel"], seeded["grigoriy"]}
    assert [item["id"] for item in queue.json()["items"] if item["counterparty_id"] in owners] == [
        str(repair_prepayment_id)
    ]

    # И экран получает признак, по которому подписывает строку «документа не будет».
    response = client.get(f"/api/v1/accounting/suppliers/{seeded['pavel']}/ledger", headers=HEADERS)
    assert response.status_code == 200, response.text
    rows = response.json()["rows"]
    assert rows and all(
        row["owner_settlement"] and row["status"] == "ok" and row["expected_by"] is None
        for row in rows
    )
