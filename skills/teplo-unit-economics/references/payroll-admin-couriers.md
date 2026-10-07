# Административная зарплата и курьеры

Проверено статическим чтением HEAD `170833b8`.
Пути ниже относительны корню репозитория. Функции и ключи — устойчивые якоря поиска.
Реальные ставки, персональные данные, API и БД не читались; тесты не запускались.
Фикстуры, константы fallback и старые спецификации не подтверждают действующие ставки.

## Граница контуров

- `payroll_admin.py` считает административные оклады, доплату за исполняемую административную роль и мойку.
- Реестр `position_registry.py`: `admin_payroll_positions()` = `okladnik` + `shift_pool` + `gets_admin_oklad`.
- «Старший курьер» имеет архетип `courier`, но `gets_admin_oklad=True`.
- «Кассир» имеет архетип `production_percent`; производственная роль `administrator` не означает административный оклад.
- `seniority_premium_positions()` включает `production_percent`, не добавляет администрации производственную надбавку Старший/Зам.
- В административной базе нет дневной или месячной выручки: `percent_pay=0`.
- Производственная роль `administrator` использует дневную выручку через `payroll_calculator.percent_components_for_day()`; её формула описывается отдельно.

## Компоненты и предельный эффект

Обозначения: `M` — подтверждённый месячный оклад; `D` — календарные дни полупериода;
`d` — дни после ограничений найма/увольнения; `r` — подтверждённая ставка мойки; `N` — собственные смены.
Денежные значения базы округляются до копеек.

| Компонент | Формула или правило | Источник / смысловой якорь | Эффект роста |
|---|---|---|---|
| Оклад, `split` | `B=(M/2)×d/D` в каждой половине | `services/payroll_admin.py::_okladnik_base_pay`, `_prorated_amount` | Дополнительный рубль/заказ не меняет базу; найм и изменение ставки — ступень |
| Оклад, `first_half` | `B=M×d/D` только за первую половину | Та же функция, `PAYOUT_MODE_FIRST_HALF` | Режим определяет распределение по половинам; не второй расход |
| Оклад, `second_half` | `B=M×d/D` только за вторую половину | Та же функция, `PAYOUT_MODE_SECOND_HALF` | Аналогично |
| Оклад, `on_demand` | `base_pay=0`; месячное начисление в `proration.accrual_amount` | Та же функция, `PAYOUT_MODE_ON_DEMAND` | Рост выплаты долга не означает рост начисления |
| Дополнительная административная роль | Ещё одна база по персональному окладу роли | `services/payroll_admin.py::_load_acting_admin_employees` | Возникает при подтверждённом назначении, не при каждом заказе |
| Мойка | `B=N×r` | `services/payroll_admin.py::_load_dishwasher_shift_rate`, `_load_dishwasher_shift_counts` | Новая собственная оплачиваемая смена `+r`; заказы внутри смены базу не меняют |
| Премия | Сумма явных `PayrollAdjustment(type='bonus')` | `services/payroll_admin.py::_sum_adjustments` | Только отдельное подтверждённое начисление; нет автоматического revenue-множителя |
| Штраф | Уменьшение выплаты по `type='penalty'` | Та же функция; в P&L отдельная классификация | Не любой deduction уменьшает трудовой расход |
| Старший курьер | Административный оклад по правилам выше | `services/position_registry.py::DEFAULT_POSITIONS`, `gets_admin_oklad` | Fixed/step; учитывается отдельно от общей статьи iiko |
| Обычные курьеры | Факт: сумма `Sum.ResignedSum` статьи iiko | `services/pnl/iiko_sync.py::courier_salary_from_rows` | Средний расход на доставку вычислим из факта; marginal-формула не установлена |
| График / замена / KPI | Денежного расчёта нет | `services/couriers/`, модели расписания/замен | Нельзя автоматически начислить цену смены/заказа/балла |
| Аванс / погашение / депозит | Изменение расчётов и денег | `salary_advance*`, `employee_payout*`, `courier_deposit*` | Само движение не создаёт новую стоимость труда |

Пути в таблице относительно `apps/api/app/`, кроме явно указанных иначе.

## Ставки, даты и графики администрации

- `services/payroll_admin.py::load_admin_oklad()` читает `PayrollRate` из таблицы `payroll_rate`.
- Фильтры: `category='admin'`, `rate_type='monthly'`, `position_group=position`, `is_active=True`.
- Интервал ставки: `effective_from <= as_of`, `effective_to > as_of` либо `NULL`.
- Персональный `employee_id` override приоритетнее общей ставки должности.
- Пустая ставка даёт `excluded_no_oklad`; это отсутствие подтверждённого расчёта, не нулевая стоимость.
- Основной сотрудник: `as_of=period.end_date`; историческая должность через `get_position_on_date()`.
- Исполняемая роль: `as_of=period.payroll_date`; назначение на день выплаты может попасть в эту ведомость.
- `_load_acting_admin_employees()` добавляет персональную административную роль сверх производственной зарплаты.
- Повтор основного собственного оклада не создаётся; работник уже в основном admin-наборе исключается из acting-набора.
- `_load_admin_employees()` учитывает `admin_payroll_excluded`, пересечение hire/fire с периодом и текущую административную должность.
- Первая половина: 1–15, выплата 15-го. Вторая: 16–последний день, выплата 1-го следующего месяца.
- `_prorated_amount()` использует календарные дни включительно; записи смен/часы не являются базой обычного оклада.
- Ставка выбирается на одну дату `as_of`; изменения ставки внутри полупериода не разбиваются по дням.
- `okladnik_earned_to_date()` дополнительно обрезает дни на дату оценки заработанного для аванса.
- `_load_okladnik_payout_modes()` читает текущий `AppSetting.key='payroll.okladnik_payout_modes'`.
- Нет/невалидный режим → `split`. Значения: `split`, `first_half`, `second_half`, `on_demand`.
- Эта настройка не загружается исторически `as-of`; прошлые начисления проверяй по finalized снимкам.
- Для мойки ключ `payroll.dishwasher_shift_rate`; отсутствующее/невалидное/неположительное значение включает кодовый fallback.
- Fallback не подставляй как реальную бизнес-ставку без подтверждения.
- Мойка считается по `DishwasherShift`, уникальность `(employee_id, work_date)`, даты внутри периода включительно.
- Чужие смены, часы и число дней месяца не меняют цену собственной смены.
- Нет своих смен и корректировок → строки мойки нет.
- Старый docstring `models/payroll.py::DishwasherShift` о месячном пуле устарел: действующий код считает `N×r`.

## Начисление on_demand и выплата долга

- `_okladnik_base_pay()` кладёт полный `M`, прорейтированный по календарным дням соответствующего полупериода, в `components.proration.accrual_amount`.
- Обе половины могут содержать полный месячный оклад: складывать их нельзя.
- Ключ дедупликации — `(employee_id, components.proration.period_month)`.
- `base_pay=0`; автоматическая выплата равна нулю, кроме вручную включённых сумм.
- Премия в строке не становится автоматической on_demand выплатой.
- `load_included_payouts_by_employee()` определяет суммы, включённые в конкретный run.
- `compute_on_demand_debt()`: `debt = dedup_accrued − paid`; долг может стать отрицательным при переплате.
- Для paid используются `EmployeePayout(kind='owner_salary')`, статусы `included`, `pending`, `paid`.
- `draft`, `failed`, `cancelled` долг не уменьшают.
- Эта функция читает `PayrollLine` без фильтра finalized, поэтому не заменяет месячный P&L.
- `services/pnl/sources/payroll.py::build_payroll_month()` использует только finalized runs.
- P&L берёт on_demand один раз за сотрудника/месяц и исключает его обычные строки того же месяца при переходе режима.
- Dedup в коде — присваивание в словарь; при разных суммах снимков нет явного порядка выбора в query.
- Если месячные снимки различаются, зафиксируй конфликт; не складывай, не выбирай max и не объявляй сумму однозначной.

## Почему total_payable не является расходом

- До платёжных преобразований admin: `base_pay + premium − penalty`; для on_demand действует ручное включение.
- `run_admin_payroll()` затем выполняет `apply_advance_recoveries()` и `apply_advance_issuances()`.
- Далее `apply_employee_payout_offsets()` учитывает уже выплаченное и `apply_employee_payable_rounding()` округляет.
- Положительная выплата округляется вниз до кратности 5 на сотрудника, один раз по его строкам.
- `services/payroll_rounding.py` сохраняет разницу в `components.payroll_rounding`, не меняет `base_pay`/`premium`.
- Аванс, заём, погашение и зачёт выплаченного меняют выплату/обязательство, не создают новую зарплату.
- Поэтому нельзя брать `total_payable` как ФОТ или вычитать весь `deduction` из начислений.
- P&L без дневных компонентов использует `base_pay + percent_pay + vacation_pay`, не `total_payable`.
- Премии/штрафы P&L читает из `PayrollAdjustment` по `work_date`, не добавляет повторно `line.premium`.
- Ревизионные удержания отнесены в `audit_results`; прочие penalties уменьшают общий зарплатный расход.
- В текущем projector прочие penalties технически уменьшают `cook_payroll`, даже при иной исходной роли.
- Следовательно, `admin_payroll` отдельно не гарантирует полный all-in результат администрации с корректировками.
- Нулевые admin-поля `ndfl_withheld`, `fund_accrual`, `vacation_pay`, `percent_pay` не доказывают нулевые налоги бизнеса.
- Текущий P&L считает зарплату на руки; НДФЛ отдельной налоговой строкой. Повторно не прибавлять удержания как зарплату.

## Курьеры: факт есть, тарифная формула не утверждена в app

- `app-spec/modules/staff/payroll/couriers/spec.md` прямо обозначает payroll как заглушку; base/order/shift/mix не реализованы.
- Факт P&L: `Σ Sum.ResignedSum` только при `Account.Name='Зарплата курьеров'` и `Account.Type='EXPENSES'`.
- Источник: `apps/api/app/services/pnl/iiko_sync.py::courier_salary_from_rows()`, TRANSACTIONS preset P&L.
- По подтверждению в коде от 03.08 оклад старшего курьера не входит в эту статью: сумму iiko брать целиком.
- Старший курьер учитывается отдельно административным окладом; его нельзя вычитать из iiko статьи.
- `services/kassa/iiko_cashshift_sync.py::post_shift_cash_circuit()` отражает кассовые курьерские выплаты и баланс кассы.
- DDS «Курьерская служба -» не добавляй вторым расходом поверх той же зарплаты из iiko.
- `CourierScheduleEntry.category` primary/secondary — категория конкретного дня, не постоянный денежный тариф сотрудника.
- `CourierShiftSubstitution` связывает placeholder с реальным курьером на дату; сама замена доплату не начисляет.
- KPI и оценки не имеют денежного множителя: цвета, пороги, score_snapshot не превращать в премии или штрафы.
- Депозит: `opening + TOP_UP − RETURN − FORFEIT`; это отдельный гарантийный учёт.
- `TOP_UP` сохраняет часть заработанного как обязательство; `RETURN` погашает его; `FORFEIT` не создаёт cashflow.
- Ключи: `couriers.deposit.target_amount`, `couriers.deposit.target_amount_senior`, `couriers.deposit.withhold_primary`, `couriers.deposit.withhold_secondary`, `couriers.deposit.auto_withhold_enabled`.
- `get_deposit_settings()` читает текущие настройки; эти суммы не являются оплатой смены или заказа.
- Неизвестны marginal `C_order`, `C_shift`, гарантии, зоны, километры и сверхурочные: оставлять неизвестными/параметрами, не нулём.
- `courier_salary / deliveries` — историческая средняя, не доказанная цена следующей доставки.

## Таблицы и проверочные тесты

- `apps/api/app/models/payroll.py`: `payroll_period`, `payroll_run`, `payroll_line`, `payroll_rate`, `payroll_adjustment`, `dishwasher_shift`.
- Там же: `salary_advance`, `salary_advance_recovery`, `employee_payout_offset`, `employee_payout`.
- `models/settings.py`: `app_setting`, `app_setting_history`; `models/position.py`: `position`.
- `models/courier_schedule.py`: `courier_schedule_entry`; `models/courier_substitution.py`: `courier_shift_substitution`.
- `models/courier_deposit.py`: `courier_deposit_account`, `courier_deposit_transaction`.
- `apps/api/tests/test_payroll_admin.py`: `test_admin_run_full_period_is_half_oklad`, `test_admin_run_prorates_on_mid_period_hire`.
- Там же: `test_cashier_acting_as_assistant_manager_in_admin_run`, `test_acting_assistant_effective_on_payout_date_enters_second_half_run`.
- Там же: `test_dishwasher_shift_costs_the_same_in_both_half_month_runs`, `test_dishwasher_rate_from_settings_is_used`.
- Там же: `test_dishwasher_pay_follows_own_shift_count`, `test_dishwasher_without_shifts_not_in_run`.
- Там же: `test_admin_run_includes_admin_adjustments_but_not_production`, `test_admin_oklad_employee_override_wins`, `test_admin_run_skips_employee_without_oklad`.
- On-demand: `test_on_demand_mode_accrues_debt_not_paid`, `test_on_demand_debt_accrued_minus_payouts`.
- On-demand: `test_on_demand_premium_not_auto_paid`, `test_on_demand_include_bumps_payable_and_reduces_remaining`.
- Dedup: `test_on_demand_full_oklad_both_halves_deduped_per_month`; старший курьер: `test_senior_courier_gets_admin_oklad`.
- `apps/api/tests/test_positions.py::test_senior_courier_is_admin_okladnik_but_not_production`.
- `apps/api/tests/couriers/test_substitution_service.py`, `test_deposit_service.py`, `test_deposit_cashflow.py`: замены, баланс, денежные проводки.

Для ретроспективы используй finalized строки и компоненты; для сценария — подтверждённые действующие ставки и дату их применения.
