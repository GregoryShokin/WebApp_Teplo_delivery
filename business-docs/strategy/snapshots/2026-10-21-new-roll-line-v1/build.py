import json, math, html, hashlib, argparse, re
from pathlib import Path

def roll(code, group, name, inner, top, kind='ordinary'):
    return {'id':code,'group':group,'name':name,'kind':kind,'inner':inner,'top':top}

ROLLS=[
roll('C01','Холодные','Лосось — крышка',{'cream_cheese':65,'cucumber':20,'tamago':20},{'salmon_plain':50},'lid'),
roll('C02','Холодные','Креветка — крышка',{'crab_mix':65,'cucumber':20,'tamago':20},{'shrimp':50,'teriyaki':5},'lid'),
roll('C03','Холодные','Угорь — крышка',{'cream_cheese':65,'cucumber':20,'tamago':20},{'eel_joint':50,'unagi':5},'lid'),
roll('C04','Холодные','Копчёный лосось и масаго',{'salmon_smoked':35,'cream_cheese':45,'cucumber':25},{'masago':15}),
roll('C05','Холодные','Тартар из лосося',{'cream_cheese':50,'tamago':20,'cucumber':20},{'tartar_plain':70,'teriyaki':5},'tartar'),
roll('C06','Холодные','Тартар из копчёного лосося',{'crab_mix':50,'tamago':20,'cucumber':20},{'tartar_smoked':70,'unagi':5},'tartar'),
roll('B01','Запечённые','Лосось и креветка',{'salmon_plain':35,'shrimp':35,'crab_mix':20,'cucumber':15},{'yaki':35,'teriyaki':5}),
roll('B02','Запечённые','Мидии под сырным соусом',{'mussels':35,'crab_mix':50,'cucumber':20},{'cheese_sauce':35,'unagi':5}),
roll('B03','Запечённые','Креветка яки',{'shrimp':35,'cream_cheese':50,'cucumber':20},{'yaki':35,'teriyaki':5}),
roll('B04','Запечённые','Лосось и мидии спайси',{'salmon_plain':35,'mussels':35,'cream_cheese':20,'cucumber':15},{'spice':30,'teriyaki':5}),
roll('B05','Запечённые','Угорь, моцарелла и «Чёрный угорь»',{'eel_joint':35,'cream_cheese':30,'mozzarella':25,'cucumber':15},{'black_eel':35,'unagi':5}),
roll('B06','Запечённые','Креветка и мидии под сырным соусом',{'shrimp':35,'mussels':35,'crab_mix':20,'cucumber':15},{'cheese_sauce':35,'teriyaki':5}),
roll('F01','Жареные','Лосось спайси',{'salmon_plain':35,'cream_cheese':50,'cucumber':20},{'spice':20,'unagi':5}),
roll('F02','Жареные','Креветка и крабовая смесь',{'shrimp':35,'crab_mix':50,'cucumber':20},{'spice':20,'teriyaki':5}),
roll('F03','Жареные','Копчёный лосось и моцарелла',{'salmon_smoked':35,'cream_cheese':30,'mozzarella':25,'cucumber':15},{'cheese_sauce':20,'teriyaki':5}),
roll('F04','Жареные','Угорь и креветка',{'eel_joint':35,'shrimp':35,'crab_mix':20,'cucumber':15},{'black_eel':30,'unagi':5}),
roll('F05','Жареные','Мидии и тамаго',{'mussels':35,'crab_mix':30,'tamago':25,'cucumber':15},{'yaki':20,'teriyaki':5}),
roll('F06','Жареные','Угорь и мидии',{'eel_joint':35,'mussels':35,'cream_cheese':20,'cucumber':15},{'black_eel':35,'unagi':5}),
]
COMMON={'rice':110,'nori':0.5}
FRY={'batter':25,'crumbs':15,'oil':5}
MAIN={'salmon_plain','salmon_smoked','shrimp','mussels','eel_joint'}

def ceil5(v):
    return math.ceil((v-1e-10)/5)*5

def calculate(components):
    rows=[]
    for spec in ROLLS:
        r=json.loads(json.dumps(spec)); r['base']=COMMON.copy();r['frying']=FRY.copy() if r['group']=='Жареные' else {}
        lines=[]
        for stage in ['base','inner','top','frying']:
            for k,q in r[stage].items():
                c=components[k]
                unitcost=c.get('cost_per_g') if k!='nori' else c['cost_per_sheet']
                assert unitcost is not None and unitcost>0,(k,c)
                conservativecost=components['eel_legacy']['cost_per_g'] if k=='eel_joint' else unitcost
                lines.append({'component':k,'stage':stage,'amount':q,'unit':'лист' if k=='nori' else 'г','cost_rub':q*unitcost,'price_reserve_cost_rub':q*conservativecost})
        r['lines']=lines;r['inner_total_g']=sum(r['inner'].values())
        r['joint_scenario_cost_rub']=sum(x['cost_rub'] for x in lines)
        r['price_reserve_cost_rub']=sum(x['price_reserve_cost_rub'] for x in lines)
        r['menu_price_rub']=ceil5(r['price_reserve_cost_rub']/0.35)
        r['minimum_price_joint_rounded5_rub']=ceil5(r['joint_scenario_cost_rub']/0.35)
        r['joint_fc_pct']=r['joint_scenario_cost_rub']/r['menu_price_rub']*100
        r['reserve_fc_pct']=r['price_reserve_cost_rub']/r['menu_price_rub']*100
        r['margin_to35_rub']=.35*r['menu_price_rub']-r['price_reserve_cost_rub']
        r['principal_shares_inner']={k:q/r['inner_total_g'] for k,q in r['inner'].items() if k in MAIN}
        r['eel_g']=sum(r[s].get('eel_joint',0) for s in ['inner','top'])
        r['black_eel_sauce_g']=r['top'].get('black_eel',0)
        rows.append(r)
    return rows

def validate(rows,components):
    checks=[]
    def check(label,ok,detail=None):
        checks.append({'check':label,'status':'PASS' if ok else 'FAIL','detail':detail})
        assert ok,(label,detail)
    check('18 proposed recipes; 6 in each group',len(rows)==18 and all(sum(r['group']==g for r in rows)==6 for g in ['Холодные','Запечённые','Жареные']))
    for r in rows:
        n=r['id'];i=r['inner'];t=r['top']
        check(n+': allowed palette',all(k in components for s in ['inner','top','base','frying'] for k in r[s]))
        check(n+': rice110/nori0.5',r['base']==COMMON)
        check(n+': cheese and crab mutually exclusive inside',not ('cream_cheese' in i and 'crab_mix' in i))
        check(n+': normal filling105 or tartar90',r['inner_total_g']==(90 if r['kind']=='tartar' else 105))
        check(n+': each inside principal >=30%',all(v>=0.3-1e-12 for v in r['principal_shares_inner'].values()))
        check(n+': material FC<=35% both scenarios',max(r['joint_fc_pct'],r['reserve_fc_pct'])<=35+1e-9)
        check(n+': price rounded UP to5',r['menu_price_rub']%5==0 and r['menu_price_rub']>=r['price_reserve_cost_rub']/.35-1e-9)
        check(n+': exact cost sum',abs(sum(x['cost_rub'] for x in r['lines'])-r['joint_scenario_cost_rub'])<1e-9)
        if r['kind']=='lid':
            selected=[k for k in t if k in MAIN]
            check(n+': exactlyone lid50',len(selected)==1 and selected[0]!='mussels' and t[selected[0]]==50)
        if r['kind']=='tartar':
            selected=[k for k in t if k.startswith('tartar_')]
            check(n+': tartar70 ready mixture, filler-only inner',len(selected)==1 and t[selected[0]]==70 and set(i)<={'cream_cheese','crab_mix','cucumber','tamago'})
        if r['group']=='Жареные':
            check(n+': priced frying allowance',r['frying']==FRY and all(x['cost_rub']>0 for x in r['lines'] if x['stage']=='frying'))
    return checks

LABELS={'cream_cheese':'сыр','crab_mix':'крабовая смесь','cucumber':'огурец','tamago':'тамаго','salmon_plain':'лосось','salmon_smoked':'лосось х/к','shrimp':'креветка','mussels':'мидии','eel_joint':'угорь','masago':'масаго','mozzarella':'моцарелла','spice':'Spice','yaki':'яки','cheese_sauce':'сырный соус','black_eel':'«Чёрный угорь»','tartar_plain':'тартар лосось','tartar_smoked':'тартар лосось х/к','unagi':'унаги','teriyaki':'терияки'}
def comptext(d):
    return ' + '.join(f'{LABELS[k]} {q:g}' for k,q in d.items())

def main():
    a=argparse.ArgumentParser();a.add_argument('--inputs',type=Path,required=True);a.add_argument('--out',type=Path,required=True);args=a.parse_args()
    inp=json.loads(args.inputs.read_text());components=inp['components'];rows=calculate(components);checks=validate(rows,components)
    raw=1000.;trim=470.;x=trim/9;fillet=530-x;eelpart=trim+x;sauce=eelpart*3.84
    non_eel_sauce_per_g=components['black_eel']['cost_per_g']-components['eel_joint']['cost_per_g']/3.84
    # Isolated production-batch stress: all coupled sauce is made now; only sauce served
    # on this eel roll is sold, every remaining gram is written off. Entire raw eel purchase
    # plus all non-eel sauce inputs are charged ONCE to its direct fillet, not added to
    # the allocated costs already in the roll. Joint/legacy/stress are alternative bases.
    all_outputs_cost_per_direct_g=(1551+sauce*non_eel_sauce_per_g)/fillet
    for r in rows:
        if r['eel_g']:
            r['surplus_sauce_stress_cost_rub']=r['joint_scenario_cost_rub']-r['eel_g']*components['eel_joint']['cost_per_g']-r['black_eel_sauce_g']*components['black_eel']['cost_per_g']+r['eel_g']*all_outputs_cost_per_direct_g
            r['surplus_sauce_stress_price_rub']=ceil5(r['surplus_sauce_stress_cost_rub']/.35)
            r['surplus_sauce_stress_fc_at_proposed_price_pct']=100*r['surplus_sauce_stress_cost_rub']/r['menu_price_rub']
            assert r['black_eel_sauce_g']<=r['eel_g']*sauce/fillet+1e-9
    demo_eel=sum(r['eel_g'] for r in rows);demo_sauce=sum(r['black_eel_sauce_g'] for r in rows);ratio=sauce/fillet
    required_sauce=demo_eel*ratio
    eel={'status':'owner full-use condition, agent proposed 90%trim/10%fillet within eel part; not measured production result','raw_eel_g':raw,'original_fillet_g':530.,'trim_g':trim,'fillet_diverted_to_sauce_g':x,'direct_fillet_g':fillet,'eel_component_of_sauce_g':eelpart,'ready_sauce_g':sauce,'sauce_per_direct_fillet':ratio,'allocation':'one purchased eel cost allocated by mass among edible eel outputs before non-eel sauce additions; 1551RUB/kg to direct fillet and eelpart; sauces non-eel component costs then added','purchase_eel_cost_rub':1551.,'allocated_direct_eel_cost_rub':fillet/1000*1551,'allocated_sauce_eel_cost_rub':eelpart/1000*1551,'illustration_one_each_18_not_sales_forecast':{'direct_fillet_used_g':demo_eel,'sauce_used_g':demo_sauce,'sauce_generated_for_direct_fillet_g':required_sauce,'sauce_left_without_additional_demand_g':required_sauce-demo_sauce,'fraction_sauce_consumed':demo_sauce/required_sauce,'extra_sauce_only_portions_at30g':math.ceil((required_sauce-demo_sauce)/30),'direct_eel_only_50g_generates_sauce_g':50*ratio},'pricing_guard':'Menuprices for direct eel use legacyfillet cost1551/.53 so fullutilization saving not required for price FC<=35; purchase assumption separate, not a second charge in joint-cost scenario'}
    assert abs(fillet+eelpart-raw)<1e-8
    assert abs(eel['allocated_direct_eel_cost_rub']+eel['allocated_sauce_eel_cost_rub']-1551)<1e-8
    eel['pricing_guard']='Proposed prices cover joint allocated COGS and alternative legacy fillet COGS; NOT a guarantee against manufacturing and discarding all excess sauce. Separate stress shown.'
    eel['non_eel_sauce_cost_per_g']=non_eel_sauce_per_g
    eel['stress_alloutputs_cost_per_direct_fillet_g']=all_outputs_cost_per_direct_g
    eel['illustration_one_each_18_not_sales_forecast']['surplus_sauce_cost_rub']=(required_sauce-demo_sauce)*components['black_eel']['cost_per_g']
    eel['illustration_one_each_18_not_sales_forecast']['joint_allocated_eel_and_sauce_sold_cost_rub']=demo_eel*components['eel_joint']['cost_per_g']+demo_sauce*components['black_eel']['cost_per_g']
    eel['illustration_one_each_18_not_sales_forecast']['whole_eel_and_sauce_production_cost_rub']=demo_eel*all_outputs_cost_per_direct_g
    assert abs(eel['illustration_one_each_18_not_sales_forecast']['whole_eel_and_sauce_production_cost_rub']-eel['illustration_one_each_18_not_sales_forecast']['surplus_sauce_cost_rub']-eel['illustration_one_each_18_not_sales_forecast']['joint_allocated_eel_and_sauce_sold_cost_rub'])<1e-8
    result={'as_of':'2026-10-21','status':'first paper assortment, agent-proposed recipe/price; not accepted production cards','food_cost_basis':'recipe materials before discounts and bonus redemption; excludes unpriced water, sidekit, packaging, payroll, utilities, taxes, delivery, depreciation and financing','pricing_method':'ceil5(price_reserve_cost/0.35); price_reserve differs from joint_cost only for direct eel component; all quantities are ready-prepared quantities, frying inputs are allowance not finished yield. Both normal bases exclude forced surplus sauce writeoff; isolated-batch stress separately shown.','recipes':rows,'eel_joint_output':eel,'checks':checks,'summary':{'recipe_count':len(rows),'joint_cost_min':min(r['joint_scenario_cost_rub'] for r in rows),'joint_cost_max':max(r['joint_scenario_cost_rub'] for r in rows),'price_min':min(r['menu_price_rub'] for r in rows),'price_max':max(r['menu_price_rub'] for r in rows),'fc_max_reserve_pct':max(r['reserve_fc_pct'] for r in rows),'fc_max_joint_pct':max(r['joint_fc_pct'] for r in rows),'checks_passed':len(checks)}}
    args.out.mkdir(parents=True,exist_ok=True)
    (args.out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    md=['# Первая новая линейка «Тепла» — 21.10.2026','', '**18 бумажных кандидатов: по шесть холодных, запечённых и жареных.** Граммовки сочетаний предложены для первой проработки, не утверждены как новые техкарты.','',
    'Все значения начинки и покрытий ниже — граммы готовых компонентов на целый ролл. К каждой строке добавлены **110 г готового риса и ½ листа нори**. Для жареных дополнительно учтён расход **кляра 25 г, сухарей 15 г, масла 5 г**. Эти расходы заложены в себестоимость; они не являются измеренной удержанной массой готовой панировки.','',
    '**Материальная база для цены** — продукты по закупочному срезу 21.10 с новыми ценами краба/сыра. Для четырёх позиций с угрём ★ здесь сохранена текущая консервативная стоимость филе; более низкая себестоимость при полном использовании остатков показана отдельно ниже. **Цена** округлена вверх до 5 ₽ из этой базы / 0,35. FC указан до акций. Изготовление и списание всего лишнего соуса — отдельный стресс ниже, обычной ценой не покрыт. Вода пока без цены; полный расход бизнеса этим показателем не описывается.','']
    for group in ['Холодные','Запечённые','Жареные']:
        md += ['## '+group,'','| Позиция | Внутри, г | Снаружи, г | Материальная база для цены, ₽ | Цена меню, ₽ | FC до акций |','|---|---|---|---:|---:|---:|']
        for r in rows:
            if r['group']!=group:continue
            star='★' if r['eel_g'] else ''
            cost=f"{r['price_reserve_cost_rub']:.2f}"
            md.append(f"| {r['id']} {r['name']}{star} | {comptext(r['inner'])} | {comptext(r['top'])} | {cost} | {r['menu_price_rub']} | {r['reserve_fc_pct']:.2f}% |")
        md.append('')
    md += ['## Проверка конструкции','',
    '- Обычная начинка105г. Один или два основных продукта по35г: каждый составляет33,33% внутренней начинки, выше минимальных30%. У холодного ролла с масаго также35/105г лосося.','- У крышек ровно50г одного продукта сверху. В выбранных трёх пробах внутри105г наполнителей. Это частный вариант, основной продукт внутри крышечных роллов разрешён.','- Под тартаром90г наполнителей, сверху70г готовой смеси. Внутри рыбы нет. Текущая пропорция шапки30г подготовленного лосося+9г майонеза+1г масаго на39,3г готового выхода; для70г умножение70/39,3. Тартар без копчения — явная замена вида лосося при той же карте смеси.','- Крабовая смесь80/20 трактуется как80% готового краба и20% майонеза. Для краба учтён текущий98% выход. Сыр+30% применяется к творожному сыру; состав сырного соуса из текущей карты не менялся.','- Массы ингредиентов/закладок не названы готовым весом меню; масса после запекания/жарки не измерена.','',
    '## Отдельно угорь: совместный выход и ценовой резерв','',
    f'Для первой бумаги предложено90% остатков и10% добавленного филе в угревой части соуса. Из1кг покупки:470г остатков+{x:.2f}г добавленного филе дают{eelpart:.2f}г угревой части, остальное прямое филе{fillet:.2f}г; соуса по прежней пропорции получается{sauce:.2f}г. Сумма угревых выходов ровно1кг, стоимость покупки1551₽ распределена один раз. При распределении по массе угревого компонента прямое филе1551₽/кг, готовый соус{components['black_eel']['cost_per_g']*1000:.2f}₽/кг.','',
    f'Но каждому1г прямого филе соответствует{ratio:.3f}г соуса. Иллюстрация «по одному каждого из18» — не прогноз спроса: филе{demo_eel:g}г, соуса используется{demo_sauce:g}г, тогда как образуется{required_sauce:.2f}г. Невостребованный остаток{required_sauce-demo_sauce:.2f}г; нужно ещё≈{math.ceil((required_sauce-demo_sauce)/30)} порций соуса по30г в других заказах/блюдах. Сам факт наличия соуса в меню не доказывает его полный расход. Для цены четырёх роллов с прямым угрём потому сохранён резерв по текущей стоимости филе{components['eel_legacy']['cost_per_g']*1000:.2f}₽/кг. Это альтернативная консервативная база, не вторая покупка в совместной калькуляции.','',
    '| Угревая позиция | Себест. совместного выхода, ₽ | FC при предложенной цене | Минимум совместного выхода, ₽ | Предложенная цена, ₽ | Весь избыток соуса списан: себест. / нужная цена, ₽ |','|---|---:|---:|---:|---:|---:|']
    for r in rows:
        if r['eel_g']:md.append(f"| {r['id']} {r['name']} | {r['joint_scenario_cost_rub']:.2f} | {r['joint_fc_pct']:.2f}% | {r['minimum_price_joint_rounded5_rub']} | {r['menu_price_rub']} | {r['surplus_sauce_stress_cost_rub']:.2f} / {r['surplus_sauce_stress_price_rub']} |")
    md += ['',f'Стресс — отдельная альтернатива: всю покупку угря и все неугревые компоненты произведённого соуса относим один раз на прямое филе; соус, поданный в этой же порции, повторно не начисляем. Расход на 1 г прямого филе тогда {all_outputs_cost_per_direct_g:.5f} ₽. В иллюстрации из 18 порций непроданный соус стоит {eel["illustration_one_each_18_not_sales_forecast"]["surplus_sauce_cost_rub"]:.2f} ₽. Это показывает цену ошибки, а не прогноз обязательного списания. Для принятия угревых карт нужен общий спрос на соус, либо иной режим хранения/выпуска, подтверждённый отдельно.']
    md += ['','## Допущения и следующие доказательства','',
    '1. **Проектные массы:**105г обычной начинки,90г под тартаром,70г шапки; крышка50г — требование владельца. Варианты сочетаний, нори½листа, соусы20–35г, поливка5г и панировка/масло — предложения агента для расчёта. Одинаковый объём и качество ещё не доказаны.','2. **Цены:** покупной краб450₽/кг и сыр500,50₽/кг — условия владельца. Остальные последние подтверждённые закупки среза21.10. Цена и выход мидий взяты текущие учётные; O13 о скачке закупочной цены не закрыт. Вода в рисе/кляре не оценена, не приравнена к нулевой закупке.','3. **Краб:**80/20 по нетто. Альтернатива по входу даёт другую небольшую стоимость смеси и требует отдельной карты. Продукты и выходы остальных заготовок взяты из текущих карт; не добавлялись новые неизученные ингредиенты.','4. **Жарка:**5г масла на ролл — расходная норма для бумаги; литровая цена пересчитана по учётной плотности0,925кг/л. Реальный расход включает пополнение/замену и должен быть измерен. При10г вместо5г прирост себестоимости равен5×цене1г масла; цена пересчитывается тем же округлением, если запас до35% недостаточен.','5. **Угорь:** полное использование остатков — условие владельца;90/10 внутри угревой части и распределение затрат по массе — предложенный способ расчёта. Требуются проверка выхода и баланс фактических продаж соуса. Цены с резервом не требуют заранее признать всю экономию.','6. **Предложение гостю:** названия рабочие; сеты/скидки/бонусы/подарки и спрос по новым ценам пока не заданы.35% до акций не означает прежние33% по оплаченной выручке: ровно35% при скидке15% становятся41,18%, при20% —43,75%, до подарков. Средний FC линии не рассчитан без весов продаж.','7. **Граница себестоимости:** продукты, включая соусы и расход жарки. Отдельно нужны гарнирный комплект/упаковка, ФОТ, доставка, коммунальные, комиссии, налоги и оборудование. Заявление о500тыс. прибыли по этой таблице не делается.','',
    'Следующий результат — выбрать кандидатов, проверить геометрию/срез/вкус/поведение в доставке и реальный расход покрытий/масла; после этого пересчитать акционные продажи и сеты, затем общий бюджет. Новая съёмка/приготовление сейчас не запускались.','',
    '## Стоимость компонентов и воспроизводимость','','| Компонент | Стоимость, ₽/кг, кроме нори | Основание |','|---|---:|---|']
    for k,c in components.items():
        val=c.get('cost_per_g',0)*1000 if k!='nori' else c['cost_per_sheet']
        md.append(f"| {c['name']} | {val:.5f}"+(' ₽/лист' if k=='nori' else '')+f" | {c.get('quantity_basis','')} |")
    md += ['','Датированные источники, GUID/карты, hashes и корректировки: [inputs.json](inputs.json). Полные построчные затраты, оба сценария угря и автоматические проверки: [results.json](results.json). Пересборка: `python3 build.py --inputs inputs.json --out .` (Python3 standard library). Правила владельца: [исходный план](../2026-10-21-new-line-paper-plan/plan.md). Единый журнал: [profit-progress.md](../../profit-progress.md), Q22.','']
    # Keep Russian prose readable without changing numeric decimals, source paths or code.
    md=[re.sub(r'(\d)(?=[А-Яа-яЁё₽])',r'\1 ',re.sub(r'(?<=[А-Яа-яЁё])(\d)',r' \1',line)) if not ('[' in line or '`' in line or line.startswith('|')) else line for line in md]
    (args.out/'report.md').write_text('\n'.join(md))
    # Native static HTML, no external files/network; full ingredient quantities and all pricing checks.
    def h(s):return html.escape(str(s))
    sections=[]
    for group in ['Холодные','Запечённые','Жареные']:
        cells=[]
        for r in rows:
            if r['group']!=group:continue
            cost=f"{r['price_reserve_cost_rub']:.2f}"
            cells.append('<tr>'+''.join(f'<td>{h(v)}</td>' for v in [r['id']+' '+r['name']+(' ★' if r['eel_g'] else ''),comptext(r['inner']),comptext(r['top']),cost,r['menu_price_rub'],f"{r['reserve_fc_pct']:.2f}%"] )+'</tr>')
        sections.append(f'<section><h2>{h(group)}</h2><div class="scroll"><table><thead><tr><th>Позиция</th><th>Внутри, г</th><th>Снаружи, г</th><th>База для цены, ₽</th><th>Цена, ₽</th><th>FC до акций</th></tr></thead><tbody>'+''.join(cells)+'</tbody></table></div></section>')
    textblocks=[]
    for heading in ['Проверка конструкции','Отдельно угорь: совместный выход и ценовой резерв','Допущения и следующие доказательства']:
        pos=md.index('## '+heading);end=next((n for n in range(pos+1,len(md)) if md[n].startswith('## ')),len(md))
        parts=[f'<h2>{h(heading)}</h2>'];in_table=False
        for line in md[pos+1:end]:
            if line.startswith('|'):
                if line.startswith('|---'):continue
                cells=[v.strip() for v in line.strip('|').split('|')]
                if not in_table:
                    parts.append('<div class="scroll"><table><thead><tr>'+''.join('<th>'+h(v)+'</th>' for v in cells)+'</tr></thead><tbody>');in_table=True
                else:parts.append('<tr>'+''.join('<td>'+h(v)+'</td>' for v in cells)+'</tr>')
            else:
                if in_table:parts.append('</tbody></table></div>');in_table=False
                if line:parts.append('<p>'+re.sub(r'\*\*(.*?)\*\*',r'<strong>\1</strong>',h(line))+'</p>')
        if in_table:parts.append('</tbody></table></div>')
        textblocks.append('<section>'+''.join(parts)+'</section>')
    doc='''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Тепло · первая новая линейка</title><style>body{font:16px/1.5 system-ui,sans-serif;margin:0;background:#f5f3ed;color:#242c27}main{max-width:1420px;margin:auto;padding:32px}header,section{background:white;border:1px solid #e3e3d9;border-radius:14px;padding:24px;margin-bottom:20px}h1{font-size:34px;line-height:1.2;margin-top:8px}h2{font-size:23px}.tag{color:#507056;font-weight:650}table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;border-bottom:1px solid #e8e9e2;padding:11px 9px;vertical-align:top}th{background:#f2f5ee}td:nth-last-child(-n+3){white-space:nowrap;text-align:right}th:nth-last-child(-n+3){text-align:right}.scroll{overflow:auto}p{max-width:1120px}a{color:#185f4b}footer{font-size:14px;color:#59645a}.numbers{font-size:19px} @media print{main{padding:0}section{break-inside:avoid}body{background:white}table{font-size:11px}}</style><main><header><div class="tag">БУМАЖНЫЙ ПРОЕКТ · 21 ОКТЯБРЯ 2026</div><h1>Первая новая линейка «Тепла»</h1><p class="numbers">18 кандидатов · 6 холодных · 6 запечённых · 6 жареных</p><p>К каждой строке: рис110г и½листа нори. Жарка: кляр25г, сухари15г, масло5г включены в стоимость. Внутри105г, под тартаром90г +70г готовой шапки. Массы проектные, готовый вес не измерен.</p><p>Цена = округление вверх до5₽ от консервативной себестоимости/0,35, до скидок и бонусов. Вода без цены. *Угорь: себестоимость полного совместного использования / база с резервом для цены; FC по второй сумме. Рецептуры и цены не внедрены.</p></header>'''+''.join(sections)+''.join(textblocks)+f'<footer><p>Проверено {len(checks)} условий; все PASS. Числовая/структурная проверка, визуальный рендер HTML не заявлен.</p><p><a href="report.md">Полный отчёт и unitcosts</a> · <a href="results.json">Построчная калькуляция</a> · <a href="inputs.json">Исходные ставки/карты</a> · <a href="../../profit-progress.md">Журнал</a></p></footer></main></html>'
    doc=doc.replace('Рис110','Рис 110').replace('рис110','рис 110').replace('кляр25','кляр 25').replace('сухари15','сухари 15').replace('масло5','масло 5').replace('Внутри105','Внутри 105').replace('тартаром90','тартаром 90').replace('до5₽','до 5 ₽').replace('*Угорь: себестоимость полного совместного использования / база с резервом для цены; FC по второй сумме.','★Угорь: в основной таблице база с резервом по текущей стоимости филе; себестоимость совместного выхода показана ниже. Полное списание лишнего соуса в эту базу не входит.')
    (args.out/'report.html').write_text(doc)
    print(json.dumps({'summary':result['summary'],'rows':[{k:r[k] for k in ['id','name','joint_scenario_cost_rub','price_reserve_cost_rub','menu_price_rub','reserve_fc_pct']} for r in rows],'eel':eel},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
