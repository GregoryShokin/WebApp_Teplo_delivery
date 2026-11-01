"""Verify cash equipment effects only on the task's disposable production copy.

Never run against a live database. Requires KASSA_CHECK_COPY=1, ENVIRONMENT=local,
and the explicitly named isolated copy host/database. Outputs only aggregate deltas.
"""

import asyncio, hashlib, json, os
from urllib.parse import urlparse
from app.core.config import get_settings
from dataclasses import asdict
from decimal import Decimal
from sqlalchemy import select, func
from app.db.session import AsyncSessionLocal
from app.models import (
    AssetCashflowLink,
    CashflowTransaction,
    DdsArticle,
    FixedAsset,
    PnlArticleRule,
    User,
)
from app.services.clock import moscow_today
from app.services.wallet_balance_as_of import build_money_balance_as_of
from app.services.counterparty_balance_as_of import build_balance_as_of
from app.services.staff_balance_as_of import build_staff_balance_as_of
from app.services.asset_balance import balance_lines
from app.services.pnl.projector import build_report
from app.services.kassa.payouts import create_payout, update_payout, delete_payout, KassaPayoutError

settings = get_settings()
copy_url = urlparse(settings.database_url.replace("+asyncpg", ""))
if (
    os.environ.get("KASSA_CHECK_COPY") != "1"
    or settings.environment != "local"
    or copy_url.hostname != "teplo-kassa-equipment-check-pg"
    or copy_url.path != "/teplo_check"
):
    raise SystemExit("Refused: explicitly isolated production copy required")
DAY = moscow_today()
AMOUNT = Decimal("100.01")


def canonical(v):
    if isinstance(v, dict):
        return {k: canonical(v[k]) for k in sorted(v)}
    if isinstance(v, list):
        return sorted(
            [canonical(x) for x in v], key=lambda x: json.dumps(x, default=str, sort_keys=True)
        )
    return v


def digest(v):
    return hashlib.sha256(
        json.dumps(canonical(v), default=str, sort_keys=True).encode()
    ).hexdigest()


async def snapshot():
    async with AsyncSessionLocal() as s:
        money = await build_money_balance_as_of(s, as_of=DAY)
        cp = await build_balance_as_of(s, as_of=DAY)
        staff = await build_staff_balance_as_of(s, as_of=DAY)
        assets = await balance_lines(s, as_of=DAY)
        pnl = await build_report(s, DAY.replace(day=1), today=DAY)
        return dict(
            money=money.total,
            cp=digest(asdict(cp)),
            staff=digest(asdict(staff)),
            assets=digest([asdict(x) for x in assets]),
            pnl={l.code: (l.amount, l.status.value) for l in pnl.lines},
            count=await s.scalar(select(func.count()).select_from(CashflowTransaction)),
            links=await s.scalar(select(func.count()).select_from(AssetCashflowLink)),
            incomplete=sum(1 for l in pnl.lines if l.amount is None),
        )


async def main():
    async with AsyncSessionLocal() as s:
        articles = (
            await s.scalars(
                select(DdsArticle).where(
                    DdsArticle.is_active.is_(True),
                    DdsArticle.kassa_enabled.is_(True),
                    DdsArticle.asset_link_kind == "maintenance",
                )
            )
        ).all()
        assert articles, "No configured cash maintenance article in production copy"
        article = articles[0]
        asset = await s.scalar(
            select(FixedAsset)
            .where(FixedAsset.status == "in_use", FixedAsset.initial_cost > 0)
            .order_by(FixedAsset.id)
            .limit(1)
        )
        user = await s.scalar(
            select(User).where(User.is_active.is_(True)).order_by(User.id).limit(1)
        )
        assert asset and user
        rule = await s.scalar(
            select(PnlArticleRule)
            .where(
                PnlArticleRule.article_id == article.id,
                PnlArticleRule.is_active.is_(True),
                PnlArticleRule.in_pnl.is_(True),
            )
            .limit(1)
        )
        assert rule and rule.line_code, "Production repair article has no P&L mapping"
        aid, oid, uid, line = article.id, asset.id, user.id, rule.line_code
    before = await snapshot()
    async with AsyncSessionLocal() as s:
        try:
            await create_payout(s, article_id=aid, amount=AMOUNT, actor_user_id=uid)
            raise AssertionError("Missing asset accepted")
        except KassaPayoutError:
            pass
    rejected = await snapshot()
    assert rejected == before, "Rejected payout changed reporting data"
    async with AsyncSessionLocal() as s:
        paid = await create_payout(
            s,
            article_id=aid,
            asset_id=oid,
            amount=AMOUNT,
            actor_user_id=uid,
            comment="ISOLATED PRODUCTION COPY CHECK ONLY",
        )
        tid = paid.transaction_id
    after = await snapshot()
    async with AsyncSessionLocal() as s:
        link = await s.scalar(
            select(AssetCashflowLink).where(AssetCashflowLink.cashflow_transaction_id == tid)
        )
        assert link and link.asset_id == oid and link.amount == AMOUNT and link.kind == "repair"
    checks = dict(
        missing_asset_no_change=True,
        cash_delta=str(after["money"] - before["money"]),
        expected_cash_delta=str(-AMOUNT),
        counterparties_unchanged=after["cp"] == before["cp"],
        staff_unchanged=after["staff"] == before["staff"],
        assets_and_depreciation_unchanged=after["assets"] == before["assets"],
        one_money_row=after["count"] == before["count"] + 1,
        one_asset_link=after["links"] == before["links"] + 1,
        pnl_line=line,
        pnl_expense_delta=str((after["pnl"][line][0] or 0) - (before["pnl"][line][0] or 0)),
        pnl_incomplete_lines_before=before["incomplete"],
        pnl_incomplete_lines_after=after["incomplete"],
    )
    print("CREATE_CHECK " + json.dumps(checks, ensure_ascii=False), flush=True)
    assert checks["cash_delta"] == str(-AMOUNT)
    assert all(
        checks[k]
        for k in [
            "counterparties_unchanged",
            "staff_unchanged",
            "assets_and_depreciation_unchanged",
            "one_money_row",
            "one_asset_link",
        ]
    )
    assert Decimal(checks["pnl_expense_delta"]) == AMOUNT
    async with AsyncSessionLocal() as s:
        await update_payout(
            s,
            transaction_id=tid,
            article_id=aid,
            asset_id=oid,
            amount=Decimal("200.02"),
            actor_user_id=uid,
        )
    edited = await snapshot()
    assert edited["money"] - before["money"] == Decimal("-200.02")
    assert (edited["pnl"][line][0] or 0) - (before["pnl"][line][0] or 0) == Decimal("200.02")
    async with AsyncSessionLocal() as s:
        await delete_payout(s, transaction_id=tid, actor_user_id=uid)
    cancelled = await snapshot()
    assert cancelled == before, "Cancellation did not restore reporting data"
    print(
        "RESULT "
        + json.dumps(
            dict(
                date=str(DAY),
                cash_create_edit_cancel="PASS",
                actual_production_fixed_asset_used=True,
                source_article_metadata_used=True,
                pnl_delta_correct=True,
                other_report_blocks_unchanged=True,
                cancellation_restored_before_snapshot=True,
                full_consolidated_balance="UNAVAILABLE: deployed /balance is EmptyModule; no balance assembly in deployed version",
            ),
            ensure_ascii=False,
        ),
        flush=True,
    )


asyncio.run(main())
