#!/usr/bin/env python3
"""Read-only выгрузка iikoServer для среза 2026-10-14 (направление «А», модернизация суши-цеха).

Запускается ВНУТРИ контейнера приложения (teplo-api, /app/apps/api):

    ssh teplo-prod 'docker exec -i -w /app/apps/api -e PYTHONPATH=/app/apps/api teplo-api \
        python - <phase> /tmp/<outdir>' < iiko_readonly_fetch.py

Что делает:
- открывает отдельную транзакцию БД `READ ONLY` + statement_timeout только для того, чтобы
  штатный `_load_source_credential_env` поднял реквизиты iiko в окружение процесса; rollback, dispose;
- ОДИН экземпляр IikoClient на весь прогон (одна лицензия), последовательные запросы;
- только GET и POST построения OLAP-отчёта (`/v2/reports/olap` — чтение);
- в конце `/logout` для токенов, полученных этим прогоном (заранее заданный общий токен не трогаем);
- пишет raw-ответы и `<name>.source.json` (endpoint, параметры, время, статус, sha256) в outdir.
Секреты не печатаются и не пишутся. sync/import/запись в iiko не вызываются.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path

PHASE = sys.argv[1] if len(sys.argv) > 1 else "1"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/kitchenecon_a")
OUT.mkdir(parents=True, exist_ok=True)

DEPT_CODE = "2"  # Foodmarket Тепло Черникова (подтверждается departments.xml и контролем пресета)
NOT_DELETED = {
    "DeletedWithWriteoff": {"filterType": "IncludeValues", "values": ["NOT_DELETED"]},
    "OrderDeleted": {"filterType": "IncludeValues", "values": ["NOT_DELETED"]},
}
REVENUE_PRESET_ID = "73a25778-dafb-4065-a820-1e9c7da6fed6"
MONTHS = [("2026-07", "2026-07-01", "2026-08-01"), ("2026-08", "2026-08-01", "2026-09-01"),
          ("2026-09", "2026-09-01", "2026-10-01"), ("2026-10p", "2026-10-01", "2026-10-14")]


def date_range(frm: str, to_excl: str) -> dict:
    return {"filterType": "DateRange", "from": f"{frm}T00:00:00", "to": f"{to_excl}T00:00:00",
            "includeLow": True, "includeHigh": False, "periodType": "CUSTOM"}


def sales_body(frm: str, to_excl: str, rows: list[str], aggs: list[str], extra: dict | None = None) -> dict:
    filters = {**NOT_DELETED, "OpenDate.Typed": date_range(frm, to_excl),
               "Department.Code": {"filterType": "IncludeValues", "values": [DEPT_CODE]}}
    filters.update(extra or {})
    return {"reportType": "SALES", "buildSummary": False, "groupByRowFields": rows,
            "groupByColFields": [], "aggregateFields": aggs, "filters": filters}


SKU_ROWS = ["OpenDate.Typed", "Department.Code", "DishId", "DishName", "SoldWithDish.Id",
            "SoldWithDish", "DishSize.Id", "DishSize.Name"]
SKU_AGGS = ["DishAmountInt", "DishSumInt", "DishDiscountSumInt", "ProductCostBase.ProductCost"]


def plan_phase1() -> list[dict]:
    p: list[dict] = [
        {"name": "columns-sales", "method": "GET", "endpoint": "/v2/reports/olap/columns", "params": {"reportType": "SALES"}},
        {"name": "columns-transactions", "method": "GET", "endpoint": "/v2/reports/olap/columns", "params": {"reportType": "TRANSACTIONS"}},
        {"name": "presets", "method": "GET", "endpoint": "/v2/reports/olap/presets", "params": {}},
        {"name": "departments", "method": "GET", "endpoint": "/corporation/departments", "params": {"includeDeleted": "true"}},
        {"name": "stores", "method": "GET", "endpoint": "/corporation/stores", "params": {"includeDeleted": "true"}},
        {"name": "units", "method": "GET", "endpoint": "/v2/entities/list", "params": {"rootType": "MeasureUnit"}},
        {"name": "products", "method": "GET", "endpoint": "/v2/entities/products/list", "params": {"includeDeleted": "true"}},
        {"name": "groups", "method": "GET", "endpoint": "/v2/entities/products/group/list", "params": {"includeDeleted": "true"}},
        {"name": "charts", "method": "GET", "endpoint": "/v2/assemblyCharts/getAll",
         "params": {"dateFrom": "2026-07-01", "dateTo": "2026-10-15", "includeDeletedProducts": "true", "includePreparedCharts": "true"}},
        {"name": "incoming-invoices", "method": "GET", "endpoint": "/documents/export/incomingInvoice",
         "params": {"from": "2026-07-01", "to": "2026-10-14"}},
        {"name": "writeoffs", "method": "GET", "endpoint": "/v2/documents/writeoff",
         "params": {"dateFrom": "2026-07-01", "dateTo": "2026-10-13", "status": "PROCESSED"}},
    ]
    for label, frm, to in MONTHS:
        p.append({"name": f"sales-{label}", "method": "POST", "endpoint": "/v2/reports/olap",
                  "body": sales_body(frm, to, SKU_ROWS, SKU_AGGS)})
        p.append({"name": f"control-{label}", "method": "GET", "endpoint": f"/v2/reports/olap/byPresetId/{REVENUE_PRESET_ID}",
                  "params": {"summary": "false", "dateFrom": frm, "dateTo": to}})
    # Сентябрь: нагрузка по часу ОТКРЫТИЯ заказа (HourOpen), ось — час приёма, не час готовки.
    p.append({"name": "hour-sku-2026-09", "method": "POST", "endpoint": "/v2/reports/olap",
              "body": sales_body("2026-09-01", "2026-10-01", ["HourOpen", "DishId", "DishName", "SoldWithDish.Id"],
                                 ["DishAmountInt", "DishDiscountSumInt"])})
    p.append({"name": "hour-orders-day-2026-09", "method": "POST", "endpoint": "/v2/reports/olap",
              "body": sales_body("2026-09-01", "2026-10-01", ["OpenDate.Typed", "HourOpen"],
                                 ["UniqOrderId.OrdersCount", "UniqOrderId", "DishDiscountSumInt"])})
    p.append({"name": "hour-orders-cookingplace-day-2026-09", "method": "POST", "endpoint": "/v2/reports/olap",
              "body": sales_body("2026-09-01", "2026-10-01", ["OpenDate.Typed", "HourOpen", "CookingPlace"],
                                 ["UniqOrderId.OrdersCount", "DishAmountInt", "DishDiscountSumInt"])})
    return p


def plan_phase2() -> list[dict]:
    spec = json.loads(os.environ.get("KA_PHASE2_PLAN", "[]"))
    return spec


async def load_env() -> dict:
    from sqlalchemy import text
    from app.db.session import AsyncSessionLocal, engine
    from app.services.iiko_sync import _load_source_credential_env
    info = {}
    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        await session.execute(text("SET LOCAL statement_timeout='30000ms'"))
        info["preset_token_before"] = bool(os.environ.get("IIKO_SERVER_TOKEN", "").strip())
        await _load_source_credential_env(session)
        await session.rollback()
    await engine.dispose()
    info["preset_token_after_env"] = bool(os.environ.get("IIKO_SERVER_TOKEN", "").strip())
    return info


def main() -> None:
    info = asyncio.run(load_env())
    from app.services.iiko_sync import _load_export_employees_module, _request_iiko_with_incomplete_read_retry
    ee = _load_export_employees_module()
    ee.load_local_env()
    client = ee.IikoClient()
    preset_token = bool(client.token)
    obtained: list[str] = []
    orig_refresh = client.refresh_token

    def tracking_refresh() -> None:
        orig_refresh()
        obtained.append(client.token)

    client.refresh_token = tracking_refresh  # type: ignore[assignment]
    plan = plan_phase1() if PHASE == "1" else plan_phase2()
    log = {"phase": PHASE, "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
           "preset_token_used": preset_token, **info, "requests": []}
    try:
        for req in plan:
            rec = {"name": req["name"], "endpoint": req["endpoint"], "method": req["method"],
                   "params": req.get("params"), "body": req.get("body")}
            try:
                if req["method"] == "GET":
                    status, data = _request_iiko_with_incomplete_read_retry(client, req["endpoint"], params=req.get("params") or {})
                else:
                    status, data = _request_iiko_with_incomplete_read_retry(client, req["endpoint"], method="POST",
                                                                           params=req.get("params") or {}, json_body=req["body"])
                raw = data if isinstance(data, bytes) else str(data).encode()
                ext = "xml" if raw.lstrip()[:1] == b"<" else "json"
                (OUT / f"{req['name']}.{ext}").write_bytes(raw)
                rec.update(status=status, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(), file=f"{req['name']}.{ext}")
            except Exception as exc:  # noqa: BLE001 — фиксируем как missing, не ноль
                msg = str(getattr(exc, "body", b"") or exc)[:300]
                rec.update(status="error", error=type(exc).__name__, message=msg)
            rec["retrieved_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            log["requests"].append(rec)
            (OUT / f"{req['name']}.source.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2))
    finally:
        logouts = []
        for tok in obtained:
            try:
                client.token = tok
                st, _ = client.request("/logout", params={}, retry_auth=False)
                logouts.append(st)
            except Exception as exc:  # noqa: BLE001
                logouts.append(type(exc).__name__)
        log["tokens_obtained"] = len(obtained)
        log["logout_statuses"] = logouts
        log["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        (OUT / f"run-phase{PHASE}.log.json").write_text(json.dumps(log, ensure_ascii=False, indent=2))
        print(json.dumps({k: v for k, v in log.items() if k != "requests"}, ensure_ascii=False))
        for r in log["requests"]:
            print(r["name"], r.get("status"), r.get("bytes"), r.get("error", ""), (r.get("message") or "")[:150])


if __name__ == "__main__":
    main()
