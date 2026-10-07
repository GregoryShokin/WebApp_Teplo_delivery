# iikoCloud: краткая карта подтверждённых настроек

Snapshot UTC: 2026-10-07T19:32:22.617848+00:00; endpoint `/api/1/loyalty/iiko/program`; HTTP200.
Полные программы с исходными и разобранными settings, категории, типы оплат и скидок:
[iiko-inventory-2026-10-07.json](iiko-inventory-2026-10-07.json).
Programs[].marketingCampaigns[].*ActionConditionBindings[].actions/conditions[].settings — источник всех значений ниже.
Это конфигурация движка, не доказательство фактического применения в заказе или публикации в Starter.

## Бонусная программа
- programId `01330000-6bec-ac1f-2714-08da0d57a021`, walletId `01330000-6bec-ac1f-d9fc-08da0d57a021`.
- «Назначение категорий», campaignId `01330000-6bec-ac1f-583c-08da0d57f147`:
  OrderSumHistoryCondition: OrderSum0→SetGuestCategory `59306e9b-6863-42fa-8cd7-07e224b78c1b`;
  OrderSum10000→Set `83a5475e-ccdd-486f-9748-eda7da8b1391`,Remove первой;
  OrderSum30000→Set `66fe10cb-2fc4-4150-baea-c31ea00d2761`,Remove двух предыдущих.
  У всех Period=0,Operation=4,MultiplicityEnabled=false; stopFurtherExecution=true. Имена подтверждены по точному GUID отдельным чтением категорий 07.10.2026: Бронза, Серебро, Золото; Бронза назначается новым гостям по умолчанию.
- «Процент для категорий», campaignId `03650000-6bec-ac1f-0334-08da27750f3d`:
  RefillPercent5/7/10 соответственно этим трём GuestCategoryIds; MaxSupplementsPerDay10,ProductsFilter0,MaskItems[].
  Есть отдельный пустой actions binding с CouponSeriesIds[`03650000-6bec-ac1f-a467-08dce6ac3eb1`] и stopFurtherExecution=true.
- «Бонусная система», campaignId `03650000-6bec-ac1f-3663-08db6c11f5e0`:
  PaymentAction PaymentLimitPercent50 безconditions; отдельный PaymentLimitPercent100 при GuestCategoryIds[`94b61db1-cca9-4b3e-a117-30394d0dd1b8`] — подтверждённая категория «Сотрудники».
  У обоих WhatToPay0,AllowMinusBalance=false,NeedVerifyGuest=false,ExchangeRate1; Day/Week/MonthLimit=null,stopFurtherExecution=false.
  Это два действия настройки, не обещание всем клиентам100% и не сумма лимитов150%. Итог зависит от категории/обработки/витрины.
- В periodic кампании назначения категорий ResetBalanceAction BonusExpirationPeriodInDays90,HourOfReset0,RetryPeriod0.
  TimetableCondition Mode0,SelectedDayOfMonth0,DaysOfWeek0,AnnuallyDay0,AnnuallyMonth0.

## Уточнение семантики по интерфейсу iiko — 07.10.2026

Источник: [карточка назначения категорий](https://iiko.biz/en-GB/MarketingCampaigns/Edit/01330000-6bec-ac1f-583c-08da0d57f147?corporateNutritionOrganizationId=01330000-6bec-ac1f-2714-08da0d57a021),
UI 9.8.7.5194, сервер 9.8.7.6195. Просмотр без сохранения.
Подробности и границы — [технические возможности iiko](../../teplo-iiko-promotion-capabilities/references/current-constraints-2026-10-07.md).

- Для прочитанного `OrderSumHistoryCondition` подтверждено: `Period=0` —
  «за всё время», `Operation=4` — «больше или равно»; подсказка включает текущий заказ.
  Не переносить расшифровку на другие типы условий.
- В UI номера блоков: **1 — Золото ≥30000; 2 — Серебро ≥10000; 3 — Бронза ≥0**,
  у каждого остановка включена. API-массив сохранён как **0,10000,30000**.
  Это различие представлений: индекс массива не доказал приоритет исполнения.
  Не переставлять исходный JSON и не менять рабочие правила по этому расхождению.
- Периодическая настройка показывает **ежедневно** (`Timetable.Mode=0` в этой привязке),
  сгорание неиспользованных начислений **через 90 дней с даты начисления, в 00:00**.
  Часовой пояс и фактическое исполнение по журналу не проверены; 90 дней не равны
  трём календарным месяцам и не зависят от выключенного lifetime Starter.

## Остальные границы семантики

- ProductsFilter,AffectedGuest,PresenceType,WhatToPay,MaskType,ApplyMaskFilter,
  SpecialPriceType сохранять raw до предметной расшифровки.
- В UI вкладка овердрафта показывает «разрешён, на сумму баланса»; это не отменяет
  `PaymentAction.AllowMinusBalance=false` и не доказывает разрешённый отрицательный баланс.
- `stopFurtherExecution` останавливает дальнейшие блоки после успешного условия.
  Порядок в UI и фактическое исполнение проверять отдельно от сериализации API.
- Условия купона и категорий не равны автоматической скидке каждому клиенту.
  Название «первый» не заменяет проверку условия.

## Реестр программ (все13 имеют isActive=true в этом ответе)
| Программа | programId | Кампании / действия |
|---|---|---|
| [Foodmarket Тепло] Бонусная система1 | 01330000-6bec-ac1f-2714-08da0d57a021 | SetGuestCategoryAction, RemoveGuestCategoryAction, ResetBalanceAction, OverdraftAction, RefillWalletPercentAction, PaymentAction |
| [Foodmarket Тепло] Калифорния в подарок1 | 01330000-6bec-ac1f-8113-08da0d8f333f | PresentBonusAction |
| [Foodmarket Тепло] Три большие пиццы | 03650000-6bec-ac1f-0cd9-08da65c45fa4 | PresentBonusAction |
| [Foodmarket Тепло] Калифорния с лососем | 03650000-6bec-ac1f-8510-08da65c4d18a | PresentBonusAction |
| [Foodmarket Тепло] Калифорния с крабом в подарок | 01330000-6bec-ac1f-2136-08da79d4bc86 | PresentBonusAction |
| [Foodmarket Тепло] Камчатский в подарок | 01330000-6bec-ac1f-b668-08da80556c12 | PresentBonusAction |
| [Foodmarket Тепло] Нагасаки в подарок | 01330000-6bec-ac1f-041e-08dae109f1a4 | PresentBonusAction |
| Грибная | 03650000-6bec-ac1f-39b9-08db5a1536bf | PresentBonusAction |
| Скидка 100% на доставку с сайта | 03650000-6bec-ac1f-da38-08dbd3afbaf9 | SpecialPriceAction |
| Мясное трио 2.0 | 01330000-6bec-ac1f-a60c-08dbe1216d0d | нет действий в bindings |
| Скидка при первом заказе | 01330000-6bec-ac1f-c8ea-08dd91293c85 | OrderPercentDiscountAction, SetGuestCategoryAction |
| Купоны квиз пицца | 01330000-6bec-ac1f-95eb-08de88ca9879 | PresentBonusAction |
| Купоны квиз сет | 01330000-6bec-ac1f-ae72-08de88dfa5df | PresentBonusAction |

## Дополнительные подтверждения
- «Промокод Первый15%»: OrderPercentDiscountAction DiscountPercent15,AppliedToDiscountedItems=false; условие CouponSeriesId и действие назначения категории. Есть отдельный category guard с пустым actions и stopFurtherExecution=true.
- «Скидка100% на доставку с сайта»: DeliveryCondition.SourceMask site/site1; SpecialPriceAction для кодов00732..00737. Для перевода масок/типов в клиентское правило нужна UI/DTO семантика.
- Полученные PresentBonusAction задают Items.Code/Sizes,IncludeModifiers и связанный CouponSeriesId. Не считать их Starter шкалой подарков без сопоставления.
- Категории подтверждены последующим HTTP200 от 2026-10-07T19:48:14Z: 15 записей, точное сопоставление ID и имён. Категория «Сотрудники» активна и не назначается новым гостям по умолчанию. Серии неактивированных купонов не получены (400/timeout), это не доказательство отсутствия купонов. Данные клиентов не читали.
- payment_types: INET «Оплата бонусами» типаIikoCard связан с campaignId бонусной системы; combinable=true — совместимость оплаты, не всех акций.
- discounts: ДР15 и ДРсотрудников30 категоризированы; корпоративы15 minOrderSum2500. «Скидка для доставки» и «Сервисный сбор» FlexibleSum:percent1/−1 не считать постоянной ставкой.

## Переносимая запись доказательства
Для каждого правила хранить source_url/endpoint,метод,organizationId,fetched_at UTC,raw SHA256,programId,walletId,campaignId,bindingId,тип массива bindings,порядок,typeName,settings raw+decoded,active и даты.
Отдельно: external provider и витрина, source UI, факт применения по order/SKU, состояние неизвестных enum и дата последней проверки.
При snapshot-обновлении перечитать оба слоя. Нельзя считать isActive/пустое appliedOrganizations доказательством всеобщей применимости.


## Отдельные скидки кассы iiko

Все пять записей не удалены (`isDeleted=false`). Это справочник кассы, не доказательство
публичной рекламы, автоматического применения или доступности всем гостям.

| Запись | Настройка | Категории / ограничения |
|---|---|---|
| День рождения 15% | `mode=Percent`, 15%, manual/card, `isAutomatic=false` | Акционные товары, Напитки, Пицца, Закуски, Шаурма, Сеты, Роллы, Все блюда; minOrderSum=null. Окно ДР этим DTO не задано. |
| Скидка 30% ДР сотрудников | `mode=Percent`, 30%, manual/card, `isAutomatic=false` | Пицца, Закуски, Шаурма, Сеты, Роллы; minOrderSum=null. Не переносить исключённые напитки/акционные товары из общей ДР-скидки. |
| Новогодние корпоративы скидка 15% от 2500 | `mode=Percent`, 15%, manual, `isAutomatic=false` | Порог 2500; Напитки, Пицца, Закуски, Шаурма, Сеты, Роллы, Все блюда. В ответе не подтверждено сезонное окно или действующая публикация. |
| Скидка для доставки | `mode=FlexibleSum`, percent=1, manual/card | Не фиксированная скидка 1%; фактическую величину задаёт применение. |
| Сервисный сбор | `mode=FlexibleSum`, percent=-1, card, `isAutomatic=false` | Не доказанный сбор 1%. Нужны сумма применения и настройка сбора в Starter. |

## Что реально встречалось в учёте

OLAP 01.07–30.09.2026, Department.Code=2, NOT_DELETED; полные фильтры, ID и
сверки включены в JSON, раздел `applied_types_history`. Выручка до/после скидок
сошлась с canonical-контролем всех трёх месяцев.

| Тип события | Дни применения: июль / август / сентябрь |
|---|---|
| iikoCard | 5 / 9 / 6 |
| День рождения 15% | 19 / 16 / 14 |
| Скидка 100% на доставку с сайта | 10 / 7 / 4 |
| Скидка 30% ДР сотрудников | 3 / 0 / 1 |
| Скидка iikoCard | 31 / 31 / 30 |
| Скидка для доставки | 31 / 31 / 30 |
| Сервисный сбор (`ItemSaleEventDiscountType`) | 0 / 24 / 30 |

Дни не равны заказам, клиентам или числу скидок. `iikoCard` и `Скидка iikoCard` имеют
разные GUID. Прямое соответствие этим типам конкретных Starter-акций не получено.
Поле DiscountSum выше gross−net на 7360 ₽ в августе и 12102 ₽ в сентябре; не подменять
им денежную скидочную нагрузку. Правила расчёта — в [экономике](economics-and-compatibility.md).
