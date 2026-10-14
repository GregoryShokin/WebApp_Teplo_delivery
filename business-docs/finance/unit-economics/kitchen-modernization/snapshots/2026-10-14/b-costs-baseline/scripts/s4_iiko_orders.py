"""S4. iikoServer: контроль месячной выручки, дневные заказы, каналы (read-only, одна сессия + logout).

Запуск:
  ssh teplo-prod 'docker exec -i -w /app/apps/api -e PYTHONPATH=/app/apps/api teplo-api python -' \
      < s4_iiko_orders.py > <private>/s4_raw.json
Только GET/POST чтения OLAP; сохранённые пресеты не меняются. Вход занимает лицензию —
в конце обязательный /logout. Ответ OLAP без персональных полей (группировки — даты/каналы).
"""

import asyncio
import copy
import json
import sys
from datetime import date, datetime

from app.db.session import AsyncSessionLocal, engine
from app.services.iiko_sync import (
    _load_export_employees_module,
    _load_source_credential_env,
    _logout_iiko_client,
)
from app.services.pnl.iiko_sync import REVENUE_PRESET_ID

DAILY_PRESET_ID = "8c13763a-35bf-9f27-017f-5468b1e7001b"
PERIODS = [("2026-08", "2026-08-01", "2026-09-01"), ("2026-09", "2026-09-01", "2026-10-01")]
WANT_FIELDS = ["OrderType", "Delivery.ServiceType", "OriginName", "PayTypes", "UniqOrderId",
               "OpenDate.Typed", "SessionID.OperDay", "Department", "CookingPlace",
               "DishSumInt", "DishDiscountSumInt", "ProductCostBase.ProductCost", "DishAmountInt"]


def j(data):
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8", "replace")
    try:
        return json.loads(data)
    except Exception:
        return {"_raw_prefix": str(data)[:300]}


async def main():
    out = {"read_started_utc": datetime.utcnow().isoformat(), "requests": []}
    async with AsyncSessionLocal() as s:
        await _load_source_credential_env(s)
        await s.rollback()
    await engine.dispose()
    mod = _load_export_employees_module()
    mod.load_local_env()
    client = mod.IikoClient()

    def call(path, **kw):
        try:
            status, data = client.request(path, **kw)
        except Exception as exc:  # фиксируем ошибку чтения, не прерывая сессию
            out["requests"].append({"path": path, "error": str(exc)[:300]})
            return {"_error": str(exc)[:300]}
        out["requests"].append({"path": path, "status": status, "params": {
            k: v for k, v in (kw.get("params") or {}).items() if k != "key"}})
        return j(data)

    try:
        presets = call("/v2/reports/olap/presets")
        plist = presets if isinstance(presets, list) else presets.get("data", [])
        out["presets"] = [p for p in plist if p.get("id") in (REVENUE_PRESET_ID, DAILY_PRESET_ID)]
        cols = call("/v2/reports/olap/columns", params={"reportType": "SALES"})
        out["columns_present"] = {f: (f in cols) for f in WANT_FIELDS} if isinstance(cols, dict) else {}
        out["columns_meta"] = {f: cols.get(f) for f in WANT_FIELDS if isinstance(cols, dict) and f in cols}
        rev = next((p for p in out["presets"] if p.get("id") == REVENUE_PRESET_ID), None)
        for name, start, end in PERIODS:
            out[f"revenue_preset_{name}"] = call(
                f"/v2/reports/olap/byPresetId/{REVENUE_PRESET_ID}",
                params={"summary": "false", "dateFrom": start, "dateTo": end})
            out[f"daily_preset_{name}"] = call(
                f"/v2/reports/olap/byPresetId/{DAILY_PRESET_ID}",
                params={"summary": "false", "dateFrom": start, "dateTo": end})
            if rev is None:
                continue
            # Каналы: фильтры канонического пресета без изменений, кроме периода; своя группировка.
            filters = copy.deepcopy(rev.get("filters") or {})
            # Поле даты пресета (SessionID.OperDay) POST-движок v2 не принимает: заменяем ключ на
            # OpenDate.Typed с тем же периодом [start; end). Остальные фильтры — как в пресете.
            date_keys = [k for k, f in filters.items()
                         if isinstance(f, dict) and f.get("filterType") == "DateRange"]
            for key in date_keys:
                filters.pop(key)
            out[f"replaced_date_filter_keys_{name}"] = date_keys
            filters["OpenDate.Typed"] = {"filterType": "DateRange", "periodType": "CUSTOM",
                                         "from": start, "to": end,
                                         "includeLow": True, "includeHigh": False}
            groups = [f for f in ("OrderType", "Delivery.ServiceType", "OriginName")
                      if out["columns_present"].get(f)]
            aggs = [f for f in ("UniqOrderId", "DishSumInt", "DishDiscountSumInt",
                                "ProductCostBase.ProductCost") if out["columns_present"].get(f)]
            body = {"reportType": "SALES", "buildSummary": "false",
                    "groupByRowFields": groups, "groupByColFields": [],
                    "aggregateFields": aggs, "filters": filters}
            out[f"channels_body_{name}"] = body
            out[f"channels_{name}"] = call("/v2/reports/olap", method="POST", json_body=body)
            body2 = dict(body, groupByRowFields=[f for f in ("PayTypes",)
                                                  if out["columns_present"].get(f)])
            out[f"paytypes_{name}"] = call("/v2/reports/olap", method="POST", json_body=body2)
    finally:
        _logout_iiko_client(client)
        out["logout_done"] = True
    out["read_finished_utc"] = datetime.utcnow().isoformat()
    json.dump(out, sys.stdout, ensure_ascii=False, default=str)


asyncio.run(main())
