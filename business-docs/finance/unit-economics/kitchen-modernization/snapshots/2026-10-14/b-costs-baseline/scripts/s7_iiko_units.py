"""S7. iikoServer: штуки блюд и заказы по местам приготовления (Aug/Sep), одна сессия + logout.

Фильтры — как в каноническом пресете «Отчет о выручки по направлениям» (NOT_DELETED x2),
дата — OpenDate.Typed [start; end) (поле SessionID.OperDay POST-движок не принимает; суммы
месяца по OpenDate совпали с пресетом 14.10.2026 до копейки).
Запуск: ssh teplo-prod 'docker exec -i -w /app/apps/api -e PYTHONPATH=/app/apps/api teplo-api python -' < s7_iiko_units.py
"""
import asyncio, json, sys
from app.db.session import AsyncSessionLocal, engine
from app.services.iiko_sync import _load_export_employees_module, _load_source_credential_env, _logout_iiko_client

PERIODS = [("2026-08", "2026-08-01", "2026-09-01"), ("2026-09", "2026-09-01", "2026-10-01")]


async def main():
    out = {}
    async with AsyncSessionLocal() as s:
        await _load_source_credential_env(s)
        await s.rollback()
    await engine.dispose()
    mod = _load_export_employees_module(); mod.load_local_env()
    client = mod.IikoClient()
    try:
        for name, start, end in PERIODS:
            body = {"reportType": "SALES", "buildSummary": "false",
                    "groupByRowFields": ["CookingPlace"], "groupByColFields": [],
                    "aggregateFields": ["DishAmountInt", "UniqOrderId", "DishSumInt",
                                        "DishDiscountSumInt", "ProductCostBase.ProductCost"],
                    "filters": {"DeletedWithWriteoff": {"filterType": "IncludeValues", "values": ["NOT_DELETED"]},
                                "OrderDeleted": {"filterType": "IncludeValues", "values": ["NOT_DELETED"]},
                                "OpenDate.Typed": {"filterType": "DateRange", "periodType": "CUSTOM",
                                                   "from": start, "to": end, "includeLow": True, "includeHigh": False}}}
            try:
                st, data = client.request("/v2/reports/olap", method="POST", json_body=body)
                out[name] = json.loads(data)
            except Exception as exc:
                out[name] = {"_error": str(exc)[:300]}
    finally:
        _logout_iiko_client(client)
        out["logout_done"] = True
    json.dump(out, sys.stdout, ensure_ascii=False, default=str)

asyncio.run(main())
