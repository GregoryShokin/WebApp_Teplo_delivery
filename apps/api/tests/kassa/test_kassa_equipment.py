"""Cash repairs use the same FixedAsset and accounting engine as active payments."""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

sys.path.append(str(Path(__file__).parents[1] / "counterparties"))
from cp_helpers import admin_headers  # noqa: E402

from app.models import (
    AssetCashflowLink,
    CashflowTransaction,
    DdsArticle,
    FixedAsset,
    SafeAllocation,
    User,
    Wallet,
)  # noqa: E402
from app.services.asset_balance import balance_lines  # noqa: E402
from app.services.banking.safe_allocations import pay_allocation  # noqa: E402
from app.services.fixed_assets import accrue_depreciation, capitalize_upgrade  # noqa: E402
from app.services.kassa.payouts import (
    KassaPayoutError,
    create_payout,
    delete_payout,
    kassa_journal,
    kassa_today,
    update_payout,
)  # noqa: E402


async def seed(session, *, kind="maintenance", status="in_use"):
    article = DdsArticle(
        code=f"repair_{uuid.uuid4().hex[:10]}",
        name="Тестовый ремонт",
        movement_type="outflow",
        activity_type="operating" if kind == "maintenance" else "investing",
        kassa_enabled=True,
        asset_link_kind=kind,
    )
    asset = FixedAsset(
        name="Печь из реестра ОС",
        initial_cost=Decimal("100000"),
        useful_life_months=100,
        commissioned_on=date(2026, 1, 1),
        status=status,
        valuation_basis="payment",
    )
    user = User(
        email=f"cash-{uuid.uuid4().hex}@test.local", full_name="Кассир", hashed_password="unused"
    )
    session.add_all([article, asset, user])
    await session.commit()
    wallet = await session.scalar(select(Wallet).where(Wallet.code == "tk_chernikova"))
    assert wallet is not None
    return article, asset, user, wallet


@pytest.mark.parametrize(
    "kind,amount,link_kind,review",
    [
        ("maintenance", "500", "repair", "ok"),
        ("maintenance", "30000", "repair", "requires_owner_review"),
        ("repair", "30000", "upgrade", "requires_owner_review"),
    ],
)
async def test_cash_and_active_payment_have_same_asset_accounting(
    async_session_factory, kind, amount, link_kind, review
):
    async with async_session_factory() as session:
        article, cash_asset, user, wallet = await seed(session, kind=kind)
        active_asset = FixedAsset(
            name="Печь из активного платежа",
            initial_cost=Decimal("100000"),
            useful_life_months=100,
            commissioned_on=date(2026, 1, 1),
            status="in_use",
            valuation_basis="payment",
        )
        session.add(active_asset)
        await session.commit()
        value = Decimal(amount)
        cash = await create_payout(
            session,
            article_id=article.id,
            amount=value,
            asset_id=cash_asset.id,
            actor_user_id=user.id,
        )
        allocation = SafeAllocation(
            wallet_id=wallet.id,
            amount=value,
            amount_paid=Decimal("0"),
            status="reserved",
            article_id=article.id,
            asset_id=active_asset.id,
            purpose="Ремонт из активных платежей",
        )
        session.add(allocation)
        await session.commit()
        active_tx = await pay_allocation(
            session, allocation, amount=value, operation_date=kassa_today()
        )
        await session.commit()
        links = (
            await session.scalars(
                select(AssetCashflowLink).where(
                    AssetCashflowLink.cashflow_transaction_id.in_([cash.transaction_id, active_tx])
                )
            )
        ).all()
        assert {(l.asset_id, l.kind, l.amount) for l in links} == {
            (cash_asset.id, link_kind, value),
            (active_asset.id, link_kind, value),
        }
        await session.refresh(cash_asset)
        await session.refresh(active_asset)
        assert cash_asset.review_status == active_asset.review_status == review
        assert cash_asset.initial_cost == active_asset.initial_cost == Decimal("100000")
        # A payment does not approve capitalization. Both routes leave the balance
        # and depreciation base unchanged until the owner acts in the asset card.
        for asset in [cash_asset, active_asset]:
            entries = await accrue_depreciation(
                session, period_month=date(2026, 7, 1), asset_id=asset.id
            )
            assert [e.amount for e in entries] == [Decimal("1000")]
        lines = await balance_lines(session, as_of=date(2026, 7, 1))
        assert sum(l.initial_cost for l in lines) == Decimal("200000")
        assert sum(l.residual for l in lines) == Decimal("198000")
        assert sum(l.depreciation for l in lines) == Decimal("2000")
        if kind == "repair":
            for asset in [cash_asset, active_asset]:
                await capitalize_upgrade(session, asset=asset, amount=value)
                entries = await accrue_depreciation(
                    session, period_month=date(2026, 8, 1), asset_id=asset.id
                )
                assert [e.amount for e in entries] == [Decimal("1300")]
            lines = await balance_lines(session, as_of=date(2026, 8, 1))
            assert sum(l.initial_cost for l in lines) == Decimal("260000")
            assert sum(l.residual for l in lines) == Decimal("255400")
        await session.commit()


@pytest.mark.parametrize("target", ["missing", "unknown", "sold", "disposed"])
async def test_cash_refuses_invalid_asset_without_money_or_link(async_session_factory, target):
    async with async_session_factory() as session:
        article, asset, user, _ = await seed(
            session, status=target if target in ("sold", "disposed") else "in_use"
        )
        asset_id = (
            None if target == "missing" else uuid.uuid4() if target == "unknown" else asset.id
        )
        before = await session.scalar(select(func.count()).select_from(CashflowTransaction))
        with pytest.raises(KassaPayoutError):
            await create_payout(
                session,
                article_id=article.id,
                amount=Decimal("500"),
                asset_id=asset_id,
                actor_user_id=user.id,
            )
        assert await session.scalar(select(func.count()).select_from(CashflowTransaction)) == before
        assert await session.scalar(select(func.count()).select_from(AssetCashflowLink)) == 0


async def test_cash_edit_and_cancel_keep_asset_relationship_consistent(async_session_factory):
    async with async_session_factory() as session:
        article, first, user, _ = await seed(session)
        second = FixedAsset(
            name="Неработающее ОС",
            initial_cost=Decimal("50000"),
            status="not_working",
            valuation_basis="market",
        )
        session.add(second)
        await session.commit()
        paid = await create_payout(
            session,
            article_id=article.id,
            amount=Decimal("500"),
            asset_id=first.id,
            actor_user_id=user.id,
        )
        with pytest.raises(KassaPayoutError, match="укажите основное средство"):
            await update_payout(
                session,
                transaction_id=paid.transaction_id,
                article_id=article.id,
                amount=Decimal("700"),
                actor_user_id=user.id,
            )
        old_link = await session.scalar(select(AssetCashflowLink))
        tx = await session.get(CashflowTransaction, paid.transaction_id)
        assert (old_link.asset_id, old_link.amount, tx.amount) == (
            first.id,
            Decimal("500"),
            Decimal("500"),
        )
        await update_payout(
            session,
            transaction_id=paid.transaction_id,
            article_id=article.id,
            amount=Decimal("700"),
            asset_id=second.id,
            actor_user_id=user.id,
        )
        links = (await session.scalars(select(AssetCashflowLink))).all()
        assert [(l.asset_id, l.amount) for l in links] == [(second.id, Decimal("700"))]
        journal = await kassa_journal(
            session, date_from=kassa_today(), date_to=kassa_today(), actor_user_id=user.id
        )
        row = next(r for r in journal["items"] if r["id"] == paid.transaction_id)
        assert (row["asset_id"], row["asset_name"]) == (second.id, second.name)
        await delete_payout(session, transaction_id=paid.transaction_id, actor_user_id=user.id)
        assert await session.scalar(select(func.count()).select_from(AssetCashflowLink)) == 0
        assert await session.get(CashflowTransaction, paid.transaction_id) is None
        assert first.initial_cost == Decimal("100000")
        assert second.initial_cost == Decimal("50000")


def test_cash_picker_and_asset_card_return_the_same_registry_object(client, async_session_factory):
    headers = asyncio.run(admin_headers(async_session_factory))

    async def prepare():
        async with async_session_factory() as session:
            article, asset, _, _ = await seed(session)
            return str(article.id), str(asset.id)

    article_id, asset_id = asyncio.run(prepare())
    options = client.get("/api/v1/fixed-assets/options", headers=headers)
    assert options.status_code == 200, options.text
    assert any(
        a["asset_id"] == asset_id and a["name"] == "Печь из реестра ОС"
        for a in options.json()["items"]
    )
    articles = client.get("/api/v1/kassa/payout-articles", headers=headers)
    assert articles.status_code == 200, articles.text
    assert (
        next(a for a in articles.json() if a["id"] == article_id)["asset_link_kind"]
        == "maintenance"
    )
    rejected = client.post(
        "/api/v1/kassa/payouts", headers=headers, json={"article_id": article_id, "amount": 500}
    )
    assert rejected.status_code == 409, rejected.text
    paid = client.post(
        "/api/v1/kassa/payouts",
        headers=headers,
        json={"article_id": article_id, "amount": 500, "asset_id": asset_id},
    )
    assert paid.status_code == 201, paid.text
    card = client.get(f"/api/v1/fixed-assets/{asset_id}", headers=headers)
    assert card.status_code == 200, card.text
    assert card.json()["id"] == asset_id
    assert Decimal(str(card.json()["initial_cost"])) == Decimal("100000")
    assert Decimal(str(card.json()["monthly_amount"])) == Decimal("1000")
    journal = client.get(
        "/api/v1/kassa/journal",
        headers=headers,
        params={"date_from": "2026-07-06", "date_to": "2026-07-06"},
    )
    assert journal.status_code == 200, journal.text
    assert (
        next(r for r in journal.json()["items"] if r["id"] == paid.json()["transaction_id"])[
            "asset_id"
        ]
        == asset_id
    )
