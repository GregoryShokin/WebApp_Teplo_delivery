"""Стоимость одной смены сушиста по действующим на 14.10.2026 ставкам (сценарий, не факт).

cost = R*m/720 + A*m/720 + D + floor((R*m/720 + A*m/720 + D) * f)
R — ставка Сушист×категория (payroll_rate, с 01.01.2026), A — надбавка Повар senior 500 /
deputy 300 (с 09.06.2026), D — 200 ₽ в пт/сб при >= 8 ч (payroll.weekday_premium),
f — фонд по стажу 0/5/10/15 % (payroll.fund_rates_by_tenure: 0/6/12/18 мес).
Процент с выручки НЕ входит: дневной фонд один, лишний человек только перераспределяет его.
Запуск: python3 shift_cost.py > ../data/sushi-shift-cost.json
"""
import json, math
from decimal import Decimal as D, ROUND_FLOOR

RATES = {"category_1": 2800, "category_2": 2400, "category_3": 2200, "intern": 2000}
out = []
for cat, r in RATES.items():
    for minutes in (720, 690):
        for weekend in (False, True):
            for allowance_name, a in (("нет", 0), ("зам 300", 300), ("старший 500", 500)):
                if cat == "intern" and a:
                    continue
                for f in (0, 5, 10, 15):
                    if cat == "intern" and f:
                        continue
                    base = D(r) * minutes / 720 + D(a) * minutes / 720
                    d = D(200) if (weekend and minutes >= 480) else D(0)
                    fund = ((base + d) * D(f) / 100).to_integral_value(rounding=ROUND_FLOOR)
                    out.append({"category": cat, "minutes": minutes, "fri_sat": weekend,
                                "allowance": allowance_name, "fund_rate_pct": f,
                                "base": str(base.quantize(D("0.01"))), "weekday": str(d),
                                "fund": str(fund),
                                "total_excl_percent": str((base + d + fund).quantize(D("0.01")))})
print(json.dumps(out, ensure_ascii=False, indent=1))
