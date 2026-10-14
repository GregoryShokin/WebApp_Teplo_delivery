#!/usr/bin/env python3
"""Рис, нори, закупочные цены и сырьевая стоимость целого ролла (база «до»).

Только локально по сохранённым raw-ответам. Запуск после build_baseline.py:

    python3 build_rice_costs.py --raw <raw фазы 1> --raw2 <raw фазы 2> --work <data из build_baseline> --out <data>

Метод цен: последняя проведённая приходная накладная (status=PROCESSED) основного склада Черникова
с amount>0, price>0, dateIncoming < 2026-10-14; |price − sum/amount| ≤ 0,03 ₽; единица накладной
(amountUnit) должна совпасть с mainUnit товара. Конфликт последних цен → null. Вода без цены → литры отдельно.
Это lastpurchase, не средневзвешенная стоимость запасов. Поставщики не сохраняются.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_baseline import RICE_PF, Catalog, load  # noqa: E402

STORE = "8a55c561-2598-4150-ac28-1b03cf8a1835"  # Основной склад Черникова
CUTOFF = "2026-10-14"  # dateIncoming строго меньше — накладные до 13.10 включительно
RAW_RICE = "a546bbbc-9930-48e2-b0a7-7d9aa669d7a8"
NORI = "402101dc-2501-4341-9c13-d020a38702ab"
VINEGAR = "a3ae9ec9-9bfa-4a87-ad6a-f9c51451dc0c"
WATER = "ce899eb6-6265-4039-b62d-016b7bf66d5d"
MONTH_DAYS = {"2026-07": 31, "2026-08": 31, "2026-09": 30, "2026-10p": 13}
TX_MONTH = {"07": "2026-07", "08": "2026-08", "09": "2026-09", "10": "2026-10p"}
BATCH_YIELD_KG = None  # берётся из техкарты


def strip(tag):
    return tag.split("}")[-1]


def pricebook(raw: Path, cat: Catalog):
    root = ET.parse(raw / "incoming-invoices.xml").getroot()
    status_counts = collections.Counter()
    lines = collections.defaultdict(list)
    for doc in root:
        if strip(doc.tag) != "document":
            continue
        f = {strip(c.tag): c for c in doc}
        st = f["status"].text
        status_counts[st] += 1
        if st != "PROCESSED":
            continue
        date = f["dateIncoming"].text
        if not date or date[:10] >= CUTOFF:
            continue
        for it in f["items"]:
            g = {strip(c.tag): (c.text or "") for c in it}
            if g.get("store") != STORE:
                continue
            amount, price, ssum = float(g["amount"]), float(g["price"]), float(g["sum"])
            if amount <= 0 or price <= 0:
                continue
            lines[g["product"]].append({"date": date, "doc_number": f["documentNumber"].text, "doc_id": f["id"].text,
                                        "amount": amount, "price": price, "sum": ssum, "amountUnit": g["amountUnit"],
                                        "vatPercent": float(g.get("vatPercent") or 0)})
    book = {}
    for pid, ls in lines.items():
        last = max(x["date"] for x in ls)
        at = [x for x in ls if x["date"] == last]
        variants = {(round(x["price"], 6), x["amountUnit"]) for x in at}
        main_unit = (cat.products.get(pid) or {}).get("mainUnit")
        rec = {"guid": pid, "name": cat.name(pid), "unit": cat.unit(pid), "price_date": last,
               "doc_number": at[0]["doc_number"], "lines_in_period": len(ls),
               "purchased_qty_jul_oct13": round(sum(x["amount"] for x in ls), 3)}
        if len(variants) > 1:
            rec.update(unit_price_rub=None, status="conflicting_latest_prices",
                       variants=[{"price": p, "unit": cat.units.get(u, u)} for p, u in variants])
        else:
            x = at[0]
            dev = abs(x["price"] - x["sum"] / x["amount"])
            if x["amountUnit"] != main_unit:
                rec.update(unit_price_rub=None, status="unit_mismatch", purchase_unit=cat.units.get(x["amountUnit"], x["amountUnit"]))
            elif dev > 0.03:
                rec.update(unit_price_rub=None, status="price_sum_mismatch", deviation=dev)
            else:
                rec.update(unit_price_rub=round(x["price"], 4), status="lastpurchase_confirmed", price_sum_deviation=round(dev, 5),
                           vat_percent=x["vatPercent"])
        book[pid] = rec
    return book, status_counts, lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", type=Path, required=True)
    ap.add_argument("--raw2", type=Path, required=True)
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    cat = Catalog(a.raw)
    book, status_counts, lines = pricebook(a.raw, cat)
    rolls = json.loads((a.work / "rolls-physical-monthly.json").read_text())
    rice_th = json.loads((a.work / "rice-theoretical-monthly.json").read_text())
    rice_pf = json.loads((a.work / "rice-pf-recipe.json").read_text())
    day = "2026-10-14"

    # ---- стоимость рисового п/ф и п/ф Мицукан
    def cost_of(pid):
        flat = cat.flatten(pid, day, stop=frozenset())
        priced, unpriced, water_l = 0.0, [], 0.0
        detail = []
        for leaf, q in flat.items():
            if isinstance(leaf, tuple):
                unpriced.append({"leaf": cat.name(leaf[1]), "reason": "нет техкарты/разложения"})
                continue
            if leaf == WATER:
                water_l += q
                continue
            pb = book.get(leaf)
            price = pb and pb.get("unit_price_rub")
            if price is None:
                unpriced.append({"leaf": cat.name(leaf), "qty": round(q, 6), "unit": cat.unit(leaf),
                                 "reason": (pb or {}).get("status", "нет закупки 01.07–13.10 на складе")})
                continue
            c = q * price
            priced += c
            detail.append({"leaf": cat.name(leaf), "guid": leaf, "qty": round(q, 6), "unit": cat.unit(leaf),
                           "price_rub": price, "price_date": pb["price_date"][:10], "cost_rub": round(c, 3)})
        detail.sort(key=lambda d: -d["cost_rub"])
        return {"priced_material_cost_ex_water_rub": round(priced, 2), "water_l": round(water_l, 4),
                "unpriced_leaves": unpriced, "status": "complete_ex_water" if not unpriced else "partial", "leaves": detail}

    rice_cost = cost_of(RICE_PF)
    batch = rice_pf["batch_chart"]
    yield_kg = batch["assembledAmount"]

    # ---- стоимость целых роллов
    roll_costs = []
    for r in rolls["rolls"]:
        c = cost_of(r["roll_guid"])
        rc = r["recipe_14_10"] or {}
        rice_g = rc.get("rice_pf_g")
        rice_part = (rice_g or 0) / 1000 * rice_cost["priced_material_cost_ex_water_rub"]
        nori_price = (book.get(NORI) or {}).get("unit_price_rub")
        roll_costs.append({"roll_guid": r["roll_guid"], "name": r["name"], "q3_total": r["q3_total"],
                           "rice_pf_g": rice_g, "raw_rice_g": round((rice_g or 0) * rice_pf["flat_goods_per_kg_rice_pf"]["Рис Макото"], 2),
                           "nori_sheets": rc.get("nori_sheets"),
                           "rice_pf_cost_rub": round(rice_part, 2),
                           "nori_cost_rub": round((rc.get("nori_sheets") or 0) * nori_price, 2) if nori_price else None,
                           **c})

    # ---- сверка риса
    tx = load(a.raw2, "transactions-rice-nori-2026-07-01_2026-10-13.json")["data"]
    store_rows = [x for x in tx if x["Account.Name"] == "Основной склад Черникова"]
    flow = collections.defaultdict(lambda: collections.Counter())
    for x in store_rows:
        m = TX_MONTH[x["DateTime.Month"][:2]]
        flow[(x["Product.Name"].strip(), m)][x["TransactionType"]] += (x["Amount.In"] or 0) - (x["Amount.Out"] or 0)
    wo = load(a.raw, "writeoffs.json")["response"]
    wo_agg = collections.defaultdict(lambda: collections.Counter())
    for d in wo:
        m = d["dateIncoming"][:7]
        m = "2026-10p" if m == "2026-10" else m
        for it in d["items"]:
            if it["productId"] in (RAW_RICE, RICE_PF, NORI):
                wo_agg[(cat.name(it["productId"]), m)]["qty"] += it["amount"]
                wo_agg[(cat.name(it["productId"]), m)]["acts_lines"] += 1
    k_raw = rice_pf["flat_goods_per_kg_rice_pf"]["Рис Макото"]
    rice_rows = []
    for m in ["2026-07", "2026-08", "2026-09", "2026-10p"]:
        th = rice_th.get(m, {})
        pf_rolls = th.get("rolls_and_sets", 0.0)
        pf_all = sum(th.values())
        f = flow[("Рис Макото", m)]
        rolls_m = rolls["month_totals"][m]
        pf_wo = wo_agg[("Рис для роллов пф", m)]["qty"]
        rice_rows.append({
            "month": m, "days": MONTH_DAYS[m], "physical_rolls": rolls_m,
            "rice_pf_kg_theory_rolls": round(pf_rolls, 2), "rice_pf_kg_theory_all_dishes": round(pf_all, 2),
            "rice_pf_g_per_roll_avg": round(pf_rolls / rolls_m * 1000, 1) if rolls_m else None,
            "raw_rice_kg_theory_all_dishes": round(pf_all * k_raw, 2),
            "raw_rice_kg_iiko_sales_writeoff": round(-f["SESSION_WRITEOFF"], 3),
            "theory_minus_iiko_kg": round(pf_all * k_raw + f["SESSION_WRITEOFF"], 3),
            "raw_rice_kg_purchased": round(f["INVOICE"], 3),
            "raw_rice_kg_writeoff_acts_total": round(-f["WRITEOFF"], 3),
            "writeoff_acts_rice_pf_kg": round(pf_wo, 3), "writeoff_acts_raw_rice_kg": round(wo_agg[("Рис Макото", m)]["qty"], 3),
            "raw_rice_kg_inventory_correction_net": round(f["INVENTORY_CORRECTION"], 3),
            "batches_theory_sales": round(pf_all / yield_kg, 1),
            "batches_per_day_theory_sales": round(pf_all / yield_kg / MONTH_DAYS[m], 2),
            "batches_per_day_incl_pf_writeoffs": round((pf_all + pf_wo) / yield_kg / MONTH_DAYS[m], 2),
        })
    nori_rows = []
    for m in ["2026-07", "2026-08", "2026-09", "2026-10p"]:
        f = flow[("Нори", m)]
        nori_rows.append({"month": m, "nori_sales_writeoff_sheets": round(-f["SESSION_WRITEOFF"], 1),
                          "nori_purchased_sheets": round(f["INVOICE"], 1), "nori_writeoff_acts_sheets": round(-f["WRITEOFF"], 1),
                          "nori_inventory_correction_net": round(f["INVENTORY_CORRECTION"], 1)})
    v = flow[("Уксус рисовый", "2026-09")]

    out = a.out
    out.mkdir(parents=True, exist_ok=True)
    keep = {RAW_RICE, NORI, VINEGAR, "2760a59c-85ae-4b38-abfe-870bc8fe4ec5", "15cd4416-d60a-4715-8ea6-7322d91ef043",
            "967b70a9-ea88-4ef6-ab8d-656980e78eb2"}
    (out / "pricebook-lastpurchase-2026-10-13.json").write_text(json.dumps({
        "method": "lastpurchase: последняя PROCESSED приходная накладная склада Основной склад Черникова, dateIncoming<2026-10-14; без поставщиков",
        "source": "GET /resto/api/documents/export/incomingInvoice?from=2026-07-01&to=2026-10-14 (чтение 2026-10-14T18:06Z)",
        "document_status_counts": status_counts, "products_priced": sum(1 for b in book.values() if b.get("unit_price_rub") is not None),
        "products_conflict_or_unresolved": [b for b in book.values() if b.get("unit_price_rub") is None],
        "rice_related": [book.get(k) for k in keep if k in book],
        "all": sorted(book.values(), key=lambda b: b["name"])}, ensure_ascii=False, indent=1, default=str) + "\n")
    (out / "rice-reconciliation.json").write_text(json.dumps({
        "units": "кг; рисовый п/ф — готовый (после варки и заправки); сырой рис — Рис Макото",
        "raw_rice_per_kg_rice_pf": k_raw, "batch_yield_kg": yield_kg,
        "rice_pf_cost": rice_cost, "monthly": rice_rows, "nori_monthly_sheets": nori_rows,
        "vinegar_sep_l": dict(v),
        "sources": {"theory": "rice-theoretical-monthly.json (продажи × техкарты на дату продажи)",
                    "iiko_flows": "POST /v2/reports/olap TRANSACTIONS, Account.Name=Основной склад Черникова, 2026-07-01..2026-10-13",
                    "writeoff_acts": "GET /v2/documents/writeoff status=PROCESSED 2026-07-01..2026-10-13"}},
        ensure_ascii=False, indent=1) + "\n")
    (out / "roll-material-costs.json").write_text(json.dumps({
        "as_of_recipes": day, "prices": "lastpurchase ≤ 13.10.2026 (см. pricebook)", "unit": "₽ за целый ролл (1 порц), без воды",
        "rolls": roll_costs}, ensure_ascii=False, indent=1) + "\n")
    print(json.dumps({"rice_pf_cost_per_kg": rice_cost["priced_material_cost_ex_water_rub"], "rice_pf_unpriced": rice_cost["unpriced_leaves"],
                      "rice": rice_rows, "nori": nori_rows}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
