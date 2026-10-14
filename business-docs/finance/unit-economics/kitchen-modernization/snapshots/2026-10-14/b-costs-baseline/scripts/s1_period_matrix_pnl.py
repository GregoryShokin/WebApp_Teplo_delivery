"""S1. Матрица периода и снимки ОПиУ (read-only).

Запуск (без записи в БД; транзакция READ ONLY, rollback):
  ssh teplo-prod 'docker exec -i -w /app/apps/api -e PYTHONPATH=/app/apps/api teplo-api python -' \
      < s1_period_matrix_pnl.py > ../data/s1_raw.json

Печатает JSON в stdout. Персональные данные не выводятся: только агрегаты и коды строк.
"""

import asyncio
import dataclasses
import enum
import json
import sys
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import text

from app.db.session import AsyncSessionLocal, engine
from app.services.pnl import projector
from app.services.pnl.sources import payroll as payroll_src

MONTHS = [date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1), date(2026, 10, 1)]
TODAY = date(2026, 10, 14)


def enc(o):
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, enum.Enum):
        return o.value
    if isinstance(o, uuid.UUID):
        return str(o)
    if isinstance(o, set):
        return sorted(str(x) for x in o)
    if dataclasses.is_dataclass(o):
        return {f.name: getattr(o, f.name) for f in dataclasses.fields(o)}
    raise TypeError(type(o))


async def rows(session, sql, **params):
    res = await session.execute(text(sql), params)
    return [dict(r._mapping) for r in res]


async def main():
    out = {"read_started_utc": datetime.utcnow().isoformat(), "today_param": TODAY.isoformat()}
    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        await session.execute(text("SET LOCAL statement_timeout='60000ms'"))
        out["db_now"] = (await rows(session, "select now() as now"))[0]["now"]
        out["alembic"] = await rows(session, "select version_num from alembic_version")
        out["period_close"] = await rows(
            session,
            "select period_month, created_at from accounting_period_close order by period_month",
        )
        out["iiko_facts"] = await rows(
            session,
            """select period_month, metric_code, direction, amount, previous_amount, rows_count,
                      synced_at
               from pnl_iiko_fact where period_month >= '2026-06-01' order by 1,2,3""",
        )
        out["partner_facts"] = await rows(
            session,
            """select f.period_month, f.partner_name, f.revenue_amount, f.commission_rate, f.commission_amount
               from pnl_partner_commission_fact f where f.period_month >= '2026-06-01'
               order by 1""",
        )
        out["payroll_periods"] = await rows(
            session,
            """select p.period_type, p.start_date, p.end_date, p.payroll_date, p.status as period_status,
                      r.status as run_status, r.is_imported_legacy,
                      (r.summary->>'kind') as run_kind, count(l.id) as lines
               from payroll_period p
               left join payroll_run r on r.period_id = p.id
               left join payroll_line l on l.run_id = r.id
               where p.end_date >= '2026-06-20' and p.start_date <= '2026-10-31'
               group by 1,2,3,4,5,6,7,8 order by p.start_date, p.period_type""",
        )
        out["tax_payroll_ledger"] = await rows(
            session,
            """select year, month, count(*) as n, sum(accrued) as accrued, sum(ndfl) as ndfl,
                      sum(contributions) as contributions, sum(injury) as injury,
                      count(*) filter (where contributions is null) as contrib_null
               from tax_payroll_ledger where year = 2026 group by 1,2 order by 1,2""",
        )
        out["depreciation"] = await rows(
            session,
            "select period_month, count(*) as n, sum(amount) as amount from depreciation_entry "
            "group by 1 order by 1",
        )
        out["inventory_audits"] = await rows(
            session,
            """select date_trunc('month', business_date)::date as month, status, count(*) as n
               from inventory_audit where business_date >= '2026-06-01' group by 1,2 order by 1,2""",
        )
        out["manual_entries"] = await rows(
            session,
            """select period_month, line_code, component, direction, sign, amount
               from pnl_manual_entry where period_month >= '2026-07-01' order by 1,2""",
        )
        # Отчёт ОПиУ
        out["pnl"] = {}
        out["payroll_month"] = {}
        for m in MONTHS:
            rep = await projector.build_report(session, m, today=TODAY)
            out["pnl"][m.isoformat()] = enc(rep)
            ms, me = projector.month_bounds(m)
            pm = await payroll_src.build_payroll_month(session, ms, me)
            agg = {k: Decimal("0") for k in (
                "base_pay", "percent_pay", "vacation_pay", "bonuses", "fund_accrual",
                "other_penalties", "audit_penalties")}
            for b in pm.breakdown_by_employee.values():
                for k in agg:
                    agg[k] += getattr(b, k)
            out["payroll_month"][m.isoformat()] = {
                "cook_pay": pm.cook_pay,
                "cashier_pay": pm.cashier_pay,
                "admin_pay": pm.admin_pay,
                "bonuses": pm.bonuses,
                "fund_accrual": pm.fund_accrual,
                "audit_penalties": pm.audit_penalties,
                "other_penalties": pm.other_penalties,
                "by_role": dict(pm.by_role),
                "breakdown_sum": agg,
                "employees_n": len(pm.breakdown_by_employee),
            }
        await session.rollback()
    await engine.dispose()
    out["read_finished_utc"] = datetime.utcnow().isoformat()
    json.dump(out, sys.stdout, default=enc, ensure_ascii=False)


asyncio.run(main())
