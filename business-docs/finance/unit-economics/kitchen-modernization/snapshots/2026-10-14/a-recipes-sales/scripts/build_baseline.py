#!/usr/bin/env python3
"""База «до» для модернизации суши-цеха: физические целые роллы, рис, техкарты.

Только локальные вычисления по сохранённым raw-ответам iiko (см. iiko_readonly_fetch.py).
Сеть, БД и iiko не трогает. Запуск:

    python3 build_baseline.py --raw <каталог raw фазы 1> --out <каталог a-recipes-sales/data>

Единицы: «ролл» = одна порция-DISH ролла (целый ролл, единица iiko «порц»); кусочки не смешиваются.
Раскрытие сетов: количество ролла = Σ проданных сетов × amountIn/assembledAmount дочернего DISH
по техкарте, действовавшей на учётный день продажи. Это модель выпуска, не наблюдение и не выручка.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import re
from pathlib import Path

RICE_PF = "0cfbf0d5-7236-47bb-9ac2-047b770e55bb"  # «Рис для роллов пф», PREPARED, кг
NORI_NAMES = ("Нори",)
MONTHS = ["2026-07", "2026-08", "2026-09", "2026-10p"]
ROLL_GROUP_PREFIXES = ("Роллы/", "Проработки")
NON_ROLL_RICE_GROUPS = ("Роллы/Трайфлы", "Роллы/Онигири проработка")


def load(raw: Path, name: str):
    return json.loads((raw / name).read_text(encoding="utf-8"))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Catalog:
    def __init__(self, raw: Path):
        self.products = {x["id"]: x for x in load(raw, "products.json")}
        self.groups = {x["id"]: x for x in load(raw, "groups.json")}
        self.units = {x["id"]: x["name"] for x in load(raw, "units.json")}
        charts = load(raw, "charts.json")
        self.assembly = collections.defaultdict(list)
        for ch in charts["assemblyCharts"]:
            self.assembly[ch["assembledProductId"]].append(ch)
        self.prepared = collections.defaultdict(list)
        for ch in charts["preparedCharts"]:
            self.prepared[ch["assembledProductId"]].append(ch)
        self.issues: list[dict] = []
        self._flat_cache: dict = {}

    def name(self, pid):
        return (self.products.get(pid) or {}).get("name", f"<нет в каталоге {pid}>")

    def ptype(self, pid):
        return (self.products.get(pid) or {}).get("type")

    def unit(self, pid):
        return self.units.get((self.products.get(pid) or {}).get("mainUnit"), "?")

    def group_path(self, pid):
        gid = (self.products.get(pid) or {}).get("parent")
        out = []
        while gid and gid in self.groups:
            out.append(self.groups[gid]["name"])
            gid = self.groups[gid].get("parent")
        return "/".join(reversed(out))

    def chart(self, pid, day: str, kind="assembly"):
        pool = self.assembly if kind == "assembly" else self.prepared
        act = [c for c in pool.get(pid, []) if c["dateFrom"] <= day and (c["dateTo"] is None or day < c["dateTo"])]
        if len(act) > 1:
            self.issues.append({"issue": "multiple_active_charts", "product": self.name(pid), "date": day})
        return act[0] if len(act) == 1 else None

    def item_scoped(self, it) -> bool:
        return bool(it.get("productSizeSpecification") or it.get("storeSpecification")
                    or any(it.get(f"amountIn{k}") for k in (1, 2, 3)))

    def is_roll(self, pid, day) -> bool:
        if self.ptype(pid) != "DISH":
            return False
        gp = self.group_path(pid)
        if gp.startswith(NON_ROLL_RICE_GROUPS):
            return False
        ch = self.chart(pid, day)
        if not ch:
            return False
        has_rice = any(it["productId"] == RICE_PF for it in ch["items"])
        # дочерний DISH в «порц» = вложенное блюдо (сет); DISH в кг (например «Лава соус») — соус-заготовка, не сет
        has_dish_child = any(self.ptype(it["productId"]) in ("DISH", "MODIFIER") and self.unit(it["productId"]) == "порц"
                             for it in ch["items"])
        return has_rice and not has_dish_child

    def expand_rolls(self, pid, qty, day, path=(), out=None):
        """Вернуть {roll_guid: qty} — целые роллы в pid по техкартам на day. Рекурсия только по DISH/MODIFIER."""
        out = collections.Counter() if out is None else out
        if pid in path:
            self.issues.append({"issue": "cycle", "path": [self.name(x) for x in path + (pid,)]})
            return out
        if self.is_roll(pid, day):
            out[pid] += qty
            return out
        ch = self.chart(pid, day)
        if not ch:
            return out
        q = ch["assembledAmount"]
        for it in ch["items"]:
            cid = it["productId"]
            if self.ptype(cid) not in ("DISH", "MODIFIER"):
                continue
            if self.item_scoped(it):
                self.issues.append({"issue": "scoped_item_in_set", "parent": self.name(pid), "child": self.name(cid)})
            if self.unit(cid) != "порц":
                continue  # блюдо-заготовка в кг/л (соус) — не порция, роллов не содержит
            self.expand_rolls(cid, qty * it["amountIn"] / q, day, path + (pid,), out)
        return out

    def flatten(self, pid, day, stop=frozenset({RICE_PF}), path=()):
        """Коэффициенты листьев на 1 базовую единицу pid: рекурсивно amountIn/Q. Стоп на GOODS и stop."""
        key = (pid, day, stop)
        if key in self._flat_cache:
            return self._flat_cache[key]
        res = collections.Counter()
        if pid in stop or self.ptype(pid) == "GOODS" or pid in path:
            res[pid] += 1.0
            self._flat_cache[key] = res
            return res
        ch = self.chart(pid, day)
        if not ch or not ch["items"] or ch["assembledAmount"] <= 0:
            res[("UNRESOLVED", pid)] += 1.0
            self._flat_cache[key] = res
            return res
        q = ch["assembledAmount"]
        for it in ch["items"]:
            for leaf, k in self.flatten(it["productId"], day, stop, path + (pid,)).items():
                res[leaf] += it["amountIn"] / q * k
        self._flat_cache[key] = res
        return res

    def prepared_flat(self, pid, day):
        ch = self.chart(pid, day, "prepared")
        if not ch:
            return None
        res = collections.Counter()
        for it in ch["items"]:
            res[it["productId"]] += it["amount"]
        return res


def pieces_from_text(text: str | None):
    if not text:
        return None
    m = re.search(r"(\d{1,2})\s*(?:шт|кус)", text)
    return int(m.group(1)) if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--site", type=Path, help="проекция сайта: analyticId|кусочки|вес|цена|статус (psv)")
    args = ap.parse_args()
    site = {}
    if args.site and args.site.exists():
        for line in args.site.read_text(encoding="utf-8").split():
            gid, pcs, weight, price, status = line.split("|")
            site[gid] = {"pieces": None if pcs == "-" else int(pcs), "weight_g": int(weight), "price_rub": int(price),
                         "status": "active" if status == "a" else status}
    raw, out = args.raw, args.out
    out.mkdir(parents=True, exist_ok=True)
    cat = Catalog(raw)

    # ---------- продажи и контроль ----------
    controls = []
    month_rows = {}
    for m in MONTHS:
        rows = load(raw, f"sales-{m}.json")["data"]
        month_rows[m] = rows
        ctrl = load(raw, f"control-{m}.json")["data"]
        depts = sorted({r.get("Department.Code") for r in ctrl})
        for metric, f in (("gross", "DishSumInt"), ("net", "DishDiscountSumInt"), ("food_cost", "ProductCostBase.ProductCost")):
            src = sum(float(r[f] or 0) for r in rows)
            con = sum(float(r[f] or 0) for r in ctrl)
            controls.append({"month": m, "metric": metric, "sku_detail_total": round(src, 2), "preset_total": round(con, 2),
                             "difference": round(src - con, 4), "status": "matched" if abs(src - con) < 0.01 else "MISMATCH",
                             "preset_department_codes": depts, "sku_rows": len(rows)})

    # родители с явными дочерними строками в периоде — не раскрываются по своей карте
    parents_with_children = collections.defaultdict(set)
    for m, rows in month_rows.items():
        for r in rows:
            if r["SoldWithDish.Id"]:
                parents_with_children[m].add(r["SoldWithDish.Id"])
    # выручка родителя по дню (для классификации дочерних строк: платный набор или подарок)
    parent_day_gross = collections.Counter()
    for m, rows in month_rows.items():
        for r in rows:
            if not r["SoldWithDish.Id"]:
                parent_day_gross[(r["OpenDate.Typed"], r["DishId"])] += float(r["DishSumInt"] or 0)

    roll_month = collections.defaultdict(lambda: collections.Counter())  # (roll, month) -> mode -> qty
    roll_month_sources = collections.defaultdict(lambda: collections.Counter())  # (roll, month) -> source dish name
    parent_skips = collections.defaultdict(lambda: collections.Counter())
    rice_pf_theory = collections.defaultdict(lambda: collections.Counter())  # month -> category -> kg rice PF
    set_sales = collections.defaultdict(lambda: collections.Counter())
    unresolved_rice = collections.Counter()
    for m, rows in month_rows.items():
        for r in rows:
            day, pid, qty = r["OpenDate.Typed"], r["DishId"], float(r["DishAmountInt"] or 0)
            gross = float(r["DishSumInt"] or 0)
            if pid in parents_with_children[m] and not r["SoldWithDish.Id"]:
                rolls_in_parent = cat.expand_rolls(pid, qty, day)
                parent_skips[m][(cat.name(pid), bool(rolls_in_parent))] += qty
                continue
            parent = r["SoldWithDish.Id"]
            paid = (parent_day_gross[(day, parent)] > 0) if parent else (gross > 0)
            rolls = cat.expand_rolls(pid, qty, day)
            direct = cat.is_roll(pid, day)
            for roll, q in rolls.items():
                if direct:
                    mode = "retail" if paid else "gift_or_zero_revenue"
                else:
                    mode = "in_sets" if paid else "gift_or_zero_revenue"
                roll_month[(roll, m)][mode] += q
                roll_month_sources[(roll, m)][cat.name(pid) if not direct else "(розница)"] += q
            if rolls and not direct:
                set_sales[(pid, m)]["qty"] += qty
                set_sales[(pid, m)]["paid_qty" if paid else "zero_revenue_qty"] += qty
                set_sales[(pid, m)]["rolls"] += sum(rolls.values())
            # теоретический расход рисового п/ф по всем проданным блюдам (вкл. трайфлы/онигири/поке)
            flat = cat.flatten(pid, day)
            k = flat.get(RICE_PF, 0.0)
            if k:
                gp = cat.group_path(pid)
                catg = ("rolls_and_sets" if rolls else
                        "trifles" if gp.startswith("Роллы/Трайфлы") else
                        "onigiri" if gp.startswith("Роллы/Онигири") else f"other:{gp}")
                rice_pf_theory[m][catg] += qty * k
            for leaf in flat:
                if isinstance(leaf, tuple):
                    unresolved_rice[cat.name(leaf[1])] += qty

    # ---------- реестр роллов ----------
    all_rolls = sorted({k[0] for k in roll_month})
    today = "2026-10-14"
    roll_rows = []
    tot_month = collections.Counter()
    for (roll, m), modes in roll_month.items():
        tot_month[m] += sum(modes.values())
    for roll in all_rolls:
        pr = cat.products.get(roll, {})
        rec = {"roll_guid": roll, "name": cat.name(roll), "group": cat.group_path(roll),
               "deleted": pr.get("deleted"), "defaultIncludedInMenu": pr.get("defaultIncludedInMenu"),
               "baseline_menu_status": ("включён" if pr.get("deleted") is False and pr.get("defaultIncludedInMenu") is True
                                        else "удалён" if pr.get("deleted") is True
                                        else "не включён" if pr.get("defaultIncludedInMenu") is False else "не определено"),
               "description": pr.get("description"),
               "site_match": "exact_unique_guid" if roll in site else "not_found_on_site",
               "site_status": (site.get(roll) or {}).get("status"),
               "site_declared_weight_g": (site.get(roll) or {}).get("weight_g"),
               "site_price_rub": (site.get(roll) or {}).get("price_rub")}
        pcs_site = (site.get(roll) or {}).get("pieces")
        pcs_iiko = pieces_from_text(pr.get("description"))
        rec["pieces_per_roll"] = pcs_site or pcs_iiko
        rec["pieces_source"] = ("описание на teplofood.ru 14.10" if pcs_site else
                                "описание карточки iiko" if pcs_iiko else "не определено")
        if pcs_site and pcs_iiko and pcs_site != pcs_iiko:
            rec["pieces_conflict"] = {"site": pcs_site, "iiko": pcs_iiko}
        for m in MONTHS:
            modes = roll_month.get((roll, m), collections.Counter())
            rec[f"{m}_retail"] = round(modes["retail"], 4)
            rec[f"{m}_in_sets"] = round(modes["in_sets"], 4)
            rec[f"{m}_gift"] = round(modes["gift_or_zero_revenue"], 4)
            rec[f"{m}_total"] = round(sum(modes.values()), 4)
            rec[f"{m}_share"] = round(sum(modes.values()) / tot_month[m], 6) if tot_month[m] else None
        q3 = ["2026-07", "2026-08", "2026-09"]
        rec["q3_total"] = round(sum(rec[f"{m}_total"] for m in q3), 4)
        rec["q3_retail"] = round(sum(rec[f"{m}_retail"] for m in q3), 4)
        rec["q3_in_sets"] = round(sum(rec[f"{m}_in_sets"] for m in q3), 4)
        rec["q3_gift"] = round(sum(rec[f"{m}_gift"] for m in q3), 4)
        rec["top_sources_sep"] = dict(roll_month_sources.get((roll, "2026-09"), collections.Counter()).most_common(6))
        roll_rows.append(rec)
    q3tot = sum(r["q3_total"] for r in roll_rows)
    for r in roll_rows:
        r["q3_share"] = round(r["q3_total"] / q3tot, 6) if q3tot else None
    roll_rows.sort(key=lambda r: -r["q3_total"])
    cum = 0.0
    for i, r in enumerate(roll_rows, 1):
        cum += r["q3_total"]
        r["rank_q3"] = i
        r["q3_cum_share"] = round(cum / q3tot, 6)

    # ---------- техкарты роллов на 14.10 ----------
    def recipe(pid, day=today):
        ch = cat.chart(pid, day)
        if not ch:
            return None
        items = []
        q = ch["assembledAmount"]
        for it in ch["items"]:
            cid = it["productId"]
            items.append({"guid": cid, "name": cat.name(cid), "type": cat.ptype(cid), "unit": cat.unit(cid),
                          "amountIn_per_roll": it["amountIn"] / q, "amountOut_raw": it["amountOut"],
                          "grams_input_if_kg": round(it["amountIn"] / q * 1000, 3) if cat.unit(cid) == "кг" else None})
        # проверка через prepared: рисовый п/ф и нори как листья
        flat_goods = cat.flatten(pid, day, stop=frozenset())
        prep = cat.prepared_flat(pid, day)
        resid = None
        if prep is not None:
            keys = set(flat_goods) | set(prep)
            resid = max(abs(flat_goods.get(k, 0) - prep.get(k, 0)) for k in keys if not isinstance(k, tuple))
        return {"chart_id": ch["id"], "dateFrom": ch["dateFrom"], "dateTo": ch["dateTo"], "assembledAmount": q,
                "assembly_unit": cat.unit(pid), "outputComment": ch.get("outputComment"),
                "technologyDescription": ch.get("technologyDescription"), "items": items,
                "rice_pf_g": round(sum(i["amountIn_per_roll"] for i in items if i["guid"] == RICE_PF) * 1000, 3),
                "nori_sheets": round(sum(i["amountIn_per_roll"] for i in items if i["name"].strip() in NORI_NAMES), 4),
                "sum_direct_inputs_kg_only_g": round(sum(i["amountIn_per_roll"] for i in items if i["unit"] == "кг") * 1000, 1),
                "prepared_check_max_abs_residual": resid,
                "flat_goods_per_roll": {cat.name(k): round(v, 9) for k, v in flat_goods.items() if not isinstance(k, tuple)},
                "flat_goods_units": {cat.name(k): cat.unit(k) for k in flat_goods if not isinstance(k, tuple)}}

    for r in roll_rows:
        r["recipe_14_10"] = recipe(r["roll_guid"])

    # ---------- рисовый п/ф ----------
    rice_chart = recipe(RICE_PF)
    mitsukan = [i for i in rice_chart["items"] if cat.ptype(i["guid"]) == "PREPARED"]
    rice_sub = {i["name"]: recipe(i["guid"]) for i in mitsukan}
    rice_flat = cat.flatten(RICE_PF, today, stop=frozenset())
    rice_prepared = cat.prepared_flat(RICE_PF, today)

    # ---------- час приёма (сентябрь) ----------
    hour_rows = load(raw, "hour-sku-2026-09.json")["data"]
    sep_parents = parents_with_children["2026-09"]
    hour_rolls = collections.Counter()
    hour_rolls_mode = collections.defaultdict(collections.Counter)
    for r in hour_rows:
        pid, qty = r["DishId"], float(r["DishAmountInt"] or 0)
        if pid in sep_parents and not r["SoldWithDish.Id"]:
            continue
        rolls = cat.expand_rolls(pid, qty, "2026-09-15")
        n = sum(rolls.values())
        hour_rolls[r["HourOpen"]] += n
        hour_rolls_mode[r["HourOpen"]]["direct" if cat.is_roll(pid, "2026-09-15") else "via_sets_or_gifts"] += n
    orders_day = load(raw, "hour-orders-day-2026-09.json")["data"]
    cp_rows = load(raw, "hour-orders-cookingplace-day-2026-09.json")["data"]
    hours = sorted(set(hour_rolls) | {r["HourOpen"] for r in orders_day})
    ord_h = collections.Counter(); ord_days = collections.defaultdict(set)
    for r in orders_day:
        ord_h[r["HourOpen"]] += r["UniqOrderId.OrdersCount"]
    sushi_h = collections.Counter(); sushi_day_h = collections.defaultdict(collections.Counter)
    for r in cp_rows:
        if r["CookingPlace"] == "Суши":
            sushi_h[r["HourOpen"]] += r["UniqOrderId.OrdersCount"]
            sushi_day_h[r["OpenDate.Typed"]][r["HourOpen"]] += r["UniqOrderId.OrdersCount"]
    total_orders = sum(ord_h.values()); total_sushi = sum(sushi_h.values()); total_hr = sum(hour_rolls.values())
    hourly = []
    for h in hours:
        hourly.append({"hour_open": h, "orders_all": ord_h[h], "orders_with_sushi_cookingplace": sushi_h[h],
                       "physical_rolls_model": round(hour_rolls[h], 2),
                       "rolls_direct": round(hour_rolls_mode[h]["direct"], 2),
                       "rolls_via_sets_gifts": round(hour_rolls_mode[h]["via_sets_or_gifts"], 2),
                       "share_rolls": round(hour_rolls[h] / total_hr, 4) if total_hr else None,
                       "rolls_per_day_avg": round(hour_rolls[h] / 30, 2)})
    peak = [x for x in hourly if x["hour_open"] in ("16", "17", "18", "19")]
    sushi_daily = {d: sum(v.values()) for d, v in sushi_day_h.items()}
    peak_daily = {d: sum(v[h] for h in ("16", "17", "18", "19")) for d, v in sushi_day_h.items()}
    max_hour_orders = max((v[h], d, h) for d, v in sushi_day_h.items() for h in v)
    hourly_summary = {
        "axis": "HourOpen — час ОТКРЫТИЯ заказа в iiko (приём спроса), не час приготовления; предзаказы попадают в час приёма",
        "date_basis": "OpenDate.Typed (учётный день) 2026-09-01..2026-09-30, Department.Code=2, NOT_DELETED",
        "orders_all_month": total_orders, "orders_with_sushi_month": total_sushi,
        "physical_rolls_month_hourly_model": round(total_hr, 2),
        "peak_16_19": {"orders_all": sum(x["orders_all"] for x in peak),
                        "orders_with_sushi": sum(x["orders_with_sushi_cookingplace"] for x in peak),
                        "physical_rolls": round(sum(x["physical_rolls_model"] for x in peak), 2),
                        "share_rolls": round(sum(x["physical_rolls_model"] for x in peak) / total_hr, 4) if total_hr else None,
                        "share_sushi_orders": round(sum(x["orders_with_sushi_cookingplace"] for x in peak) / total_sushi, 4) if total_sushi else None},
        "sushi_orders_per_day": {"mean": round(total_sushi / len(sushi_daily), 2), "max": max(sushi_daily.values()), "min": min(sushi_daily.values()),
                                  "days": len(sushi_daily)},
        "sushi_orders_peak16_19_per_day": {"mean": round(sum(peak_daily.values()) / len(peak_daily), 2), "max": max(peak_daily.values())},
        "max_sushi_orders_single_hour": {"orders": max_hour_orders[0], "date": max_hour_orders[1], "hour_open": max_hour_orders[2]},
        "chart_date_for_expansion": "2026-09-15 (версии карт роллов/сетов в сентябре не менялись — проверено по getAll)",
        "note": "Заказ с суши = заказ, где есть строка с местом приготовления «Суши» (UniqOrderId.OrdersCount в разрезе CookingPlace). Роллы — модель раскрытия сетов.",
    }

    # ---------- запись ----------
    def dump(name, obj):
        (out / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")

    dump("sales-controls.json", controls)
    dump("rolls-physical-monthly.json", {
        "unit": "целый ролл (порция DISH iiko, единица «порц»); не кусочки, не сеты",
        "definition_roll": "DISH, чья действующая техкарта прямо содержит «Рис для роллов пф» и не содержит дочерних DISH/MODIFIER; группы Трайфлы/Онигири исключены",
        "modes": {"retail": "самостоятельная строка ролла с DishSumInt>0",
                  "in_sets": "раскрытие платного сета/набора по техкарте на учётный день (amountIn/assembledAmount)",
                  "gift_or_zero_revenue": "строка (сет или ролл) с нулевой выручкой до скидки: подарки/промо; причина не установлена построчно"},
        "month_totals": {m: round(v, 2) for m, v in tot_month.items()},
        "q3_total": round(q3tot, 2),
        "parents_not_expanded": {m: [{"parent": k[0], "contains_rolls_by_chart": k[1], "qty": v} for k, v in c.items()] for m, c in parent_skips.items()},
        "rolls": roll_rows})
    dump("set-sales-monthly.json", [{"set_guid": k[0], "name": cat.name(k[0]), "group": cat.group_path(k[0]), "type": cat.ptype(k[0]),
                                      "month": k[1], **{kk: round(vv, 3) for kk, vv in v.items()},
                                      "rolls_per_set": round(v["rolls"] / v["qty"], 3) if v["qty"] else None}
                                     for k, v in sorted(set_sales.items(), key=lambda x: (x[0][1], -x[1]["qty"]))])
    dump("rice-pf-recipe.json", {"as_of": today, "rice_pf_guid": RICE_PF, "batch_chart": rice_chart, "nested_prepared": rice_sub,
                                  "flat_goods_per_kg_rice_pf": {cat.name(k): round(v, 9) for k, v in rice_flat.items() if not isinstance(k, tuple)},
                                  "flat_goods_units": {cat.name(k): cat.unit(k) for k in rice_flat if not isinstance(k, tuple)},
                                  "prepared_api_per_kg": {cat.name(k): v for k, v in (rice_prepared or {}).items()}})
    dump("rice-theoretical-monthly.json", {m: {k: round(v, 4) for k, v in c.items()} for m, c in rice_pf_theory.items()})
    dump("hourly-sep-2026.json", {"summary": hourly_summary, "by_hour": hourly})
    dump("build-issues.json", {"issues": cat.issues[:200], "issue_count": len(cat.issues),
                                "unresolved_flatten_products_qty": dict(unresolved_rice)})
    print(json.dumps({"controls_ok": all(c["status"] == "matched" for c in controls), "month_totals": dict(tot_month),
                      "q3_total": q3tot, "rolls": len(roll_rows), "issues": len(cat.issues)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
