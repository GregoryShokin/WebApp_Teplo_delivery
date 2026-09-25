"""«Запомнить» держит одно правило на пару «личность + направление», а не на одну личность.

С 01.08 «запомнить» искало существующее правило по одному ИНН и переписывало ему статью. Поставщик,
которому мы платим, иногда и возвращает деньги: входящий возврат с «Запомнить» перезаписывал его
ИСХОДЯЩЕЕ правило, и следующие оплаты этому поставщику размечались входящей статьёй. На проде
случилось зеркально: 22.09 входящий перевод с ИНН 890307589201 завёл правило «Поступление —
перевод», а через шесть секунд исходящий переписал ему статью на «Выбытие».

Тем же болела карт-ветка: мерчант один у покупки и у её возврата («Оплата в OZON» / «Возврат
средств по операции оплаты OZON»), а совпавшему правилу ещё и переворачивали направление.

И решение владельца 25.09: правило со статьёй возврата переплаты не заводится ни одной дверью —
авторазметка гасила бы авансы фоном, мимо сторожа задвоенного возврата.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from cp_helpers import (
    admin_headers,
    make_account,
    make_bank_operation,
    make_counterparty,
    make_expense_article,
    make_wallet,
)
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    BankOperation,
    CashflowTransaction,
    ClassificationRule,
    CounterpartyPayableProfile,
    DdsArticle,
    ReconciliationCase,
)
from app.services.banking.classifier import run_classification_rules
from app.services.refund_twins import REFUND_RULE_REFUSAL
from app.services.supplier_prepayments import SUPPLIER_REFUND_ARTICLE_CODE

pytestmark = pytest.mark.usefixtures("migrated_db")

SUPPLIER_INN = "7701234567"
ACQUIRER_INN = "7710140679"


def _admin(factory) -> dict[str, str]:
    return asyncio.run(admin_headers(factory))


async def _income_article(session: AsyncSession, *, code: str, name: str) -> DdsArticle:
    article = DdsArticle(code=code, name=name, movement_type="inflow", activity_type="operating")
    session.add(article)
    await session.flush()
    return article


def _seed(factory: async_sessionmaker[AsyncSession]) -> dict[str, uuid.UUID]:
    async def _run() -> dict[str, uuid.UUID]:
        async with factory() as session:
            account = await make_account(session)
            await make_wallet(session, wallet_type="bank", account_id=account.id)
            expense = await make_expense_article(session, code="produkty", name="Продукты")
            income = await _income_article(
                session, code="test_rule_dir_income", name="Поступления от поставщика"
            )
            refund = await make_expense_article(
                session, code=SUPPLIER_REFUND_ARTICLE_CODE, name="Возврат переплаты"
            )
            supplier = await make_counterparty(session, name="ООО Поставщик", inn=SUPPLIER_INN)
            await session.commit()
            return {
                "account": account.id,
                "expense": expense.id,
                "income": income.id,
                "refund": refund.id,
                "supplier": supplier.id,
            }

    return asyncio.run(_run())


def _new_operation(
    factory: async_sessionmaker[AsyncSession],
    *,
    account_id: uuid.UUID,
    direction: str,
    amount: str,
    purpose: str,
    inn: str = SUPPLIER_INN,
    name: str = "ООО ПОСТАВЩИК",
) -> uuid.UUID:
    async def _run() -> uuid.UUID:
        async with factory() as session:
            operation = await make_bank_operation(
                session,
                amount=amount,
                direction=direction,
                inn=inn,
                name=name,
                account_id=account_id,
            )
            operation.payment_purpose = purpose
            await session.commit()
            return operation.id

    return asyncio.run(_run())


def _classify(
    client: TestClient,
    headers: dict[str, str],
    operation_id: uuid.UUID,
    *,
    article_id: uuid.UUID,
    amount: str,
    counterparty_id: uuid.UUID | None,
    remember: bool = True,
) -> dict[str, object]:
    response = client.post(
        f"/api/v1/dds/operations/{operation_id}/classify",
        headers=headers,
        json={
            "action": "split",
            "splits": [
                {
                    "article_id": str(article_id),
                    "amount": amount,
                    "counterparty_id": str(counterparty_id) if counterparty_id else None,
                }
            ],
            "remember_as_rule": remember,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _auto_classify(
    factory: async_sessionmaker[AsyncSession], operation_id: uuid.UUID
) -> tuple[str, uuid.UUID | None]:
    """Прогнать классификатор, как это делает фоновый приём выписки: статус и статья."""

    async def _run() -> tuple[str, uuid.UUID | None]:
        async with factory() as session:
            operation = await session.get(BankOperation, operation_id)
            await run_classification_rules(session, [operation])
            await session.commit()
            tx = await session.scalar(
                select(CashflowTransaction).where(
                    CashflowTransaction.source_kind == "bank_operation",
                    CashflowTransaction.source_id == operation_id,
                )
            )
            return operation.classification_status, tx.article_id if tx else None

    return asyncio.run(_run())


def _rules(factory: async_sessionmaker[AsyncSession], **where: object) -> list[ClassificationRule]:
    async def _run() -> list[ClassificationRule]:
        async with factory() as session:
            query = select(ClassificationRule)
            for column, value in where.items():
                query = query.where(getattr(ClassificationRule, column) == value)
            return list((await session.scalars(query)).all())

    return asyncio.run(_run())


def test_incoming_remember_keeps_the_suppliers_outgoing_rule(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Сценарий бага: оплату поставщику запомнили, потом запомнили его входящий платёж.

    Исходящее правило обязано остаться нетронутым — и следующая оплата этому поставщику
    размечается прежней статьёй, а не входящей."""
    ids = _seed(async_session_factory)
    headers = _admin(async_session_factory)

    payment = _new_operation(
        async_session_factory,
        account_id=ids["account"],
        direction="out",
        amount="12000.00",
        purpose="Оплата по счету 15 за продукты",
    )
    first = _classify(
        client,
        headers,
        payment,
        article_id=ids["expense"],
        amount="12000.00",
        counterparty_id=ids["supplier"],
    )
    outgoing_rule_id = uuid.UUID(str(first["rule_id"]))

    incoming = _new_operation(
        async_session_factory,
        account_id=ids["account"],
        direction="in",
        amount="700.00",
        purpose="Возврат излишне уплаченных средств",
    )
    second = _classify(
        client,
        headers,
        incoming,
        article_id=ids["income"],
        amount="700.00",
        counterparty_id=ids["supplier"],
    )
    assert second["rule_warning"] is None
    incoming_rule_id = uuid.UUID(str(second["rule_id"]))
    assert incoming_rule_id != outgoing_rule_id

    rules = {
        rule.id: rule for rule in _rules(async_session_factory, counterparty_inn_match=SUPPLIER_INN)
    }
    assert len(rules) == 2
    assert rules[outgoing_rule_id].direction == "out"
    assert rules[outgoing_rule_id].article_id == ids["expense"]
    assert rules[incoming_rule_id].direction == "in"
    assert rules[incoming_rule_id].article_id == ids["income"]

    next_payment = _new_operation(
        async_session_factory,
        account_id=ids["account"],
        direction="out",
        amount="9000.00",
        purpose="Оплата по счету 16 за продукты",
    )
    assert _auto_classify(async_session_factory, next_payment) == ("classified", ids["expense"])
    next_incoming = _new_operation(
        async_session_factory,
        account_id=ids["account"],
        direction="in",
        amount="300.00",
        purpose="Возврат излишне уплаченных средств по счету 16",
    )
    assert _auto_classify(async_session_factory, next_incoming) == ("classified", ids["income"])


def test_outgoing_remember_keeps_the_incoming_rule(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Прод 22.09 (ИНН 890307589201): входящий перевод запомнили, через шесть секунд — зеркальный
    исходящий. Раньше исходящий переписывал статью входящему правилу, и следующий приход с этого
    ИНН размечался бы расходной статьёй."""
    ids = _seed(async_session_factory)
    headers = _admin(async_session_factory)

    incoming = _new_operation(
        async_session_factory,
        account_id=ids["account"],
        direction="in",
        amount="450000.00",
        purpose="Перевод собственных средств",
    )
    first = _classify(
        client,
        headers,
        incoming,
        article_id=ids["income"],
        amount="450000.00",
        counterparty_id=None,
    )
    outgoing = _new_operation(
        async_session_factory,
        account_id=ids["account"],
        direction="out",
        amount="450000.00",
        purpose="Перевод собственных средств",
    )
    _classify(
        client,
        headers,
        outgoing,
        article_id=ids["expense"],
        amount="450000.00",
        counterparty_id=None,
    )

    incoming_rule = next(
        rule
        for rule in _rules(async_session_factory, counterparty_inn_match=SUPPLIER_INN)
        if rule.id == uuid.UUID(str(first["rule_id"]))
    )
    assert incoming_rule.direction == "in"
    assert incoming_rule.article_id == ids["income"]

    next_incoming = _new_operation(
        async_session_factory,
        account_id=ids["account"],
        direction="in",
        amount="520000.00",
        purpose="Перевод собственных средств",
    )
    assert _auto_classify(async_session_factory, next_incoming) == ("classified", ids["income"])


def test_remember_narrows_an_undirected_inn_rule_instead_of_overwriting_it(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Правило без направления (из настроек ДДС) ловит и оплаты, и приходы. «Запомнить» на
    приходе не переписывает его, а сужает до списаний и заводит приходам своё правило — с
    приоритетом не ниже суженного, иначе пере-решение уступило бы третьему правилу."""
    ids = _seed(async_session_factory)
    headers = _admin(async_session_factory)

    async def _undirected() -> uuid.UUID:
        async with async_session_factory() as session:
            rule = ClassificationRule(
                name="Поставщик из настроек",
                priority=10,
                is_active=True,
                counterparty_inn_match=SUPPLIER_INN,
                action="set_article",
                article_id=ids["expense"],
                counterparty_id=ids["supplier"],
            )
            session.add(rule)
            await session.commit()
            return rule.id

    undirected_id = asyncio.run(_undirected())
    incoming = _new_operation(
        async_session_factory,
        account_id=ids["account"],
        direction="in",
        amount="700.00",
        purpose="Возврат излишне уплаченных средств",
    )
    answer = _classify(
        client, headers, incoming, article_id=ids["income"], amount="700.00", counterparty_id=None
    )
    assert uuid.UUID(str(answer["rule_id"])) != undirected_id

    rules = {
        rule.id: rule for rule in _rules(async_session_factory, counterparty_inn_match=SUPPLIER_INN)
    }
    assert rules[undirected_id].direction == "out"
    assert rules[undirected_id].article_id == ids["expense"]
    incoming_rule = rules[uuid.UUID(str(answer["rule_id"]))]
    assert incoming_rule.direction == "in"
    assert incoming_rule.article_id == ids["income"]
    assert incoming_rule.priority == 10

    next_payment = _new_operation(
        async_session_factory,
        account_id=ids["account"],
        direction="out",
        amount="9000.00",
        purpose="Оплата по счету 16 за продукты",
    )
    assert _auto_classify(async_session_factory, next_payment) == ("classified", ids["expense"])
    next_incoming = _new_operation(
        async_session_factory,
        account_id=ids["account"],
        direction="in",
        amount="300.00",
        purpose="Возврат излишне уплаченных средств по счету 16",
    )
    assert _auto_classify(async_session_factory, next_incoming) == ("classified", ids["income"])


def test_card_refund_remember_keeps_the_merchant_purchase_rule(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Карт-ветка: мерчант «OZON» один у покупки и у её возврата. Запомненный возврат не
    переворачивает правило покупок на поступления и не переписывает ему статью."""
    ids = _seed(async_session_factory)
    headers = _admin(async_session_factory)
    card = {"inn": ACQUIRER_INN, "name": 'АО "ТБанк"'}

    purchase = _new_operation(
        async_session_factory,
        account_id=ids["account"],
        direction="out",
        amount="5972.00",
        purpose="Оплата в OZON Moskva RUS",
        **card,
    )
    first = _classify(
        client,
        headers,
        purchase,
        article_id=ids["expense"],
        amount="5972.00",
        counterparty_id=ids["supplier"],
    )
    purchase_rule_id = uuid.UUID(str(first["rule_id"]))

    refund = _new_operation(
        async_session_factory,
        account_id=ids["account"],
        direction="in",
        amount="1200.00",
        purpose="Возврат средств по операции оплаты OZON Moskva RUS",
        **card,
    )
    second = _classify(
        client,
        headers,
        refund,
        article_id=ids["income"],
        amount="1200.00",
        counterparty_id=ids["supplier"],
    )
    assert second["rule_warning"] is None
    assert uuid.UUID(str(second["rule_id"])) != purchase_rule_id

    rules = {rule.id: rule for rule in _rules(async_session_factory, purpose_pattern="OZON")}
    assert rules[purchase_rule_id].direction == "out"
    assert rules[purchase_rule_id].article_id == ids["expense"]
    assert rules[purchase_rule_id].name == "Карт-списания: OZON"
    refund_rule = rules[uuid.UUID(str(second["rule_id"]))]
    assert refund_rule.direction == "in"
    assert refund_rule.name == "Карт-поступления: OZON"

    next_purchase = _new_operation(
        async_session_factory,
        account_id=ids["account"],
        direction="out",
        amount="3100.00",
        purpose="Оплата в OZON Moskva RUS",
        **card,
    )
    assert _auto_classify(async_session_factory, next_purchase) == ("classified", ids["expense"])


def test_remember_with_refund_article_classifies_but_keeps_no_rule(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Возврат переплаты разбирается, но правилом не запоминается: следующая выписка того же
    поставщика не проведётся возвратом фоном, мимо сторожа, — она ждёт человека."""
    ids = _seed(async_session_factory)
    headers = _admin(async_session_factory)

    refund = _new_operation(
        async_session_factory,
        account_id=ids["account"],
        direction="in",
        amount="300.00",
        purpose="Возврат переплаты по счету 15",
    )
    answer = _classify(
        client,
        headers,
        refund,
        article_id=ids["refund"],
        amount="300.00",
        counterparty_id=ids["supplier"],
    )
    assert answer["classification_status"] == "classified"
    assert answer["rule_id"] is None
    assert answer["rule_warning"] == REFUND_RULE_REFUSAL
    assert _rules(async_session_factory, article_id=ids["refund"]) == []

    next_refund = _new_operation(
        async_session_factory,
        account_id=ids["account"],
        direction="in",
        amount="300.00",
        purpose="Возврат переплаты по счету 16",
    )
    assert _auto_classify(async_session_factory, next_refund) == ("needs_review", None)


def test_owner_review_remember_with_refund_article_keeps_no_rule(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Вторая дверь «Запомнить» — разбор кейса собственником — отказывает тем же текстом."""
    ids = _seed(async_session_factory)
    operation_id = _new_operation(
        async_session_factory,
        account_id=ids["account"],
        direction="in",
        amount="300.00",
        purpose="Возврат переплаты по счету 15",
    )

    async def _case() -> uuid.UUID:
        async with async_session_factory() as session:
            case = ReconciliationCase(
                kind="unclassified_operation",
                status="pending",
                provider="tbank",
                bank_operation_id=operation_id,
                payload={"reason": "test"},
            )
            session.add(case)
            await session.commit()
            return case.id

    case_id = asyncio.run(_case())
    response = client.post(
        f"/api/v1/dds/owner-review/{case_id}/classify",
        headers={"X-User-Role": "admin"},
        json={
            "action": "set_article",
            "article_id": str(ids["refund"]),
            "counterparty_id": str(ids["supplier"]),
            "remember_as_rule": True,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["classification_status"] == "classified"
    assert body["rule_id"] is None
    assert body["rule_warning"] == REFUND_RULE_REFUSAL
    assert _rules(async_session_factory, article_id=ids["refund"]) == []


def test_settings_and_merchant_doors_refuse_a_refund_article_rule(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Правило с возвратной статьёй не заводится и в обход разбора: ни в настройках ДДС (создать,
    перенастроить), ни merchant-правилом из карточки контрагента."""
    ids = _seed(async_session_factory)
    admin = {"X-User-Role": "admin"}

    created = client.post(
        "/api/v1/dds/classification-rules",
        headers=admin,
        json={
            "name": "Возвраты поставщика",
            "direction": "in",
            "counterparty_inn_match": SUPPLIER_INN,
            "action": "set_article",
            "article_id": str(ids["refund"]),
        },
    )
    assert created.status_code == 422, created.text
    assert created.json()["detail"] == REFUND_RULE_REFUSAL

    allowed = client.post(
        "/api/v1/dds/classification-rules",
        headers=admin,
        json={
            "name": "Приходы поставщика",
            "direction": "in",
            "counterparty_inn_match": SUPPLIER_INN,
            "action": "set_article",
            "article_id": str(ids["income"]),
        },
    )
    assert allowed.status_code == 201, allowed.text
    patched = client.patch(
        f"/api/v1/dds/classification-rules/{allowed.json()['id']}",
        headers=admin,
        json={"article_id": str(ids["refund"])},
    )
    assert patched.status_code == 422, patched.text
    # Правка без статьи — обычная правка, запрет её не касается.
    renamed = client.patch(
        f"/api/v1/dds/classification-rules/{allowed.json()['id']}",
        headers=admin,
        json={"name": "Приходы поставщика"},
    )
    assert renamed.status_code == 200, renamed.text

    merchant = client.post(
        f"/api/v1/counterparties/{ids['supplier']}/merchant-rule",
        headers=_admin(async_session_factory),
        json={"purpose_pattern": "OZON", "article_id": str(ids["refund"])},
    )
    assert merchant.status_code == 409, merchant.text
    assert merchant.json()["detail"] == REFUND_RULE_REFUSAL
    assert _rules(async_session_factory, article_id=ids["refund"]) == []
    # Отказ правки не задел правило: статья осталась прежней.
    kept = _rules(async_session_factory, name="Приходы поставщика")
    assert [rule.article_id for rule in kept] == [ids["income"]]


def test_rule_carrying_refund_article_cannot_be_turned_into_set_article_or_revived(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Статью возврата правило может принести не из поля статьи: разбор владельцем с «исключить»
    сохраняет её в правиле. Правка одного действия или включение такого правила — отказ."""
    ids = _seed(async_session_factory)
    admin = {"X-User-Role": "admin"}

    async def _legacy() -> uuid.UUID:
        async with async_session_factory() as session:
            rule = ClassificationRule(
                name="Исключить возвраты",
                priority=50,
                is_active=False,
                direction="in",
                counterparty_inn_match=SUPPLIER_INN,
                action="exclude",
                article_id=ids["refund"],
            )
            session.add(rule)
            await session.commit()
            return rule.id

    rule_id = asyncio.run(_legacy())
    turned = client.patch(
        f"/api/v1/dds/classification-rules/{rule_id}",
        headers=admin,
        json={"action": "set_article"},
    )
    assert turned.status_code == 422, turned.text
    revived = client.post(f"/api/v1/dds/classification-rules/{rule_id}/toggle", headers=admin)
    assert revived.status_code == 422, revived.text
    kept = _rules(async_session_factory, name="Исключить возвраты")
    assert [(rule.action, rule.is_active) for rule in kept] == [("exclude", False)]


def test_merchant_registry_fallback_does_not_book_a_refund_article(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Последняя фоновая дверь — статья по умолчанию из карточки, по которой классификатор
    узнаёт мерчанта без правила. Возвратную статью так не ставим: операция ждёт человека.
    Контроль — тот же контрагент с обычной статьёй размечается сам."""
    ids = _seed(async_session_factory)
    card = {"inn": ACQUIRER_INN, "name": 'АО "ТБанк"'}

    async def _set_default(article_id: uuid.UUID) -> None:
        async with async_session_factory() as session:
            lavka = await make_counterparty(session, name="LAVKA")
            profile = await session.scalar(
                select(CounterpartyPayableProfile).where(
                    CounterpartyPayableProfile.counterparty_id == lavka.id
                )
            )
            profile.default_dds_article_id = article_id
            await session.commit()

    asyncio.run(_set_default(ids["refund"]))
    refund = _new_operation(
        async_session_factory,
        account_id=ids["account"],
        direction="in",
        amount="450.00",
        purpose="Возврат средств по операции оплаты LAVKA Moskva RUS",
        **card,
    )
    assert _auto_classify(async_session_factory, refund) == ("needs_review", None)

    async def _switch_to_expense() -> None:
        async with async_session_factory() as session:
            profile = await session.scalar(
                select(CounterpartyPayableProfile).where(
                    CounterpartyPayableProfile.default_dds_article_id == ids["refund"]
                )
            )
            profile.default_dds_article_id = ids["expense"]
            await session.commit()

    asyncio.run(_switch_to_expense())
    purchase = _new_operation(
        async_session_factory,
        account_id=ids["account"],
        direction="out",
        amount="990.00",
        purpose="Оплата в LAVKA Moskva RUS",
        **card,
    )
    assert _auto_classify(async_session_factory, purchase) == ("classified", ids["expense"])
