#!/usr/bin/env python3
"""Combine transcribed labels, reviewed overrides, and the cached iiko catalog."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def status_for(confidence: int, has_match: bool) -> str:
    if not has_match:
        return "Не найдено"
    if confidence >= 95:
        return "Точное"
    if confidence >= 75:
        return "По смыслу"
    return "Требует проверки"


def main() -> None:
    candidates = json.loads((ROOT / "tmp" / "iiko_match_candidates.json").read_text(encoding="utf-8"))
    overrides = json.loads((ROOT / "tmp" / "manual_iiko_overrides.json").read_text(encoding="utf-8"))
    products = json.loads((ROOT / "tmp" / "iiko_products_live.json").read_text(encoding="utf-8"))
    by_code = {str(product["code"]): product for product in products}

    output = []
    for source in candidates:
        override = overrides[source["source_name"]]
        product = by_code.get(str(override["code"])) if override["code"] is not None else None
        confidence = int(override["confidence"])
        output.append(
            {
                "pages": ", ".join(str(page) for page in source["pages"]),
                "sections": "; ".join(source["sections"]),
                "source_name": source["source_name"],
                "iiko_name": product["name"] if product else "",
                "iiko_code": product["code"] if product else "",
                "iiko_unit": product["unit"] if product else "",
                "iiko_type": product["type"] if product else "",
                "iiko_id": product["iiko_id"] if product else "",
                "confidence": confidence,
                "status": status_for(confidence, product is not None),
                "comment": override.get("comment", ""),
            }
        )

    path = ROOT / "tmp" / "final_iiko_matches.json"
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    counts = {}
    for row in output:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    print(f"rows={len(output)}")
    print("status_counts=" + json.dumps(counts, ensure_ascii=False, sort_keys=True))
    print(f"output={path}")


if __name__ == "__main__":
    main()
