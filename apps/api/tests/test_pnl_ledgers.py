"""Леджеры объясняют те же цифры, что и свод ОПиУ."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.jobs.pnl_iiko_sync_job import target_months
from app.models import (
    AccountingPeriodClose,
    CashflowTransaction,
    DdsArticle,
    InventoryAudit,
    InventoryAuditItem,
    InventoryAuditPosition,
    SupplierPrepayment,
    User,
)
from app.models.pnl import (
    PnlArticleRule,
    PnlIikoFact,
    PnlIikoGoodsFact,
    PnlIikoProductObservation,
    PnlIikoStockFact,
    PnlIikoWriteoffFact,
    PnlPartnerCommissionFact,
    PnlPartnerCommissionRule,
    PnlProductMonthlyDecision,
    PnlProductWhitelist,
)
from app.services import accounting_periods
from app.services.pnl import goods_classifier, iiko_sync, projector
from app.services.pnl.iiko_sync import (
    aggregate_partner_rows,
    build_stock_facts,
    product_catalog,
    product_observations,
    whitelist_details,
)
from app.services.pnl.ledgers import (
    GOODS_LINES_BY_SOURCE,
    build_goods_classifications,
    build_goods_ledger,
    build_partner_commission_ledger,
    build_recognition_ledger,
    pnl_goods_amount,
    rebuild_goods_from_observations,
    recognition_reason,
    save_goods_classification,
)
from app.services.pnl.sources import iiko as iiko_source
from app.services.pnl.sources.inventory import build_inventory_month, load_packaging_guids
from app.services.pnl.sources.payroll import PayrollBreakdown
from app.services.pnl.sources.recognition import (
    ORIGIN_AWAITING_DOCUMENT,
    ORIGIN_BY_TARIFF,
    ORIGIN_DOCUMENT,
)
from app.services.pnl.types import LineStatus, LineValue, PnlReport


def test_payroll_breakdown_salary_expense_uses_pnl_components() -> None:
    row = PayrollBreakdown(
        base_pay=Decimal("10000.00"),
        percent_pay=Decimal("2500.00"),
        vacation_pay=Decimal("1000.00"),
        bonuses=Decimal("500.00"),
        other_penalties=Decimal("300.00"),
        # Фонд и ревизионный штраф живут в других строках ОПиУ и сюда не входят.
        fund_accrual=Decimal("700.00"),
        audit_penalties=Decimal("200.00"),
    )
    assert row.salary_expense == Decimal("13700.00")


def test_whitelist_details_keep_each_product_contribution() -> None:
    rows = [
        {"product": "pack", "sum": "-10.25"},
        {"productId": "pack", "sum": "2.00"},
        {"iiko_product_guid": "gloves", "sum": "15.50"},
        {"product": "outside", "sum": "999.00"},
    ]
    details = whitelist_details(
        rows,
        {"pack": "packaging_result", "gloves": "aux_goods_invoices"},
        "sum",
        source_kind="inventory",
    )
    by_guid = {item.iiko_product_guid: item for item in details}
    assert by_guid["pack"].amount == Decimal("-8.25")
    assert by_guid["pack"].rows_count == 2
    assert by_guid["gloves"].amount == Decimal("15.50")
    assert "outside" not in by_guid


def test_partner_commission_is_revenue_with_discount_times_contract_rate() -> None:
    preset_id = "28b87ee7-0af5-41ad-baae-de735189169c"
    rows = [
        {"Delivery.CustomerName": " В Гостях У Алисы", "DishDiscountSumInt": "118050"},
        {"Delivery.CustomerName": "В Гостях У Алисы", "DishDiscountSumInt": "79809"},
        {"Delivery.CustomerName": "В Гостях У Алисы", "DishDiscountSumInt": "6594"},
    ]
    facts, unmapped = aggregate_partner_rows(
        rows,
        {
            "в гостях у алисы": (
                "rule-id",
                "В гостях у Алисы",
                Decimal("0.20"),
                preset_id,
            )
        },
    )
    assert unmapped == set()
    assert len(facts) == 1
    assert facts[0].revenue_amount == Decimal("204453.00")
    assert facts[0].commission_amount == Decimal("40890.60")
    assert facts[0].rows_count == 3


def test_partner_ledger_explains_the_iiko_commission(async_session_factory) -> None:
    async def scenario() -> None:
        async with async_session_factory() as session:
            rule = await session.scalar(
                select(PnlPartnerCommissionRule).where(
                    PnlPartnerCommissionRule.partner_key == "в гостях у алисы"
                )
            )
            assert rule is not None
            session.add(
                PnlPartnerCommissionFact(
                    period_month=date(2026, 7, 1),
                    rule_id=rule.id,
                    partner_key=rule.partner_key,
                    partner_name=rule.partner_name,
                    revenue_amount=Decimal("204453.00"),
                    commission_rate=Decimal("0.20"),
                    commission_amount=Decimal("40890.60"),
                    rows_count=3,
                    source_ref=rule.source_preset_id,
                )
            )
            session.add(
                PnlIikoFact(
                    period_month=date(2026, 7, 1),
                    metric_code="partner_commission",
                    direction="total",
                    amount=Decimal("40890.60"),
                    rows_count=3,
                    source_ref=rule.source_preset_id,
                )
            )
            await session.commit()

            ledger = await build_partner_commission_ledger(session, date(2026, 7, 1))
            assert ledger.revenue_amount == Decimal("204453.00")
            assert ledger.commission_amount == Decimal("40890.60")
            assert ledger.rows[0].partner_name == "В гостях у Алисы"
            assert ledger.rows[0].commission_rate == Decimal("0.20")

            report = await projector.build_report(session, date(2026, 7, 1))
            line = next(item for item in report.lines if item.code == "partner_commission")
            assert line.amount == Decimal("40890.60")
            assert [item.stream for item in line.components] == ["iiko"]

    asyncio.run(scenario())


def test_product_observations_keep_unknown_products_for_review() -> None:
    catalog = product_catalog(
        {
            "products": [
                {"id": "known", "name": "Перчатки", "code": "A-1"},
                {"id": "unknown", "name": "Новый товар", "code": "N-7"},
            ]
        }
    )
    rows = [
        {"product": "known", "sum": "10.25"},
        {"productId": "unknown", "sum": "20.00"},
        {"productId": "unknown", "sum": "-2.50"},
    ]
    observations = product_observations(
        rows,
        "sum",
        source_kind="incoming_invoice",
        catalog=catalog,
    )
    by_guid = {item.iiko_product_guid: item for item in observations}
    assert by_guid["known"].product_name == "Перчатки"
    assert by_guid["known"].product_code == "A-1"
    assert by_guid["unknown"].amount == Decimal("17.50")
    assert by_guid["unknown"].rows_count == 2


def test_product_classification_options_are_source_specific() -> None:
    # У складского источника это строки БАРНОЙ ревизии: товар пересчитывают на складе, и его
    # расхождение идёт в свою строку. Пока список был пуст, человек не мог ни увидеть привязку,
    # ни выбрать её — а каждое сохранение «складского учёта» её стирало.
    assert GOODS_LINES_BY_SOURCE["inventory"] == {
        "packaging_inventory",
        "pizza_box_inventory",
        "beverage_inventory",
    }
    assert GOODS_LINES_BY_SOURCE["incoming_invoice"] == {
        "shop_maintenance",
        "aux_goods",
    }


def test_stock_rollforward_includes_shortage_and_surplus() -> None:
    receipts = [
        iiko_sync.GoodsProductObservation(
            source_kind="incoming_invoice",
            iiko_product_guid="product",
            product_name="Товар",
            product_code="P-1",
            amount=Decimal("40.00"),
            rows_count=1,
        )
    ]
    facts = build_stock_facts(
        [
            {"store": "one", "product": "product", "amount": "2", "sum": "100.00"},
            {"store": "two", "product": "product", "amount": "1", "sum": "50.00"},
            {"store": "one", "product": "surplus", "amount": "1", "sum": "20.00"},
        ],
        [
            {"store": "one", "product": "product", "amount": "1", "sum": "70.00"},
            {"store": "two", "product": "product", "amount": "1", "sum": "30.00"},
            {"store": "one", "product": "surplus", "amount": "2", "sum": "35.00"},
        ],
        receipts,
        catalog={"surplus": ("Излишек", "S-1")},
    )
    by_guid = {fact.iiko_product_guid: fact for fact in facts}
    assert by_guid["product"].opening_amount == Decimal("150.00")
    assert by_guid["product"].receipts_amount == Decimal("40.00")
    assert by_guid["product"].closing_amount == Decimal("100.00")
    assert by_guid["product"].consumption_amount == Decimal("90.00")
    assert by_guid["product"].stores_count == 2
    assert by_guid["surplus"].consumption_amount == Decimal("-15.00")


def test_projector_nets_surplus_against_shortage_and_ignores_stock_consumption(
    async_session_factory,
) -> None:
    async def scenario() -> None:
        async with async_session_factory() as session:
            session.add_all(
                [
                    PnlIikoFact(
                        period_month=date(2026, 7, 1),
                        metric_code="stock_consumption",
                        direction="total",
                        amount=Decimal("100.00"),
                    ),
                    PnlIikoFact(
                        period_month=date(2026, 7, 1),
                        metric_code="food_cost",
                        direction="total",
                        amount=Decimal("120.00"),
                    ),
                ]
            )
            audit = InventoryAudit(
                business_date=date(2026, 7, 13),
                status="applied",
                total_shortage_amount=Decimal("15.00"),
                total_penalty_amount=Decimal("0.00"),
            )
            session.add(audit)
            await session.flush()
            session.add_all(
                [
                    InventoryAuditItem(
                        audit_id=audit.id,
                        product_name_snapshot="Недостача",
                        shortage_amount=Decimal("25.00"),
                        amount=Decimal("-25.00"),
                    ),
                    InventoryAuditItem(
                        audit_id=audit.id,
                        product_name_snapshot="Излишек",
                        shortage_amount=Decimal("0.00"),
                        amount=Decimal("7.00"),
                    ),
                ]
            )
            await session.commit()
            line = LineValue(
                code="audit_results",
                title="Результат складского учёта",
                block="gross_profit",
                kind="source",
                level=1,
                sort_order=1,
                sign_role=-1,
                month_basis="calendar",
                amount=None,
                status=LineStatus.NO_DATA,
            )
            report = PnlReport(month=date(2026, 7, 1))
            await projector._apply_inventory(
                session,
                {"audit_results": line},
                date(2026, 7, 1),
                date(2026, 7, 31),
                report,
            )
            # Недостача 25,00 минус излишек 7,00. Владелец 05.08.2026: «излишки тоже должны
            # влиять на расчёт прибыли», без всякого зачёта пересорта — просто разность.
            assert line.components[0].amount == Decimal("18.00")
            assert line.components[0].status == LineStatus.OK
            assert "минус излишки" in (line.components[0].note or "")

            ledger = await build_goods_ledger(session, date(2026, 7, 1))
            # По КОДУ, а не по источнику: рядом с продуктовыми ревизиями в том же источнике
            # теперь стоят результаты инвентаризации упаковки, и «первый inventory» —
            # уже не то, чем кажется.
            revision_summary = next(
                item for item in ledger.summaries if item.line_code == "audit_results"
            )
            # Плитка расшифровки показывает ровно то же, что свод, плюс обе составляющие.
            assert revision_summary.amount == Decimal("18.00")
            assert revision_summary.shortage_amount == Decimal("25.00")
            assert revision_summary.surplus_amount == Decimal("7.00")
            assert {(row.product_name, row.amount, row.surplus_amount) for row in ledger.rows} >= {
                ("Недостача", Decimal("25.00"), Decimal("0.00")),
                ("Излишек", Decimal("0.00"), Decimal("7.00")),
            }

    asyncio.run(scenario())


def test_revision_position_marks_product_and_blocks_duplicate_goods_expense(
    async_session_factory,
) -> None:
    async def scenario() -> None:
        product_guid = "revision-product-guid"
        async with async_session_factory() as session:
            session.add(
                InventoryAuditPosition(
                    code="revision-product",
                    display_name="Товар продуктовой ревизии",
                    allocation_group="chefs",
                    iiko_product_guid=product_guid,
                    is_active=True,
                )
            )
            session.add(
                PnlIikoProductObservation(
                    period_month=date(2026, 7, 1),
                    source_kind="incoming_invoice",
                    iiko_product_guid=product_guid,
                    product_name="Товар продуктовой ревизии",
                    product_code="REV-1",
                    amount=Decimal("500.00"),
                    rows_count=1,
                )
            )
            # Даже ошибочное старое правило накладных не должно создать второй расход.
            session.add(
                PnlProductWhitelist(
                    iiko_product_guid=product_guid,
                    source_kind="incoming_invoice",
                    line_code="aux_goods",
                    include_status="include",
                    product_name="Товар продуктовой ревизии",
                    product_code="REV-1",
                )
            )
            await session.commit()

            await rebuild_goods_from_observations(
                session,
                date(2026, 7, 1),
                "incoming_invoice",
            )
            classifications = await build_goods_classifications(session, date(2026, 7, 1))
            row = next(item for item in classifications.rows if item.product_guid == product_guid)

            assert row.status == "stocked"
            assert row.revision_product is True
            assert row.selected_source_kind == "inventory"
            assert row.line_code is None
            assert "активный список складского учёта" in (row.note or "")
            assert classifications.attention_count == 0
            assert product_guid not in await iiko_sync.load_whitelist(
                session,
                "incoming_invoice",
                date(2026, 7, 1),
            )
            assert (
                await session.scalar(
                    select(PnlIikoGoodsFact).where(
                        PnlIikoGoodsFact.iiko_product_guid == product_guid
                    )
                )
                is None
            )

    asyncio.run(scenario())


def test_inactive_revision_position_does_not_claim_product(async_session_factory) -> None:
    async def scenario() -> None:
        product_guid = "inactive-revision-product-guid"
        async with async_session_factory() as session:
            session.add(
                InventoryAuditPosition(
                    code="inactive-revision-product",
                    display_name="Выключенный товар ревизии",
                    allocation_group="chefs",
                    iiko_product_guid=product_guid,
                    is_active=False,
                )
            )
            session.add(
                PnlIikoProductObservation(
                    period_month=date(2026, 7, 1),
                    source_kind="incoming_invoice",
                    iiko_product_guid=product_guid,
                    product_name="Выключенный товар ревизии",
                    product_code="REV-OFF",
                    amount=Decimal("100.00"),
                    rows_count=1,
                )
            )
            await session.commit()

            classifications = await build_goods_classifications(session, date(2026, 7, 1))
            row = next(item for item in classifications.rows if item.product_guid == product_guid)

            assert row.status == "unclassified"
            assert row.revision_product is False
            assert {source.source_kind for source in row.sources} == {
                "inventory",
                "incoming_invoice",
            }
            assert [source.source_kind for source in row.sources] == [
                "inventory",
                "incoming_invoice",
            ]
            assert classifications.attention_count == 1

    asyncio.run(scenario())


def test_invoice_only_product_can_be_moved_to_stocked(async_session_factory) -> None:
    async def scenario() -> None:
        product_guid = "invoice-only-stock-product"
        async with async_session_factory() as session:
            session.add(
                PnlIikoProductObservation(
                    period_month=date(2026, 8, 1),
                    source_kind="incoming_invoice",
                    iiko_product_guid=product_guid,
                    product_name="Напиток без движения",
                    product_code="DRINK-1",
                    amount=Decimal("0.00"),
                    rows_count=0,
                )
            )
            await session.commit()

            before = await build_goods_classifications(session, date(2026, 8, 1))
            before_row = next(row for row in before.rows if row.product_guid == product_guid)
            assert [source.source_kind for source in before_row.sources] == [
                "inventory",
                "incoming_invoice",
            ]
            assert before_row.sources[0].amount is None

            after = await save_goods_classification(
                session,
                display_month=date(2026, 8, 1),
                product_guid=product_guid,
                source_kind="inventory",
                status="stocked",
                line_code=None,
                note="Хранится на складе, но в месяце не использовался",
                user_id=None,
            )
            after_row = next(row for row in after.rows if row.product_guid == product_guid)
            assert after_row.status == "stocked"
            assert after_row.selected_source_kind == "inventory"
            stock_total = await session.scalar(
                select(PnlIikoFact).where(
                    PnlIikoFact.period_month == date(2026, 8, 1),
                    PnlIikoFact.metric_code == "stock_consumption",
                    PnlIikoFact.direction == "total",
                )
            )
            assert stock_total is not None
            assert stock_total.amount == Decimal("0.00")

    asyncio.run(scenario())


def test_classification_edit_rebuilds_goods_facts(async_session_factory) -> None:
    async def scenario() -> None:
        async with async_session_factory() as session:
            session.add(
                PnlIikoProductObservation(
                    period_month=date(2026, 7, 1),
                    source_kind="inventory",
                    iiko_product_guid="new-packaging-guid",
                    product_name="Новая упаковка",
                    product_code="NEW-1",
                    amount=Decimal("-125.50"),
                    rows_count=2,
                )
            )
            session.add(
                PnlIikoProductObservation(
                    period_month=date(2026, 7, 1),
                    source_kind="incoming_invoice",
                    iiko_product_guid="new-packaging-guid",
                    product_name="Новая упаковка",
                    product_code="NEW-1",
                    amount=Decimal("500.00"),
                    rows_count=1,
                )
            )
            session.add(
                PnlIikoStockFact(
                    period_month=date(2026, 7, 1),
                    iiko_product_guid="new-packaging-guid",
                    product_name="Новая упаковка",
                    product_code="NEW-1",
                    opening_quantity=Decimal("10.000000"),
                    opening_amount=Decimal("100.00"),
                    receipts_amount=Decimal("500.00"),
                    closing_quantity=Decimal("5.000000"),
                    closing_amount=Decimal("225.00"),
                    consumption_amount=Decimal("375.00"),
                    stores_count=2,
                )
            )
            await session.commit()

            classifications = await build_goods_classifications(session, date(2026, 7, 1))
            new_rows = [
                row for row in classifications.rows if row.product_guid == "new-packaging-guid"
            ]
            assert len(new_rows) == 1
            assert classifications.attention_count == 0
            assert new_rows[0].status == "stocked"
            assert new_rows[0].selected_source_kind == "inventory"
            assert "складских остатках iiko" in (new_rows[0].note or "")
            assert {source.source_kind for source in new_rows[0].sources} == {
                "inventory",
                "incoming_invoice",
            }

            await rebuild_goods_from_observations(session, date(2026, 7, 1), "inventory")
            detail = await session.scalar(
                select(PnlIikoGoodsFact).where(
                    PnlIikoGoodsFact.iiko_product_guid == "new-packaging-guid"
                )
            )
            assert detail is None
            fact = await session.scalar(
                select(PnlIikoFact).where(
                    PnlIikoFact.period_month == date(2026, 7, 1),
                    PnlIikoFact.metric_code == "stock_consumption",
                    PnlIikoFact.direction == "total",
                )
            )
            assert fact is not None
            assert fact.amount == Decimal("375.00")
            goods_ledger = await build_goods_ledger(session, date(2026, 7, 1))
            stock_summary = next(
                item for item in goods_ledger.summaries if item.source_kind == "inventory"
            )
            assert stock_summary.amount is None
            assert stock_summary.surplus_amount is None
            assert not any(
                item.product_guid == "new-packaging-guid" and item.source_kind == "inventory"
                for item in goods_ledger.rows
            )

            switched = await save_goods_classification(
                session,
                display_month=date(2026, 7, 1),
                product_guid="new-packaging-guid",
                source_kind="incoming_invoice",
                status="include",
                line_code="aux_goods",
                note=None,
                user_id=None,
            )
            switched_row = next(
                row for row in switched.rows if row.product_guid == "new-packaging-guid"
            )
            assert switched_row.selected_source_kind == "incoming_invoice"
            assert switched_row.line_code == "aux_goods"
            details = (
                (
                    await session.execute(
                        select(PnlIikoGoodsFact).where(
                            PnlIikoGoodsFact.iiko_product_guid == "new-packaging-guid"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(details) == 1
            assert details[0].source_kind == "incoming_invoice"
            assert details[0].amount == Decimal("500.00")
            await session.refresh(fact)
            assert fact.amount == Decimal("0.00")

            await save_goods_classification(
                session,
                display_month=date(2026, 7, 1),
                product_guid="new-packaging-guid",
                source_kind="incoming_invoice",
                status="exclude",
                line_code=None,
                note="Отнесено к другому контуру",
                user_id=None,
            )
            assert (
                await session.scalar(
                    select(PnlIikoGoodsFact).where(
                        PnlIikoGoodsFact.iiko_product_guid == "new-packaging-guid"
                    )
                )
                is None
            )
            await session.refresh(fact)
            assert fact.amount == Decimal("0.00")

    asyncio.run(scenario())


def test_goods_ledger_uses_same_sign_policy_as_pnl() -> None:
    assert pnl_goods_amount("stock_consumption", Decimal("-30810.00")) == Decimal("-30810.00")
    assert pnl_goods_amount("aux_goods_invoices", Decimal("5177.36")) == Decimal("5177.36")


def test_workup_counts_current_month_and_asks_again_next_month(
    async_session_factory,
) -> None:
    async def scenario() -> None:
        product_guid = "monthly-workup-guid"
        async with async_session_factory() as session:
            for period_month, amount in (
                (date(2026, 7, 1), Decimal("75.25")),
                (date(2026, 8, 1), Decimal("10.00")),
            ):
                session.add(
                    PnlIikoProductObservation(
                        period_month=period_month,
                        source_kind="incoming_invoice",
                        iiko_product_guid=product_guid,
                        product_name="Новый расходник",
                        product_code="TEMP-1",
                        amount=amount,
                        rows_count=1,
                    )
                )
            await session.commit()

            # Первый dropdown запоминает источник, второй выбирает временную статью.
            await save_goods_classification(
                session,
                display_month=date(2026, 7, 1),
                product_guid=product_guid,
                source_kind="incoming_invoice",
                status="requires_owner_review",
                line_code=None,
                note=None,
                user_id=None,
            )
            july = await save_goods_classification(
                session,
                display_month=date(2026, 7, 1),
                product_guid=product_guid,
                source_kind="incoming_invoice",
                status="workup",
                line_code="goods_workup",
                note=None,
                user_id=None,
            )
            july_row = next(row for row in july.rows if row.product_guid == product_guid)
            assert july_row.status == "workup"
            assert july_row.line_code == "goods_workup"
            assert july_row.selected_source_kind == "incoming_invoice"

            goods = await build_goods_ledger(session, date(2026, 7, 1))
            workup_summary = next(
                item for item in goods.summaries if item.line_code == "goods_workup"
            )
            workup_row = next(item for item in goods.rows if item.product_guid == product_guid)
            assert workup_summary.amount == Decimal("75.25")
            assert workup_row.source_amount == Decimal("75.25")
            assert workup_row.amount == Decimal("75.25")

            august = await build_goods_classifications(session, date(2026, 8, 1))
            august_row = next(row for row in august.rows if row.product_guid == product_guid)
            assert august_row.status == "unclassified"
            assert august_row.selected_source_kind is None
            assert (
                await session.scalar(
                    select(PnlProductWhitelist).where(
                        PnlProductWhitelist.iiko_product_guid == product_guid
                    )
                )
                is None
            )
            assert (
                await session.scalar(
                    select(PnlProductMonthlyDecision).where(
                        PnlProductMonthlyDecision.iiko_product_guid == product_guid
                    )
                )
                is not None
            )

            # В следующем месяце товар можно разметить постоянно. Июльская «Проработка»
            # при этом остаётся историческим решением и не дублируется постоянной статьёй.
            august = await save_goods_classification(
                session,
                display_month=date(2026, 8, 1),
                product_guid=product_guid,
                source_kind="incoming_invoice",
                status="include",
                line_code="aux_goods",
                note=None,
                user_id=None,
            )
            august_row = next(row for row in august.rows if row.product_guid == product_guid)
            assert august_row.status == "include"
            assert august_row.line_code == "aux_goods"

            july_goods = await build_goods_ledger(session, date(2026, 7, 1))
            july_product_rows = [row for row in july_goods.rows if row.product_guid == product_guid]
            assert len(july_product_rows) == 1
            assert july_product_rows[0].line_code == "goods_workup"
            assert july_product_rows[0].amount == Decimal("75.25")

            assert product_guid not in await iiko_sync.load_whitelist(
                session, "incoming_invoice", date(2026, 7, 1)
            )
            assert product_guid in await iiko_sync.load_whitelist(
                session, "incoming_invoice", date(2026, 8, 1)
            )

    asyncio.run(scenario())


def test_menu_usage_traverses_prepared_products_to_active_dishes() -> None:
    products = [
        {"id": "ingredient", "name": "Сыр", "type": "GOODS"},
        {"id": "inactive-only", "name": "Тестовый соус", "type": "GOODS"},
        {"id": "prepared", "name": "Сырный микс", "type": "PREPARED"},
        {
            "id": "active-dish",
            "name": "Пицца в продаже",
            "type": "DISH",
            "defaultIncludedInMenu": True,
            "deleted": False,
        },
        {
            "id": "inactive-dish",
            "name": "Проработка пиццы",
            "type": "DISH",
            "defaultIncludedInMenu": False,
            "deleted": False,
        },
    ]
    charts = {
        "assemblyCharts": [
            {
                "assembledProductId": "prepared",
                "dateFrom": "2026-01-01",
                "dateTo": None,
                "items": [{"productId": "ingredient"}],
            },
            {
                "assembledProductId": "active-dish",
                "dateFrom": "2026-01-01",
                "dateTo": None,
                "items": [{"productId": "prepared"}],
            },
            {
                "assembledProductId": "inactive-dish",
                "dateFrom": "2026-01-01",
                "dateTo": None,
                "items": [
                    {"productId": "ingredient"},
                    {"productId": "inactive-only"},
                ],
            },
        ],
        "preparedCharts": [],
    }

    snapshot = goods_classifier.build_menu_usage_snapshot(
        json.dumps(products, ensure_ascii=False).encode(),
        json.dumps(charts, ensure_ascii=False).encode(),
        month_start=date(2026, 7, 1),
        month_end=date(2026, 7, 31),
        product_guids={"ingredient", "inactive-only"},
    )

    assert snapshot.usage["ingredient"].active_dishes == ("Пицца в продаже",)
    assert snapshot.usage["ingredient"].inactive_dishes == ("Проработка пиццы",)
    assert snapshot.usage["inactive-only"].only_inactive is True


def test_auto_classification_uses_menu_rules_and_safe_llm_fallback(
    async_session_factory,
    monkeypatch,
) -> None:
    async def fake_call_tool(_settings, *, prompt, **_kwargs):
        # Внешней модели не передаются суммы или цены — только классификационный контекст.
        assert '"amount"' not in prompt
        assert '"price"' not in prompt
        # Правило 4: товару с тремя заказами проработка уже не предлагается, и модель
        # физически не может её выбрать — решения нет в allowed_decisions его строки.
        sent = {row["product_guid"]: row for row in json.loads(prompt)["candidates"]}
        assert "workup" not in sent["inactive-old"]["allowed_decisions"]
        assert "workup" in sent["llm-workup"]["allowed_decisions"]
        return {
            "decisions": [
                {
                    "product_guid": "gloves",
                    "decision": "aux_goods",
                    "confidence": 0.95,
                    "reason": "одноразовый расходный материал",
                },
                {
                    "product_guid": "uncertain-pack",
                    "decision": "stocked",
                    "confidence": 0.4,
                    "reason": "название неоднозначно",
                },
                {
                    "product_guid": "llm-workup",
                    "decision": "workup",
                    "confidence": 0.96,
                    "reason": "новый тестовый ингредиент",
                },
                {
                    "product_guid": "inactive-old",
                    "decision": "shop_maintenance",
                    "confidence": 0.9,
                    "reason": "закупается регулярно, к блюдам отношения не имеет",
                },
            ]
        }

    monkeypatch.setattr(goods_classifier, "call_tool", fake_call_tool)

    async def scenario() -> None:
        observations = [
            iiko_sync.GoodsProductObservation(
                source_kind="incoming_invoice",
                iiko_product_guid="inactive-new",
                product_name="Тестовый ингредиент",
                product_code="T-1",
                amount=Decimal("100.00"),
                rows_count=1,
            ),
            iiko_sync.GoodsProductObservation(
                source_kind="inventory",
                iiko_product_guid="food",
                product_name="Сыр",
                product_code="F-1",
                amount=Decimal("-50.00"),
                rows_count=1,
            ),
            iiko_sync.GoodsProductObservation(
                source_kind="incoming_invoice",
                iiko_product_guid="gloves",
                product_name="Перчатки винил",
                product_code="A-1",
                amount=Decimal("250.00"),
                rows_count=1,
            ),
            iiko_sync.GoodsProductObservation(
                source_kind="inventory",
                iiko_product_guid="uncertain-pack",
                product_name="Набор новый",
                product_code="U-1",
                amount=Decimal("-30.00"),
                rows_count=1,
            ),
            iiko_sync.GoodsProductObservation(
                source_kind="incoming_invoice",
                iiko_product_guid="llm-workup",
                product_name="Тестовая приправа",
                product_code="L-1",
                amount=Decimal("45.00"),
                rows_count=1,
            ),
            iiko_sync.GoodsProductObservation(
                source_kind="incoming_invoice",
                iiko_product_guid="previous-workup",
                product_name="Старая проработка",
                product_code="P-1",
                amount=Decimal("80.00"),
                rows_count=1,
            ),
            iiko_sync.GoodsProductObservation(
                source_kind="incoming_invoice",
                iiko_product_guid="inactive-old",
                product_name="Старая тестовая позиция",
                product_code="P-2",
                amount=Decimal("90.00"),
                rows_count=1,
            ),
        ]
        products = [
            {"id": "inactive-new", "name": "Тестовый ингредиент", "type": "GOODS"},
            {"id": "previous-workup", "name": "Старая проработка", "type": "GOODS"},
            {"id": "inactive-old", "name": "Старая тестовая позиция", "type": "GOODS"},
            {"id": "food", "name": "Сыр", "type": "GOODS"},
            {"id": "gloves", "name": "Перчатки винил", "type": "GOODS"},
            {"id": "uncertain-pack", "name": "Набор новый", "type": "GOODS"},
            {"id": "llm-workup", "name": "Тестовая приправа", "type": "GOODS"},
            {
                "id": "active-dish",
                "name": "Пицца",
                "type": "DISH",
                "defaultIncludedInMenu": True,
                "deleted": False,
            },
            {
                "id": "inactive-dish",
                "name": "Пицца тест",
                "type": "DISH",
                "defaultIncludedInMenu": False,
                "deleted": False,
            },
        ]
        charts = {
            "assemblyCharts": [
                {
                    "assembledProductId": "active-dish",
                    "dateFrom": "2026-01-01",
                    "dateTo": None,
                    "items": [{"productId": "food"}],
                },
                {
                    "assembledProductId": "inactive-dish",
                    "dateFrom": "2026-01-01",
                    "dateTo": None,
                    "items": [
                        {"productId": "inactive-new"},
                        {"productId": "previous-workup"},
                        {"productId": "inactive-old"},
                    ],
                },
            ],
            "preparedCharts": [],
        }

        async with async_session_factory() as session:
            # Два июньских заказа плюс июльский — третий. По правилу владельца проработка
            # для этого товара исчерпана, и решать его должна постоянная статья.
            session.add(
                PnlIikoProductObservation(
                    period_month=date(2026, 6, 1),
                    source_kind="incoming_invoice",
                    iiko_product_guid="inactive-old",
                    product_name="Старая тестовая позиция",
                    product_code="P-2",
                    amount=Decimal("70.00"),
                    rows_count=2,
                )
            )
            session.add(
                PnlProductMonthlyDecision(
                    period_month=date(2026, 6, 1),
                    iiko_product_guid="previous-workup",
                    source_kind="incoming_invoice",
                    decision_kind="workup",
                )
            )
            await session.commit()

            result = await goods_classifier.auto_classify_new_goods(
                session,
                month_start=date(2026, 7, 1),
                month_end=date(2026, 7, 31),
                observations=observations,
                products_payload=products,
                charts_payload=charts,
                settings=Settings(anthropic_api_key="test-key"),
            )
            await session.commit()

            # inactive-new и llm-workup — первые заказы, previous-workup — второй: все три
            # законно уходят в Проработку. Третьего заказа нет ни у кого из них.
            assert result.workup == 3
            assert result.classified == 4
            assert result.review == 0

            rules = {
                rule.iiko_product_guid: rule
                for rule in (await session.execute(select(PnlProductWhitelist))).scalars()
            }
            assert rules["food"].include_status == "stocked"
            assert rules["food"].source_kind == "inventory"
            assert rules["food"].line_code is None
            assert rules["gloves"].include_status == "include"
            assert rules["gloves"].source_kind == "incoming_invoice"
            assert rules["gloves"].line_code == "aux_goods"
            assert rules["uncertain-pack"].include_status == "stocked"
            assert rules["uncertain-pack"].source_kind == "inventory"
            # Вторая проработка подряд остаётся проработкой, а не вопросом владельцу.
            assert "previous-workup" not in rules
            assert await session.scalar(
                select(PnlProductMonthlyDecision.id).where(
                    PnlProductMonthlyDecision.period_month == date(2026, 7, 1),
                    PnlProductMonthlyDecision.iiko_product_guid == "previous-workup",
                )
            )
            # Третий заказ закрывает проработку постоянной статьёй.
            assert rules["inactive-old"].include_status == "include"
            assert rules["inactive-old"].line_code == "shop_maintenance"

            july_workup = await session.scalar(
                select(PnlProductMonthlyDecision).where(
                    PnlProductMonthlyDecision.period_month == date(2026, 7, 1),
                    PnlProductMonthlyDecision.iiko_product_guid == "inactive-new",
                )
            )
            assert july_workup is not None
            assert july_workup.source_kind == "incoming_invoice"
            llm_workup = await session.scalar(
                select(PnlProductMonthlyDecision).where(
                    PnlProductMonthlyDecision.period_month == date(2026, 7, 1),
                    PnlProductMonthlyDecision.iiko_product_guid == "llm-workup",
                )
            )
            assert llm_workup is not None
            assert "следующем месяце" in (llm_workup.note or "")

    asyncio.run(scenario())


def test_active_dish_resolves_pending_question_but_not_owner_decision(
    async_session_factory,
) -> None:
    """Правило 3: блюдо включили в продажу — вопрос снят фактом, а не ответом.

    Своё автоматическое «нужен ответ владельца» разметка обязана пересматривать, иначе товар
    висит без статьи навсегда, хотя блюдо с ним уже продаётся. А пометку ЧЕЛОВЕКА — того же
    вида — не трогает: это его осознанная отсрочка.
    """

    async def scenario() -> None:
        async with async_session_factory() as session:
            owner = User(
                id=uuid.uuid4(),
                email=f"pnl-goods-{uuid.uuid4()}@test.local",
                hashed_password="hashed",
                full_name="Владелец",
                is_active=True,
            )
            session.add(owner)
            await session.flush()
            session.add(
                PnlProductWhitelist(
                    iiko_product_guid="pending-auto",
                    source_kind="incoming_invoice",
                    line_code=None,
                    include_status="requires_owner_review",
                    product_name="Спорный ингредиент",
                    note="Автоматически: модель не дала уверенного решения",
                    updated_by_user_id=None,
                )
            )
            session.add(
                PnlProductWhitelist(
                    iiko_product_guid="pending-owner",
                    source_kind="incoming_invoice",
                    line_code=None,
                    include_status="requires_owner_review",
                    product_name="Отложено владельцем",
                    note="Разберусь позже",
                    updated_by_user_id=owner.id,
                )
            )
            await session.commit()

            observations = [
                iiko_sync.GoodsProductObservation(
                    source_kind="incoming_invoice",
                    iiko_product_guid=guid,
                    product_name=guid,
                    product_code=None,
                    amount=Decimal("10.00"),
                    rows_count=1,
                )
                for guid in ("pending-auto", "pending-owner")
            ]
            products = [
                {"id": "pending-auto", "name": "Спорный ингредиент", "type": "GOODS"},
                {"id": "pending-owner", "name": "Отложено владельцем", "type": "GOODS"},
                {
                    "id": "sold-dish",
                    "name": "Новинка",
                    "type": "DISH",
                    "defaultIncludedInMenu": True,
                    "deleted": False,
                },
            ]
            charts = {
                "assemblyCharts": [
                    {
                        "assembledProductId": "sold-dish",
                        "dateFrom": "2026-01-01",
                        "dateTo": None,
                        "items": [{"productId": "pending-auto"}, {"productId": "pending-owner"}],
                    }
                ],
                "preparedCharts": [],
            }

            await goods_classifier.auto_classify_new_goods(
                session,
                month_start=date(2026, 7, 1),
                month_end=date(2026, 7, 31),
                observations=observations,
                products_payload=products,
                charts_payload=charts,
                settings=Settings(anthropic_api_key="test-key"),
            )
            await session.commit()

            rules = {
                rule.iiko_product_guid: rule
                for rule in (await session.execute(select(PnlProductWhitelist))).scalars()
                if rule.iiko_product_guid.startswith("pending-")
            }
            # Одно правило на товар, а не второе рядом со старым.
            assert len(rules) == 2
            assert rules["pending-auto"].include_status == "stocked"
            assert rules["pending-auto"].source_kind == "inventory"
            assert "активное блюдо" in (rules["pending-auto"].note or "")
            assert rules["pending-owner"].include_status == "requires_owner_review"
            assert rules["pending-owner"].updated_by_user_id == owner.id

    asyncio.run(scenario())


def test_recognition_reasons_distinguish_document_expectations() -> None:
    assert recognition_reason(ORIGIN_DOCUMENT) == "Расход признан по документу контрагента"
    assert "не требуется" in recognition_reason(ORIGIN_BY_TARIFF)
    assert "ожидается" in recognition_reason(ORIGIN_AWAITING_DOCUMENT)


def test_recognition_ledger_excludes_non_pnl_loan_waiting(
    async_session_factory,
) -> None:
    """Выдача займа остаётся вне ОПиУ и не требует закрывающего документа."""
    # Хелперы лежат в tests/counterparties и подключаются к пути так же, как у соседних
    # тестов: без этого файл проходит только когда его запускают вместе с ними, а в
    # одиночку падает на ModuleNotFoundError.
    import sys
    from pathlib import Path

    sys.path.append(str(Path(__file__).parent / "counterparties"))

    from cp_helpers import make_counterparty, make_wallet

    async def scenario() -> None:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Павел-заём", inn="6155019991")
            wallet = await make_wallet(session, code="loan-wallet", name="Банк займа")
            article = DdsArticle(
                code="test_vydacha_kreditov_i_zaimov",
                name="Выдача кредитов и займов — тест",
                movement_type="outflow",
                activity_type="investing",
            )
            session.add(article)
            await session.flush()
            session.add(
                PnlArticleRule(
                    article_id=article.id,
                    line_code=None,
                    in_pnl=False,
                    owner_stream="financing",
                    sign=1,
                    applies_to="both",
                    is_active=True,
                )
            )
            transaction = CashflowTransaction(
                wallet_id=wallet.id,
                direction="out",
                amount=Decimal("30000.00"),
                operation_date=date(2026, 7, 14),
                article_id=article.id,
                counterparty_id=cp.id,
                source_kind="bank_operation",
                payment_purpose="Выдача займа",
                quality_status="manual_override",
            )
            session.add(transaction)
            await session.flush()
            prepayment = SupplierPrepayment(
                counterparty_id=cp.id,
                kind="subscription",
                amount=Decimal("30000.00"),
                amount_settled=Decimal("0.00"),
                status="open",
                cashflow_transaction_id=transaction.id,
                article_id=article.id,
                service_period_status="missing",
            )
            session.add(prepayment)
            await session.commit()

            ledger = await build_recognition_ledger(session, date(2026, 7, 1))

            assert all(row.source_id != prepayment.id for row in ledger.rows)
            assert ledger.totals.waiting_document == Decimal("0.00")

    asyncio.run(scenario())


def test_pnl_iiko_job_refreshes_current_and_previous_month() -> None:
    # В начале месяца прошлый ещё добирает поздние документы.
    assert target_months(date(2026, 8, 4)) == (date(2026, 8, 1), date(2026, 7, 1))
    assert target_months(date(2027, 1, 1)) == (date(2027, 1, 1), date(2026, 12, 1))
    assert target_months(date(2026, 8, 10)) == (date(2026, 8, 1), date(2026, 7, 1))


def test_pnl_iiko_job_stops_touching_the_previous_month_after_the_window() -> None:
    """После десятого числа прошлый месяц застывает — иначе его переписала бы любая разметка.

    Правило разметки глобально и не знает периода: ночной прогон применил бы к июлю то, что
    человек выбрал в августе. Синхронная правка так делать уже не умеет, ночная не должна тоже.
    """
    assert target_months(date(2026, 8, 11)) == (date(2026, 8, 1),)
    assert target_months(date(2026, 8, 31)) == (date(2026, 8, 1),)


def _invoice_observation(month: date, guid: str, amount: str, rows: int = 1):
    return PnlIikoProductObservation(
        period_month=month,
        source_kind="incoming_invoice",
        iiko_product_guid=guid,
        product_name="Товар с историей",
        product_code="H-1",
        amount=Decimal(amount),
        rows_count=rows,
    )


def test_classification_edit_does_not_rewrite_earlier_months(async_session_factory) -> None:
    """Разметка августа не должна менять прибыль июля — он уже показан и сверен.

    Прежде пересчитывались ВСЕ месяцы, где встречался товар: правка на августовской странице
    молча переписывала июль. Владелец назвал это косяком 05.08.2026.
    """

    async def scenario() -> None:
        guid = "retro-guid"
        async with async_session_factory() as session:
            session.add(_invoice_observation(date(2026, 7, 1), guid, "700.00"))
            session.add(_invoice_observation(date(2026, 8, 1), guid, "800.00"))
            await session.commit()

            # Июль размечен и посчитан на своей странице — как это и происходит в жизни.
            await save_goods_classification(
                session,
                display_month=date(2026, 7, 1),
                product_guid=guid,
                source_kind="incoming_invoice",
                status="include",
                line_code="aux_goods",
                note=None,
                user_id=None,
            )
            july = await session.scalar(
                select(PnlIikoFact).where(
                    PnlIikoFact.period_month == date(2026, 7, 1),
                    PnlIikoFact.metric_code == "aux_goods_invoices",
                    PnlIikoFact.direction == "total",
                )
            )
            assert july is not None
            assert july.amount == Decimal("700.00")

            # А теперь тот же товар переразмечают уже на августовской странице.
            await save_goods_classification(
                session,
                display_month=date(2026, 8, 1),
                product_guid=guid,
                source_kind="incoming_invoice",
                status="exclude",
                line_code=None,
                note="С августа считаем иначе",
                user_id=None,
            )
            august = await session.scalar(
                select(PnlIikoFact).where(
                    PnlIikoFact.period_month == date(2026, 8, 1),
                    PnlIikoFact.metric_code == "aux_goods_invoices",
                    PnlIikoFact.direction == "total",
                )
            )
            assert august is not None
            assert august.amount == Decimal("0.00")

            await session.refresh(july)
            assert july.amount == Decimal("700.00")
            july_detail = await session.scalar(
                select(PnlIikoGoodsFact).where(
                    PnlIikoGoodsFact.period_month == date(2026, 7, 1),
                    PnlIikoGoodsFact.iiko_product_guid == guid,
                )
            )
            assert july_detail is not None

    asyncio.run(scenario())


def test_classification_edit_is_refused_in_a_closed_month(async_session_factory) -> None:
    """Закрытый месяц не правится даже на своей странице — замок сильнее направления."""

    async def scenario() -> None:
        guid = "closed-month-guid"
        async with async_session_factory() as session:
            session.add(_invoice_observation(date(2026, 7, 1), guid, "500.00"))
            session.add(AccountingPeriodClose(period_month=date(2026, 7, 1)))
            await session.commit()

            with pytest.raises(accounting_periods.PeriodClosed):
                await save_goods_classification(
                    session,
                    display_month=date(2026, 7, 1),
                    product_guid=guid,
                    source_kind="incoming_invoice",
                    status="include",
                    line_code="aux_goods",
                    note=None,
                    user_id=None,
                )

    asyncio.run(scenario())


def test_forward_rebuild_skips_closed_future_month(async_session_factory) -> None:
    """Правка июля не переписывает август, если август уже закрыли."""

    async def scenario() -> None:
        guid = "closed-forward-guid"
        async with async_session_factory() as session:
            session.add(_invoice_observation(date(2026, 7, 1), guid, "300.00"))
            session.add(_invoice_observation(date(2026, 8, 1), guid, "400.00"))
            session.add(AccountingPeriodClose(period_month=date(2026, 8, 1)))
            await session.commit()

            await save_goods_classification(
                session,
                display_month=date(2026, 7, 1),
                product_guid=guid,
                source_kind="incoming_invoice",
                status="include",
                line_code="aux_goods",
                note=None,
                user_id=None,
            )
            assert await session.scalar(
                select(PnlIikoGoodsFact.id).where(
                    PnlIikoGoodsFact.period_month == date(2026, 7, 1),
                    PnlIikoGoodsFact.iiko_product_guid == guid,
                )
            )
            assert (
                await session.scalar(
                    select(PnlIikoGoodsFact.id).where(
                        PnlIikoGoodsFact.period_month == date(2026, 8, 1),
                        PnlIikoGoodsFact.iiko_product_guid == guid,
                    )
                )
                is None
            )

    asyncio.run(scenario())


def test_bar_audit_products_never_hit_the_cook_audit_line(async_session_factory) -> None:
    """Проведённая барная ревизия не должна попасть ещё и в строку поварской.

    Модуль «Ревизии» не различает вид ревизии: барную туда тоже заводили — документ от
    01.06.2026 на 34 позиции лежит отменённым. Проведи его кто-нибудь, и расхождение барной
    стойки посчиталось бы дважды, в двух разных блоках отчёта. Единственная защита —
    исключение по ``line_code``, и она обязана покрывать все три барные строки, включая
    напитки.
    """

    async def scenario() -> None:
        packaging_guid = "bar-packaging-guid"
        beverage_guid = "bar-beverage-guid"
        cook_guid = "cook-raw-guid"
        async with async_session_factory() as session:
            for guid, line_code in (
                (packaging_guid, "packaging_inventory"),
                (beverage_guid, "beverage_inventory"),
            ):
                session.add(
                    PnlProductWhitelist(
                        iiko_product_guid=guid,
                        source_kind="inventory",
                        line_code=line_code,
                        include_status="stocked",
                        product_name=guid,
                    )
                )
            audit = InventoryAudit(business_date=date(2026, 7, 20), status="applied")
            session.add(audit)
            await session.flush()
            for guid, shortage in (
                (packaging_guid, "1000.00"),
                (beverage_guid, "500.00"),
                (cook_guid, "300.00"),
            ):
                session.add(
                    InventoryAuditItem(
                        audit_id=audit.id,
                        iiko_product_guid=guid,
                        product_name_snapshot=guid,
                        shortage_amount=Decimal(shortage),
                        # У недостачи знаковый amount отрицателен — так его пишет загрузчик
                        # ревизии (``signed_amount = -shortage_amount``). Положительный
                        # amount означал бы излишек, и он уменьшил бы расход.
                        amount=-Decimal(shortage),
                    )
                )
            await session.commit()

            excluded = await load_packaging_guids(session)
            assert {packaging_guid, beverage_guid} <= excluded
            assert cook_guid not in excluded

            month = await build_inventory_month(
                session,
                date(2026, 7, 1),
                date(2026, 7, 31),
                packaging_guids=excluded,
            )
            # В строке поварской ревизии остаётся только сырьё.
            assert month.product_result == Decimal("300.00")

    asyncio.run(scenario())


def test_stocked_product_keeps_its_bar_audit_line(async_session_factory) -> None:
    """Повторное «Складской учёт» не должно стирать привязку к строке барной ревизии.

    До правки экран не показывал строку у складского товара и не давал её выбрать: единственный
    пункт слал ``line_code = null``, и один клик молча уносил упаковку из своей строки. Вернуть
    привязку было нечем — автоматика ``stocked`` не пересматривает. Ровно так уже пропала
    разметка напитков.
    """

    async def scenario() -> None:
        guid = "bar-line-keeper"
        async with async_session_factory() as session:
            session.add(
                PnlIikoProductObservation(
                    period_month=date(2026, 7, 1),
                    source_kind="inventory",
                    iiko_product_guid=guid,
                    product_name="Стакан барный",
                    product_code="BAR-1",
                    amount=Decimal("-40.00"),
                    rows_count=1,
                )
            )
            await session.commit()

            saved = await save_goods_classification(
                session,
                display_month=date(2026, 7, 1),
                product_guid=guid,
                source_kind="inventory",
                status="stocked",
                line_code="packaging_inventory",
                note=None,
                user_id=None,
            )
            row = next(item for item in saved.rows if item.product_guid == guid)
            assert row.status == "stocked"
            assert row.line_code == "packaging_inventory"

            # Экран предлагает эту строку — иначе выбрать её было бы нечем.
            assert "packaging_inventory" in {
                option.line_code for option in saved.options if option.source_kind == "inventory"
            }

            # Тот же товар сохраняют как «включён» — источник инвентаризации не знает разницы
            # между include и stocked, и привязка обязана уцелеть.
            again = await save_goods_classification(
                session,
                display_month=date(2026, 7, 1),
                product_guid=guid,
                source_kind="inventory",
                status="include",
                line_code="packaging_inventory",
                note=None,
                user_id=None,
            )
            row = next(item for item in again.rows if item.product_guid == guid)
            assert row.status == "stocked"
            assert row.line_code == "packaging_inventory"

    asyncio.run(scenario())


def test_classification_edit_keeps_bar_audit_details(async_session_factory) -> None:
    """Правка разметки не должна стирать расшифровку барной ревизии и обязана её пересобрать."""

    async def scenario() -> None:
        guid = "bar-detail-guid"
        async with async_session_factory() as session:
            session.add(
                PnlIikoProductObservation(
                    period_month=date(2026, 7, 1),
                    source_kind="inventory",
                    iiko_product_guid=guid,
                    product_name="Кола 0.5",
                    product_code="COLA-1",
                    amount=Decimal("-90.00"),
                    rows_count=1,
                )
            )
            session.add(
                PnlProductWhitelist(
                    iiko_product_guid=guid,
                    source_kind="inventory",
                    line_code="packaging_inventory",
                    include_status="stocked",
                    product_name="Кола 0.5",
                )
            )
            session.add(
                PnlIikoGoodsFact(
                    period_month=date(2026, 7, 1),
                    metric_code="packaging_result",
                    line_code="packaging_inventory",
                    source_kind="inventory",
                    iiko_product_guid=guid,
                    product_name="Кола 0.5",
                    amount=Decimal("-90.00"),
                    rows_count=1,
                )
            )
            session.add(
                PnlIikoFact(
                    period_month=date(2026, 7, 1),
                    metric_code="packaging_result",
                    direction="total",
                    amount=Decimal("-90.00"),
                    rows_count=1,
                    source_ref="/reports/storeOperations",
                )
            )
            await session.commit()

            # Товар переносят из упаковки в напитки — обе величины обязаны переехать.
            await save_goods_classification(
                session,
                display_month=date(2026, 7, 1),
                product_guid=guid,
                source_kind="inventory",
                status="stocked",
                line_code="beverage_inventory",
                note=None,
                user_id=None,
            )

            detail = await session.scalar(
                select(PnlIikoGoodsFact).where(
                    PnlIikoGoodsFact.period_month == date(2026, 7, 1),
                    PnlIikoGoodsFact.iiko_product_guid == guid,
                )
            )
            assert detail is not None, "расшифровка барной строки не должна пропадать"
            assert detail.metric_code == "beverage_result"
            beverage = await session.scalar(
                select(PnlIikoFact).where(
                    PnlIikoFact.period_month == date(2026, 7, 1),
                    PnlIikoFact.metric_code == "beverage_result",
                )
            )
            assert beverage is not None
            assert beverage.amount == Decimal("-90.00")
            packaging = await session.scalar(
                select(PnlIikoFact).where(
                    PnlIikoFact.period_month == date(2026, 7, 1),
                    PnlIikoFact.metric_code == "packaging_result",
                )
            )
            assert packaging is not None
            assert packaging.amount == Decimal("0.00")
            # Корзина, про которую ничего не известно, остаётся без факта: ноль в отчёте
            # читается как утверждение, а тут утверждать нечего.
            assert (
                await session.scalar(
                    select(PnlIikoFact).where(
                        PnlIikoFact.period_month == date(2026, 7, 1),
                        PnlIikoFact.metric_code == "pizza_box_result",
                    )
                )
                is None
            )

    asyncio.run(scenario())


def test_invoice_expense_product_never_becomes_an_audit_loss(async_session_factory) -> None:
    """Товар с расходом по факту покупки не попадает в ревизионную потерю.

    Правило владельца 05.08.2026: перчатки, шпажки, полотенца, чековая лента в складском учёте
    не участвуют, расход признаётся по закупке, в балансе их нет. Значит и недостача по ним
    расходом быть не может — за товар заплатили бы дважды. Барную ревизию заводили в модуль
    «Ревизии» (документ 01.06.2026), так что путь для такого задвоения открыт.
    """

    async def scenario() -> None:
        gloves = "invoice-expense-gloves"
        raw = "cook-raw-product"
        async with async_session_factory() as session:
            session.add(
                PnlProductWhitelist(
                    iiko_product_guid=gloves,
                    source_kind="incoming_invoice",
                    line_code="aux_goods",
                    include_status="include",
                    product_name="Перчатки винил",
                )
            )
            audit = InventoryAudit(business_date=date(2026, 7, 6), status="applied")
            session.add(audit)
            await session.flush()
            for guid, shortage in ((gloves, "2807.45"), (raw, "500.00")):
                session.add(
                    InventoryAuditItem(
                        audit_id=audit.id,
                        iiko_product_guid=guid,
                        product_name_snapshot=guid,
                        shortage_amount=Decimal(shortage),
                        # У недостачи знаковый amount отрицателен — так его пишет загрузчик
                        # ревизии (``signed_amount = -shortage_amount``). Положительный
                        # amount означал бы излишек, и он уменьшил бы расход.
                        amount=-Decimal(shortage),
                    )
                )
            await session.commit()

            excluded = await load_packaging_guids(session)
            assert gloves in excluded
            assert raw not in excluded

            month = await build_inventory_month(
                session,
                date(2026, 7, 1),
                date(2026, 7, 31),
                packaging_guids=excluded,
            )
            assert month.product_result == Decimal("500.00")

    asyncio.run(scenario())


def test_writeoff_observations_keep_names_behind_the_total() -> None:
    """Строка одна, но номенклатуру храним: без имён задвоение проработки невидимо."""
    payload = {
        "response": [
            {
                "status": "PROCESSED",
                "items": [
                    {"productId": "cup", "cost": "656.00"},
                    {"productId": "cup", "cost": "44.00"},
                    {"productId": "cheese", "cost": "1200.00"},
                ],
            },
            # Черновик не списание: товар физически на месте.
            {"status": "NEW", "items": [{"productId": "cup", "cost": "9999.00"}]},
        ]
    }
    assert iiko_sync.writeoff_total(payload) == Decimal("1900.00")

    observations = iiko_sync.writeoff_observations(
        payload, catalog={"cup": ("Контейнер 300мл", "C-1")}
    )
    by_guid = {item.iiko_product_guid: item for item in observations}
    assert by_guid["cup"].amount == Decimal("700.00")
    assert by_guid["cup"].rows_count == 2
    assert by_guid["cup"].product_name == "Контейнер 300мл"
    assert by_guid["cheese"].amount == Decimal("1200.00")
    # Товар без имени в каталоге всё равно сохраняется: пропасть он не должен.
    assert by_guid["cheese"].product_name is None
    assert sum(item.amount for item in observations) == iiko_sync.writeoff_total(payload)


def test_workup_written_off_by_act_is_reported_as_double_expense(async_session_factory) -> None:
    """Проработка, списанная ещё и актом, — расход дважды, и это должно быть видно.

    Товар проработки не заводят в складской учёт, поэтому его расход признаётся сразу по
    приходной накладной. Управляющий, списавший тот же товар актом, добавляет его
    себестоимость в «Списание продукции и сырья» — тот же расход вторым разом.
    """

    async def scenario() -> None:
        guid = "workup-and-writeoff"
        async with async_session_factory() as session:
            session.add(
                PnlIikoProductObservation(
                    period_month=date(2026, 7, 1),
                    source_kind="incoming_invoice",
                    iiko_product_guid=guid,
                    product_name="Контейнер 300мл",
                    product_code="C-1",
                    amount=Decimal("656.00"),
                    rows_count=1,
                )
            )
            session.add(
                PnlProductMonthlyDecision(
                    period_month=date(2026, 7, 1),
                    iiko_product_guid=guid,
                    source_kind="incoming_invoice",
                    decision_kind="workup",
                )
            )
            session.add(
                PnlIikoWriteoffFact(
                    period_month=date(2026, 7, 1),
                    iiko_product_guid=guid,
                    product_name="Контейнер 300мл",
                    amount=Decimal("612.00"),
                    rows_count=1,
                )
            )
            # Списанное сырьё, которое проработкой не было, — это не задвоение.
            session.add(
                PnlIikoWriteoffFact(
                    period_month=date(2026, 7, 1),
                    iiko_product_guid="plain-raw",
                    product_name="Семга",
                    amount=Decimal("5000.00"),
                    rows_count=2,
                )
            )
            await session.commit()

            overlaps = await iiko_source.month_workup_writeoff_overlap(session, date(2026, 7, 1))
            assert len(overlaps) == 1
            assert overlaps[0].product_name == "Контейнер 300мл"
            assert overlaps[0].workup_amount == Decimal("656.00")
            assert overlaps[0].writeoff_amount == Decimal("612.00")

            report = await projector.build_report(session, date(2026, 7, 1))
            warning = next(
                item for item in report.warnings if item.code == "workup_written_off_twice"
            )
            assert "Контейнер 300мл" in warning.message
            assert warning.line_code == "goods_workup"
            # Названа сумма именно повторного расхода, а не суммы обеих строк.
            assert "612,00" in warning.message
            assert "Семга" not in warning.message

    asyncio.run(scenario())


def test_rebuild_leaves_unsynced_month_untouched(async_session_factory) -> None:
    """В месяце без выгрузки iiko метрика не обнуляется: ноль там значил бы «нет данных»."""

    async def scenario() -> None:
        async with async_session_factory() as session:
            session.add(
                PnlIikoFact(
                    period_month=date(2026, 5, 1),
                    metric_code="aux_goods_invoices",
                    direction="total",
                    amount=Decimal("1234.00"),
                    rows_count=3,
                    source_ref="manual-backfill",
                )
            )
            await session.commit()

            await rebuild_goods_from_observations(session, date(2026, 5, 1), "incoming_invoice")

            fact = await session.scalar(
                select(PnlIikoFact).where(
                    PnlIikoFact.period_month == date(2026, 5, 1),
                    PnlIikoFact.metric_code == "aux_goods_invoices",
                )
            )
            assert fact is not None
            assert fact.amount == Decimal("1234.00")

    asyncio.run(scenario())
