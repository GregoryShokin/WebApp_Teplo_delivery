#!/usr/bin/env python3
"""Three independent, tie-aware ABC rankings of a reconciled local SKU CSV."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

REQUIRED = {"sku_key", "name", "quantity", "revenue", "food_cost", "cost_status", "eligible"}
METRICS = ("quantity", "revenue", "contribution")
GENERATED = {"contribution", "analysis_flags"} | {
    f"{metric}_{field}" for metric in METRICS
    for field in ("abc", "rank", "share", "cumulative_share")
}
RESERVED = GENERATED | {"_values", "_eligible"}


def number(raw: str | None) -> Decimal | None:
    if raw is None or not raw.strip():
        return None
    try:
        value = Decimal(raw.strip())
    except InvalidOperation as exc:
        raise ValueError(f"Invalid numeric value: {raw!r}") from exc
    if not value.is_finite():
        raise ValueError("NaN and Infinity are not permitted")
    return value


def dec(value: Decimal | None) -> str:
    return "" if value is None else format(value, "f")


def analyze(rows: list[dict], a: Decimal, b: Decimal, coverage: str):
    if not Decimal(0) < a < b < Decimal(1):
        raise ValueError("Require 0 < a < b < 1")
    if coverage not in {"complete", "partial"}:
        raise ValueError("Unknown coverage")
    seen, out = set(), []
    for source in rows:
        if not REQUIRED.issubset(source):
            raise ValueError(f"Missing columns: {sorted(REQUIRED - source.keys())}")
        if RESERVED & source.keys():
            raise ValueError(f"Input contains reserved columns: {sorted(RESERVED & source.keys())}")
        row = dict(source)
        key = row["sku_key"].strip()
        if not key or key in seen:
            raise ValueError(f"Empty or duplicate sku_key: {key!r}")
        seen.add(key)
        eligible = row["eligible"].strip().lower()
        if eligible not in {"true", "false"}:
            raise ValueError(f"Invalid eligible flag for {key}")
        cost_status = row["cost_status"].strip().lower()
        if cost_status not in {"verified", "unresolved"}:
            raise ValueError(f"Invalid cost_status for {key}")
        values = {k: number(row[k]) for k in ("quantity", "revenue", "food_cost")}
        known_cost = cost_status == "verified" and values["food_cost"] is not None
        contribution = values["revenue"] - values["food_cost"] if known_cost and values["revenue"] is not None else None
        values["contribution"] = contribution
        flags = []
        if not known_cost: flags.append("UNVERIFIED_OR_MISSING_COST")
        if known_cost and values["food_cost"] == 0 and (values["revenue"] or 0) > 0:
            flags.append("VERIFIED_ZERO_COST")
        if coverage == "partial": flags.append("PARTIAL_SOURCE_COVERAGE")
        row.update(sku_key=key, contribution=dec(contribution), analysis_flags=";".join(flags))
        row["_values"], row["_eligible"] = values, eligible == "true"
        out.append(row)
    if not out:
        raise ValueError("Input contains no SKU rows")
    metadata = {}
    for metric in METRICS:
        groups = defaultdict(list)
        statuses = defaultdict(int)
        signed_total, negative_total = Decimal(0), Decimal(0)
        for row in out:
            v = row["_values"][metric]
            status = "EXCLUDED" if not row["_eligible"] else "UNKNOWN" if v is None else "NEGATIVE" if v < 0 else "ZERO" if v == 0 else "POSITIVE"
            statuses[status] += 1
            row.update({f"{metric}_{f}": "" for f in ("rank", "share", "cumulative_share")})
            row[f"{metric}_abc"] = status
            if row["_eligible"] and v is not None:
                signed_total += v
                if v < 0: negative_total += v
            if status == "POSITIVE": groups[v].append(row)
        positive_total = sum((v * len(group) for v, group in groups.items()), Decimal(0))
        cumulative, rank = Decimal(0), 1
        for value in sorted(groups, reverse=True):
            group = sorted(groups[value], key=lambda r: r["sku_key"])
            category = "A" if cumulative < a * positive_total else "B" if cumulative < b * positive_total else "C"
            cumulative += value * len(group)
            for row in group:
                row[f"{metric}_abc"] = category
                row[f"{metric}_rank"] = str(rank)
                row[f"{metric}_share"] = dec(value / positive_total)
                row[f"{metric}_cumulative_share"] = dec(cumulative / positive_total)
            rank += len(group)
        metadata[metric] = {
            "positive_base": dec(positive_total), "known_signed_total": dec(signed_total),
            "negative_total": dec(negative_total), "counts": dict(statuses),
            "status": "no_positive_base" if not groups else "provisional" if coverage == "partial" or statuses["UNKNOWN"] else "classified_known_population",
        }
    for row in out:
        del row["_values"], row["_eligible"]
    return sorted(out, key=lambda row: row["sku_key"]), metadata


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--population", required=True)
    p.add_argument("--coverage", choices=("complete", "partial"), required=True)
    p.add_argument("--a", type=Decimal, default=Decimal("0.80"))
    p.add_argument("--b", type=Decimal, default=Decimal("0.95"))
    args = p.parse_args()
    meta_path = Path(str(args.output) + ".meta.json")
    try:
        if args.input.resolve() in {args.output.resolve(), meta_path.resolve()}:
            raise ValueError("Output must differ from input")
        if args.output.exists() or meta_path.exists():
            raise ValueError("Output already exists; choose a new path")
        raw = args.input.read_bytes()
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        fields = reader.fieldnames or []
        if len(fields) != len(set(fields)) or not REQUIRED.issubset(fields):
            raise ValueError("Missing or duplicate CSV headers")
        rows = list(reader)
        if any(None in row or any(v is None for v in row.values()) for row in rows):
            raise ValueError("Malformed CSV row width")
        result, totals = analyze(rows, args.a, args.b, args.coverage)
        meta = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "population": args.population, "source_coverage": args.coverage,
            "input_sha256": hashlib.sha256(raw).hexdigest(), "row_count": len(rows),
            "thresholds": {"a": dec(args.a), "b": dec(args.b)},
            "boundary_rule": "class by cumulative positive mass before equal-value group; crossing group included",
            "contribution_definition": "revenue minus verified food_cost; not full profit",
            "metrics": totals,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields + ["contribution", "analysis_flags"] + [f"{m}_{c}" for m in METRICS for c in ("abc", "rank", "share", "cumulative_share")])
            writer.writeheader(); writer.writerows(result)
        with meta_path.open("x", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2); f.write("\n")
        print(json.dumps({"rows": len(result), "output": str(args.output), "metadata": str(meta_path)}, ensure_ascii=False))
    except (OSError, UnicodeError, ValueError, InvalidOperation) as exc:
        p.exit(2, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
