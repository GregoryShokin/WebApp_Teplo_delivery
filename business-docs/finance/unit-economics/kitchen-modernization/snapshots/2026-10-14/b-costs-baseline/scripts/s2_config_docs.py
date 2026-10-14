"""S2. Действующие ставки/настройки, документы признания, ОС, доставки (read-only).

Запуск:
  ssh teplo-prod 'docker exec -i -w /app/apps/api -e PYTHONPATH=/app/apps/api teplo-api python -' \
      < s2_config_docs.py > <private>/s2_raw.json
Вывод может содержать названия контрагентов-юрлиц и фрагменты OCR-текста коммунальных актов,
поэтому сырой JSON хранится в приватной папке; в репозиторий кладутся агрегаты.
"""

import asyncio
import json
import re
import sys
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import text

from app.db.session import AsyncSessionLocal, engine


def enc(o):
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, uuid.UUID):
        return str(o)
    raise TypeError(type(o))


async def rows(session, sql, **params):
    res = await session.execute(text(sql), params)
    return [dict(r._mapping) for r in res]


SETTING_KEYS = [
    "payroll.weekday_premium",
    "payroll.fund_rates_by_tenure",
    "payroll.category_rules",
    "payroll.ndfl",
    "vacation.daily_amount",
    "payroll.dishwasher_shift_rate",
    "payroll.okladnik_payout_modes_present",
    "fixed_assets.capitalization_threshold_rub",
    "fixed_assets.repair_modernization_threshold_ratio",
    "fixed_assets.capital_repair_floor_rub",
    "schedule.fot_warning_threshold_pct",
]

KW = re.compile(r"(кВт|квт|kwh|тариф|цена|потер|объ[её]м|количеств|электроэнерг)", re.I)


async def main():
    out = {"read_started_utc": datetime.utcnow().isoformat()}
    async with AsyncSessionLocal() as s:
        await s.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        await s.execute(text("SET LOCAL statement_timeout='60000ms'"))
        out["payroll_rate"] = await rows(
            s,
            """select position_group, category, station, rate_type, amount, is_active,
                      effective_from, effective_to, (employee_id is not null) as personal_override
               from payroll_rate order by position_group, category, effective_from""",
        )
        out["availability"] = await rows(
            s, "select position_group, category, is_enabled from payroll_role_category_availability "
               "order by 1,2")
        out["revenue_tier"] = await rows(
            s, "select min_revenue, max_revenue, rate_percent, effective_from, effective_to, created_at "
               "from revenue_tier order by effective_from, min_revenue")
        out["category_coefficient"] = await rows(
            s, "select category, coefficient, effective_from, effective_to from category_coefficient "
               "order by category, effective_from")
        out["seniority_premium"] = await rows(
            s, "select position, role, amount, percent_of_base, effective_from, effective_to "
               "from payroll_seniority_premium order by 1,2,5")
        out["settings"] = await rows(
            s, "select key, value, widget_options, updated_at from app_setting where key = any(:k)",
            k=SETTING_KEYS)
        out["settings_history"] = await rows(
            s,
            """select a.key, h.old_value, h.new_value, h.changed_at from app_setting_history h
               join app_setting a on a.id = h.setting_id where a.key = any(:k) order by h.changed_at""",
            k=SETTING_KEYS)
        # ОС
        out["asset_categories"] = await rows(
            s, """select c.name, c.useful_life_months, count(a.id) as n,
                         sum(a.initial_cost) as initial_cost,
                         count(*) filter (where a.status not in ('disposed','sold','not_working')) as n_active
                  from asset_category c left join fixed_asset a on a.category_id = c.id
                  group by 1,2 order by 1""")
        out["depr_by_category_2026_09"] = await rows(
            s, """select c.name, c.useful_life_months, count(*) as n, sum(d.amount) as amount
                  from depreciation_entry d join fixed_asset a on a.id = d.asset_id
                  left join asset_category c on c.id = a.category_id
                  where d.period_month = '2026-09-01' group by 1,2 order by 4 desc""")
        out["assets_commissioned_2026"] = await rows(
            s, """select date_trunc('month', commissioned_on)::date as m, count(*) n, sum(initial_cost) s
                  from fixed_asset where commissioned_on >= '2026-01-01' group by 1 order by 1""")
        # Статьи → строки ОПиУ
        out["article_rules"] = await rows(
            s, """select r.line_code, a.code as article_code, a.name as article_name, r.in_pnl, r.sign,
                         r.owner_stream, r.is_active
                  from pnl_article_rule r join dds_articles a on a.id = r.article_id
                  where r.is_active order by r.line_code""")
        # Признанные начисления выбранных строк Jul–Oct
        out["accruals"] = await rows(
            s, """select r.line_code, cp.name as counterparty, i.source, i.number, i.invoice_date,
                         e.service_period_start, e.service_period_end, e.recognition_month,
                         e.amount, e.status, i.line_items
                  from supplier_expense_accrual e
                  left join supplier_invoice i on i.id = e.invoice_id
                  left join counterparty cp on cp.id = e.counterparty_id
                  left join counterparty_payable_profile p on p.counterparty_id = e.counterparty_id
                  left join pnl_article_rule r on r.article_id = coalesce(e.article_id, p.default_dds_article_id)
                       and r.is_active
                  where coalesce(e.service_period_end, e.recognition_month) >= '2026-06-01'
                    and coalesce(e.service_period_start, e.recognition_month) <= '2026-10-31'
                    and r.line_code in ('utilities_chernikova','website_app','automation','telecom',
                        'context_ads','seo','rent_chernikova','warehouse_rent','equipment_repair',
                        'shop_maintenance','aggregator_commission','fd_nk_services','recruiting')
                  order by r.line_code, e.service_period_start""")
        # Коммуналка: потоки и документы по видам
        out["utility_accounts"] = await rows(
            s, """select u.kind, u.expected_day, u.started_on, u.ended_on, u.is_active,
                         a.code as article_code
                  from utility_account u left join dds_articles a on a.id = u.dds_article_id""")
        intakes = await rows(
            s, """select u.kind, x.status, x.created_at, i.service_period_start, i.service_period_end,
                         i.amount, i.payment_status, x.recognition
                  from email_invoice_intake x
                  join utility_account u on u.id = x.utility_account_id
                  left join supplier_invoice i on i.id = x.invoice_id
                  where x.created_at >= '2026-05-01' order by u.kind, i.service_period_start""")
        util = []
        for r in intakes:
            rec = r.pop("recognition") or {}
            raw_text = rec.get("raw_text") or ""
            util_info = rec.get("utility") or {}
            kw_lines = [ln.strip()[:160] for ln in raw_text.splitlines() if KW.search(ln)]
            r["kw_lines"] = kw_lines[:40]
            r["parsed"] = {k: v for k, v in (util_info.get("raw") or util_info).items()
                           if k in ("kind", "electricity_period_amount", "act_kind", "period_month",
                                    "amount", "losses_amount", "energy_amount", "advance_amount")} \
                if isinstance(util_info, dict) else {}
            util.append(r)
        out["utility_intakes"] = util
        # Яндекс / агрегаторы: входящие банк-операции по месяцам
        out["bank_in_by_counterparty"] = await rows(
            s, """select date_trunc('month', operation_date)::date m, counterparty_name_raw,
                         count(*) n, sum(amount) s
                  from bank_operations
                  where operation_date >= '2026-07-01' and operation_date < '2026-10-14'
                    and direction = 'in'
                  group by 1,2 having sum(amount) > 20000 order by 1, 4 desc""")
        out["bank_yandex_purposes"] = await rows(
            s, """select operation_date, direction, amount, left(payment_purpose, 220) as purpose
                  from bank_operations
                  where operation_date >= '2026-08-01' and operation_date < '2026-10-14'
                    and (counterparty_name_raw ilike '%яндекс%' or counterparty_name_raw ilike '%yandex%'
                         or payment_purpose ilike '%яндекс%еда%')
                  order by operation_date limit 60""")
        # Кредиты/займы: исходящие движения по статьям, похожим на кредит
        out["loan_like_cash"] = await rows(
            s, """select date_trunc('month', t.operation_date)::date m, a.code, a.name, t.direction,
                         count(*) n, sum(t.amount) s
                  from cashflow_transactions t join dds_articles a on a.id = t.article_id
                  where t.operation_date >= '2026-07-01' and t.operation_date < '2026-10-01'
                    and t.quality_status <> 'excluded'
                    and (a.name ilike '%кредит%' or a.name ilike '%займ%' or a.name ilike '%процент%'
                         or a.name ilike '%лизинг%' or a.name ilike '%овердрафт%')
                  group by 1,2,3,4 order by 1,2""")
        # Эквайринг: денежный компонент по назначению (сентябрь, август)
        out["acquiring_cash"] = await rows(
            s, """select date_trunc('month', t.operation_date)::date m, t.direction, t.amount,
                         left(t.payment_purpose, 200) purpose
                  from cashflow_transactions t join dds_articles a on a.id = t.article_id
                  join pnl_article_rule r on r.article_id = a.id and r.is_active
                  where r.line_code = 'acquiring' and t.operation_date >= '2026-07-01'
                    and t.operation_date < '2026-10-14' and t.quality_status <> 'excluded'
                  order by t.operation_date""")
        # Доставки: операционная популяция
        out["delivery_orders"] = await rows(
            s, """select date_trunc('month', work_date)::date m, service_type, status, count(*) n,
                         sum(revenue) revenue
                  from delivery_order where work_date >= '2026-07-01' and work_date < '2026-10-14'
                  group by 1,2,3 order by 1,2,3""")
        # Закупки упаковки/расходников (товарные факты) для справки
        out["goods_facts"] = await rows(
            s, """select period_month, metric_code, line_code, source_kind, count(*) n, sum(amount) s
                  from pnl_iiko_goods_fact where period_month >= '2026-07-01'
                  group by 1,2,3,4 order by 1,2,3,4""")
        await s.rollback()
    await engine.dispose()
    out["read_finished_utc"] = datetime.utcnow().isoformat()
    json.dump(out, sys.stdout, default=enc, ensure_ascii=False)


asyncio.run(main())
