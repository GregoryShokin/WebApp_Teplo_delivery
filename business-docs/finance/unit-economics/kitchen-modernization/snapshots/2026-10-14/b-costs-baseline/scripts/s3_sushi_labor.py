"""S3. Труд суши-цеха и дневной процентный фонд по finalized-ведомостям (read-only, без имён).

Запуск:
  ssh teplo-prod 'docker exec -i -w /app/apps/api -e PYTHONPATH=/app/apps/api teplo-api python -' \
      < s3_sushi_labor.py > ../data/s3_sushi_labor.json

Источник: payroll_line.components.days (только run.status='finalized'), календарная нарезка
по дате дня — как pnl/sources/payroll.py::build_payroll_month. Явки: attendance_entry
finalized-периодов. Сотрудники обезличены: выводятся только агрегаты по дням/категориям.
"""

import asyncio
import json
import sys
import uuid
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.db.session import AsyncSessionLocal, engine

MSK = ZoneInfo("Europe/Moscow")
MONTHS = [(date(2026, 8, 1), date(2026, 8, 31)), (date(2026, 9, 1), date(2026, 9, 30)),
          (date(2026, 10, 1), date(2026, 10, 12))]


def D(x):
    try:
        return Decimal(str(x or 0))
    except Exception:
        return Decimal("0")


def enc(o):
    if isinstance(o, Decimal):
        return str(o.quantize(Decimal("0.01")))
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, uuid.UUID):
        return str(o)
    raise TypeError(type(o))


async def main():
    out = {"read_started_utc": datetime.utcnow().isoformat(), "months": {}}
    async with AsyncSessionLocal() as s:
        await s.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        await s.execute(text("SET LOCAL statement_timeout='60000ms'"))
        out["attendance_role_station"] = [dict(r._mapping) for r in await s.execute(text(
            """select a.role, a.station, count(*) n from attendance_entry a
               where a.work_date between '2026-08-01' and '2026-10-12' group by 1,2 order by 3 desc"""))]
        for ms, me in MONTHS:
            res = await s.execute(text(
                """select l.employee_id, l.role as line_role, l.components
                   from payroll_line l join payroll_run r on r.id = l.run_id
                   join payroll_period p on p.id = r.period_id
                   where r.status = 'finalized' and p.start_date <= :me and p.end_date >= :ms
                     and p.period_type = 'week'"""), {"ms": ms, "me": me})
            per_day = defaultdict(lambda: {
                "sushi_people": set(), "sushi_groups": 0, "sushi_minutes": 0,
                "base_shift": Decimal(0), "seniority": Decimal(0), "weekday": Decimal(0),
                "percent": Decimal(0), "fund": Decimal(0), "vacation": Decimal(0),
                "sushi_coeff": Decimal(0),
                "all_percent": Decimal(0), "revenue": None, "rate": None, "pool": None,
                "total_coeff": None, "cat_minutes": defaultdict(int),
                "all_people": set(), "all_minutes": 0, "kitchen_people": set()})
            cat_stats = defaultdict(lambda: {"groups": 0, "minutes": 0, "base_shift": Decimal(0),
                                             "seniority": Decimal(0), "weekday": Decimal(0),
                                             "fund": Decimal(0), "percent": Decimal(0)})
            fund_rates = defaultdict(int)
            senior_groups = 0
            people_month = set()
            sushi_emp_days = set()
            role_totals = defaultdict(lambda: defaultdict(Decimal))
            for emp, line_role, comp in res.all():
                days = (comp or {}).get("days") or []
                for day in days:
                    if not isinstance(day, dict) or not day.get("date"):
                        continue
                    wd = date.fromisoformat(str(day["date"])[:10])
                    if wd < ms or wd > me:
                        continue
                    role = str(day.get("role") or line_role)
                    rt = role_totals[role]
                    for k in ("base_pay", "base_pay_shift", "seniority_allowance_pay",
                              "weekday_premium", "percent_pay", "fund_accrual", "vacation_pay"):
                        rt[k] += D(day.get(k))
                    rt["minutes"] += D(day.get("minutes"))
                    pd = per_day[wd]
                    pd["all_percent"] += D(day.get("percent_pay"))
                    if day.get("kind") == "shift" and D(day.get("minutes")) > 0:
                        pd["all_people"].add(emp)
                        pd["all_minutes"] += int(D(day.get("minutes")))
                        if role in ("sushi", "pizza", "shawarma", "prep"):
                            pd["kitchen_people"].add(emp)
                    if day.get("daily_revenue") is not None and pd["revenue"] is None:
                        pd["revenue"] = D(day.get("daily_revenue"))
                        pd["rate"] = day.get("revenue_rate")
                        pd["pool"] = D(day.get("daily_percent_pool"))
                        pd["total_coeff"] = day.get("daily_total_coeff")
                    if role != "sushi":
                        continue
                    people_month.add(emp)
                    pd["vacation"] += D(day.get("vacation_pay"))
                    if day.get("kind") != "shift":
                        continue
                    m = int(D(day.get("minutes")))
                    cat = str(day.get("category"))
                    pd["sushi_people"].add(emp)
                    sushi_emp_days.add((emp, wd))
                    pd["sushi_groups"] += 1
                    pd["sushi_minutes"] += m
                    pd["base_shift"] += D(day.get("base_pay_shift"))
                    pd["seniority"] += D(day.get("seniority_allowance_pay"))
                    pd["weekday"] += D(day.get("weekday_premium"))
                    pd["percent"] += D(day.get("percent_pay"))
                    pd["fund"] += D(day.get("fund_accrual"))
                    pd["sushi_coeff"] += D(day.get("adjusted_coeff"))
                    pd["cat_minutes"][cat] += m
                    cs = cat_stats[cat]
                    cs["groups"] += 1
                    cs["minutes"] += m
                    cs["base_shift"] += D(day.get("base_pay_shift"))
                    cs["seniority"] += D(day.get("seniority_allowance_pay"))
                    cs["weekday"] += D(day.get("weekday_premium"))
                    cs["fund"] += D(day.get("fund_accrual"))
                    cs["percent"] += D(day.get("percent_pay"))
                    fund_rates[str(day.get("fund_rate_percent"))] += 1
                    if D(day.get("seniority_allowance_pay")) > 0:
                        senior_groups += 1
            # Явки суши: пиковая одновременность (интервалы finalized-периодов)
            att = await s.execute(text(
                """select a.employee_id, a.work_date, a.started_at, a.ended_at, a.is_open
                   from attendance_entry a join payroll_period p on p.id = a.period_id
                   where p.status = 'finalized' and a.work_date between :ms and :me"""),
                {"ms": ms, "me": me})
            # role/station у явок пусты (NULL) — цех берём из подневной роли finalized-ведомости
            # того же сотрудника и даты.
            intervals = defaultdict(list)
            open_n = 0
            for emp, wd, st, en, is_open in att.all():
                if (emp, wd) not in sushi_emp_days:
                    continue
                if en is None:
                    open_n += 1
                    continue
                intervals[wd].append((emp, st.astimezone(MSK), en.astimezone(MSK)))
            slots = [time(h, mm) for h in range(9, 24) for mm in (0, 30)]
            conc = {}
            for wd, ivs in sorted(intervals.items()):
                row = {}
                for sl in slots:
                    t = datetime.combine(wd, sl, MSK)
                    row[sl.strftime("%H:%M")] = len({e for e, a, b in ivs if a <= t < b})
                conc[wd.isoformat()] = row
            days_out = []
            for wd in sorted(per_day):
                pd = per_day[wd]
                c = conc.get(wd.isoformat(), {})
                peak = [c.get(k) for k in ("16:00", "16:30", "17:00", "17:30", "18:00", "18:30")
                        if c.get(k) is not None]
                days_out.append({
                    "date": wd.isoformat(), "dow": wd.isoweekday(),
                    "daily_revenue_payroll": pd["revenue"], "tier_rate": pd["rate"],
                    "daily_pool": pd["pool"], "percent_paid_all_roles": pd["all_percent"],
                    "total_coeff": pd["total_coeff"],
                    "sushi_people": len(pd["sushi_people"]), "sushi_groups": pd["sushi_groups"],
                    "sushi_hours": round(pd["sushi_minutes"] / 60, 2),
                    "sushi_cat_hours": {k: round(v / 60, 2) for k, v in pd["cat_minutes"].items()},
                    "sushi_base_shift": pd["base_shift"], "sushi_seniority": pd["seniority"],
                    "sushi_weekday": pd["weekday"], "sushi_percent": pd["percent"],
                    "sushi_fund": pd["fund"], "sushi_vacation": pd["vacation"],
                    "sushi_coeff_weight": pd["sushi_coeff"],
                    "kitchen_people": len(pd["kitchen_people"]),
                    "all_production_people": len(pd["all_people"]),
                    "sushi_concurrency_16_19_max": max(peak) if peak else None,
                    "sushi_concurrency_16_19_min": min(peak) if peak else None,
                    "sushi_concurrency_by_slot": c,
                })
            adj = await s.execute(text(
                """select role, type, count(*) n, sum(amount) s from payroll_adjustment
                   where work_date between :ms and :me group by 1,2 order by 1,2"""),
                {"ms": ms, "me": me})
            out["months"][ms.isoformat()] = {
                "period": [ms.isoformat(), me.isoformat()],
                "sushi_distinct_people": len(people_month),
                "role_totals": {r: dict(v) for r, v in role_totals.items()},
                "sushi_by_category": {k: dict(v) for k, v in cat_stats.items()},
                "sushi_fund_rate_groups": dict(fund_rates),
                "sushi_groups_with_seniority": senior_groups,
                "attendance_open_skipped": open_n,
                "adjustments_by_role": [dict(r._mapping) for r in adj],
                "days": days_out,
            }
        await s.rollback()
    await engine.dispose()
    out["read_finished_utc"] = datetime.utcnow().isoformat()
    json.dump(out, sys.stdout, default=enc, ensure_ascii=False)


asyncio.run(main())
