"""Owner decisions must alter only dated valuation and future depreciation, never history."""

import asyncio
import sys
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

sys.path.append(str(Path(__file__).parent / "counterparties"))
from cp_helpers import admin_headers, headers_for

from app.models import (
    AccountingPeriodClose,
    AssetCapitalization,
    AssetCashflowLink,
    CashflowTransaction,
    DdsArticle,
    DepreciationEntry,
    FixedAsset,
    User,
    Wallet,
)
from app.services import asset_capitalization as cap
from app.services.asset_analytics import (
    AssetLinkError,
    link_transaction_to_asset,
    resolve_asset_context,
    unlink_transaction,
)
from app.services.asset_balance import balance_lines, compare_with_snapshot, snapshot_month
from app.services.fixed_assets import accrue_depreciation, recompute_residuals

BASE = "/api/v1/fixed-assets"


@pytest.fixture
async def repair_case(async_session_factory, monkeypatch):
    monkeypatch.setattr(cap, "today", lambda: date(2026, 6, 30))
    async with async_session_factory() as s:
        wallet = await s.scalar(select(Wallet).limit(1))
        actor = User(
            email="capitalization@test.local", full_name="Владелец", hashed_password="unused"
        )
        asset = FixedAsset(
            name="Печь",
            initial_cost=D("100000"),
            useful_life_months=100,
            commissioned_on=date(2026, 1, 1),
            valuation_basis="payment",
            status="in_use",
        )
        article = DdsArticle(
            code="cap_test",
            name="Капитальный ремонт ОС",
            movement_type="outflow",
            activity_type="investing",
            asset_link_kind="repair",
        )
        s.add_all([actor, asset, article])
        await s.flush()
        tx = CashflowTransaction(
            wallet_id=wallet.id,
            direction="out",
            amount=D("30000"),
            operation_date=date(2026, 6, 1),
            article_id=article.id,
            source_kind="test",
            quality_status="final",
        )
        s.add(tx)
        await s.flush()
        context = await resolve_asset_context(
            s, article=article, asset_id=asset.id, amount=tx.amount
        )
        link = await link_transaction_to_asset(
            s, context=context, transaction_id=tx.id, amount=tx.amount
        )
        await s.commit()
        yield s, asset, article, tx, link, actor


async def approve(case, when=date(2026, 6, 15)):
    s, asset, _, _, link, actor = case
    return await cap.decide(
        s, asset_id=asset.id, link_id=link.id, status="confirmed", when=when, user_id=actor.id
    )


async def test_dated_decision_preserves_entries_reports_snapshots_and_changes_future(repair_case):
    s, asset, _, tx, link, actor = repair_case
    for month in range(1, 6):
        await accrue_depreciation(s, period_month=date(2026, month, 1), asset_id=asset.id)
    await snapshot_month(s, period_month=date(2026, 5, 1))
    await s.commit()
    old = [
        (e.id, e.amount, e.residual_after)
        for e in (
            await s.scalars(
                select(DepreciationEntry)
                .where(DepreciationEntry.asset_id == asset.id)
                .order_by(DepreciationEntry.period_month)
            )
        ).all()
    ]
    may = await balance_lines(s, as_of=date(2026, 5, 1))
    expected = await cap.preview(s, asset, link.id, date(2026, 6, 15))
    assert expected["cost_before"] == D("100000") and expected["cost_after"] == D("130000")
    assert expected["monthly_before"] == D("1000") and expected["monthly_after"] == D("1300")
    decision = await approve(repair_case)
    await s.commit()
    assert asset.initial_cost == D("100000")  # the undated historical base is not overwritten
    assert decision.amount == D("30000") and decision.depreciation_from == date(2026, 7, 1)
    assert await cap.cost_at(s, asset, on=date(2026, 6, 14)) == D("100000")
    assert await cap.cost_at(s, asset, on=date(2026, 6, 15)) == D("130000")
    assert await balance_lines(s, as_of=date(2026, 5, 1)) == may
    assert await compare_with_snapshot(s, period_month=date(2026, 5, 1)) == []
    june = await accrue_depreciation(s, period_month=date(2026, 6, 1), asset_id=asset.id)
    assert june[0].amount == D("1000") and june[0].residual_after == D("124000")
    july = await accrue_depreciation(s, period_month=date(2026, 7, 1), asset_id=asset.id)
    assert july[0].amount == D("1300") and july[0].residual_after == D("122700")
    await recompute_residuals(s, asset.id)
    actual = [
        (e.id, e.amount, e.residual_after)
        for e in (
            await s.scalars(
                select(DepreciationEntry)
                .where(
                    DepreciationEntry.asset_id == asset.id,
                    DepreciationEntry.period_month < date(2026, 6, 1),
                )
                .order_by(DepreciationEntry.period_month)
            )
        ).all()
    ]
    assert actual == old
    assert (await approve(repair_case)).id == decision.id
    assert len((await s.scalars(select(AssetCapitalization))).all()) == 1
    assert await accrue_depreciation(s, period_month=date(2026, 7, 1), asset_id=asset.id) == []


@pytest.mark.parametrize(
    "blocker", ["snapshot", "period_close", "current_accrual", "later_accrual"]
)
async def test_closed_or_already_accrued_period_is_rejected(repair_case, blocker):
    s, asset, *_ = repair_case
    if blocker == "snapshot":
        await snapshot_month(s, period_month=date(2026, 6, 1))
    elif blocker == "period_close":
        s.add(AccountingPeriodClose(period_month=date(2026, 7, 1)))
    else:
        await accrue_depreciation(
            s,
            period_month=date(2026, 6 if blocker == "current_accrual" else 7, 1),
            asset_id=asset.id,
        )
    await s.commit()
    with pytest.raises(cap.CapitalizationError):
        await approve(repair_case)
    assert not await s.scalar(select(AssetCapitalization.id))


@pytest.mark.parametrize("when", [date(2026, 5, 31), date(2026, 7, 1)])
async def test_date_before_payment_or_in_future_is_rejected(repair_case, when):
    with pytest.raises(cap.CapitalizationError):
        await approve(repair_case, when)


async def test_source_cannot_be_deleted_reclassified_or_changed_after_decision(repair_case):
    s, asset, _, tx, link, _ = repair_case
    await approve(repair_case)
    await s.commit()
    with pytest.raises(AssetLinkError):
        await unlink_transaction(s, tx.id)
    for stmt in (
        update(CashflowTransaction).where(CashflowTransaction.id == tx.id).values(amount=1),
        delete(CashflowTransaction).where(CashflowTransaction.id == tx.id),
        update(AssetCashflowLink).where(AssetCashflowLink.id == link.id).values(amount=1),
        delete(AssetCashflowLink).where(AssetCashflowLink.id == link.id),
    ):
        with pytest.raises(IntegrityError):
            async with s.begin_nested():
                await s.execute(stmt)
    assert await cap.cost_at(s, asset) == D("130000")


async def test_dismissal_does_not_change_cost_or_depreciation(repair_case):
    s, asset, _, _, link, actor = repair_case
    decision = await cap.decide(
        s,
        asset_id=asset.id,
        link_id=link.id,
        status="dismissed",
        when=None,
        user_id=actor.id,
        note="Обычное восстановление",
    )
    await s.commit()
    assert decision.effective_on is None
    assert await cap.cost_at(s, asset) == D("100000")
    entries = await accrue_depreciation(s, period_month=date(2026, 7, 1), asset_id=asset.id)
    assert entries[0].amount == D("1000")
    with pytest.raises(cap.CapitalizationError):
        await approve(repair_case)


async def test_multiple_repairs_add_once_with_their_own_dates(repair_case):
    s, asset, article, tx, link, actor = repair_case
    await approve(repair_case)
    other = CashflowTransaction(
        wallet_id=tx.wallet_id,
        direction="out",
        amount=D("20000"),
        operation_date=date(2026, 6, 16),
        article_id=article.id,
        source_kind="test",
        quality_status="final",
    )
    s.add(other)
    await s.flush()
    context = await resolve_asset_context(
        s, article=article, asset_id=asset.id, amount=other.amount
    )
    link2 = await link_transaction_to_asset(
        s, context=context, transaction_id=other.id, amount=other.amount
    )
    await cap.decide(
        s,
        asset_id=asset.id,
        link_id=link2.id,
        status="confirmed",
        when=date(2026, 6, 20),
        user_id=actor.id,
    )
    assert await cap.cost_at(s, asset, on=date(2026, 6, 17)) == D("130000")
    assert await cap.cost_at(s, asset, on=date(2026, 6, 20)) == D("150000")
    july = await accrue_depreciation(s, period_month=date(2026, 7, 1), asset_id=asset.id)
    assert july[0].amount == D("1500")


async def test_http_confirmation_rights_card_summary_and_no_manual_bypass(
    repair_case, client, async_session_factory
):
    s, asset, _, _, link, _ = repair_case
    admin = await admin_headers(async_session_factory)
    manager = await headers_for(async_session_factory, "reader@test.local", ["manager"])
    endpoint = f"{BASE}/{asset.id}/repairs/{link.id}"
    body = {"status": "confirmed", "effective_on": "2026-06-15"}
    assert client.post(endpoint + "/decision", headers=manager, json=body).status_code == 403
    assert (
        client.patch(
            f"{BASE}/{asset.id}", headers=admin, json={"initial_cost": "130000"}
        ).status_code
        == 409
    )
    estimate = client.get(
        endpoint + "/preview", headers=admin, params={"effective_on": "2026-06-15"}
    )
    assert estimate.status_code == 200, estimate.text
    response = client.post(endpoint + "/decision", headers=admin, json=body)
    assert response.status_code == 200, response.text
    data = response.json()
    assert D(data["initial_cost"]) == D("130000") and data["repairs"][0]["status"] == "confirmed"
    assert data["repairs"][0]["decided_by"]
    assert D(data["repairs"][0]["monthly_after"]) == D("1300")
    duplicate = client.post(endpoint + "/decision", headers=admin, json=body)
    assert duplicate.status_code == 200
    assert D(duplicate.json()["initial_cost"]) == D("130000")
    assert (
        client.patch(
            f"{BASE}/{asset.id}", headers=admin, json={"initial_cost": "999999"}
        ).status_code
        == 409
    )
    summary = client.get(f"{BASE}/summary", headers=admin)
    assert summary.status_code == 200 and D(summary.json()["initial_cost"]) == D("130000")


async def test_simultaneous_confirmation_is_applied_once(repair_case, async_session_factory):
    s, asset, _, _, link, actor = repair_case
    aid, lid, uid = asset.id, link.id, actor.id
    await s.commit()

    async def do():
        async with async_session_factory() as other:
            decision = await cap.decide(
                other,
                asset_id=aid,
                link_id=lid,
                status="confirmed",
                when=date(2026, 6, 15),
                user_id=uid,
            )
            await other.commit()
            return decision.id

    first, second = await asyncio.gather(do(), do())
    assert first == second
    assert await cap.cost_at(s, asset) == D("130000")
