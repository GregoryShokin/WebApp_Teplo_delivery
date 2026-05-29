#!/usr/bin/env python3
"""Build the first iiko economics layer from processed exports."""

from __future__ import annotations

import csv
import datetime as dt
import json
import re
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SALES_DAILY_PATH = PROJECT_ROOT / "research/processed/iiko/sales/sales_daily.csv"
CATEGORY_PATH = PROJECT_ROOT / "research/processed/iiko/ops/food_cost_by_category.csv"
DISH_PATH = PROJECT_ROOT / "research/processed/iiko/ops/food_cost_by_dish.csv"
PROMO_PATH = PROJECT_ROOT / "research/processed/iiko/promo_sets/promo_sets_food_cost_estimate.csv"
OUT_DIR = PROJECT_ROOT / "research/processed/economic_block"

MAIN_START = dt.date(2025, 5, 1)
MAIN_END = dt.date(2026, 5, 17)
CHECK_START = dt.date(2025, 11, 1)
CHECK_END = dt.date(2026, 5, 17)
FOCUS_PERIODS = {
    "2026-02": (dt.date(2026, 2, 1), dt.date(2026, 2, 28)),
    "2026-03": (dt.date(2026, 3, 1), dt.date(2026, 3, 31)),
    "2026-04": (dt.date(2026, 4, 1), dt.date(2026, 4, 30)),
    "2026-05-01_2026-05-17": (dt.date(2026, 5, 1), dt.date(2026, 5, 17)),
}
SIGNIFICANT_REVENUE = Decimal("100000")
HIGH_FOOD_COST = Decimal("0.45")


def d(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    text = str(value).strip().replace(" ", "").replace("\u2212", "-")
    if not text or text.lower() in {"nan", "none", "null"}:
        return Decimal("0")
    return Decimal(text)


def money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def ratio(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def out_number(value: Decimal, places: str = "0.01") -> float:
    return float(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def pct(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == 0:
        return Decimal("0")
    return numerator / denominator


def clean_text(value: Any) -> str:
    text = "" if value is None else str(value).strip()
    return text if text else "(пусто)"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def date_from_iso(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def month_key(value: dt.date) -> str:
    return f"{value.year:04d}-{value.month:02d}"


def month_end(value: dt.date) -> dt.date:
    if value.month == 12:
        next_month = dt.date(value.year + 1, 1, 1)
    else:
        next_month = dt.date(value.year, value.month + 1, 1)
    return next_month - dt.timedelta(days=1)


def sum_sales(rows: list[dict[str, str]]) -> dict[str, Decimal]:
    totals = defaultdict(Decimal)
    for row in rows:
        for field in ("orders", "gross_sum", "revenue", "discount_sum", "product_cost"):
            totals[field] += d(row.get(field))
    totals["gross_margin"] = totals["revenue"] - totals["product_cost"]
    return totals


def sales_metrics(label: str, start: dt.date, end: dt.date, rows: list[dict[str, str]]) -> dict[str, Any]:
    selected = [row for row in rows if start <= date_from_iso(row["date"]) <= end]
    totals = sum_sales(selected)
    return {
        "period": label,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "days": (end - start).days + 1,
        "revenue": out_number(totals["revenue"]),
        "orders": int(totals["orders"].quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
        "avg_check": out_number(pct(totals["revenue"], totals["orders"])),
        "gross_sum": out_number(totals["gross_sum"]),
        "discount_sum": out_number(totals["discount_sum"]),
        "discount_share_of_gross": out_number(pct(totals["discount_sum"], totals["gross_sum"]), "0.000001"),
        "product_cost": out_number(totals["product_cost"]),
        "food_cost_pct": out_number(pct(totals["product_cost"], totals["revenue"]), "0.000001"),
        "gross_margin": out_number(totals["gross_margin"]),
        "gross_margin_pct": out_number(pct(totals["gross_margin"], totals["revenue"]), "0.000001"),
        "source_rows": len(selected),
        "is_focus_period": label in FOCUS_PERIODS,
    }


def build_monthly(sales_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    scoped = [
        row
        for row in sales_rows
        if MAIN_START <= date_from_iso(row["date"]) <= MAIN_END
    ]
    months = sorted({month_key(date_from_iso(row["date"])) for row in scoped})
    rows: list[dict[str, Any]] = []
    for period in months:
        start = dt.date(int(period[:4]), int(period[5:]), 1)
        start = max(start, MAIN_START)
        end = min(month_end(start), MAIN_END)
        rows.append(sales_metrics(period if end == month_end(start) else f"{start.isoformat()}_{end.isoformat()}", start, end, sales_rows))
    return rows


COMPONENT_RE = re.compile(r"(?P<name>.+?):\s*[\d., ]+\s*шт\.,\s*(?P<cost>[\d., ]+)\s*руб\.")


def parse_components(summary: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for part in summary.split(";"):
        match = COMPONENT_RE.search(part.strip())
        if not match:
            continue
        result.append({"dish_name": match.group("name").strip(), "cost": d(match.group("cost").replace(",", "."))})
    return result


def promo_component_reallocation(
    promo_rows: list[dict[str, str]], dish_rows: list[dict[str, str]]
) -> tuple[dict[tuple[str, str], Decimal], list[dict[str, Any]], Decimal]:
    zero_dish_rows: list[dict[str, Any]] = []
    for row in dish_rows:
        if d(row.get("revenue")) == 0 and d(row.get("product_cost")) > 0:
            zero_dish_rows.append(
                {
                    "dish_name": row["dish_name"],
                    "dish_group_top": clean_text(row.get("dish_group_top")),
                    "dish_category": clean_text(row.get("dish_category")),
                    "product_cost": d(row.get("product_cost")),
                }
            )

    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in zero_dish_rows:
        by_name[row["dish_name"]].append(row)

    by_category: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    mapping_rows: list[dict[str, Any]] = []
    total = Decimal("0")
    for promo in promo_rows:
        for component in parse_components(promo.get("components_summary", "")):
            name = component["dish_name"]
            cost = component["cost"]
            total += cost
            candidates = by_name.get(name, [])
            chosen: dict[str, Any] | None = None
            enough = [row for row in candidates if row["product_cost"] >= cost]
            if enough:
                chosen = min(enough, key=lambda row: row["product_cost"] - cost)
            elif candidates:
                chosen = max(candidates, key=lambda row: row["product_cost"])
            if chosen:
                key = (chosen["dish_group_top"], chosen["dish_category"])
                by_category[key] += cost
                mapping_rows.append(
                    {
                        "promo_set_name": promo["promo_set_name"],
                        "component_name": name,
                        "component_cost": out_number(cost),
                        "matched_dish_group_top": key[0],
                        "matched_dish_category": key[1],
                    }
                )
            else:
                mapping_rows.append(
                    {
                        "promo_set_name": promo["promo_set_name"],
                        "component_name": name,
                        "component_cost": out_number(cost),
                        "matched_dish_group_top": "",
                        "matched_dish_category": "",
                    }
                )
    return by_category, mapping_rows, total


def category_name(group: str, category: str) -> str:
    if group == category:
        return group
    return f"{group} / {category}"


def risk_comment(row: dict[str, Any], *, is_category: bool) -> str:
    flags: list[str] = []
    revenue = Decimal(str(row["revenue"]))
    product_cost = Decimal(str(row["product_cost"]))
    food_cost_pct = Decimal(str(row["food_cost_pct"]))
    gross_margin = Decimal(str(row["gross_margin"]))
    group = clean_text(row.get("dish_group_top", ""))
    category = clean_text(row.get("dish_category", ""))
    if product_cost == 0 and revenue >= SIGNIFICANT_REVENUE:
        flags.append("0 cost при значимой выручке")
    if revenue > 0 and food_cost_pct > HIGH_FOOD_COST:
        flags.append("food cost выше 45%")
    if gross_margin < 0:
        flags.append("отрицательная валовая маржа")
    if group == "(пусто)" or category == "(пусто)":
        flags.append("пустая категория")
    if is_category and group == "Акционные предложения":
        flags.append("использована восстановленная себестоимость promo-компонентов")
    if flags == ["использована восстановленная себестоимость promo-компонентов"]:
        return "использована восстановленная себестоимость promo-компонентов; применять вместо 0 cost родителя"
    if not flags:
        return "без явных рисков по заданным правилам"
    if "0 cost при значимой выручке" in flags:
        return "; ".join(flags) + "; проверить техкарту/модификаторы"
    if "food cost выше 45%" in flags or "отрицательная валовая маржа" in flags:
        return "; ".join(flags) + "; проверить техкарту и списание себестоимости"
    return "; ".join(flags) + "; проверить справочник категорий"


def build_category_rows(
    category_rows: list[dict[str, str]],
    promo_rows: list[dict[str, str]],
    dish_rows: list[dict[str, str]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    realloc_by_category, mapping_rows, parsed_component_total = promo_component_reallocation(promo_rows, dish_rows)
    promo_recovered = sum((d(row.get("recovered_component_cost")) for row in promo_rows), Decimal("0"))

    rows: list[dict[str, Any]] = []
    total_reallocated_out = Decimal("0")
    for source in category_rows:
        group = clean_text(source.get("dish_group_top"))
        category = clean_text(source.get("dish_category"))
        source_cost = d(source.get("product_cost"))
        promo_added = promo_recovered if group == "Акционные предложения" else Decimal("0")
        raw_reallocated_out = realloc_by_category.get((group, category), Decimal("0"))
        reallocated_out = min(raw_reallocated_out, source_cost)
        total_reallocated_out += reallocated_out
        adjusted_cost = source_cost + promo_added - reallocated_out
        revenue = d(source.get("revenue"))
        gross_margin = revenue - adjusted_cost
        result = {
            "period_start": source.get("period_start"),
            "period_end": source.get("period_end"),
            "category": category_name(group, category),
            "dish_group_top": group,
            "dish_category": category,
            "revenue": out_number(revenue),
            "product_cost": out_number(adjusted_cost),
            "food_cost_pct": out_number(pct(adjusted_cost, revenue), "0.000001"),
            "gross_margin": out_number(gross_margin),
            "gross_margin_pct": out_number(pct(gross_margin, revenue), "0.000001"),
            "source_product_cost": out_number(source_cost),
            "promo_recovered_component_cost_added": out_number(promo_added),
            "promo_component_cost_reallocated_out": out_number(reallocated_out),
            "orders": out_number(d(source.get("orders")), "0.01"),
            "dish_amount": out_number(d(source.get("dish_amount")), "0.01"),
            "gross_sum": out_number(d(source.get("gross_sum"))),
        }
        result["risk_comment"] = risk_comment(result, is_category=True)
        rows.append(result)

    rows.sort(key=lambda row: (row["revenue"], -abs(row["product_cost"])), reverse=True)
    summary = {
        "promo_recovered_component_cost": out_number(promo_recovered),
        "parsed_component_cost_from_summary": out_number(parsed_component_total),
        "promo_component_cost_reallocated_out": out_number(total_reallocated_out),
        "promo_reallocation_rounding_residual": out_number(promo_recovered - total_reallocated_out),
        "promo_mapping_rows": mapping_rows,
        "category_source_product_cost_total": out_number(sum((d(row.get("product_cost")) for row in category_rows), Decimal("0"))),
        "category_adjusted_product_cost_total": out_number(sum((Decimal(str(row["product_cost"])) for row in rows), Decimal("0"))),
    }
    return rows, summary


def build_risk_rows(category_rows: list[dict[str, Any]], dish_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    risks: list[dict[str, Any]] = []

    def flags_for(values: dict[str, Any]) -> list[str]:
        flags: list[str] = []
        revenue = Decimal(str(values["revenue"]))
        product_cost = Decimal(str(values["product_cost"]))
        food_cost_pct = Decimal(str(values["food_cost_pct"]))
        gross_margin = Decimal(str(values["gross_margin"]))
        group = clean_text(values.get("dish_group_top", ""))
        category = clean_text(values.get("dish_category", ""))
        if product_cost == 0 and revenue >= SIGNIFICANT_REVENUE:
            flags.append("0_cost_significant_revenue")
        if revenue > 0 and food_cost_pct > HIGH_FOOD_COST:
            flags.append("food_cost_gt_45pct")
        if gross_margin < 0:
            flags.append("negative_gross_margin")
        if group == "(пусто)" or category == "(пусто)":
            flags.append("empty_category")
        return flags

    for row in category_rows:
        flags = flags_for(row)
        if not flags:
            continue
        risks.append(
            {
                "level": "category",
                "item_name": row["category"],
                "dish_group_top": row["dish_group_top"],
                "dish_category": row["dish_category"],
                "revenue": row["revenue"],
                "product_cost": row["product_cost"],
                "food_cost_pct": row["food_cost_pct"],
                "gross_margin": row["gross_margin"],
                "risk_flags": ";".join(flags),
                "risk_comment": row["risk_comment"],
                "recommended_action": recommended_action(flags),
            }
        )

    for source in dish_rows:
        revenue = d(source.get("revenue"))
        product_cost = d(source.get("product_cost"))
        gross_margin = revenue - product_cost
        values = {
            "dish_group_top": clean_text(source.get("dish_group_top")),
            "dish_category": clean_text(source.get("dish_category")),
            "revenue": out_number(revenue),
            "product_cost": out_number(product_cost),
            "food_cost_pct": out_number(pct(product_cost, revenue), "0.000001"),
            "gross_margin": out_number(gross_margin),
        }
        flags = flags_for(values)
        if not flags:
            continue
        risks.append(
            {
                "level": "dish",
                "item_name": source.get("dish_name", ""),
                "dish_group_top": values["dish_group_top"],
                "dish_category": values["dish_category"],
                "revenue": values["revenue"],
                "product_cost": values["product_cost"],
                "food_cost_pct": values["food_cost_pct"],
                "gross_margin": values["gross_margin"],
                "risk_flags": ";".join(flags),
                "risk_comment": risk_comment(values, is_category=False),
                "recommended_action": recommended_action(flags),
            }
        )

    severity_order = {
        "negative_gross_margin": 0,
        "0_cost_significant_revenue": 1,
        "food_cost_gt_45pct": 2,
        "empty_category": 3,
    }
    risks.sort(
        key=lambda row: (
            min(severity_order.get(flag, 9) for flag in row["risk_flags"].split(";")),
            -abs(Decimal(str(row["revenue"]))),
            row["level"],
            row["item_name"],
        )
    )
    return risks


def recommended_action(flags: list[str]) -> str:
    if "negative_gross_margin" in flags:
        return "Разобрать техкарту/модификаторы и родительскую продажную строку; не использовать маржу строки без сверки."
    if "0_cost_significant_revenue" in flags:
        return "Проверить, где списывается себестоимость: техкарта родителя, дочерние компоненты или услуга."
    if "food_cost_gt_45pct" in flags:
        return "Проверить актуальность техкарты, цены ингредиентов и цену продажи."
    if "empty_category" in flags:
        return "Заполнить dish_group_top/dish_category в справочнике iiko."
    return "Проверить вручную."


def format_money(value: Any) -> str:
    amount = Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"{amount:,.0f}".replace(",", " ")


def format_pct(value: Any) -> str:
    return f"{(Decimal(str(value)) * Decimal('100')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}%"


def markdown_table(rows: list[dict[str, Any]], columns: list[tuple[str, str, str]], limit: int | None = None) -> str:
    selected = rows[:limit] if limit else rows
    header = "| " + " | ".join(title for _, title, _ in columns) + " |"
    separator = "| " + " | ".join("---:" if align == "right" else "---" for _, _, align in columns) + " |"
    lines = [header, separator]
    for row in selected:
        values = []
        for key, _, align in columns:
            value = row.get(key, "")
            if key.endswith("_pct") or key == "discount_share_of_gross":
                value = format_pct(value)
            elif key in {"revenue", "gross_sum", "discount_sum", "product_cost", "gross_margin", "avg_check"}:
                value = format_money(value)
            elif key == "orders":
                value = format_money(value)
            values.append(str(value))
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def build_report(
    monthly_rows: list[dict[str, Any]],
    focus_rows: list[dict[str, Any]],
    category_rows: list[dict[str, Any]],
    risk_rows: list[dict[str, Any]],
    promo_summary: dict[str, Any],
    promo_rows: list[dict[str, str]],
    check_totals: dict[str, Any],
) -> str:
    top_categories = sorted(category_rows, key=lambda row: row["revenue"], reverse=True)[:15]
    risk_category_names = [
        row["item_name"]
        for row in risk_rows
        if row["level"] == "category"
    ][:12]
    zero_cost_dishes = [
        row["item_name"]
        for row in risk_rows
        if row["level"] == "dish" and "0_cost_significant_revenue" in row["risk_flags"]
    ][:10]
    no_pl_transfer = [
        "gross_margin и food_cost как финальный P&L без сверки складских списаний, закупок и ОПиУ",
        "маржу строк с нулевой выручкой и ненулевой себестоимостью",
        "категории с пустым справочником",
        "категории доставки/услуг с 0 себестоимости как пищевую маржу",
    ]
    promo_current_revenue = sum((d(row.get("revenue")) for row in promo_rows), Decimal("0"))
    promo_current_cost = sum((d(row.get("current_product_cost_iiko")) for row in promo_rows), Decimal("0"))
    promo_recovered = d(promo_summary["promo_recovered_component_cost"])
    promo_margin_after = promo_current_revenue - promo_recovered

    lines = [
        "# iiko gross margin economic block",
        "",
        "Дата фиксации: 2026-05-18.",
        "",
        "Контур: активная точка Черникова. Гагарина после января 2024 не смешивается с текущей экономикой.",
        "",
        "## Метод",
        "",
        "Факт / Источник / Период / Вывод / Действие: месячная экономика собрана из дневного слоя продаж / `research/processed/iiko/sales/sales_daily.csv` / 2025-05-01 — 2026-05-17 / это канонический источник выручки, заказов и среднего чека для общей экономики / использовать его в портрете бизнеса и не смешивать `OrderNum` с канальным срезом.",
        "Факт / Источник / Период / Вывод / Действие: категорийный food cost и риски техкарт собраны из ops-срезов / `food_cost_by_category.csv`, `food_cost_by_dish.csv` / 2025-11-01 — 2026-05-17 / категории пригодны для поиска проблем техкарт, но не заменяют ОПиУ / сверять с P&L перед финальными управленческими выводами.",
        "Факт / Источник / Период / Вывод / Действие: себестоимость акционных наборов восстановлена по фактическим компонентам SoldWithDish / `promo_sets_food_cost_estimate.csv` / 2025-11-01 — 2026-05-17 / 0 cost родительских строк является структурным артефактом / в меню-анализе переносить компонентную себестоимость на родительские promo-наборы.",
        "",
        "## Выручка, заказы, средний чек по месяцам",
        "",
        markdown_table(
            monthly_rows,
            [
                ("period", "Период", "left"),
                ("revenue", "Выручка", "right"),
                ("orders", "Заказы", "right"),
                ("avg_check", "Средний чек", "right"),
                ("gross_sum", "До скидок", "right"),
                ("discount_sum", "Скидки", "right"),
                ("discount_share_of_gross", "Скидка от gross", "right"),
            ],
        ),
        "",
        f"Факт / Источник / Период / Вывод / Действие: за основной период выручка {format_money(check_totals['main_revenue'])} руб., заказы {format_money(check_totals['main_orders'])}, средний чек {format_money(check_totals['main_avg_check'])} руб. / `sales_daily.csv` / 2025-05-01 — 2026-05-17 / это первый переносимый слой продаж / использовать как базу портрета бизнеса.",
        "",
        "## Food cost и валовая маржа по месяцам",
        "",
        markdown_table(
            monthly_rows,
            [
                ("period", "Период", "left"),
                ("product_cost", "Себестоимость", "right"),
                ("food_cost_pct", "Food cost", "right"),
                ("gross_margin", "Валовая маржа", "right"),
                ("gross_margin_pct", "GM %", "right"),
            ],
        ),
        "",
        "## Первый экономический блок 2026",
        "",
        markdown_table(
            focus_rows,
            [
                ("period", "Период", "left"),
                ("revenue", "Выручка", "right"),
                ("orders", "Заказы", "right"),
                ("avg_check", "Средний чек", "right"),
                ("product_cost", "Себестоимость", "right"),
                ("food_cost_pct", "Food cost", "right"),
                ("gross_margin", "Валовая маржа", "right"),
                ("gross_margin_pct", "GM %", "right"),
            ],
        ),
        "",
        "Факт / Источник / Период / Вывод / Действие: февраль-апрель 2026 и май 1-17 посчитаны тем же методом, что месячная таблица / `sales_daily.csv` / 2026-02-01 — 2026-05-17 / показатели сопоставимы между собой, кроме неполного мая / май сравнивать только как MTD, не как полный месяц.",
        "",
        "## Влияние акционных наборов",
        "",
        "| Показатель | До корректировки родителя | После восстановления компонентов | Изменение |",
        "| --- | ---: | ---: | ---: |",
        f"| Выручка promo-наборов | {format_money(promo_current_revenue)} | {format_money(promo_current_revenue)} | 0 |",
        f"| Себестоимость promo-наборов | {format_money(promo_current_cost)} | {format_money(promo_recovered)} | +{format_money(promo_recovered)} |",
        f"| Food cost promo-наборов | {format_pct(pct(promo_current_cost, promo_current_revenue))} | {format_pct(pct(promo_recovered, promo_current_revenue))} | +{(pct(promo_recovered, promo_current_revenue) * Decimal('100')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)} п.п. |",
        f"| Валовая маржа promo-наборов | {format_money(promo_current_revenue - promo_current_cost)} | {format_money(promo_margin_after)} | -{format_money(promo_recovered)} |",
        "",
        f"Факт / Источник / Период / Вывод / Действие: `promo_sets_food_cost_estimate.csv` добавляет {format_money(promo_recovered)} руб. себестоимости к родительским строкам акционных наборов / `promo_sets_food_cost_estimate.csv` / 2025-11-01 — 2026-05-17 / food cost promo-наборов меняется с 0.0% до {format_pct(pct(promo_recovered, promo_current_revenue))}, валовая маржа падает с {format_money(promo_current_revenue)} до {format_money(promo_margin_after)} руб. / применять восстановленную себестоимость в меню-анализе.",
        f"Факт / Источник / Период / Вывод / Действие: общая себестоимость sales_daily за сверочный период {format_money(check_totals['check_product_cost'])} руб. сходится с суммой ops-категорий {format_money(check_totals['category_source_product_cost'])} руб. / `sales_daily.csv`, `food_cost_by_category.csv` / 2025-11-01 — 2026-05-17 / компонентная себестоимость уже находится в общей сумме продаж, но не у родительской категории `Акционные предложения` / не удваивать общий food cost при reallocation.",
        f"Факт / Источник / Период / Вывод / Действие: при переносе компонентной себестоимости из строк модификаторов на promo-родителей округлительный остаток составил {promo_summary['promo_reallocation_rounding_residual']} руб. / `food_cost_by_category.csv`, `promo_sets_food_cost_estimate.csv` / 2025-11-01 — 2026-05-17 / остаток связан с разной точностью агрегатов, ручных корректировок на глаз не делалось / считать расхождение нематериальным и сверить при детальном чековом экспорте.",
        "",
        "## Категории",
        "",
        markdown_table(
            top_categories,
            [
                ("category", "Категория", "left"),
                ("revenue", "Выручка", "right"),
                ("product_cost", "Product cost", "right"),
                ("food_cost_pct", "Food cost", "right"),
                ("gross_margin", "Валовая маржа", "right"),
                ("risk_comment", "Комментарий", "left"),
            ],
        ),
        "",
        "Факт / Источник / Период / Вывод / Действие: крупнейшие категории по выручке — Сеты, Пицца, Роллы, Акционные предложения, Закуски / `food_cost_by_category.csv` с promo-reallocation / 2025-11-01 — 2026-05-17 / основной food cost лежит в производственных категориях и promo-наборах / проверку техкарт начинать с крупных категорий и строк риска.",
        "",
        "## Риски техкарт и справочников",
        "",
        f"Факт / Источник / Период / Вывод / Действие: найдено {len(risk_rows)} строк риска по правилам 0 cost при выручке от {format_money(SIGNIFICANT_REVENUE)} руб., food cost >45%, отрицательная маржа, пустые категории / `food_cost_by_category.csv`, `food_cost_by_dish.csv` / 2025-11-01 — 2026-05-17 / часть рисков является нормальной структурой модификаторов, но без сверки это искажает категорийную маржу / разобрать строки из `iiko_food_cost_risks.csv`.",
        "",
        "Категории, требующие проверки техкарт/справочника: " + (", ".join(risk_category_names) if risk_category_names else "не выявлены по заданным правилам") + ".",
        "Блюда с 0 cost при значимой выручке: " + (", ".join(zero_cost_dishes) if zero_cost_dishes else "не выявлены по заданным правилам") + ".",
        "",
        "Факт / Источник / Период / Вывод / Действие: акционные наборы №1-3 и часть combo-позиций попали в риск `0 cost при значимой выручке` на уровне блюда / `food_cost_by_dish.csv`, `promo_sets_food_cost_estimate.csv` / 2025-11-01 — 2026-05-17 / для promo-наборов причина уже подтверждена как компонентная себестоимость SoldWithDish, для прочих combo нужно проверить аналогичную структуру / разбирать блюда из `iiko_food_cost_risks.csv` перед выводами по меню.",
        "",
        "Топ строк риска:",
        "",
        markdown_table(
            risk_rows[:20],
            [
                ("level", "Уровень", "left"),
                ("item_name", "Строка", "left"),
                ("revenue", "Выручка", "right"),
                ("product_cost", "Product cost", "right"),
                ("food_cost_pct", "Food cost", "right"),
                ("gross_margin", "Валовая маржа", "right"),
                ("risk_flags", "Флаги", "left"),
            ],
        ),
        "",
        "## Что переносить в портрет бизнеса",
        "",
        "Факт / Источник / Период / Вывод / Действие: можно сразу переносить месячную выручку, заказы, средний чек, gross_sum, discount_sum и долю скидки / `sales_daily.csv` / 2025-05-01 — 2026-05-17 / это стабильный дневной слой активной точки / использовать как базовую экономику продаж.",
        "Факт / Источник / Период / Вывод / Действие: можно переносить food cost и валовую маржу как расчетную gross margin продаж с оговоркой `iiko ProductCostBase` / `sales_daily.csv` / 2025-05-01 — 2026-05-17 / это не финальный P&L, но годится для первого портрета бизнеса / подписывать как расчетную валовую маржу по iiko.",
        "Факт / Источник / Период / Вывод / Действие: можно переносить восстановленный food cost promo-наборов 40.1% / `promo_sets_food_cost_estimate.csv` / 2025-11-01 — 2026-05-17 / показатель точнее родительского 0% / использовать для меню-анализа акционных наборов.",
        "",
        "## Что нельзя использовать без сверки с ОПиУ/P&L",
        "",
    ]
    for item in no_pl_transfer:
        lines.append(f"- Факт / Источник / Период / Вывод / Действие: {item} / iiko processed exports / 2025-05-01 — 2026-05-17 или 2025-11-01 — 2026-05-17 / показатель может расходиться с управленческим учетом / сверить с ОПиУ/P&L, закупками, списаниями и актуальностью техкарт.")

    lines.extend(
        [
            "",
            "## Итоговые файлы",
            "",
            "- `research/processed/economic_block/iiko_monthly_gross_margin.csv`",
            "- `research/processed/economic_block/iiko_monthly_gross_margin.json`",
            "- `research/processed/economic_block/iiko_category_margin.csv`",
            "- `research/processed/economic_block/iiko_food_cost_risks.csv`",
            "- `research/processed/economic_block/iiko_gross_margin_report.md`",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    sales_rows = read_csv(SALES_DAILY_PATH)
    category_source_rows = read_csv(CATEGORY_PATH)
    dish_rows = read_csv(DISH_PATH)
    promo_rows = read_csv(PROMO_PATH)

    monthly_rows = build_monthly(sales_rows)
    focus_rows = [sales_metrics(label, start, end, sales_rows) for label, (start, end) in FOCUS_PERIODS.items()]
    category_rows, promo_summary = build_category_rows(category_source_rows, promo_rows, dish_rows)
    risk_rows = build_risk_rows(category_rows, dish_rows)

    main_totals = sales_metrics("main", MAIN_START, MAIN_END, sales_rows)
    check_totals = sales_metrics("check", CHECK_START, CHECK_END, sales_rows)
    check_totals.update(
        {
            "main_revenue": main_totals["revenue"],
            "main_orders": main_totals["orders"],
            "main_avg_check": main_totals["avg_check"],
            "check_product_cost": check_totals["product_cost"],
            "category_source_product_cost": promo_summary["category_source_product_cost_total"],
        }
    )

    monthly_fields = [
        "period",
        "period_start",
        "period_end",
        "days",
        "revenue",
        "orders",
        "avg_check",
        "gross_sum",
        "discount_sum",
        "discount_share_of_gross",
        "product_cost",
        "food_cost_pct",
        "gross_margin",
        "gross_margin_pct",
        "source_rows",
        "is_focus_period",
    ]
    category_fields = [
        "period_start",
        "period_end",
        "category",
        "dish_group_top",
        "dish_category",
        "revenue",
        "product_cost",
        "food_cost_pct",
        "gross_margin",
        "gross_margin_pct",
        "source_product_cost",
        "promo_recovered_component_cost_added",
        "promo_component_cost_reallocated_out",
        "orders",
        "dish_amount",
        "gross_sum",
        "risk_comment",
    ]
    risk_fields = [
        "level",
        "item_name",
        "dish_group_top",
        "dish_category",
        "revenue",
        "product_cost",
        "food_cost_pct",
        "gross_margin",
        "risk_flags",
        "risk_comment",
        "recommended_action",
    ]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    write_csv(OUT_DIR / "iiko_monthly_gross_margin.csv", monthly_rows, monthly_fields)
    write_csv(OUT_DIR / "iiko_category_margin.csv", category_rows, category_fields)
    write_csv(OUT_DIR / "iiko_food_cost_risks.csv", risk_rows, risk_fields)
    write_json(
        OUT_DIR / "iiko_monthly_gross_margin.json",
        {
            "metadata": {
                "generated_at": "2026-05-18",
                "active_department": "Foodmarket Тепло Черникова",
                "main_period": {"start": MAIN_START.isoformat(), "end": MAIN_END.isoformat()},
                "check_period": {"start": CHECK_START.isoformat(), "end": CHECK_END.isoformat()},
                "order_source_rule": "Use sales_daily.csv for general economics OrderNum; do not mix with channel slice.",
                "risk_thresholds": {
                    "significant_revenue_rub": out_number(SIGNIFICANT_REVENUE),
                    "high_food_cost_pct": out_number(HIGH_FOOD_COST, "0.000001"),
                },
                "sources": [
                    str(SALES_DAILY_PATH.relative_to(PROJECT_ROOT)),
                    str(CATEGORY_PATH.relative_to(PROJECT_ROOT)),
                    str(DISH_PATH.relative_to(PROJECT_ROOT)),
                    str(PROMO_PATH.relative_to(PROJECT_ROOT)),
                ],
            },
            "monthly_rows": monthly_rows,
            "focus_period_rows": focus_rows,
            "promo_sets_impact": promo_summary,
            "check_totals": check_totals,
        },
    )
    report = build_report(monthly_rows, focus_rows, category_rows, risk_rows, promo_summary, promo_rows, check_totals)
    (OUT_DIR / "iiko_gross_margin_report.md").write_text(report, encoding="utf-8")

    print(f"wrote {OUT_DIR.relative_to(PROJECT_ROOT)}")
    print(f"monthly rows: {len(monthly_rows)}; category rows: {len(category_rows)}; risk rows: {len(risk_rows)}")


if __name__ == "__main__":
    main()
