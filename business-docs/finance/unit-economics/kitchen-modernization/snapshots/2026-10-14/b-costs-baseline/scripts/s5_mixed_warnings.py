"""S5. Разбор предупреждений ОПиУ Aug/Sep: касса рядом с признанием, непривязанное признание,
неразнесённые проводки (read-only).

Запуск:
  ssh teplo-prod 'docker exec -i -w /app/apps/api -e PYTHONPATH=/app/apps/api teplo-api python -' \
      < s5_mixed_warnings.py > <private>/s5_raw.json
Назначения платежей могут содержать ФИО — сырой вывод хранится в приватной папке.
"""

import asyncio
import json
import sys
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import text

from app.db.session import AsyncSessionLocal, engine
from app.services.pnl.sources import cashflow, recognition

MONTHS = [(date(2026, 8, 1), date(2026, 8, 31)), (date(2026, 9, 1), date(2026, 9, 30))]
LINES = ["automation", "shop_maintenance", "utilities_chernikova", "staff_meals", "equipment_repair",
         "acquiring", "staff_expenses", "rko"]


def enc(o):
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, uuid.UUID):
        return str(o)
    raise TypeError(type(o))


async def main():
    out = {"read_started_utc": datetime.utcnow().isoformat()}
    async with AsyncSessionLocal() as s:
        await s.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        await s.execute(text("SET LOCAL statement_timeout='60000ms'"))
        names = {str(r[0]): r[1] for r in await s.execute(text("select id, name from counterparty"))}
        for ms, me in MONTHS:
            key = ms.isoformat()
            out[key] = {"lines": {}}
            for code in LINES:
                details = await cashflow.explain_line(s, ms, me, code)
                out[key]["lines"][code] = [
                    {"date": d.operation_date, "dir": d.direction, "amount": d.amount,
                     "contribution": d.contribution, "verdict": d.verdict,
                     "counterparty": names.get(str(d.counterparty_id)) if d.counterparty_id else None,
                     "purpose": (d.payment_purpose or "")[:160], "expense_month": d.expense_month}
                    for d in details]
            layer = await recognition.build_recognition_layer(s, ms, me)
            out[key]["unattributed"] = [
                {"counterparty": names.get(str(d.counterparty_id)) if d.counterparty_id else None,
                 "amount": d.amount, "period": [d.service_period_start, d.service_period_end],
                 "origin": d.origin}
                for d in layer.details if d.article_id is None]
            res = await s.execute(text(
                """select t.operation_date, t.direction, t.amount, left(t.payment_purpose,160) p,
                          t.article_id is null as no_article, t.quality_status
                   from cashflow_transactions t
                   left join pnl_article_rule r on r.article_id = t.article_id and r.is_active
                   where t.operation_date between :ms and :me and t.quality_status <> 'excluded'
                     and t.transfer_group_id is null and (t.article_id is null)"""),
                {"ms": ms, "me": me})
            out[key]["no_article_cash"] = [dict(r._mapping) for r in res]
        await s.rollback()
    await engine.dispose()
    json.dump(out, sys.stdout, default=enc, ensure_ascii=False)


asyncio.run(main())
