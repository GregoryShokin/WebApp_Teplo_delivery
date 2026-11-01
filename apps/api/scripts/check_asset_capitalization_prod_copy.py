"""Read-copy rehearsal only. Leave a clearly labelled pending demo for owner review."""

import asyncio
import hashlib
import json
from dataclasses import asdict
from datetime import date
from decimal import ROUND_HALF_UP
from decimal import Decimal as D

from sqlalchemy import select, text

from app.api.v1.routes.fixed_assets import get_asset
from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.models import (
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
from app.services.asset_balance import balance_lines
from app.services.fixed_assets import accrue_depreciation

TAG = "СТЕНД: капитальный ремонт для проверки даты применения"


def digest(value):
    return hashlib.sha256(
        json.dumps(value, default=str, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


async def raw(s, table):
    rows = (
        (
            await s.execute(
                text(f"SELECT to_jsonb(t)::text FROM {table} t ORDER BY to_jsonb(t)::text")
            )
        )
        .scalars()
        .all()
    )
    return digest(rows)


async def past_reports(s, today):
    return {
        f"{year}-{month:02}": digest(
            [asdict(x) for x in await balance_lines(s, as_of=date(year, month, 1))]
        )
        for year in (2025, 2026)
        for month in range(1, 13)
        if date(year, month, 1) < today.replace(day=1)
    }


async def main():
    settings = get_settings()
    assert (
        settings.environment == "local"
        and "teplo-capitalization-preview-db" in settings.database_url
        and "/teplo_capitalization_preview" in settings.database_url
    )
    assert settings.scheduler_enabled is False and settings.teplo_bank_client_mode == "mock"
    day = cap.today()
    period = day.replace(day=1)
    async with AsyncSessionLocal() as s:
        user = await s.scalar(select(User).where(User.email == "admin1@teplo.local"))
        article = await s.scalar(
            select(DdsArticle).where(
                DdsArticle.asset_link_kind == "repair", DdsArticle.is_active.is_(True)
            )
        )
        tx = await s.scalar(
            select(CashflowTransaction).where(CashflowTransaction.payment_purpose == TAG)
        )
        if tx:
            link = await s.scalar(
                select(AssetCashflowLink).where(AssetCashflowLink.cashflow_transaction_id == tx.id)
            )
            asset = await s.get(FixedAsset, link.asset_id)
        else:
            asset = await s.scalar(
                select(FixedAsset)
                .where(
                    FixedAsset.status == "in_use",
                    FixedAsset.initial_cost > 0,
                    FixedAsset.commissioned_on <= day,
                    ~select(DepreciationEntry.id)
                    .where(
                        DepreciationEntry.asset_id == FixedAsset.id,
                        DepreciationEntry.period_month >= period,
                    )
                    .exists(),
                )
                .order_by(FixedAsset.inventory_number)
                .limit(1)
            )
            assert asset and user and article
            wallet = await s.scalar(select(Wallet).where(Wallet.code == "tk_chernikova"))
            amount = max(
                D("6000"),
                (await cap.cost_at(s, asset) * D(".30")).quantize(D(".01"), rounding=ROUND_HALF_UP),
            )
            tx = CashflowTransaction(
                wallet_id=wallet.id,
                direction="out",
                amount=amount,
                operation_date=day,
                article_id=article.id,
                source_kind="capitalization_preview",
                quality_status="final",
                payment_purpose=TAG,
                created_by_user_id=user.id,
            )
            s.add(tx)
            await s.flush()
            ctx = await resolve_asset_context(s, article=article, asset_id=asset.id, amount=amount)
            link = await link_transaction_to_asset(
                s, context=ctx, transaction_id=tx.id, amount=amount
            )
            await s.commit()
        assert not await s.scalar(
            select(AssetCapitalization.id).where(AssetCapitalization.link_id == link.id)
        ), "Demo already approved; do not alter it silently"
        aid, lid, uid = asset.id, link.id, user.id
        before_card = await get_asset(aid, s)
        before = {
            t: await raw(s, t)
            for t in ("depreciation_entry", "asset_balance_snapshot", "cashflow_transactions")
        }
        past = await past_reports(s, day)
        base_cost = asset.initial_cost
        estimate = await cap.preview(s, asset, lid, day)
        decision = await cap.decide(
            s,
            asset_id=aid,
            link_id=lid,
            status="confirmed",
            when=day,
            user_id=uid,
            note="Репетиция на копии: будет откатана",
        )
        after_card = await get_asset(aid, s)
        assert after_card.initial_cost - before_card.initial_cost == link.amount
        assert after_card.residual - before_card.residual == link.amount
        assert asset.initial_cost == base_cost
        assert {t: await raw(s, t) for t in before} == before
        assert await past_reports(s, day) == past
        assert (
            await cap.decide(
                s, asset_id=aid, link_id=lid, status="confirmed", when=day, user_id=uid
            )
        ).id == decision.id
        try:
            await unlink_transaction(s, tx.id)
        except AssetLinkError:
            pass
        else:
            raise AssertionError("Source repair could be unlinked after confirmation")
        current = await accrue_depreciation(s, period_month=period, asset_id=aid)
        future = await accrue_depreciation(s, period_month=cap.next_month(day), asset_id=aid)
        assert current[0].amount == estimate["monthly_before"], (current[0].amount, estimate)
        assert future[0].amount == estimate["monthly_after"], (future[0].amount, estimate)
        assert await past_reports(s, day) == past
        details = dict(
            asset_id=str(aid),
            inventory_number=asset.inventory_number,
            name=asset.name,
            link_id=str(lid),
            date=str(day),
            cost_before=str(estimate["cost_before"]),
            cost_after=str(estimate["cost_after"]),
            amount=str(link.amount),
            current_month_amount=str(current[0].amount),
            future_month=str(cap.next_month(day)),
            future_month_amount=str(future[0].amount),
            past_reports_checked=len(past),
            old_entries_and_snapshots_unchanged=True,
            money_unchanged_by_confirmation=True,
            idempotence=True,
            source_protected=True,
        )
        await s.rollback()
    async with AsyncSessionLocal() as s:
        assert {t: await raw(s, t) for t in before} == before
        assert await past_reports(s, day) == past
        assert not await s.scalar(
            select(AssetCapitalization.id).where(AssetCapitalization.link_id == lid)
        )
    print("PASS " + json.dumps(details, ensure_ascii=False), flush=True)
    print("Pending demo preserved; approval rehearsal and future accruals rolled back.", flush=True)


asyncio.run(main())
