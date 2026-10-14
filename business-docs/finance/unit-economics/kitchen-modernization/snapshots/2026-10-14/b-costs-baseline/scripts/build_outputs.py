"""Собрать обезличенные агрегаты снимка 14.10.2026 из сырых read-only выгрузок S1–S7.

Запуск (локально, без сети):
  python3 build_outputs.py --private "/Users/grigorii/Documents/Teplo/Unit Economics/2026-10-14/b-private"
Читает: <private>/s1_raw.json, s2_raw.json, s4_raw.json, s5_raw.json, s6_yandex.json;
        ../data/s3_sushi_labor.json, ../data/s7_units_by_cooking_place.json.
Пишет в ../data/: pnl-snapshots.json, period-matrix.json, orders-and-thresholds.json,
        sushi-labor-summary.json, rates-effective-2026-10-14.json, electricity.json,
        fixed-assets.json, channel-fees.json.
"""

import argparse
import json
from collections import Counter
from decimal import Decimal as D
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
TIERS = [(D(0), D(50000), D(0)), (D(50000), D(140000), D("0.035")),
         (D(140000), D(190000), D("0.045")), (D(190000), D(550000), D("0.055")),
         (D(550000), D(10) ** 12, D("0.065"))]


def q(x):
    return str(D(str(x)).quantize(D("0.01")))


def rows(x):
    return x["data"] if isinstance(x, dict) and "data" in x else x


def tier(r):
    return next(t for t in TIERS if t[0] <= r < t[1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--private", required=True)
    P = Path(ap.parse_args().private)
    s1 = json.load(open(P / "s1_raw.json"))
    s2 = json.load(open(P / "s2_raw.json"))
    s4 = json.load(open(P / "s4_raw.json"))
    s5 = json.load(open(P / "s5_raw.json"))
    s6 = json.load(open(P / "s6_yandex.json"))
    s3 = json.load(open(DATA / "s3_sushi_labor.json"))
    s7 = json.load(open(DATA / "s7_units_by_cooking_place.json"))

    # 1. Снимки ОПиУ без текстов предупреждений (там встречаются имена ИП-контрагентов)
    pnl = {"read_utc": s1["read_started_utc"], "db_now": s1["db_now"], "alembic": s1["alembic"],
           "today_param": s1["today_param"], "months": {}}
    for m, rep in s1["pnl"].items():
        pnl["months"][m] = {
            "lines": [{k: l[k] for k in ("code", "title", "block", "kind", "amount", "status",
                                         "missing_lines", "pct_of_revenue")}
                      | {"components": [{k: c.get(k) for k in ("stream", "component", "amount",
                                                               "status", "excluded_amount",
                                                               "excluded_reason")}
                                        for c in l["components"]]}
                      for l in rep["lines"] if l["status"] != "not_used"],
            "warnings": [{"code": w["code"], "line_code": w.get("line_code"),
                          "amount": w.get("amount")} for w in rep["warnings"]],
            "reconciliation": rep["reconciliation"], "quality": rep["quality"],
        }
    pnl["payroll_month"] = s1["payroll_month"]
    json.dump(pnl, open(DATA / "pnl-snapshots.json", "w"), ensure_ascii=False, indent=1)

    # 2. Матрица периода
    matrix = {"period_close_rows": s1["period_close"], "tax_payroll_ledger": s1["tax_payroll_ledger"],
              "depreciation": s1["depreciation"], "inventory_audits": s1["inventory_audits"],
              "manual_entries": s1["manual_entries"], "payroll_periods": s1["payroll_periods"],
              "partner_facts": s1["partner_facts"],
              "iiko_facts_total": [r for r in s1["iiko_facts"] if r["direction"] in ("total", "rolls")],
              "iiko_control_read_2026_10_14": {}}
    for m in ("2026-08", "2026-09"):
        rr = rows(s4[f"revenue_preset_{m}"])
        matrix["iiko_control_read_2026_10_14"][m] = {
            k: q(sum(D(str(x.get(k) or 0)) for x in rr))
            for k in ("DishSumInt", "DishDiscountSumInt", "DiscountSum", "ProductCostBase.ProductCost")}
    util = {}
    for a in s2["accruals"]:
        if a["line_code"] != "utilities_chernikova" or a["status"] != "recognized":
            continue
        cp = a["counterparty"] or ""
        kind = ("electricity" if "электр" in (a["number"] or "") else
                "water" if "вода" in (a["number"] or "") else
                "waste" if ("ЭкоЦентр" in cp or "Мусор" in cp) else "other")
        key = (a["recognition_month"] or a["service_period_start"])[:7]
        util.setdefault(key, Counter())[kind] += float(a["amount"])
    matrix["utilities_by_kind"] = {k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in util.items()}
    json.dump(matrix, open(DATA / "period-matrix.json", "w"), ensure_ascii=False, indent=1, default=str)

    # 3. Заказы, каналы, пороги процентного фонда
    ot = {}
    for m in ("2026-08", "2026-09"):
        daily = rows(s4[f"daily_preset_{m}"])
        net = sum(D(str(x["DishDiscountSumInt"])) for x in daily)
        n = sum(int(x["UniqOrderId"]) for x in daily)
        ch = rows(s4[f"channels_{m}"])
        zero = sum(int(x["UniqOrderId"]) for x in ch if not x["DishDiscountSumInt"])
        channels = Counter()
        chn = Counter()
        for x in ch:
            t = x["OrderType"] or "?"
            grp = ("Starter доставка" if t == "Доставка Курьер Стартер" else
                   "Starter самовывоз" if t == "Самовывоз Стартер" else
                   "Яндекс Еда" if t.startswith("Яндекс") else
                   "Телефон/оператор доставка" if t == "Доставка курьером" else
                   "Самовывоз (не Starter)" if t == "Самовывоз" else t)
            channels[grp] += D(str(x["DishDiscountSumInt"] or 0))
            chn[grp] += int(x["UniqOrderId"])
        courier_orders = sum(int(x["UniqOrderId"]) for x in ch
                             if x.get("Delivery.ServiceType") == "COURIER")
        days = s3["months"][m + "-01"]["days"]
        tc = Counter()
        near = []
        pool_sum = D(0)
        paid_sum = D(0)
        for d in days:
            r = D(d["daily_revenue_payroll"] or 0)
            lo, hi, rate = tier(r)
            tc[str(rate)] += 1
            pool_sum += D(d["daily_pool"] or 0)
            paid_sum += D(d["percent_paid_all_roles"] or 0)
            near.append({"date": d["date"], "dow": d["dow"], "revenue": q(r), "rate": str(rate),
                         "to_next_threshold": q(hi - r), "above_lower_threshold": q(r - lo)})
        ot[m] = {
            "orders_uniq": n, "orders_zero_revenue": zero, "orders_paid": n - zero,
            "revenue_net": q(net),
            "avg_net_check_all": q(net / n), "avg_net_check_paid": q(net / (n - zero)),
            "channels_net": {k: q(v) for k, v in channels.items()},
            "channels_orders": dict(chn),
            "courier_orders_iiko": courier_orders,
            "units_by_cooking_place": rows(s7[m]),
            "days_by_tier": dict(tc),
            "days_within_10k_below_next": sum(1 for x in near if D(x["to_next_threshold"]) <= 10000),
            "days_within_5k_below_next": sum(1 for x in near if D(x["to_next_threshold"]) <= 5000),
            "days_within_10k_above_lower": sum(1 for x in near if D(x["above_lower_threshold"]) <= 10000
                                                and x["rate"] != "0"),
            "theoretical_pool_sum": q(pool_sum), "percent_paid_sum": q(paid_sum),
            "days": near,
        }
    json.dump(ot, open(DATA / "orders-and-thresholds.json", "w"), ensure_ascii=False, indent=1)

    # 4. Труд суши-цеха (без построчных слотов, только агрегаты)
    lab = {}
    for m, v in s3["months"].items():
        days = v["days"]
        lab[m] = {
            "period": v["period"], "sushi_distinct_people": v["sushi_distinct_people"],
            "role_totals": v["role_totals"], "sushi_by_category": v["sushi_by_category"],
            "sushi_fund_rate_groups": v["sushi_fund_rate_groups"],
            "sushi_groups_with_seniority": v["sushi_groups_with_seniority"],
            "adjustments_by_role": v["adjustments_by_role"],
            "people_per_day_dist": dict(Counter(d["sushi_people"] for d in days)),
            "peak_16_19_max_dist": dict(Counter(d["sushi_concurrency_16_19_max"] for d in days)),
            "peak_16_19_min_dist": dict(Counter(d["sushi_concurrency_16_19_min"] for d in days)),
            "person_shifts": sum(d["sushi_groups"] for d in days),
            "hours": round(sum(d["sushi_hours"] for d in days), 2),
            "production_person_days_all_roles": sum(d["all_production_people"] for d in days),
            "days": [{k: d[k] for k in d if k != "sushi_concurrency_by_slot"} for d in days],
            "avg_concurrency_by_slot": {
                sl: round(sum(d["sushi_concurrency_by_slot"].get(sl, 0) for d in days) / len(days), 2)
                for sl in (days[0]["sushi_concurrency_by_slot"].keys() if days else [])},
        }
    json.dump(lab, open(DATA / "sushi-labor-summary.json", "w"), ensure_ascii=False, indent=1)

    # 5. Ставки на 14.10.2026
    eff = lambda r: (r["effective_from"] <= "2026-10-14" and
                     (r["effective_to"] is None or r["effective_to"] > "2026-10-14"))
    rates = {
        "payroll_rate": [r for r in s2["payroll_rate"] if r["is_active"] and eff(r)
                         and not r["personal_override"]],
        "personal_overrides_count": sum(1 for r in s2["payroll_rate"] if r["personal_override"]),
        "availability_enabled": [r for r in s2["availability"] if r["is_enabled"]],
        "revenue_tier": [r for r in s2["revenue_tier"] if eff(r)],
        "category_coefficient": [r for r in s2["category_coefficient"] if eff(r)],
        "seniority_premium": [r for r in s2["seniority_premium"] if eff(r)],
        "settings": {r["key"]: r["value"] for r in s2["settings"]},
        "settings_history_fixed_assets": [r for r in s2["settings_history"]
                                          if r["key"].startswith("fixed_assets")],
    }
    json.dump(rates, open(DATA / "rates-effective-2026-10-14.json", "w"), ensure_ascii=False, indent=1)

    # 6. Электроэнергия: только числа из актов, без реквизитов сторон
    el = []
    for r in s2["utility_intakes"]:
        if r["kind"] != "electricity":
            continue
        lines = [ln for ln in r["kw_lines"] if "кВт" in ln or "аванс" in ln.lower()]
        el.append({"service_period": [r["service_period_start"], r["service_period_end"]],
                   "act_kind": r["parsed"].get("act_kind"), "amount": r["amount"],
                   "energy_amount": r["parsed"].get("energy_amount"),
                   "losses_amount": r["parsed"].get("losses_amount"),
                   "lines": lines})
    json.dump(el, open(DATA / "electricity.json", "w"), ensure_ascii=False, indent=1)

    # 7. ОС
    json.dump({"categories": s2["asset_categories"], "depr_2026_09": s2["depr_by_category_2026_09"],
               "commissioned_2026": s2["assets_commissioned_2026"],
               "settings": {k: v for k, v in rates["settings"].items() if k.startswith("fixed_assets")},
               "settings_history": rates["settings_history_fixed_assets"]},
              open(DATA / "fixed-assets.json", "w"), ensure_ascii=False, indent=1)

    # 8. Комиссии каналов: Starter, Яндекс, эквайринг, банк
    fees = {"starter_docs": [{k: a[k] for k in ("number", "service_period_start", "amount")}
                             for a in s2["accruals"] if a["line_code"] == "website_app"],
            "yandex_docs": [{k: a[k] for k in ("number", "service_period_start", "amount", "vat_total",
                                               "no_article")} for a in s6],
            "yandex_bank_in": [r for r in s2["bank_in_by_counterparty"] if "ЯНДЕКС" in r["counterparty_name_raw"]],
            "acquiring_cash": s2["acquiring_cash"],
            "paytypes": {m: rows(s4[f"paytypes_{m}"]) for m in ("2026-08", "2026-09")},
            "cash_alongside_checked": {m: {code: [x for x in s5[m]["lines"][code] if x["verdict"] == "included"
                                                  and code in ("automation",)]
                                           for code in ("automation",)} for m in ("2026-08-01", "2026-09-01")}}
    json.dump(fees, open(DATA / "channel-fees.json", "w"), ensure_ascii=False, indent=1, default=str)
    print("ok")


if __name__ == "__main__":
    main()
