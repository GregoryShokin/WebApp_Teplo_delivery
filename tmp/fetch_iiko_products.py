#!/usr/bin/env python3
"""Fetch the live iiko product catalogue with read-only GET requests."""

from __future__ import annotations

import json
import time
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
IIKO_SCRIPTS = PROJECT_ROOT / "integrations" / "iiko" / "scripts"
sys.path.insert(0, str(IIKO_SCRIPTS))

from build_inventory_results import PRODUCTS_ENDPOINT, product_records_from_payload  # noqa: E402
from export_orders_delivery import IikoClient, load_local_env, value_from  # noqa: E402


OUTPUT = PROJECT_ROOT / "tmp" / "iiko_products_live.json"
MEASURE_UNITS_ENDPOINT = "/v2/entities/list"
DELETED_TRUE = {"true", "1", "yes", "deleted"}


def clean(value: object) -> str:
    return str(value).replace("\xa0", " ").strip() if value is not None else ""


def main() -> int:
    load_local_env()
    client = IikoClient()

    _, units_raw = client.request(MEASURE_UNITS_ENDPOINT, params={"rootType": "MeasureUnit"})
    units_payload = json.loads(units_raw.decode("utf-8"))
    units = {
        clean(row.get("id")): clean(row.get("name"))
        for row in units_payload
        if isinstance(row, dict) and clean(row.get("id")) and clean(row.get("name"))
    }

    products_raw: bytes | None = None
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            _, products_raw = client.request(PRODUCTS_ENDPOINT, params={"includeDeleted": "false"})
            break
        except Exception as exc:  # iiko occasionally closes a chunked response early
            last_error = exc
            if attempt < 3:
                time.sleep(1.5 * (attempt + 1))
    if products_raw is None:
        raise RuntimeError("Could not read the complete iiko product catalogue") from last_error
    products_payload = json.loads(products_raw.decode("utf-8"))
    products: list[dict[str, object]] = []
    for row in product_records_from_payload(products_payload):
        product_type = clean(value_from(row, ["type", "productType", "product_type"])).upper()
        deleted = clean(value_from(row, ["deleted", "isDeleted", "is_deleted"])).casefold()
        if product_type != "GOODS" or deleted in DELETED_TRUE:
            continue
        guid = clean(value_from(row, ["id", "product", "productId"]))
        name = clean(value_from(row, ["name", "productName"]))
        if not guid or not name:
            continue
        unit_guid = clean(value_from(row, ["mainUnit", "mainUnitId"]))
        products.append(
            {
                "iiko_id": guid,
                "name": name,
                "code": clean(value_from(row, ["code", "num"])) or None,
                "unit": units.get(unit_guid) or None,
                "unit_guid": unit_guid or None,
                "type": product_type,
            }
        )

    products.sort(key=lambda item: str(item["name"]).casefold())
    OUTPUT.write_text(json.dumps(products, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"fetched_active_goods={len(products)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
