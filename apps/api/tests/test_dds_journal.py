"""Журнал ДДС показывает внутренние переводы отдельным статусом (движение счёт→счёт).
Регресс: раньше операции со статусом ``internal_transfer`` были видны в балансе, но
выпадали из журнала (он брал только ``needs_review`` + проводки-cashflow)."""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Account, BankOperation, CashflowTransaction, Counterparty, DdsArticle, Wallet

HEADERS = {"X-User-Role": "finance_manager"}
WINDOW = {"from": "2026-06-01", "to": "2026-06-30"}


def _run(coro):
    return asyncio.run(coro)


def _seed(factory: async_sessionmaker[AsyncSession]) -> None:
    async def go() -> None:
        async with factory() as session:
            session.add_all(
                [
                    BankOperation(
                        provider="sber",
                        provider_operation_id="jr-transfer-1",
                        operation_date=date(2026, 6, 22),
                        direction="out",
                        amount=Decimal("630000.00"),
                        currency="RUB",
                        payment_purpose="Перевод собственных средств НДС не облагается.",
                        raw_payload={},
                        classification_status="internal_transfer",
                    ),
                    BankOperation(
                        provider="tbank",
                        provider_operation_id="jr-review-1",
                        operation_date=date(2026, 6, 22),
                        direction="out",
                        amount=Decimal("1000.00"),
                        currency="RUB",
                        payment_purpose="Оплата поставщику",
                        raw_payload={},
                        classification_status="needs_review",
                    ),
                ]
            )
            await session.commit()

    _run(go())


def _seed_filter_data(factory: async_sessionmaker[AsyncSession]) -> dict[str, str]:
    async def go() -> dict[str, str]:
        async with factory() as session:
            tbank_account = Account(
                bank_code="tbank",
                account_number="40702810000000000001",
                legal_entity="ООО Тест ДДС",
            )
            sber_account = Account(
                bank_code="sber",
                account_number="40702810000000000002",
                legal_entity="ООО Тест ДДС",
            )
            session.add_all([tbank_account, sber_account])
            await session.flush()

            tbank_wallet = Wallet(
                code="jr_filter_tbank",
                name="T-Bank фильтр",
                type="bank",
                account_id=tbank_account.id,
            )
            sber_wallet = Wallet(
                code="jr_filter_sber",
                name="Сбер фильтр",
                type="bank",
                account_id=sber_account.id,
            )
            article = DdsArticle(
                code="jr_filter_food",
                name="Фильтр продукты",
                movement_type="outflow",
                activity_type="operating",
            )
            other_article = DdsArticle(
                code="jr_filter_rent",
                name="Фильтр аренда",
                movement_type="outflow",
                activity_type="operating",
            )
            counterparty = Counterparty(
                name="ООО Журнал Фильтр",
                inn="7712345678",
                type="legal_entity",
                status="active",
            )
            other_counterparty = Counterparty(
                name="ООО Другой Фильтр",
                inn="7712345679",
                type="legal_entity",
                status="active",
            )
            session.add_all(
                [
                    tbank_wallet,
                    sber_wallet,
                    article,
                    other_article,
                    counterparty,
                    other_counterparty,
                ]
            )
            await session.flush()

            cashflow = CashflowTransaction(
                wallet_id=tbank_wallet.id,
                direction="out",
                amount=Decimal("1200.00"),
                operation_date=date(2026, 6, 15),
                article_id=article.id,
                counterparty_id=counterparty.id,
                source_kind="manual",
                payment_purpose="Покупка тестовых продуктов",
            )
            other_cashflow = CashflowTransaction(
                wallet_id=sber_wallet.id,
                direction="out",
                amount=Decimal("900.00"),
                operation_date=date(2026, 6, 16),
                article_id=other_article.id,
                counterparty_id=other_counterparty.id,
                source_kind="manual",
                payment_purpose="Аренда тестового склада",
            )
            operation = BankOperation(
                provider="tbank",
                provider_operation_id="jr-filter-review-1",
                account_id=tbank_account.id,
                operation_date=date(2026, 6, 17),
                direction="out",
                amount=Decimal("500.00"),
                currency="RUB",
                counterparty_name_raw="ООО Журнал Фильтр",
                counterparty_inn_raw="7712345678",
                payment_purpose="Провайдер фильтр молоко",
                raw_payload={},
                classification_status="needs_review",
            )
            other_operation = BankOperation(
                provider="sber",
                provider_operation_id="jr-filter-review-2",
                account_id=sber_account.id,
                operation_date=date(2026, 6, 18),
                direction="out",
                amount=Decimal("700.00"),
                currency="RUB",
                counterparty_name_raw="ООО Другой Фильтр",
                counterparty_inn_raw="7712345679",
                payment_purpose="Провайдер фильтр аренда",
                raw_payload={},
                classification_status="needs_review",
            )
            session.add_all([cashflow, other_cashflow, operation, other_operation])
            await session.commit()
            return {
                "wallet_id": str(tbank_wallet.id),
                "article_id": str(article.id),
                "counterparty_id": str(counterparty.id),
                "cashflow_id": str(cashflow.id),
                "operation_id": str(operation.id),
            }

    return _run(go())


def _statuses(client: TestClient, status: str) -> list[str]:
    r = client.get("/api/v1/dds/journal", params={"status": status, **WINDOW}, headers=HEADERS)
    assert r.status_code == 200, r.text
    return [item["status"] for item in r.json()["items"]]


def test_journal_internal_transfer_visible(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _seed(async_session_factory)

    # Вкладка «Внутренние переводы»: только переводы, без needs_review.
    r = client.get(
        "/api/v1/dds/journal", params={"status": "transfers", **WINDOW}, headers=HEADERS
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["transfer_total"] >= 1
    assert "internal_transfer" in [it["status"] for it in data["items"]]
    assert "needs_review" not in [it["status"] for it in data["items"]]

    # «Требуют проверки» НЕ содержит перевод; «Все» содержит и то, и другое.
    unmarked = _statuses(client, "unmarked")
    assert "internal_transfer" not in unmarked
    assert "needs_review" in unmarked

    all_rows = _statuses(client, "all")
    assert "internal_transfer" in all_rows
    assert "needs_review" in all_rows


def _seed_unmarked_cashflow(factory: async_sessionmaker[AsyncSession]) -> dict[str, str]:
    """Проводки-cashflow трёх видов: без статьи, исключённая и обычная размеченная."""

    async def go() -> dict[str, str]:
        async with factory() as session:
            wallet = Wallet(code="jr_unmarked_safe", name="Сейф журнал", type="cash")
            article = DdsArticle(
                code="jr_unmarked_food",
                name="Журнал продукты",
                movement_type="outflow",
                activity_type="operating",
            )
            session.add_all([wallet, article])
            await session.flush()

            # Заливка истории Сейфа: статью проставить было нечем — строка ждёт разметки.
            no_article = CashflowTransaction(
                wallet_id=wallet.id,
                direction="out",
                amount=Decimal("10000.00"),
                operation_date=date(2026, 6, 20),
                source_kind="template_import",
                payment_purpose="Внутренний перевод на договор — требует разметки",
            )
            # Исключённая из ДДС проводка «размечена» ровно настолько, чтобы не мозолить
            # глаза во вкладке «Требуют проверки», даже если статьи у неё нет.
            excluded = CashflowTransaction(
                wallet_id=wallet.id,
                direction="out",
                amount=Decimal("500.00"),
                operation_date=date(2026, 6, 21),
                source_kind="manual",
                quality_status="excluded",
                payment_purpose="Корректировка кассы",
            )
            classified = CashflowTransaction(
                wallet_id=wallet.id,
                direction="out",
                amount=Decimal("300.00"),
                operation_date=date(2026, 6, 22),
                article_id=article.id,
                source_kind="manual",
                payment_purpose="Обычная размеченная проводка",
            )
            session.add_all([no_article, excluded, classified])
            await session.commit()
            return {
                "no_article_id": str(no_article.id),
                "excluded_id": str(excluded.id),
                "classified_id": str(classified.id),
            }

    return _run(go())


def test_journal_cashflow_without_article_counts_as_unmarked(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Регресс: проводка без статьи горела «Требует разбора», а счётчик показывал 0.

    Счётчики делили журнал по происхождению строки (cashflow — размечено, банковская
    операция — нет), поэтому неразмеченные проводки попадали в «Размеченные» и на вкладке
    «Требуют проверки» их было не открыть.
    """
    _seed(async_session_factory)  # даёт одну банковскую операцию needs_review
    ids = _seed_unmarked_cashflow(async_session_factory)

    r = client.get("/api/v1/dds/journal", params={"status": "all", **WINDOW}, headers=HEADERS)
    assert r.status_code == 200, r.text
    data = r.json()
    # Банковская операция + проводка без статьи; excluded и classified — в «Размеченных».
    assert data["unmarked_total"] == 2
    assert data["marked_total"] == 2
    # Сумма счётчиков сходится с числом строк вкладки «Все».
    assert data["marked_total"] + data["unmarked_total"] + data["transfer_total"] == data["total"]

    unmarked = client.get(
        "/api/v1/dds/journal", params={"status": "unmarked", **WINDOW}, headers=HEADERS
    )
    assert unmarked.status_code == 200, unmarked.text
    unmarked_data = unmarked.json()
    assert ids["no_article_id"] in {item["id"] for item in unmarked_data["items"]}
    assert unmarked_data["total"] == unmarked_data["unmarked_total"]
    assert set(item["status"] for item in unmarked_data["items"]) == {"needs_review"}

    marked = client.get(
        "/api/v1/dds/journal", params={"status": "marked", **WINDOW}, headers=HEADERS
    )
    assert marked.status_code == 200, marked.text
    marked_ids = {item["id"] for item in marked.json()["items"]}
    assert ids["no_article_id"] not in marked_ids
    assert marked_ids == {ids["excluded_id"], ids["classified_id"]}


def test_journal_filters_across_cashflow_and_bank_operations(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ids = _seed_filter_data(async_session_factory)

    by_wallet = client.get(
        "/api/v1/dds/journal",
        params={"status": "all", "wallet_id": ids["wallet_id"], **WINDOW},
        headers=HEADERS,
    )
    assert by_wallet.status_code == 200, by_wallet.text
    wallet_items = by_wallet.json()["items"]
    assert {item["id"] for item in wallet_items} == {ids["cashflow_id"], ids["operation_id"]}
    assert {item["wallet_id"] for item in wallet_items} == {ids["wallet_id"]}

    by_article = client.get(
        "/api/v1/dds/journal",
        params={"status": "all", "article_id": ids["article_id"], **WINDOW},
        headers=HEADERS,
    )
    assert by_article.status_code == 200, by_article.text
    article_data = by_article.json()
    assert [item["id"] for item in article_data["items"]] == [ids["cashflow_id"]]
    assert article_data["marked_total"] == 1
    assert article_data["unmarked_total"] == 0

    by_counterparty = client.get(
        "/api/v1/dds/journal",
        params={"status": "all", "counterparty_id": ids["counterparty_id"], **WINDOW},
        headers=HEADERS,
    )
    assert by_counterparty.status_code == 200, by_counterparty.text
    assert {item["id"] for item in by_counterparty.json()["items"]} == {
        ids["cashflow_id"],
        ids["operation_id"],
    }
