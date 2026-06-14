"""Sync the iiko nomenclature directory into the queryable ``iiko_product`` cache.

Pulls `/v2/entities/products/list` (the rich JSON catalogue — name, code, type, mainUnit)
and upserts every product by its iiko GUID. Unlike the legacy JSON-blob cache in
``iiko_inventory`` this keeps ALL types and deleted products (flagged) so the warehouse
picker can search/filter and so a GUID stays resolvable even after a product is removed.

The measure unit arrives only as a GUID (`mainUnit`); the catalogue has just a handful of
distinct ones. We resolve it via the ``iiko.unit_guid_map`` setting ({guid: «кг»}); a GUID
with no mapping leaves ``unit`` NULL and is reported in ``needs_unit_mapping`` so the owner
can fill those few values once.

Run from a BackgroundScheduler job with a throwaway NullPool engine — sharing the global
asyncpg pool from a thread corrupts it (known project pitfall).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting, IikoProduct
from app.services.iiko_inventory import PRODUCTS_ENDPOINT, _load_inventory_module

# iiko exposes the measure-units directory (кг/л/шт/порц) here; we resolve mainUnit
# GUIDs against it automatically each run, so no manual mapping is normally needed.
MEASURE_UNITS_ENDPOINT = "/v2/entities/list"
# Fallback/override map for GUIDs the directory doesn't return (rare): {guid: «кг»}.
UNIT_MAP_SETTING_KEY = "iiko.unit_guid_map"

_DELETED_TRUE = {"true", "1", "yes", "deleted"}


@dataclass
class IikoProductSyncResult:
    seen: int = 0
    created: int = 0
    updated: int = 0
    goods_count: int = 0
    # mainUnit GUIDs seen with no entry in iiko.unit_guid_map — fill these once.
    needs_unit_mapping: list[str] = field(default_factory=list)


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _fetch_units_map(client: Any) -> dict[str, str]:
    """GUID → unit label («кг»/«л»/«шт»/«порц») from the iiko measure-units directory."""
    _status, data = client.request(MEASURE_UNITS_ENDPOINT, params={"rootType": "MeasureUnit"})
    units: dict[str, str] = {}
    for unit in json.loads(data.decode("utf-8")):
        guid = _clean(unit.get("id"))
        name = _clean(unit.get("name"))
        if guid and name:
            units[guid] = name
    return units


def _fetch_catalogue_sync() -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Fetch products + measure units in one thread hop (the iiko client is sync)."""
    inventory = _load_inventory_module()
    inventory.load_local_env()
    client = inventory.IikoClient()
    units_map = _fetch_units_map(client)
    _status, data = client.request(PRODUCTS_ENDPOINT, params={"includeDeleted": "true"})
    payload = json.loads(data.decode("utf-8"))
    value_from = inventory.value_from
    rows: list[dict[str, Any]] = []
    for row in inventory.product_records_from_payload(payload):
        guid = _clean(value_from(row, ["id", "product", "productId"]))
        if not guid:
            continue
        product_type = _clean(value_from(row, ["type", "productType", "product_type"]))
        deleted = _clean(value_from(row, ["deleted", "isDeleted", "is_deleted"]))
        rows.append(
            {
                "iiko_id": guid,
                "name": _clean(value_from(row, ["name", "productName"])) or guid,
                "code": _clean(value_from(row, ["code", "num"])) or None,
                "type": (product_type or "UNKNOWN").upper(),
                "main_unit_guid": _clean(value_from(row, ["mainUnit", "mainUnitId"])) or None,
                "deleted": deleted.casefold() in _DELETED_TRUE,
            }
        )
    return rows, units_map


async def _load_unit_map(session: AsyncSession) -> dict[str, str]:
    setting = await session.scalar(
        select(AppSetting).where(AppSetting.key == UNIT_MAP_SETTING_KEY)
    )
    if setting is None or not isinstance(setting.value, dict):
        return {}
    return {str(guid): str(label) for guid, label in setting.value.items() if label}


async def sync_iiko_products(session: AsyncSession) -> IikoProductSyncResult:
    rows, units_map = await anyio.to_thread.run_sync(_fetch_catalogue_sync)
    # Directory units first; the setting only overrides/fills GUIDs iiko didn't return.
    unit_map = {**units_map, **await _load_unit_map(session)}
    existing = {p.iiko_id: p for p in (await session.scalars(select(IikoProduct))).all()}

    result = IikoProductSyncResult(seen=len(rows))
    now = datetime.now(UTC)
    needs_unit: set[str] = set()
    for row in rows:
        unit_guid = row["main_unit_guid"]
        unit = unit_map.get(unit_guid) if unit_guid else None
        if unit_guid and unit is None:
            needs_unit.add(unit_guid)
        if row["type"] == "GOODS":
            result.goods_count += 1

        product = existing.get(row["iiko_id"])
        if product is None:
            session.add(
                IikoProduct(
                    iiko_id=row["iiko_id"],
                    name=row["name"],
                    code=row["code"],
                    type=row["type"],
                    main_unit_guid=unit_guid,
                    unit=unit,
                    deleted=row["deleted"],
                    synced_at=now,
                )
            )
            result.created += 1
        else:
            product.name = row["name"]
            product.code = row["code"]
            product.type = row["type"]
            product.main_unit_guid = unit_guid
            product.unit = unit
            product.deleted = row["deleted"]
            product.synced_at = now
            result.updated += 1

    result.needs_unit_mapping = sorted(needs_unit)
    await session.commit()
    return result
