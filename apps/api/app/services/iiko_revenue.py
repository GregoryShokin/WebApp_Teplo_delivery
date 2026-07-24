from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

import anyio
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.iiko_location import get_revenue_department_name
from app.services.iiko_sync import (
    _load_export_employees_module,
    _load_source_credential_env,
    _request_iiko_with_incomplete_read_retry,
)

IIKO_REVENUE_ENDPOINT = "/v2/reports/olap"
IIKO_REVENUE_GROUP_BY_ROW_FIELDS = ["OpenDate.Typed"]
IIKO_REVENUE_AGGREGATE_FIELDS = ["DishDiscountSumInt"]
IIKO_REVENUE_DATE_FIELD = "OpenDate.Typed"
IIKO_REVENUE_FIELD = "DishDiscountSumInt"


async def fetch_daily_revenue(
    session: AsyncSession,
    date_from: dt.date,
    date_to: dt.date,
) -> dict[dt.date, Decimal]:
    if date_from > date_to:
        raise ValueError("date_from must be before or equal to date_to")

    await _load_source_credential_env(session)
    return await anyio.to_thread.run_sync(_fetch_daily_revenue_sync, date_from, date_to)


def _fetch_daily_revenue_sync(
    date_from: dt.date,
    date_to: dt.date,
) -> dict[dt.date, Decimal]:
    export_employees = _load_export_employees_module()
    export_employees.load_local_env()
    client = export_employees.IikoClient()

    _status, data = _request_iiko_with_incomplete_read_retry(
        client,
        IIKO_REVENUE_ENDPOINT,
        method="POST",
        json_body=build_daily_revenue_olap_body(date_from, date_to),
    )
    return parse_daily_revenue_olap_payload(data)


def build_daily_revenue_olap_body(date_from: dt.date, date_to: dt.date) -> dict[str, Any]:
    return {
        "reportType": "SALES",
        "buildSummary": "false",
        "groupByRowFields": IIKO_REVENUE_GROUP_BY_ROW_FIELDS,
        "aggregateFields": IIKO_REVENUE_AGGREGATE_FIELDS,
        "filters": {
            "OpenDate.Typed": {
                "filterType": "DateRange",
                "periodType": "CUSTOM",
                "from": date_from.isoformat(),
                # iiko OLAP трактует `to` как exclusive — без +1 последний день периода
                # выпадает из выборки (см. эмпирический тест 2026-05-31).
                "to": (date_to + dt.timedelta(days=1)).isoformat(),
            },
            "Department": {
                "filterType": "IncludeValues",
                "values": [get_revenue_department_name()],
            },
        },
    }


def parse_daily_revenue_olap_payload(data: bytes) -> dict[dt.date, Decimal]:
    rows = _json_rows(data)
    revenue_by_date: dict[dt.date, Decimal] = {}
    for row in rows:
        work_date = _parse_date(
            _value_from(row, IIKO_REVENUE_DATE_FIELD, "OpenDate", "openDate", "date")
        )
        if work_date is None:
            continue
        revenue = _parse_decimal(
            _value_from(
                row,
                IIKO_REVENUE_FIELD,
                "DishDiscountSumInt.sum",
                "sumAfterDiscountWithoutVAT",
                "revenue_with_discount",
                "revenue",
            )
        )
        revenue_by_date[work_date] = revenue_by_date.get(work_date, Decimal("0")) + revenue
    return revenue_by_date


def _json_rows(data: bytes) -> list[Mapping[str, Any]]:
    import json

    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []

    return _records(payload, _column_names(payload))


def _records(value: Any, columns: list[str]) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        if all(isinstance(item, Mapping) for item in value):
            return [_normalize_row(item, columns) for item in value]
        if columns and all(isinstance(item, list) for item in value):
            return [dict(zip(columns, item, strict=False)) for item in value]
        rows: list[Mapping[str, Any]] = []
        for item in value:
            rows.extend(_records(item, columns))
        return rows

    if isinstance(value, Mapping):
        normalized = _normalize_row(value, columns)
        if _has_revenue_fields(normalized):
            return [normalized]
        for key in ("rows", "data", "items", "records", "result", "response"):
            if key in value:
                found = _records(value[key], _column_names(value) or columns)
                if found:
                    return found
    return []


def _normalize_row(row: Mapping[str, Any], columns: list[str]) -> Mapping[str, Any]:
    for key in ("values", "value", "cells"):
        value = row.get(key)
        if isinstance(value, Mapping):
            return row | value
        if columns and isinstance(value, list):
            return row | dict(zip(columns, value, strict=False))

    dimensions = row.get("dimensions")
    measures = row.get("measures")
    if columns and isinstance(dimensions, list) and isinstance(measures, list):
        return row | dict(zip(columns, [*dimensions, *measures], strict=False))

    return row


def _column_names(payload: Any) -> list[str]:
    if not isinstance(payload, Mapping):
        return []
    raw_columns = payload.get("columns") or payload.get("headers") or payload.get("fields")
    if not isinstance(raw_columns, list):
        return []

    columns: list[str] = []
    for column in raw_columns:
        if isinstance(column, str):
            columns.append(column)
        elif isinstance(column, Mapping):
            name = _value_from(column, "name", "field", "id", "key", "title")
            if name:
                columns.append(str(name))
    return columns


def _has_revenue_fields(row: Mapping[str, Any]) -> bool:
    return (
        _value_from(row, IIKO_REVENUE_DATE_FIELD, "OpenDate", "openDate", "date") is not None
        and _value_from(row, IIKO_REVENUE_FIELD, "DishDiscountSumInt.sum", "revenue") is not None
    )


def _value_from(row: Mapping[str, Any], *candidates: str) -> Any:
    for candidate in candidates:
        if candidate in row:
            return row[candidate]
    lowered = {str(key).casefold(): value for key, value in row.items()}
    for candidate in candidates:
        value = lowered.get(candidate.casefold())
        if value is not None:
            return value
    return None


def _parse_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, Mapping):
        return _parse_date(_value_from(value, "value", "date", IIKO_REVENUE_DATE_FIELD))
    if value is None:
        return None

    text = str(value).replace("\xa0", " ").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return dt.datetime.strptime(text[:10], fmt).date()
        except ValueError:
            pass
    try:
        return dt.datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _parse_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Mapping):
        return _parse_decimal(_value_from(value, "value", "sum", IIKO_REVENUE_FIELD))
    if isinstance(value, int | Decimal):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))

    text = str(value).replace("\xa0", " ").replace(" ", "").strip()
    if not text:
        return Decimal("0")
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal("0")
