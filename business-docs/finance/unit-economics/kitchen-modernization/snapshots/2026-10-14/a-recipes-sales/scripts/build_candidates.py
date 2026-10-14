#!/usr/bin/env python3
"""Признаки конструкции роллов, плоская таблица и отбор кандидатов для хронометража.

Локально, по результатам build_baseline.py и build_rice_costs.py:

    python3 build_candidates.py --data <каталог data>

Конструкция — по группе меню iiko + составу прямых строк техкарты (не по наблюдению на кухне).
Список кандидатов задан явно (CANDIDATES) с причинами; скрипт считает доли покрытия.
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
from pathlib import Path

NORI_PRICE_RANGE = (8.9, 9.4)  # две цены в последней накладной DX001480A от 08.10 — конфликт, см. pricebook
TOPPING_SAUCES = ("Сырный соус", "Соус  Яки", "Соус Яки", "Юкихиро", "Соус Макси", "Соус острый с крабом", "черный угорь",
                  "Соус Лава", "Лава соус", "Спайси")
CANDIDATES = [
    ("Цезарь", "№1 по физическому объёму; жареный: кляр + сухари → фритюр целого ролла; резак режет горячую/хрустящую корку"),
    ("Хоккайдо", "№2 по объёму; запечённый: соус-шапка Юкихиро 60 г + кунжут; резак и шапка соуса"),
    ("Камчатский", "№3 по объёму и главный ролл подарочного сета «Денег нет»; базовый урамаки рис наружу с кунжутом — эталон без доп. операций"),
    ("Запеченная Калифорния", "№4; запечённый с сырным соусом 60 г + масаго; тянущийся сыр — типичная проблема резака"),
    ("Жареная калифорния", "№5; жареный, но на ЦЕЛОМ листе нори (1,0) — для матов другой формат раскладки, чем 0,5"),
    ("Лава краб", "№7; соус-шапка «Лава» 60 г без запекания (холодный топпинг) — отдельный случай для резака"),
    ("Классический с крабом", "№13, лидер «Классических»: рис 120 г (не 130), нори 1,0 — единственная группа с другим весом риса в меню"),
    ("Филадельфия лайт", "тип «рыба сверху» (по названию/описанию; лидер семейства Филадельфий по объёму, 1,1 %); резка сырой рыбы"),
]
RESERVE = [
    ("Честер", "нори 1,0 + соус Лава + унаги + кунжут; замена «Лава краба», если нужен целый лист"),
    ("Тори Яки", "запечённый на целом листе нори (1,0); замена «Запеченной Калифорнии»"),
    (" Динамит", "6 кусочков (а не 8); всего 0,45 % объёма в сентябре — для проверки резака на 6"),
    ("Сакура", "жареный №3 по объёму; дубль «Цезаря» по технологии"),
]


def construction(r):
    rc = r["recipe_14_10"] or {}
    names = [i["name"] for i in rc.get("items", [])]
    g = r["group"]
    fried = any("Кляр" in n for n in names) and any("Сухари" in n or "Панко" in n for n in names)
    toppings = sorted({n.strip() for n in names for t in TOPPING_SAUCES if t in n})
    outer = sorted({n.strip() for n in names if any(k in n for k in ("Кунжут", "Масаго"))})
    if fried:
        kind = "жареный (кляр + панировка, фритюр целого ролла)"
    elif g.endswith("Запеченные"):
        kind = "запечённый (соус-шапка + запекание)"
    elif g.endswith("Классические"):
        kind = "классический (рис, нори, 1 начинка)"
    elif "Филадельфия" in r["name"]:
        kind = "урамаки с рыбой сверху (по названию; подтвердить)"
    elif toppings:
        kind = "урамаки с соусом без запекания (соус-шапка — по описанию сайта, подтвердить)"
    else:
        kind = "урамаки (рис наружу; по составу/группе, подтвердить)"
    ops = []
    if fried:
        ops.append("фритюр")
    if g.endswith("Запеченные"):
        ops.append("запекание (оборудование не указано в техкарте)")
    if toppings:
        ops.append("соус в техкарте (место нанесения не указано): " + ", ".join(toppings))
    if outer:
        ops.append("кунжут/масаго (снаружи или внутри — техкарта не указывает): " + ", ".join(outer))
    pf_fried = [n.strip() for n in names if any(k in n for k in ("Стрипсы", "Мидии жареные", "в кляре"))]
    if pf_fried:
        ops.append("жареная начинка-п/ф: " + ", ".join(pf_fried))
    return kind, ops


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    a = ap.parse_args()
    d = a.data
    rolls = json.loads((d / "rolls-physical-monthly.json").read_text())
    costs = {c["roll_guid"]: c for c in json.loads((d / "roll-material-costs.json").read_text())["rolls"]}
    R = rolls["rolls"]
    q3 = sum(r["q3_total"] for r in R)
    sep = sum(r["2026-09_total"] for r in R)
    flat = []
    for r in R:
        kind, ops = construction(r)
        rc = r["recipe_14_10"] or {}
        c = costs[r["roll_guid"]]
        nori = rc.get("nori_sheets") or 0
        row = {
            "rank_q3": r["rank_q3"], "roll_guid": r["roll_guid"], "name": r["name"].strip(), "group_iiko": r["group"],
            "baseline_menu_status": r["baseline_menu_status"], "site": r["site_match"], "pieces_per_roll": r["pieces_per_roll"],
            "pieces_source": r["pieces_source"], "construction": kind, "extra_operations": "; ".join(ops),
            "rice_pf_g": rc.get("rice_pf_g"), "raw_rice_g": c["raw_rice_g"], "nori_sheets": nori,
            "sum_direct_inputs_g_kg_only": rc.get("sum_direct_inputs_kg_only_g"), "site_declared_weight_g": r["site_declared_weight_g"],
            "chart_id": rc.get("chart_id"), "chart_dateFrom": rc.get("dateFrom"),
        }
        for m in ("2026-07", "2026-08", "2026-09", "2026-10p"):
            for k in ("retail", "in_sets", "gift", "total"):
                row[f"{m}_{k}"] = r[f"{m}_{k}"]
        row.update(q3_total=r["q3_total"], q3_share=r["q3_share"], q3_cum_share=r["q3_cum_share"],
                   sep_share=round(r["2026-09_total"] / sep, 6),
                   material_ex_nori_ex_water_rub=c["priced_material_cost_ex_water_rub"],
                   material_with_nori_low_rub=round(c["priced_material_cost_ex_water_rub"] + nori * NORI_PRICE_RANGE[0], 2),
                   material_with_nori_high_rub=round(c["priced_material_cost_ex_water_rub"] + nori * NORI_PRICE_RANGE[1], 2),
                   rice_pf_cost_rub=c["rice_pf_cost_rub"], water_l=c["water_l"],
                   unpriced_leaves_except_nori="; ".join(f"{u['leaf']} {u.get('qty','')}{u.get('unit','')}" for u in c["unpriced_leaves"] if u["leaf"] != "Нори"),
                   cost_top_leaves="; ".join(f"{l['leaf']} {l['cost_rub']}₽ ({l['price_date']})" for l in c["leaves"][:5]))
        flat.append(row)
    with (d / "rolls-physical-monthly.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(flat[0].keys()))
        w.writeheader()
        w.writerows(flat)
    by_name = {r["name"]: r for r in flat}

    def cluster(r):
        return (r["construction"], r["nori_sheets"], r["rice_pf_g"], r["pieces_per_roll"])

    cand_rows, covered_clusters = [], set()
    for name, why in CANDIDATES:
        r = by_name[name.strip()]
        covered_clusters.add(cluster(r))
        cand_rows.append({**{k: r[k] for k in ("rank_q3", "roll_guid", "name", "group_iiko", "baseline_menu_status", "site",
                                               "construction", "extra_operations", "pieces_per_roll", "rice_pf_g", "raw_rice_g",
                                               "nori_sheets", "sum_direct_inputs_g_kg_only", "site_declared_weight_g",
                                               "2026-09_retail", "2026-09_in_sets", "2026-09_gift", "2026-09_total", "q3_total",
                                               "q3_share", "sep_share", "material_ex_nori_ex_water_rub", "material_with_nori_low_rub",
                                               "material_with_nori_high_rub", "rice_pf_cost_rub", "unpriced_leaves_except_nori",
                                               "cost_top_leaves", "chart_id", "chart_dateFrom")}, "why": why})
    direct_q3 = sum(c["q3_total"] for c in cand_rows)
    direct_sep = sum(c["2026-09_total"] for c in cand_rows)
    clus_q3 = sum(r["q3_total"] for r in flat if cluster(r) in covered_clusters)
    clus_sep = sum(r["2026-09_total"] for r in flat if cluster(r) in covered_clusters)
    kinds_q3 = collections.Counter()
    for r in flat:
        kinds_q3[r["construction"]] += r["q3_total"]
    cand_kinds = {c["construction"] for c in cand_rows}
    out = {
        "unit": "целые роллы (порц iiko); доли — от физического объёма роллов (розница + в сетах + подарки)",
        "physical_rolls_q3": q3, "physical_rolls_sep": sep,
        "candidates": cand_rows, "reserve": [{"name": n.strip(), "why": w, "q3_total": by_name[n.strip()]["q3_total"],
                                              "sep_total": by_name[n.strip()]["2026-09_total"]} for n, w in RESERVE],
        "coverage": {
            "direct_share_q3": round(direct_q3 / q3, 4), "direct_share_sep": round(direct_sep / sep, 4),
            "cluster_definition": "кластер = (конструкция, листов нори, г рисового п/ф, кусочков)",
            "cluster_share_q3": round(clus_q3 / q3, 4), "cluster_share_sep": round(clus_sep / sep, 4),
            "construction_types_share_q3": {k: round(v / q3, 4) for k, v in kinds_q3.most_common()},
            "construction_types_covered_share_q3": round(sum(v for k, v in kinds_q3.items() if k in cand_kinds) / q3, 4),
        },
        "excluded_high_volume": [{"name": r["name"], "q3_total": r["q3_total"], "reason": "defaultIncludedInMenu=false (Проработки); производится только в сетах «Нейросет»/«Денег нет»"}
                                 for r in flat if r["baseline_menu_status"] != "включён" and r["q3_total"] > 300],
    }
    (d / "timing-candidates.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
    with (d / "timing-candidates.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(cand_rows[0].keys()))
        w.writeheader()
        w.writerows(cand_rows)
    print(json.dumps(out["coverage"], ensure_ascii=False, indent=1))
    for c in cand_rows:
        print(c["name"], c["construction"], c["q3_total"], c["sep_share"], c["material_ex_nori_ex_water_rub"],
              c["material_with_nori_high_rub"], c["unpriced_leaves_except_nori"], "|", c["extra_operations"])


if __name__ == "__main__":
    main()
