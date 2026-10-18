"""Offline rebase on owner-selected app P&L. Does not mutate business systems."""
import calendar
import datetime as dt
import hashlib
import json
import math
import sys
from pathlib import Path

ROOT=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else Path(__file__).resolve().parent
V=Path('/Users/grigorii/.codex/visualizations/2026/10/16/01a14574-6918-7cf2-9b19-34d62fc90c38')
OUT=ROOT/'public';OUT.mkdir(exist_ok=True)
P=json.loads((ROOT/'closed-pnl-private.json').read_text())
LAB=json.loads((ROOT/'sushi-labor-private.json').read_text())
RATES=json.loads((ROOT/'current-rates-private.json').read_text())
ROLL=json.loads((ROOT/'roll-food-allocation.json').read_text())['periods']
DAILY=json.loads((ROOT/'daily-sales-safe-aggregates.json').read_text())
CHANNEL={x['period']:x for x in json.loads((ROOT/'iiko-channels-manifest.json').read_text())}
CTRL={x['period']:x for x in json.loads((ROOT/'iiko-controls-manifest.json').read_text())}
OLD=json.loads((V/'fc33-peak-2026-10-18/finance.json').read_text())
MAX_Q=8000
TIERS=sorted([(float(x['min_revenue']),float(x['rate_percent'])) for x in RATES['revenue_tiers']])
assert next(float(x['amount']) for x in RATES['rates'] if x['station']=='sushi' and x['category']=='category_2' and not x['personal_override'])==2400

def pool_day(revenue):
    return next((revenue*rate for minimum,rate in reversed(TIERS) if revenue>=minimum),0.)

def n(v):
    assert v is not None, 'Unknown amount must not be converted to zero'
    return float(v)

class Model:
    def __init__(self,period):
        self.period=period;self.report=P['pnl'][period+'-01']
        self.lines={x['code']:x for x in self.report['lines']}
        self.labor=LAB['months'][period+'-01'];self.days=self.labor['days']
        self.roll=ROLL[period];self.channels=CHANNEL[period]
        self.q0=sum(x['orders_accounted'] for x in DAILY[period]['days'])
        self.r0=self.roll['enterprise']['net'];self.food0=self.roll['enterprise']['food']
        self.check=self.r0/self.q0
        assert abs(self.r0-CTRL[period]['net'])<.01
        assert abs(self.food0-CTRL[period]['food'])<.01
        assert abs(self.r0-self.channels['net'])<.01
        self.food_correction=self.food0-self.a('food_cost_chernikova')
        self.yandex={'2026-07':8768.51,'2026-08':12381.21,'2026-09':14333.47}[period]
        self.fuel=2640.0 if period=='2026-08' else 0.0
        self.report_profit=self.a('net_profit')
        # Preserve authoritative reported profit, then disclose analytical overlays separately.
        self.base_profit=self.report_profit-self.food_correction-self.yandex-self.fuel
        self.report_costs_at_iiko_net=self.r0-self.report_profit
        self.overlay_costs=self.r0-self.base_profit
        year,month=map(int,period.split('-'));self.calendar_days=calendar.monthrange(year,month)[1]
        self.premium_days=sum(dt.date(year,month,d).weekday() in (4,5) for d in range(1,self.calendar_days+1))
        self.fixed_new=4*2400*self.calendar_days+500*self.calendar_days+4*200*self.premium_days
        self.fund_new=math.floor(2400*.05)*self.calendar_days+math.floor(200*.05)*self.premium_days
        sushi=self.labor['role_totals']['sushi']
        self.fixed_old=n(sushi['base_pay'])+n(sushi['fund_accrual'])
        self.staff_delta=self.fixed_new+self.fund_new-self.fixed_old
        self.vacation_retained=n(sushi['vacation_pay'])
        self.daily_revenue=[n(x['daily_revenue_payroll']) for x in self.days]
        assert abs(sum(self.daily_revenue)-self.r0)<.01
        self.weights=[r/self.r0 for r in self.daily_revenue]
        self.pool0=self.pool(self.r0)
        self.utilities_variable_base={'2026-07':112469+9654.25,'2026-08':117708+9429.75,'2026-09':94136+12348.49}[period]
        self.normalize=self.a('other_income')+self.a('unclaimed_deposits_writeoff')+n(P['payroll_month'][period+'-01']['other_penalties'])
        self.food33=.33*self.roll['rolls']['net_allocated']+self.roll['other']['food']
        self.net_fee_delta=.06+.03*self.channels['starter_net']/self.r0+.25*self.channels['yandex_net']/self.r0+self.a('acquiring')/self.r0+(self.a('partner_commission')+self.a('customer_refunds'))/self.r0

    def a(self,code):return n(self.lines[code]['amount'])
    def pool(self,revenue):return sum(pool_day(revenue*w) for w in self.weights)

    def estimate(self,q,food33=True,four=True,normalize=True,extra=0,details=False):
        k=q/self.q0;r=q*self.check
        food=(self.food33 if food33 else self.food0)*k
        food_delta=food-self.food0
        delta={
            'food':food_delta,
            'writeoffs_inventory':(self.a('writeoffs')+self.a('audit_results'))*(food/self.food0-1),
            'separate_packaging':(self.a('packaging_inventory')+self.a('pizza_box_inventory')+self.a('beverage_inventory'))*(k-1),
            'consumables_maintenance':.5*(self.a('aux_goods')+self.a('shop_maintenance'))*(k-1),
            'courier_budget':self.a('courier_service')*(k-1),
            'electricity_water':.5*self.utilities_variable_base*(k-1),
            'acquiring':self.a('acquiring')*(k-1),
            'starter_increment':.03*self.channels['starter_net']*(k-1),
            'yandex_increment':.25*self.channels['yandex_net']*(k-1),
            'partner_refunds':(self.a('partner_commission')+self.a('customer_refunds'))*(k-1),
            'business_tax_increment':.06*self.r0*(k-1),
            'single_company_percent_pool':self.pool(r)-self.pool0,
            'four_sushi_fixed_fund_replacement':self.staff_delta if four else 0.,
            'remove_historical_favorable_penalties_and_income':self.normalize if normalize else 0.,
            'additional_future_costs':extra,
        }
        profit=self.base_profit+r-self.r0-sum(delta.values())
        if not details:return profit
        return {'orders':q,'orders_per_day':q/self.calendar_days,'revenue_rub':r,'net_check_rub':self.check,'roll_fc':.33 if food33 else self.roll['rolls']['food_cost_ratio'],'food_rub':food,'enterprise_fc':food/r,'profit_rub':profit,'costs_rub':r-profit,'delta_costs_vs_analytical_base':delta,'sushi_payroll':self.payroll(q)}

    def payroll(self,q):
        percent=0.
        for d,w in zip(self.days,self.weights):
            other=max(0,n(d['total_coeff'])-n(d['sushi_coeff_weight']))
            percent+=4*math.floor(pool_day(q*self.check*w)*7.5/(other+30))
        return {'salary_fixed':self.fixed_new,'fund':self.fund_new,'percent_sushi_all4':percent,'salary_plus_fund':self.fixed_new+self.fund_new+percent,'company_percent_pool':self.pool(q*self.check),'historical_sushi_vacation_retained_elsewhere':self.vacation_retained}

def thresholds(profits):
    suffix={};current=math.inf
    for q in reversed(range(1,MAX_Q+1)):
        current=min(current,profits[q]);suffix[q]=current
    result={}
    for target in [0,500000]:
        first=next(q for q in profits if profits[q]>=target)
        safe=next(q for q in profits if suffix[q]>=target)
        rounded=math.ceil(safe/50)*50
        result[str(target)]={'first_crossing_orders':first,'safe_orders_through8000':safe,'planning_orders_rounded50':rounded,'profit_at_safe':profits[safe],'profit_at_previous_order':profits[safe-1] if safe>1 else None,'profit_at_planning':profits[rounded]}
    return result

models={p:Model(p) for p in ['2026-07','2026-08','2026-09']}
monthly={};curves={}
for period,m in models.items():
    assert abs(m.estimate(m.q0,food33=False,four=False,normalize=False)-m.base_profit)<.01
    profits={q:m.estimate(q) for q in range(1,MAX_Q+1)}
    tt=thresholds(profits);curves[period]=profits
    monthly[period]={
        'calendar_days':m.calendar_days,'actual_orders':m.q0,'actual_net_check':m.check,
        'reported_net_profit':m.report_profit,'reported_pnl_revenue_after_partner_refunds':m.a('revenue'),
        'iiko_net_before_partner_refunds':m.r0,
        'analytic_bridge':{'food_cost_correction':-m.food_correction,'yandex_outside_report':-m.yandex,'fuel_net_reclassification':-m.fuel},
        'analytical_profit_before_scenario':m.base_profit,
        'report_component_statuses_preserved':{k:{'amount':m.lines[k]['amount'],'status':m.lines[k]['status']} for k in ['payroll_taxes','loan_interest']},
        'baseline_revenue_minus_costs_reconciles':True,'actual_roll_fc':m.roll['rolls']['food_cost_ratio'],
        'food33_increase_at_observed_volume':m.food33-m.food0,'nonroll_recipe_food_unchanged':True,
        'staff_fixed_fund_delta':m.staff_delta,'new_fixed_sushi_salary_fund':m.fixed_new+m.fund_new,
        'old_fixed_sushi_salary_fund':m.fixed_old,'normalization_removed_gains':m.normalize,
        'thresholds_current_actual_offers':tt,
        'at3000':m.estimate(3000,details=True),
        'at_target_planning':m.estimate(tt['500000']['planning_orders_rounded50'],details=True),
        'additional_future_cost_sensitivity':{str(extra):thresholds({q:p-extra for q,p in profits.items()}) for extra in [20000,50000,100000]},
    }

# Average month, preserving actual quarter mix/relative load and its 92-day calendar.
mean_orders=sum(m.q0 for m in models.values())/3
quarter_profits={q:sum(m.estimate(q*m.q0/mean_orders) for m in models.values())/3 for q in range(1,MAX_Q+1)}
quarter_thresholds=thresholds(quarter_profits)
quarter={'calendar_days':sum(m.calendar_days for m in models.values())/3,'observed_orders_per_mean_month':mean_orders,'weighted_net_check':sum(m.r0 for m in models.values())/sum(m.q0 for m in models.values()),'thresholds':quarter_thresholds,'profit_at3000':quarter_profits[3000],'at_target':{},'additional_future_cost_sensitivity':{str(x):thresholds({q:p-x for q,p in quarter_profits.items()}) for x in [20000,50000,100000]}}
for target,t in quarter_thresholds.items():
    q=t['planning_orders_rounded50'];scenario=[m.estimate(q*m.q0/mean_orders,details=True) for m in models.values()]
    quarter['at_target'][target]={'mean_orders':q,'mean_revenue':sum(x['revenue_rub'] for x in scenario)/3,'mean_profit':sum(x['profit_rub'] for x in scenario)/3,'mean_sushi_payroll':sum(x['sushi_payroll']['salary_plus_fund'] for x in scenario)/3,'modeled_calendar_cases':{p:{'orders':x['orders'],'profit':x['profit_rub']} for p,x in zip(models,scenario)}}

# Old selected August policy remains a separate like-for-like comparator.
old_curve={r['q']:r['profit_before_unknown_equipment_and_other_deltas_rub'] for r in OLD['curves']}
aug=models['2026-08']
old_aug_roll=OLD['baseline']['roll_revenue'];new_aug_roll=aug.roll['rolls']['net_allocated']
delta_roll_food=.33*(new_aug_roll-old_aug_roll)
old_roll_share=OLD['target_food']['allocation_share']
new_roll_share=new_aug_roll/aug.r0
delta_roll_food+=.33*OLD['target_food']['revenue_recovery']*(new_roll_share-old_roll_share)
old_report_aug=json.loads(Path('/Users/grigorii/Developer/Projects/Teplo-agent-kitchenecon/business-docs/finance/unit-economics/kitchen-modernization/snapshots/2026-10-15/baseline-reconciliation.json').read_text())
base_profit_delta=aug.base_profit-float(old_report_aug['known_profit_rub'])
correction_factor=1+(aug.a('writeoffs')+aug.a('audit_results'))/aug.food0
new_selected={q:p+base_profit_delta-delta_roll_food*(q/aug.q0)*correction_factor for q,p in old_curve.items()}
# Comparator source starts at actual August volume, which is below both target crossings.
def partial_thresholds(curve):
    out={};suffix={};running=math.inf
    for q in sorted(curve,reverse=True):running=min(running,curve[q]);suffix[q]=running
    for target in [0,500000]:
        first=next(q for q in sorted(curve) if curve[q]>=target)
        safe=next(q for q in sorted(curve) if suffix[q]>=target)
        out[str(target)]={'first_crossing_orders':first,'safe_orders_through5000':safe,'planning_orders_rounded50':math.ceil(safe/50)*50,'profit_at_safe':curve[safe]}
    return out

report={
    'as_of_local':'2026-10-19','business_timezone':'Europe/Moscow',
    'source_authority':'Owner explicitly selected application P&L as the final July–September report source; closed status accepted. No sync, close, recognize or financial mutations performed.',
    'source_read_utc':P['read_finished_utc'],
    'scope':'Chernikova Department.Code=2; four daily category2 sushi positions; rolls only FC33%; other recipes unchanged.',
    'offer_policy_for_three_month_model':'Each month actual observed offers retained. Simplified future policy replay is only available for August and is shown separately; not extrapolated as fact to July/Sep.',
    'monthly':monthly,'quarter_mean_current_offers':quarter,
    'like_for_like_august_selected_simplified_offers':{'old_exact_break_even':2156,'old_safe_target500k':2910,'old_planning_break_even':2200,'old_planning_target500k':2950,'new_thresholds':partial_thresholds(new_selected),'closed_report_amount_update_delta_profit':base_profit_delta,'daily_vs_month_roll_allocation_food_delta_at_base':delta_roll_food,'old_profit_at3000':old_curve[3000],'new_profit_at3000':new_selected[3000]},
    'assumptions':['Four 12h sushi positions every day; other roles fixed structure retained; this is not four people working without days off.','One senior500/day, one nonsenior5%fund and others0%, retained historical sushi vacation separately; owner selected categories but not this individual staffing mix.','Single total daily production percentage recomputed with current revenue tiers and observed daily revenue shape; employee allocation only for FOT display. Pool distribution rounding retains historical rounding residual.','Product/roll/channel mix and net check stay at each observed month; no demand lift assumed.','Couriers and separate packaging scale with ordercount; consumables/maintenance50% variable; electricity/water50% variable; fixed waste retained. These are old model budget assumptions, not measured marginal tariffs.','Writeoffs/inventory scale with food budget as before; negative oneoff inventory component not independently promoted into savings.','Acquiring uses observed effective rate; Starter marginal3%, Yandex25%, business tax marginal6% as in previous model/app. No legal tax regime recommendation is made.','Other fixed costs retained; favorable historical penalties/otherincome removed from future profit, as in old model.','Unknown new equipment, financing, training, repair/downtime, additional capacity and other future deltas remain excluded from main result and shown as explicit +20/50/100k sensitivity.','Approved report total is preserved even where a component has no_data/not_configured; no missing field was converted to a confirmed zero. September payroll tax is not separately displayed; additional cost sensitivity is not a claim of missing expense.','Unclassified cash flows July/Sep are not automatically treated as expense/income.'],
    'validation':{'iiko_control_read_fresh':True,'monthly_roll_allocation_matches_fresh_controls':True,'baseline_pnl_identity':True,'old_aug_like_for_like_bridge_verified':True,'all_months_closed_by_owner':True,'calculation_is_conditional_forecast_not_capacity_or_demand_proof':True},
}
(OUT/'recalculation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
(OUT/'curves.json').write_text(json.dumps({'current_offers':curves,'quarter_mean':quarter_profits,'august_simplified_like_for_like':new_selected},ensure_ascii=False)+'\n')
summary={'monthly':{p:{'P_app':r['reported_net_profit'],'P_analytic':r['analytical_profit_before_scenario'],'zero':r['thresholds_current_actual_offers']['0'],'target500k':r['thresholds_current_actual_offers']['500000'],'profit_at3000':r['at3000']['profit_rub'],'fot_at_target':r['at_target_planning']['sushi_payroll']['salary_plus_fund']} for p,r in monthly.items()},'mean':quarter,'likeforlike':report['like_for_like_august_selected_simplified_offers']}
print(json.dumps(summary,ensure_ascii=False,indent=2))
