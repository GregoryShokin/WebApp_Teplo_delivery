from __future__ import annotations

import importlib
import json
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import ModuleType
from typing import Any

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting
from app.services.iiko_sync import _candidate_project_roots

INVENTORY_ENDPOINT = "/reports/storeOperations"
PRODUCTS_ENDPOINT = "/v2/entities/products/list"
PRODUCTS_CACHE_KEY = "iiko.products_cache"
PRODUCTS_CACHE_TTL = timedelta(hours=24)


def _load_inventory_module() -> ModuleType:
    for root in _candidate_project_roots():
        script_dir = Path(root) / "integrations/iiko/scripts"
        if not (script_dir / "build_inventory_results.py").exists():
            continue
        script_dir_str = str(script_dir)
        if script_dir_str not in sys.path:
            sys.path.insert(0, script_dir_str)
        return importlib.import_module("build_inventory_results")
    raise RuntimeError("integrations/iiko/scripts/build_inventory_results.py is not available")


async def fetch_inventory_documents(business_date: date) -> list[dict[str, Any]]:
    return await anyio.to_thread.run_sync(_fetch_inventory_documents_sync, business_date)


async def fetch_inventory_document(business_date: date) -> dict[str, Any] | None:
    documents = await fetch_inventory_documents(business_date)
    return documents[0] if documents else None


async def fetch_products_catalog(
    session: AsyncSession | None = None,
    *,
    refresh: bool = False,
) -> dict[str, dict[str, str]]:
    if session is not None and not refresh:
        cached = await _read_products_cache(session)
        if cached is not None:
            return cached

    products = await anyio.to_thread.run_sync(_fetch_products_catalog_sync)
    if session is not None:
        await _write_products_cache(session, products)
    return products


def _fetch_inventory_documents_sync(business_date: date) -> list[dict[str, Any]]:
    inventory = _load_inventory_module()
    inventory.load_local_env()
    client = inventory.IikoClient()
    params = inventory.inventory_params(business_date, business_date)
    _status, data = client.request(INVENTORY_ENDPOINT, params=params)
    _response_format, rows = inventory.parse_store_operations(data)
    products: dict[str, dict[str, str]] = {}
    try:
        products = inventory.load_products(client, refresh=False)
    except Exception:  # noqa: BLE001 - product names may still be present in rows
        products = {}

    documents: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = _shortage_item_from_row(inventory, row, products, business_date)
        if item is None:
            continue
        document_id = item.pop("document_id")
        document = documents.setdefault(
            document_id,
            {
                "document_id": document_id,
                "document_num": item.pop("document_num"),
                "items": [],
                "total_shortage": Decimal("0"),
            },
        )
        document["items"].append(item)
        document["total_shortage"] += item["shortage_amount"]

    result = sorted(
        documents.values(),
        key=lambda item: (str(item.get("document_num") or ""), str(item.get("document_id") or "")),
    )
    for document in result:
        document["total_shortage"] = _money(document["total_shortage"])
        for item in document["items"]:
            item["shortage_amount"] = _money(item["shortage_amount"])
    return result


def _fetch_products_catalog_sync() -> dict[str, dict[str, str]]:
    inventory = _load_inventory_module()
    inventory.load_local_env()
    client = inventory.IikoClient()
    _status, data = client.request(PRODUCTS_ENDPOINT)
    payload = json.loads(data.decode("utf-8"))
    products: dict[str, dict[str, str]] = {}
    for row in inventory.product_records_from_payload(payload):
        product_type = _clean(inventory.value_from(row, ["type", "productType", "product_type"]))
        if product_type.upper() != "GOODS":
            continue
        deleted = _clean(inventory.value_from(row, ["deleted", "isDeleted", "is_deleted"]))
        if deleted.casefold() in {"true", "1", "yes", "deleted"}:
            continue
        guid = _clean(inventory.value_from(row, ["id", "product", "productId"]))
        if not guid:
            continue
        products[guid] = {
            "guid": guid,
            "id": guid,
            "code": _clean(inventory.value_from(row, ["code", "num"])),
            "name": _clean(inventory.value_from(row, ["name", "productName"])),
        }
    return products


def _shortage_item_from_row(
    inventory: ModuleType,
    row: dict[str, Any],
    products: dict[str, dict[str, str]],
    business_date: date,
) -> dict[str, Any] | None:
    document_type = _clean(inventory.value_from(row, ["documentType", "document_type"]))
    if document_type and document_type != "INCOMING_INVENTORY":
        return None

    operation_type = _clean(
        inventory.value_from(
            row,
            ["type", "operationType", "operation_type", "documentOperationType"],
        )
    )
    if operation_type and operation_type != "INVENTORY_CORRECTION":
        return None

    document_date = inventory.parse_iiko_date(
        inventory.value_from(row, ["date", "documentDate", "document_date"])
    )
    if document_date is not None and document_date != business_date:
        return None

    # iiko возвращает secondaryAccount как GUID счёта, не имя.
    # Различаем недостачу/излишек по полю incoming: false = недостача (списание),
    # true = излишек (приход). Дополнительно проверяем знак sum.
    incoming_raw = _clean(inventory.value_from(row, ["incoming", "is_incoming"]))
    is_incoming = str(incoming_raw).casefold() in {"true", "1", "yes"}

    amount = _decimal(inventory.value_from(row, ["sum", "cost", "documentSum", "amount"]))
    if amount == 0:
        return None

    # Недостача = либо incoming=false, либо отрицательная sum.
    is_shortage = (not is_incoming) or amount < 0
    if not is_shortage:
        return None

    shortage_amount = abs(amount)
    if shortage_amount <= 0:
        return None

    document_id = _clean(inventory.value_from(row, ["documentId", "document_id", "document"]))
    if not document_id:
        return None

    product_guid = _clean(inventory.value_from(row, ["product", "productId", "product_id"]))
    product_ref = products.get(product_guid, {})
    product_name = _clean(
        inventory.value_from(row, ["productName", "product.name", "name"])
        or product_ref.get("name")
    )
    if not product_name:
        product_name = product_guid or "Позиция iiko"

    return {
        "document_id": document_id,
        "document_num": _clean(
            inventory.value_from(row, ["documentNum", "documentNumber", "number"])
        ),
        "product_guid": product_guid or None,
        "product_name": product_name,
        "shortage_amount": shortage_amount,
    }


async def _read_products_cache(session: AsyncSession) -> dict[str, dict[str, str]] | None:
    setting = await session.scalar(select(AppSetting).where(AppSetting.key == PRODUCTS_CACHE_KEY))
    if setting is None or not isinstance(setting.value, dict):
        return None
    fetched_at_raw = setting.value.get("fetched_at")
    products = setting.value.get("products")
    if not isinstance(fetched_at_raw, str) or not isinstance(products, dict):
        return None
    try:
        fetched_at = datetime.fromisoformat(fetched_at_raw)
    except ValueError:
        return None
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=UTC)
    if datetime.now(UTC) - fetched_at > PRODUCTS_CACHE_TTL:
        return None
    return {
        str(guid): product
        for guid, product in products.items()
        if isinstance(product, dict) and product.get("guid")
    }


async def _write_products_cache(
    session: AsyncSession,
    products: dict[str, dict[str, str]],
) -> None:
    setting = await session.scalar(select(AppSetting).where(AppSetting.key == PRODUCTS_CACHE_KEY))
    value = {"fetched_at": datetime.now(UTC).isoformat(), "products": products}
    if setting is None:
        setting = AppSetting(
            key=PRODUCTS_CACHE_KEY,
            value=value,
            value_type="json",
            category="integrations",
            display_name="Кэш товаров iiko",
            description="Временный кэш справочника товаров iiko для маппинга ревизий.",
            widget_type="json",
            widget_options=None,
            unit=None,
        )
        session.add(setting)
    else:
        setting.value = value
        setting.updated_at = datetime.now(UTC)
    await session.commit()


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\xa0", " ").strip()
    if text.casefold() in {"none", "null", "nan"}:
        return ""
    return text


def _decimal(value: Any) -> Decimal:
    text = _clean(value).replace(" ", "").replace("\u2212", "-")
    if not text:
        return Decimal("0")
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    else:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal("0")


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))
