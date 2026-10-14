"""Реестр затрат базового месяца (август 2026) и сравнение с сентябрём.

Запуск: python3 build_registry.py  (читает ../data/pnl-snapshots.json, пишет ../data/cost-registry.json)
Суммы строк — из исходного ОПиУ (projector.build_report, снимок 14.10.2026 18:06 UTC, прод 8cf57490).
Числа вне ОПиУ (документы Яндекса, контроль iiko, акты электроэнергии) — из S2/S4/S6, см. notes.md.
"""
import json
from decimal import Decimal as D
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
p = json.load(open(DATA / "pnl-snapshots.json"))
L = {m: {l["code"]: l for l in v["lines"]} for m, v in p["months"].items()}
N = {"2026-08-01": 1819, "2026-09-01": 1709}          # UniqOrderId, пресет «Выручка по дням»
NC = {"2026-08-01": 1327, "2026-09-01": 1211}         # курьерские доставки (iiko COURIER = delivery_order Closed)


def amt(m, code):
    a = L[m][code]["amount"]
    return None if a is None else D(a)


def per(m, x, n=None):
    return None if x is None else str((x / (n or N[m])).quantize(D("0.01")))


R = []


def add(item, driver, formula, source, status, codes=None, aug=None, sep=None, n_key=None, note=""):
    a = aug if aug is not None else (sum(amt("2026-08-01", c) or 0 for c in codes) if codes else None)
    s = sep if sep is not None else (
        None if codes and any(amt("2026-09-01", c) is None for c in codes)
        else (sum(amt("2026-09-01", c) or 0 for c in codes) if codes else None))
    nm = NC if n_key == "courier" else N
    R.append({"item": item, "driver": driver, "formula": formula, "source": source,
              "aug_2026": None if a is None else str(D(a).quantize(D("0.01"))),
              "aug_per_order": per("2026-08-01", D(a), nm["2026-08-01"]) if a is not None else None,
              "per_order_denominator": ("курьерские доставки авг 1327 / сен 1211" if n_key == "courier"
                                        else "все заказы iiko UniqOrderId авг 1819 / сен 1709"),
              "sep_2026": None if s is None else str(D(s).quantize(D("0.01"))),
              "status": status, "note": note})


add("Food cost (себестоимость продаж iiko)", "на блюдо/ролл (состав)", "Σ ProductCostBase.ProductCost, пресет выручки",
    "pnl_iiko_fact food_cost total; контроль iiko 14.10: авг 962 639,64 (+115,20 бар), сен совпал",
    "подтверждено", aug=D("962639.64"), sep=amt("2026-09-01", "food_cost_chernikova"),
    note="авг: зеркало 962 524,44 устарело на 115,20 (доказанная поправка)")
add("Акты списания", "на партию/период (полупеременная)", "Σ items.cost актов PROCESSED", "ОПиУ writeoffs", "подтверждено", ["writeoffs"])
add("Ревизии (недостача − излишки − ревиз. штрафы)", "период", "shortage − surplus − audit_penalties", "ОПиУ audit_results", "подтверждено", ["audit_results"],
    note="авг 55 438,34 − 25 062,45; сен 57 935,29 − 39 119,71")
add("Упаковка: недостача по инвентаризации + коробки пиццы + напитки", "на заказ (частично в техкартах)", "−результат инвентаризации",
    "ОПиУ packaging/pizza_box/beverage_inventory", "условно", ["packaging_inventory", "pizza_box_inventory", "beverage_inventory"],
    note="Часть упаковки (ланч-боксы) уже в техкартах → в food cost; полной нормы упаковки на заказ нет")
add("Расходники (накладные) + содержание точки", "период/на заказ", "приходные накладные whitelist + касса + документы",
    "ОПиУ aux_goods, shop_maintenance", "подтверждено", ["aux_goods", "shop_maintenance"])
add("ФОТ производства: суши-цех", "смена (ступень) + % дневной выручки", "Σ дней роли sushi (base+percent+vacation)",
    "build_payroll_month by_role.sushi (finalized)", "подтверждено", aug=D("280309.22"), sep=D("267635.81"))
add("ФОТ производства: пицца + шаурма + заготовка", "смена + %", "by_role pizza+shawarma+prep", "build_payroll_month",
    "подтверждено", aug=D("161909.64") + D("80100") + D("2400"), sep=D("136479.36") + D("76035"))
add("Прочие штрафы (уменьшают строку поваров)", "событие", "payroll_adjustment penalty не ревизионные", "build_payroll_month",
    "подтверждено", aug=D("-8573"), sep=D("-6749"))
add("ФОТ кассиров/админов смены (роль administrator)", "смена + %", "by_role administrator", "ОПиУ administrator_payroll",
    "подтверждено", ["administrator_payroll"])
add("Премии", "событие", "payroll_adjustment bonus по work_date", "ОПиУ bonuses", "подтверждено", ["bonuses"])
add("Накопительный фонд", "смена × ставка стажа", "floor((S+A+D)×f) по дню", "ОПиУ accumulation_fund", "подтверждено", ["accumulation_fund"],
    note="суши: авг 4 889, сен 5 720")
add("ФОТ администрации (оклады, мойка, ст. курьер, собственник)", "постоянная/ступенчатая", "оклад × дни/D; мойка 500 ₽ × смены",
    "ОПиУ admin_payroll", "подтверждено", ["admin_payroll"],
    note="Управляющий 90 000, Менеджер 40 000, Ст. курьер 20 000, Уборщица 15 000, Мойка 15 000, Пом. менеджера 6 000, оклад по востребованию 140 000")
add("Курьеры (iiko «Зарплата курьеров»)", "на доставку + выход (ступень)", "Σ Sum.ResignedSum", "ОПиУ courier_service",
    "подтверждено (факт); тариф — со слов владельца", ["courier_service"], n_key="courier",
    note="владелец 08.10: выход 500/700/1000 ₽, доставка 120–130 ₽; средн. авг 198,69 ₽/доставку — не предельная")
add("Налоги на труд (оборотка)", "официальная зарплата", "ndfl+contributions+injury tax_payroll_ledger", "ОПиУ payroll_taxes",
    "авг подтверждено; сен не определено", ["payroll_taxes"],
    note="1 официальный сотрудник: 50 000 начисл., взносы 13 595,93, травм. 100, НДФЛ 6 318; не добавлять 30% на всех")
add("Питание персонала", "на человеко-смену (proxy)", "кассовые чеки по статье", "ОПиУ staff_meals", "подтверждено (касса)", ["staff_meals"],
    note="авг 239 человеко-дней производства → ≈127,53 ₽/чел-день (proxy)")
add("Эквайринг (удержание + терминал + доплата до минимума)", "% оплат картой/онлайн + floor", "withheld + cash; доплата авг 5 636,95, сен 5 807,63",
    "ОПиУ acquiring", "подтверждено (сумма); тариф не определён", ["acquiring"])
add("Starter (сайт и приложение)", "% выручки канала Starter", "≈3% × net продаж OrderType «…Стартер»",
    "УПД ООО «Назад в будущее» (признание), iiko каналы", "условно (база выведена, договор не читан)", ["website_app"],
    note="авг 77 171 / 2 552 645,61 = 3,02%; сен 73 106 / 2 424 982,04 = 3,01%")
add("Яндекс Еда: комиссия по закрывающим документам (НЕ в ОПиУ)", "% продаж Яндекса", "акт СБИС; статьи ДДС нет → recognition_unattributed",
    "supplier_invoice ООО «ЯНДЕКС.ЕДА»", "подтверждено (документ), вне исходного ОПиУ", aug=D("12381.21"), sep=D("14333.47"),
    note="17,3% / 17,5% от продаж Яндекса в iiko (71 566 / 81 754); владелец говорит 25% — разница не определена")
add("Партнёр «В гостях у Алисы» (уменьшает выручку)", "% продаж партнёра", "20% × выручка партнёра после скидок",
    "pnl_partner_commission_fact", "подтверждено", ["partner_commission"])
add("Возвраты клиентам", "событие", "денежный слой статьи возврата", "ОПиУ customer_refunds", "подтверждено", ["customer_refunds"])
add("Агрегатор «К порогу» (ИП)", "период", "самоакт", "ОПиУ aggregator_commission", "подтверждено", ["aggregator_commission"],
    note="июль 6 000, окт 6 000")
add("Налог бизнеса — модель приложения", "% выручки + фикс.", "6% × revenue_net + 57 390/12", "ОПиУ taxes", "подтверждено как модель; полный режим не определён", ["taxes"],
    note="нет допвзноса 1% (≤321 818/год), вычета взносов, НДС")
add("Аренда точки", "постоянная", "2 × 50 000 по договорам", "ОПиУ rent_chernikova", "подтверждено", ["rent_chernikova"])
add("Аренда склада", "постоянная", "10 000", "ОПиУ warehouse_rent", "подтверждено", ["warehouse_rent"])
add("Электроэнергия", "кВт·ч × тариф (+потери)", "авг 9 036 кВт·ч × 12,00 + потери 773 × 12,00", "акт приёма-передачи (OCR в приёмке)",
    "авг подтверждено; сен не определено (акт факта не поступил)", aug=D("117708"), sep=None,
    note="июль 8 933 × 11,54 + потери июня 813; аванс сентября 70 000 («70%»)")
add("Вода", "м³ (счётчик)", "строки 4–7 счёта Водоканала", "возмещение арендодателю", "подтверждено", aug=D("9429.75"), sep=D("12348.49"))
add("Вывоз мусора/отходы", "постоянная: ЭкоЦентр + 1 000 ₽/еженедельный вывоз", "ЭкоЦентр + самоакты + касса", "ОПиУ utilities (recognition + cash)", "подтверждено", aug=D("8957.65"), sep=D("9120.59"), note="авг: 2 000 наличными без контрагента — другие даты вывоза (04.08, 18.08), не дубль")
add("Газ", "потребление", "—", "документов нет ни за один месяц", "не определено", aug=None, sep=None)
add("Амортизация", "постоянная (ОС)", "initial_cost/СПИ, линейно", "depreciation_entry", "подтверждено", ["depreciation"],
    note="149 объектов, ввод 07.2026, 2 313 150 ₽")
add("Ремонт оборудования", "событие", "касса/документы", "ОПиУ equipment_repair", "подтверждено", ["equipment_repair"], note="сен: ремонт холодильника")
add("Автоматизация (iiko, ДоксИнбокс, Лема, серверы, нейросеть)", "постоянная", "документы + касса", "ОПиУ automation", "подтверждено", ["automation"])
add("Телеком, РКО, банк, ФД/НК, найм, персонал, офис", "постоянная", "документы/касса", "ОПиУ", "подтверждено",
    ["telecom", "rko", "bank_fees", "fd_nk_services", "recruiting", "staff_expenses", "office_maintenance", "transport"])
add("Маркетинг: контекст + SEO", "постоянная (бюджет)", "документы", "ОПиУ context_ads, seo", "подтверждено", ["context_ads", "seo"])
add("Прочие доходы ниже EBITDA (знак −)", "событие", "", "ОПиУ other_income, unclaimed_deposits_writeoff", "подтверждено",
    aug=-(amt("2026-08-01", "other_income") + amt("2026-08-01", "unclaimed_deposits_writeoff")),
    sep=-(amt("2026-09-01", "other_income") + amt("2026-09-01", "unclaimed_deposits_writeoff")))
add("Прочие расходы", "событие", "", "ОПиУ other_expenses", "подтверждено", ["other_expenses"])
add("Проценты по кредитам", "долг", "—", "строка не настроена; движений по кредитам в июл–сен не найдено", "не определено", aug=None, sep=None)
add("Неразнесённые проводки", "—", "out/in без статьи", "reconciliation.unmapped", "не определено", aug=None, sep=None,
    note="авг: расход 3 960, приход 1 320; сен: расход 2 615, приход 5 320")
json.dump(R, open(DATA / "cost-registry.json", "w"), ensure_ascii=False, indent=1)
for r in R:
    print(f'{r["item"][:58]:58} {str(r["aug_2026"]):>12} {str(r["aug_per_order"]):>8} {str(r["sep_2026"]):>12}  {r["status"][:30]}')
