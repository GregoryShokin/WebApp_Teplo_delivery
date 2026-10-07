# Источники ФОТ и сверка с P&L

Проверено по коду на `170833b8`, 07.10.2026. Пути относительно корня проекта.
Это карта реализованной логики; актуальные production-значения и полнота месяцев здесь не подтверждены.
Без чтения секретов/персональных сумм, вызовов production, пересчётов и изменений источников.
## 1. Выбор источника

- Исторический расход: финализированные начисления и правила календарного P&L, а не деньги к выдаче.
- Объяснение закрытой ведомости: её `components`, `summary` и сохранённые версии ставок.
- Сценарий будущего: конфигурация на дату + состав/минуты смен + формула из проверенного коммита.
- Документы объясняют договорённость; даты и противоречия с действующим кодом указывать явно.
- Defaults, seed и старый Google Sheets не доказывают текущую ставку production.
- В отчёте фиксировать период, дату чтения, HEAD, источник, статус и непокрытые компоненты.
- P&L — пересчитываемая проекция; наличие числа само по себе не доказывает закрытие месяца.
- Ручные компоненты добавляются к источникам, а не заменяют их: `apps/api/app/services/pnl/projector.py:716` (путь ниже).
## 2. Канонический календарный ФОТ

- Источник: `apps/api/app/services/pnl/sources/payroll.py:113`, `build_payroll_month`.
- Таблицы: `payroll_line` → `payroll_run` → `payroll_period`; пересечение периода с месяцем.
- Факт для этого источника — только `run.status='finalized'`: тот же файл, строки 45 и 128.
- Производство считается неделями вторник–понедельник; неделя может относиться к двум месяцам.
- Брать `components.days[].date` внутри месяца и `day.role` прежде `line.role`: строки 173–208.
- Зарплата дня = `base_pay + percent_pay + vacation_pay`: функция `_day_amount`, строка 104.
- День роли повара идёт в `cook_payroll`; кассир `administrator` — в `administrator_payroll`.
- Остальные роли идут в `admin_payroll`; это не то же самое, что строка кассиров.
- `fund_accrual` учитывается отдельно от зарплаты: строки 209–213.
- Административные строки без дней берутся целиком, только если весь период внутри месяца: строка 214.
- Legacy-строка без дней, пересекающая границу месяца, этим fallback не распределяется — она пропускается.
- Поэтому отдельно проверять покрытие днями, типы периодов и импортированные исторические строки.
- Lump-отпускные имеют день payout_date за границами недели работы. На стыке месяца отбор по пересечению payroll_period может не захватить нужный run; сверять такие транши отдельно, не объявлять пропуск нулём.
## 3. Оклад по востребованию и корректировки

- `on_demand`: `base_pay=0`, начисление находится в `components.proration.accrual_amount`.
- Обе полумесячные строки могут содержать полный месячный оклад; ключ дедупликации — сотрудник + месяц.
- Обычная строка этого же сотрудника/месяца подавляется, если есть месячное on-demand начисление.
- Реализация: `apps/api/app/services/pnl/sources/payroll.py:135`, `:149`, `:157`, `:248`.
- `total_payable` on-demand содержит только включённую к выдаче сумму, а не заработанный оклад.
- Платёжная логика: `apps/api/app/services/payroll_admin.py:408`, `compute_on_demand_debt` на строке 452.
- Премии и штрафы P&L читает из `payroll_adjustment` по `work_date`: `apps/api/app/services/pnl/sources/payroll.py:263`.
- Эта выборка не присоединяет finalized run; проверять корректировки, ещё не применённые в ведомости.
- Премии — отдельная строка `bonuses`; не складывать их повторно с `line.premium`.
- Ревизионные штрафы уменьшают `audit_results`, а не одновременно зарплату: строки 292–295.
- Все прочие штрафы текущий P&L относит в уменьшение `cook_payroll`: строки 296–306; это ограничение разреза ролей.
## 4. Опасности общего aggregate и gross/net

- `apps/api/app/services/payroll_aggregate_service.py:13`, `build_aggregate` — диагностический агрегат.
- Он складывает целые пересекающиеся периоды, включая `completed` и `finalized`: строки 18–54.
- Он не делает помесячное разнесение дней и не заменяет `build_payroll_month`.
- Его `gross = base + premium + percent + vacation + bonus_total`: строка 90.
- Но production bonus уже увеличивает `premium` И записывается в `adjustments.bonuses`.
- Подтверждение: `apps/api/app/services/payroll_calculator.py:1329`; у admin — `apps/api/app/services/payroll_admin.py:403`, `:422`.
- Следовательно, `aggregate.gross` может удвоить премию; не использовать как каноническое начисление.
- `base_pay` уже включает надбавку Старший/Зам и доплату пт/сб: `apps/api/app/services/payroll_calculator.py:710`, `:742`, `:750`.
- Не добавлять `seniority_allowance_pay` и `weekday_premium` второй раз к этому base.
- `total_payable` — остаток к выдаче после удержаний/зачётов; это не расход ФОТ и не revenue_net.
- `deposit_payout_scheduled` не входит в `total_payable`: `apps/api/app/models/payroll.py:497`.
- Термины gross/net всегда сопровождать определением: выручка, начисление и выдача — разные величины.
## 5. История ставок и конфигурации

- `payroll_rate`: роль/категория/станция/тип ставки, employee override, сумма, активность, effective даты.
- Модель: `apps/api/app/models/payroll.py:676`, `PayrollRate`; месячные и сменные ставки не смешивать.
- Дневной пул: `revenue_tier`; вес участия: `category_coefficient`; модель `RevenueTier` — строка 814.
- Интервал действия `[effective_from, effective_to)`; верхняя граница исключена.
- Источник фильтра: `apps/api/app/services/payroll_config.py:735`; выбор ставки — `apps/api/app/services/payroll_calculator.py:1954`.
- GET текущей матрицы использует `date.today()`: `apps/api/app/services/payroll_config.py:175`, `:194`.
- Для истории запрашивать `history=true` и выбирать версии на дату расчёта, а не сегодняшние.
- `app_setting` и `app_setting_history`: `apps/api/app/models/settings.py:14`, `:35`.
- Часть payroll-правил живёт в AppSetting; читать только разрешённые ключи, не дамп всей таблицы.
- Кадровые условия — `employee_role_assignment`, `employee_position_event`, `employee_allowance_event`.
- Модели кадровых событий: `apps/api/app/models/employee.py:367`, `:437`, `:481`.
- Посещаемость/корректировки смен: `attendance_entry`, `shift_ledger_entry`; `apps/api/app/models/payroll.py:56`, `:94`.
- Admin acting appointment выбирается на `payroll_date`, не всегда на конец периода: `apps/api/app/services/payroll_admin.py:684`.
## 6. Snapshot и воспроизводимость

- `run.summary.payroll_rate_snapshot`: reader `apps/api/app/services/payroll_runner.py:653`.
- На финализации snapshot помечается locked: тот же файл, строка 1337.
- Сохранённый список ставок может передаваться calculator override: `apps/api/app/services/payroll_calculator.py:239`.
- Tiers, category coefficients и кадровые данные загружаются отдельно: строки 249–280.
- Поэтому rate snapshot НЕ гарантирует полный snapshot всех входов расчёта.
- Исторические `components.days` — предпочтительное объяснение уже зафиксированного начисления.
- Не переоткрывать/пересчитывать ведомость ради проверки; это изменяет и связанные субсчета.
## 7. Дневная выручка и ступени

- `apps/api/app/services/iiko_revenue.py:18`: SALES OLAP, `DishDiscountSumInt`, группа `OpenDate.Typed`.
- Фильтр — настроенный Department; `to` exclusive, в запросе добавляется день: строка 54.
- Это учетный день iiko; он не обязан совпадать с календарной датой закрытия каждого заказа.
- Это сумма после скидок в семантике iiko; не сумма платежей до сдачи и не сверенная банковская выручка.
- В этом body нет явных фильтров deleted/returned; не считать его популяцию равной отдельной выгрузке заказов.
- Кэш: `app_setting['payroll.daily_revenue_by_date']`; `apps/api/app/services/payroll_runner.py:103`.
- При загрузке отсутствующий день ответа превращается в 0: `apps/api/app/services/payroll_runner.py:123` — нужна проверка полноты.
- `ensure_daily_revenue_cached` пишет кэш; в read-only исследовании его не вызывать.
- Пул = вся дневная выручка × ставка выбранного диапазона: `apps/api/app/services/payroll_percent.py:149`, `:205`.
- Диапазон `[min_revenue,max_revenue)`; новая ставка применяется ко всей сумме, не только превышению.
- На границе возможен скачок общего расхода; моделировать сумму дневных дельт, а не средний процент месяца.
- Распределение пула — вес категории × ограниченные 12 часами часы / 12: `apps/api/app/services/payroll_percent.py:257`.
- Доли округляются вниз до рубля; сумма выплат может быть меньше номинального пула: строка 161.
- Таблица `payroll_revenue_share` существует, но active calculator грузит tiers/coefficients: `apps/api/app/services/payroll_calculator.py:249`.
## 8. Три зарплатных контура

- Production: недельные runs; admin: полумесячные runs; различать `period_type` и `summary.kind`.
- Admin включает вспомогательный персонал и старшего курьера; cashier-производство — отдельная роль.
- DDS-разнесение admin: вспомогательный персонал → `soderzhanie_torgovyh_tochek`, остальные → admin salary.
- Маппинг: `apps/api/app/services/payroll_payout_allocation.py:20`, `:41`; это классификация денег, не новый ФОТ.
- Обычные курьеры в P&L: `pnl_iiko_fact`, `metric_code='courier_salary'`, `direction='total'`, `period_month`.
- Источник — iiko P&L: Account.Name «Зарплата курьеров», Account.Type EXPENSES, `Sum.ResignedSum`.
- `apps/api/app/services/pnl/iiko_sync.py:223`, `courier_salary_from_rows`; запись зеркала на строке 320.
- Старший курьер уже в admin и явно не включён в эту iiko-строку; не вычитать/добавлять его повторно.
- `delivery_order`, `courier_iiko_shift`, `courier_shift_match` — операционные факты, не тариф начисления.
- `app-spec/modules/staff/payroll/couriers/spec.md:1` — датированная заглушка, не подтверждение нынешнего тарифа.
- Средний courier expense / deliveries — только proxy; рейсы, пакетирование и дополнительная смена требуют источника правил.

## 9. Официальная зарплата и налоги

- P&L payroll_taxes = `tax_payroll_ledger.ndfl + contributions + injury` за year/month.
- `apps/api/app/services/pnl/sources/taxes.py:38`; модель `TaxPayrollLedger` — `apps/api/app/models/tax.py:497`.
- Оборотка уникальна по `(year, month, tab_number)`; `intake_id` хранит происхождение, повторный документ обновляет строку, а не создаёт второе начисление.
- `None` в исходной ячейке не означает подтверждённый ноль; проверить покрытие сотрудников и всех налоговых компонентов.
- Не добавлять `TaxPayrollLedger.accrued/oklad` второй зарплатой к управленческому ФОТ.
- Не суммировать tax_payment, уведомление и банковский платёж: один налог может присутствовать несколько раз.
- Парная конвенция P&L: зарплата «на руки», НДФЛ отдельной налоговой строкой: `apps/api/app/services/pnl/sources/payroll.py:10`, `apps/api/app/services/pnl/sources/taxes.py:47`.
- Текущий payroll source фактически не вычитает ненулевой ndfl из суммы начисления.
- Если обнаружен `ndfl_withheld != 0`, сначала сверить конвенцию, иначе возможен повторный НДФЛ.
- Отсутствие оборотки → `None`/NO_DATA, не ноль: `apps/api/app/services/pnl/sources/taxes.py:66`, `apps/api/app/services/pnl/projector.py:303`.
- Старые документы называли источником ДДС: `business-docs/staff/payroll-policy.md:31`.
- Новый код с решением 03.08.2026 использует оборотку; старую рекомендацию не переносить как действующую.

## 10. Налоговый прогноз и пределы

- `apps/api/app/services/taxes/official_payroll.py:1`: виртуальный прогноз, не запись tax_payment.
- Полные месяцы без оборотки; используются текущие Employee.is_official/official_status/official_salary и hire/fire даты; нестандарт пропущен: строки 114, 166, 209.
- Источник параметров — `YearConfig`, `YEAR_CONFIGS`: `apps/api/app/services/taxes/engine.py:88`, `:142`.
- Взносы: money(min(base,T)×full_rate + max(0,base−T)×reduced_rate), T=money(MROT×threshold): `apps/api/app/services/taxes/official_payroll.py:61`.
- NDFL=rub(max(base−deduction,0)×rate); injury=money(base×injury_rate); суммы считаются по сотруднику, затем складываются: строки 69, 95, 178.
- Вычет: сумма по числу детей ×2 для single_parent; обнуляется при превышении YTD-лимита YearConfig: строки 75, 130, 183; это код, не правовая верификация.
- Прогноз не складывать с фактической обороткой; P&L payroll tax сам этот fallback не использует.
- Налог бизнеса отдельный: `apps/api/app/services/pnl/sources/taxes.py:90`; USN + 1/12 фиксированного взноса ИП.
- В этой месячной функции нет годового вычета USN и дополнительного 1%: строки 19, 114–120.
- Фиксированный взнос ИП не превращать в переменный расход каждого дополнительного заказа.

## 11. Касса, резервы и обязательства

- `apps/api/app/services/payroll_obligations.py:135`, `run_obligations`: salary и назначенный deposit отдельно.
- `PayrollPayment.amount/booked_amount` — только salary: `apps/api/app/services/payroll_obligations.py:1`, `apps/api/app/models/payroll.py:296`.
- Deposit paid определяется unreversed payout booking → неexcluded cashflow по статье депозита: строка 28.
- Один `status='paid'` не доказывает выдачу депозита; legacy fallback требует фактической проводки.
- Остаток = max(0,salary−paid) + max(0,deposit−deposit_paid): `apps/api/app/services/payroll_obligations.py:114`.
- Из-за max отдельно смотреть переплату; `overpaid_amount` в `apps/api/app/models/payroll.py:345`.
- И paid, и booked должны сходиться с обязательством: `apps/api/app/services/payroll_reserves.py:240`, `run_payment_settlement`.
- `SafeAllocation` лишь резервирует деньги; не создаёт expense/cashflow: `apps/api/app/services/payroll_reserves.py:1`.
- Банк → Сейф — внутренний перевод; paid банковского draft не означает выдачу сотруднику.
- Доплата проводит дельту amount−booked_amount; не считать partial и последующий full дважды.
- Отмены: `payroll_payout_booking.reversal_transaction_id`, cashflow quality excluded; `apps/api/app/models/payroll.py:365`.
- Draft created/updated/failed/deleted — не доказательство зарплатного расхода/выдачи: `apps/api/app/services/payroll_payouts.py:406`.

## 12. Авансы, депозиты, фонд и округление

- Возврат аванса уменьшает payable, не начисленный расход; preview ещё не меняет баланс: `apps/api/app/services/payroll_advance_recovery.py:1`.
- При finalized двигаются balances, при unfinalize — реверс; не читать все recovery строки как окончательные.
- Зачёт ранее выплаченного DDS уменьшает payable один раз: `apps/api/app/services/payroll_employee_payout_offset.py:1`.
- Deposit withholding — обязательство; выдача возвращает долг; ни одно не повторный расход ФОТ.
- Списание невостребованного депозита — доход ниже EBITDA: `apps/api/app/services/pnl/sources/deposits.py:5`.
- Фонд начисляется сверх зарплаты расходом; выплата погашает обязательство; не добавлять её повторно.
- Forfeit фонда отменяет прежний расход только в пределах горизонта учёта: `apps/api/app/services/pnl/sources/deposits.py:11`, `:159`.
- Для списаний created_at переводится в MSK; run-linked preview не факт: тот же файл, строки 29, 56.
- `apps/api/app/services/payroll_rounding.py:19`: положительный payable округляется вниз до ближайших 5 рублей.
- Округление применяется ОДИН раз к сумме сотрудника по всем ролям, не к каждой роли/смене: строка 28.
- Разница записана в `components.payroll_rounding`; она объясняет payable, не вычитается P&L-источником из accrual.
- Процентные доли и fund тоже имеют собственные floor-правила; не заменять их единым round в конце месяца.

## 13. Прогноз смен не равен полному ФОТ

- `apps/api/app/services/shift_cost_estimate_service.py:187`: total = base_salary + weekday_premium + revenue_percent.
- Allowance уже включён в base; fund_accrual посчитан отдельно и НЕ входит total.
- `apps/api/app/services/payroll_forecast_run_service.py:125`, `:149` суммирует этот total и строит fot_to_revenue_pct.
- Поэтому forecast ratio не включает весь зарплатный/P&L-контур; фонд и прочее добавлять отдельно один раз.

## 14. Разрешённое чтение и ловушки GET

- Полные пути: `/api/v1` (`apps/api/app/main.py:65`), prefixes в `api/v1/router.py:52–78`.
- GET `/reports/pnl?month=YYYY-MM`, `/reports/pnl/ledgers/payroll?month=YYYY-MM`: `apps/api/app/api/v1/routes/reports_pnl.py:510`, `:550`.
- GET `/payroll/runs`, `/runs/{id}`, `/runs/{id}/lines`: `apps/api/app/api/v1/routes/payroll.py:187`, `:401`, `:417`.
- GET `/payroll/admin/runs`, `/runs/{id}/lines`, `/salaries`: `apps/api/app/api/v1/routes/payroll_admin.py:168`, `:220`, `:313`.
- GET `/payroll/config/rates?history=true`, `revenue-tiers`, `category-coefficients`, `revenue-share`, `deductions`, `seniority-premium` с history=true.
- Реализация config GET: `apps/api/app/api/v1/payroll_config.py:63`, `:142`, `:176`, `:210`, `:244`, `:278`.
- Для денег GET `/payroll/runs/{id}/payout-cashflows`, `/payout-allocation`, `/funding-sources`, `/bank-draft/delta`, `/solvency`.
- Эти GET — диагностические читатели, не команды выплат; route anchors: `apps/api/app/api/v1/routes/payroll.py:671`, `:833`, `:864`, `:902`, `:1364`.
- GET `/payroll/aggregate` — только с оговорками раздела 4: `apps/api/app/api/v1/routes/payroll.py:352`.
- GET `/reports/pnl/workup-review` ПИШЕТ очередь и COMMIT: `apps/api/app/api/v1/routes/reports_pnl.py:931`; исключить из read-only allowlist.
- Не вызывать recalculate/finalize/sync/ensure/restore/create-draft/mark-payment для исследования.
- DB: BEGIN READ ONLY; точечные SELECT; ROLLBACK. Не SELECT FOR UPDATE и не write-capable service calls.
- Ответы ledger/lines содержат персональные данные; в вывод только необходимые агрегаты, без имён/сумм сотрудников.

## 15. Экономическая интерпретация и контроль

- Уже запланированные смены и admin оклады условно фиксированы лишь до исчерпания мощности.
- Дополнительные смены/минуты — ступень расхода; Старший/Зам, пт/сб и фонд считать по применимым правилам.
- Дневной процент пересчитывать от всей применимой выручки бизнеса, включая прочие каналы.
- Общую дельту пула учитывать один раз; перераспределение долей сотрудников само не новый расход компании.
- Проверять days/roles, finalized coverage, blocking_issues, legacy flags, версии, adjustments, tax coverage, iiko synced_at.
- NO_DATA/failed/blocked/completed не превращать в нулевой окончательный расход.
- Рост вклада после foodcost не называть ростом полной прибыли: нужны комиссии, доставка, упаковка, налоги, ΔФОТ и мощность.
- Все неподтверждённые ставки, незакрытые расходы и допущения каннибализации оставлять видимыми.
