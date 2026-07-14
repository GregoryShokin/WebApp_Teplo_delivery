#!/usr/bin/env python3
"""Suggest semantic iiko nomenclature candidates for transcribed inventory labels."""

from __future__ import annotations

import difflib
import json
import re
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHEETS_PATH = ROOT / "tmp" / "inventory_sheet_items.json"
IIKO_PATH = ROOT / "tmp" / "iiko_products_live.json"
OUT_PATH = ROOT / "tmp" / "iiko_match_candidates.json"


REPLACEMENTS = {
    "ё": "е",
    "п/ф": " полуфабрикат ",
    "п.ф.": " полуфабрикат ",
    "п.ф": " полуфабрикат ",
    "с/м": " свежемороженый ",
    "х/к": " холодного копчения ",
    "ч/в": " чистый вес ",
    "св-гв": " свинина говядина ",
    "св-гв.": " свинина говядина ",
    "свинина-говядина": " свинина говядина ",
    "эби-фрай": " эби фрай креветка кляр ",
    "эби фрай": " эби фрай креветка кляр ",
    "том-ям": " том ям ",
    "манго-чили": " манго чили ",
    "тар-тар": " тартар ",
    "дорблю": " дор блю голубая плесень ",
    "дон-дар": " дон дар ",
    "шрирача": " срирача шрирача ",
}

STOPWORDS = {
    "товар",
    "полуфабрикат",
    "соус",
    "вакуум",
    "чистый",
    "вес",
    "готовое",
    "готовый",
    "пачка",
    "кг",
    "шт",
    "для",
    "на",
}


def normalize(value: str, *, drop_stopwords: bool = False) -> str:
    text = value.lower().strip()
    for old, new in REPLACEMENTS.items():
        text = text.replace(old, new)
    text = re.sub(r"\b1\s*пачка\s*=\s*0[,.]7\s*кг\b", " ", text)
    text = re.sub(r"\b1[,.]8\s*кг\b", " ", text)
    text = re.sub(r"[^a-zа-я0-9%]+", " ", text)
    tokens = text.split()
    if drop_stopwords:
        tokens = [token for token in tokens if token not in STOPWORDS]
    return " ".join(tokens)


def preferred_types(label: str) -> list[str]:
    n = normalize(label)
    if "полуфабрикат" in n:
        return ["PREPARED", "GOODS"]
    if "товар" in n:
        return ["GOODS", "PREPARED"]
    return ["GOODS", "PREPARED", "DISH", "MODIFIER"]


def score(source: str, candidate: str) -> float:
    a = normalize(source, drop_stopwords=True)
    b = normalize(candidate, drop_stopwords=True)
    if not a or not b:
        return 0.0
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    overlap = len(a_tokens & b_tokens)
    union = len(a_tokens | b_tokens)
    jaccard = overlap / union if union else 0
    containment = overlap / min(len(a_tokens), len(b_tokens))
    sequence = difflib.SequenceMatcher(None, a, b).ratio()
    exact_bonus = 0.25 if a == b else 0
    contains_bonus = 0.12 if a in b or b in a else 0
    return min(1.0, 0.45 * sequence + 0.30 * containment + 0.25 * jaccard + exact_bonus + contains_bonus)


def deduplicate(rows: list[dict]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for row in rows:
        key = normalize(row["name"])
        if key not in grouped:
            grouped[key] = {
                "source_name": row["name"],
                "pages": [],
                "sections": [],
            }
        if row["page"] not in grouped[key]["pages"]:
            grouped[key]["pages"].append(row["page"])
        if row["section"] not in grouped[key]["sections"]:
            grouped[key]["sections"].append(row["section"])
    return sorted(grouped.values(), key=lambda row: (min(row["pages"]), row["source_name"].lower()))


def main() -> None:
    source_rows = json.loads(SHEETS_PATH.read_text(encoding="utf-8"))
    products = json.loads(IIKO_PATH.read_text(encoding="utf-8"))
    unique_rows = deduplicate(source_rows)
    by_type: dict[str, list[dict]] = defaultdict(list)
    for product in products:
        by_type[product["type"]].append(product)

    output = []
    for row in unique_rows:
        types = preferred_types(row["source_name"])
        candidates = []
        for type_rank, product_type in enumerate(types):
            for product in by_type.get(product_type, []):
                raw_score = score(row["source_name"], product["name"])
                adjusted = raw_score - type_rank * 0.04
                candidates.append((adjusted, raw_score, product))
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        output.append(
            {
                **row,
                "candidates": [
                    {
                        **product,
                        "score": round(raw_score * 100),
                    }
                    for _adjusted, raw_score, product in candidates[:8]
                ],
            }
        )

    OUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"source_rows={len(source_rows)}")
    print(f"unique_source_labels={len(output)}")
    print(f"iiko_items={len(products)}")
    print(f"output={OUT_PATH}")


if __name__ == "__main__":
    main()
