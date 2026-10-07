# Техкарты, номенклатура и граммовки

Пути ниже относительны корню проекта; `<inventory-skill>` — установленный `iiko-inventory-from-scans`.
Карта API зафиксирована 18.05.2026; inventory reference проверен 22.07.2026.
Это проверка локальных контрактов и кода, а не гарантия свежести каждой выгрузки.
Для текущего блюда нужны датированный ответ iiko и контекст подразделения/размера.

## 1. Сначала определить объект и дату

1. Зафиксировать дату действия D, подразделение, размер, нужную порцию и вид результата.
2. Отличать продаваемый ролл, заготовку, модификатор, сет и сырьё.
3. Найти кандидатов по названию/артикулу, затем работать по GUID номенклатуры.
4. Развести `productId`/`assembledProductId` (номенклатура) и `chart.id` (версия техкарты).
5. Не брать первую одноимённую запись и не склеивать разные GUID по похожим названиям.
6. Удалённый сегодня продукт может быть нужен для исторических продаж/версий.
7. За период со сменой рецептуры разбивать расчёт по версиям и датам продаж.
8. Текущее включение в меню не доказывает доступность блюда в прошлом.

## 2. Каталог и поиск GUID

Канонический полный каталог: GET `/resto/api/v2/entities/products/list`.
Для истории передавать `includeDeleted=true`; для текущих кандидатов исключать deleted явно.
Локальная WADL-карта перечисляет `ids`, `nums`, `codes`, `types`, `categoryIds`, `parentIds`.
Параметр поиска `q` в этом Server endpoint не подтверждён; искать локально в ответе.
Формат списковых параметров уточнять по актуальному контракту, не придумывать сериализацию.
Сохранять `id`, `name`, `code`, `num`, `type`, `parent`, `deleted`, `mainUnit`.
Для веса и вариантов также сохранять `unitWeight`, `unitCapacity`, `containers`, `productScaleId`.
Для контекста меню сохранять `defaultIncludedInMenu`, `placeType`, `modifiers`.
`defaultSalePrice` — поле каталога, не доказательство фактической цены продажи в периоде.
`GOODS`, `PREPARED`, `DISH`, `MODIFIER`, `SERVICE` — типы, а не маркетинговые категории.
Группа/название «Роллы» и тип DISH требуют проверки по каталогу и составу.

Локальная таблица: `IikoProduct`, `apps/api/app/models/iiko_product.py`, `iiko_product`.
Её `id` — внутренний UUID приложения; во внешний iiko передавать **`iiko_id`**.
Она хранит name/code/type/main_unit_guid/unit/deleted/synced_at, но не полную техкарту/вес.
Читающий поиск: GET `/api/v1/warehouse/products` → `list_products` в routes/warehouse.py.
Параметры: `q`, `type`, `include_deleted`, `limit`; default `type=GOODS` скрывает блюда.
Для роллов использовать `type=DISH`, для всех типов — `type=`; проверять обрезку по limit.
Default limit=500, максимум 2000, пагинация в этом методе не реализована.
`q` ищет подстроку name/code без учёта регистра, а не идентичность рецептуры.
Endpoint не возвращает synced_at; свежесть кэша при необходимости читать из таблицы.

`iiko_product_sync._fetch_catalogue_sync` читает products/list с includeDeleted=true.
`_fetch_units_map` читает `/v2/entities/list?rootType=MeasureUnit`.
В `sync_iiko_products` AppSetting `iiko.unit_guid_map` перекрывает справочник единиц.
Для единицы хранить GUID, исходную метку и источник override, если он применён.
`synced_at` в этой таблице обновляется на каждой обработанной записи успешного sync.
Это не история изменений и не время начала действия рецептуры.
`sync_iiko_products` делает commit: не запускать его как часть read-only анализа.
`iiko_inventory.fetch_products_catalog(session=...)` также может обновить AppSetting-кэш.
Даже `refresh=False` не гарантирует отсутствие записи, если кэш отсутствует/устарел.

`<inventory-skill>/scripts/iiko_data.py::product_records` полезен для простого справочника.
Он исключает deleted, дедуплицирует по id и оставляет ограниченный набор полей.
Для исторических карт/size/weight использовать исходный богатый JSON, не только его вывод.
`normalize_name` помогает искать кандидатов; это не разрешение автоматически объединять GUID.
`normalize_unit` нормализует подписи кг/л/шт, но не пересчитывает количества.
iikoCloud `/api/1/nomenclature` — опубликованное меню, не полный складской справочник Server.

## 3. Читающие методы техкарт

Все пути в таблице относительны `/resto/api`; применять только GET.
| Метод | Параметры | Назначение |
| --- | --- | --- |
| `/v2/assemblyCharts/getTree` | date, productId, departmentId | Дерево на точную дату и готовое разложение |
| `/v2/assemblyCharts/getAssembled` | date, productId, departmentId | Исходная карта на дату |
| `/v2/assemblyCharts/getPrepared` | date, productId, departmentId | Подготовленное разложение |
| `/v2/assemblyCharts/getHistory` | productId, departmentId | Поиск версий продукта |
| `/v2/assemblyCharts/byId` | id | Конкретная версия по ID техкарты |
| `/v2/assemblyCharts/getAll` | dateFrom, dateTo, includeDeletedProducts, includePreparedCharts | Кандидаты за интервал |
| `/v2/assemblyCharts/getAllUpdate` | knownRevision и параметры getAll | Инкрементальная загрузка при полном исходном снимке |

Проверенный локальный вызов getTree: `iiko_server_inventory.py`, ветка `prepared-recipes`.
Он передаёт ISO дату `YYYY-MM-DD`, productId и departmentId; разбирает JSON из bytes.
Его CLI пишет output-файлы; использовать как образец вызова, а не запускать бездумно целиком.
Отличать HTTP 200/валидный JSON от наличия нужной карты и полного дерева.
`getAllUpdate` без исходного snapshot и обработки deleted-ID не даёт полный каталог карт.
`byId` принимает ID карты из ответа/history, не GUID блюда.
Только наличие метода в локальной WADL не доказывает полноту его ответа для всех вариантов.

## 4. Дата, подразделение, склад, размер

`prepare_inventory.chart_is_active`: `dateFrom <= D` и (`dateTo` пусто или `D < dateTo`).
В этом helper неверные даты дают неактивную карту; границы сохранять в исходном виде.
Требовать ровно одну подходящую корневую версию после выбора контекста; неоднозначность — ошибка.
Для спорной границы проверять датированный getTree/getAssembled и соседние версии/history.
`goods_classifier._chart_overlaps` использует пересечение месяца с **включительным** dateTo.
Он пригоден для поиска возможных связей, но не для точного выбора действующей версии.
`departmentId` — GUID подразделения; это не числовой Department.Code из OLAP.
`storeId` — GUID склада; это не departmentId и не terminalGroupId iikoCloud.
Справочники: GET `/corporation/departments` и `/corporation/stores` с includeDeleted по задаче.
Не выбирать склад/подразделение по первой строке; сохранить подтверждённую привязку к точке.

В сырых картах присутствуют `effectiveDirectWriteoffStoreSpecification` и `productWriteoffStrategy`.
В строках встречаются `storeSpecification` и `productSizeSpecification`.
У store specification сохранять исходный объект, включая departments/inverse, если он есть.
Не реализовывать собственное толкование include/exclude при непроверенной семантике.
Размер связан с `productSizeAssemblyStrategy`; в локальных ответах есть COMMON и SPECIFIC.
Непустой `productSizeSpecification` в проверенных ответах — строковый GUID, не вложенный объект.
Для размеров доступны GET `/v2/entities/productScales` и `/v2/entities/productScales/{productScaleId}`.
Также карта перечисляет GET `/v2/entities/products/{productId}/productScale`.
У getTree/getAssembled в проверенной карте нет query-параметра sizeId: не изобретать его.
Если ответ содержит строки разных размеров, не суммировать их вместе; сначала подтвердить выбор.
Нулевые/пустые selectors не толковать автоматически как «все размеры» без контракта.

## 5. Выход, брутто/нетто и единицы

Корень `assemblyCharts`: id, assembledProductId, dateFrom/dateTo, **assembledAmount**, items.
Строки исходной карты: productId, amountIn, amountMiddle, amountOut, selectors.
В ответах также есть amountIn1/amountOut1 … amountIn3/amountOut3, packageCount/packageTypeId.
Не складывать альтернативные колонки 1…3 с основными и не игнорировать их смысл при ненулевых значениях.
Сохранять technologyDescription, description, appearance, organoleptic, outputComment как текст источника.
`assembledAmount` — знаменатель выхода карты; единицу выхода проверять по продукту/контракту.
Число 1 само по себе не означает один грамм, один кусочек или один стандартный ролл.
Локальный код не подтверждает физические единицы всех amountIn/Middle/Out и unitWeight.
Поэтому не подписывать эти числа «г» только по имени поля или порядку величины.
Для таблицы брутто/нетто/выхода подтвердить mapping колонок и масштаб по DTO/экспорту iikoOffice.
До подтверждения показывать исходные поля и единицы как непроверенные, без итогового веса в граммах.

Практическая проверка масштаба: получить экспорт/печатную техкарту iikoOffice той же
версии, даты, подразделения и размера; сопоставить строки по ингредиенту и колонки
«брутто в ед. изм.», вес брутто, нетто и выход. Сверить хотя бы ингредиент в кг,
ингредиент в штуках и вложенную заготовку, если они есть. Сохранить доказанный mapping
API-поле → колонка → единица → коэффициент. Одно похожее число без подписанной
колонки не подтверждает пересчёт; контрольное взвешивание проверяет фактическую
закладку, но не заменяет документированную семантику API.

Если величина `a` подтверждена на весь выход `Q=assembledAmount`, то на q единиц выхода: `a*q/Q`.
Применять эту формулу лишь к **исходной batch-карте**, после выбора size/store и единиц.
Если подтверждена единица кг: `g=kg*1000`; если штуки — нужен подтверждённый вес штуки.
Для литров нужен подтверждённый коэффициент массы/плотности; литр не равен килограмму автоматически.
`unitWeight`/`unitCapacity`/упаковки — возможные источники коэффициента, требующие проверки единиц.
Сумма сырьевого брутто не равна выходу готового блюда: потери, жидкость, заготовки и упаковка различаются.
Не восстанавливать выход простым суммированием расхода GOODS и не применять потери второй раз.

## 6. Вложенные заготовки и готовое разложение

`getTree` возвращает assemblyCharts и preparedCharts; это разные представления одного состава.
По inventory reference, flattened `preparedCharts.items[].amount` дан на **одну базовую единицу** продукта.
Проверенная формула `prepare_inventory.py`: `leaf_qty = product_qty * prepared_item.amount`.
На этой ветке assembledAmount проверяется >0, но НЕ является повторным делителем коэффициента.
У проверенных preparedCharts есть amount в строках и нет assembledAmount на корне.
Не складывать исходный корень, вложенные карты и flattened листья: это двойной/тройной расход.
Для плоского сырьевого результата агрегировать одинаковые leaf productId в подтверждённых единицах.
Helper требует один active root, одну active prepared-карту, непустые items, известные GOODS/единицы.
Дополнительно сверять assembledProductId prepared-карты с запрошенным продуктом, если поле есть.
Также проверяет неотрицательный coefficient; ошибки не превращать в нулевой расход.
Его проверки количества карт недостаточны для неподтверждённого смешения размеров в items.
Если разворачивать дерево самостоятельно, проверять циклы, версии каждого узла и совместимость единиц.
Сохранять цепочку parent→child и коэффициенты без промежуточного округления.
GOODS-разложение полезно для расхода/себестоимости; оно теряет идентичность роллов внутри сета.

## 7. Сеты, блюда и связь с продажами

Набор может быть DISH, состоящим из других DISH; отдельного доказанного типа SET здесь нет.
Для состава сета сохранять непосредственные дочерние блюда до разворачивания в сырьё.
Связь DISH→DISH сама по себе не доказывает сет: это может быть технологическая заготовка блюда.
Проверять название/группу, техкарту, размер, количество порций и наблюдаемый формат продажи.
Модификаторы из products[].modifiers/childModifiers — возможные связи, не обязательные ингредиенты каждой продажи.
Доказательство доступной опции не равно фактически выбранной опции в заказе.
Структурная оценка выпуска: `sold_parent_qty * child_qty_per_parent`, по версиям/размерам на дату продажи.
Она не доказывает фактический выпуск при заменах/отменах и не создаёт дополнительную выручку.
Не складывать parent sales и уже построчно отражённые children как независимые продажи.
`build_menu_usage_snapshot` объединяет связи assembly+prepared+modifiers и игнорирует количества/size/store.
Он ограничивает списки названий 12 значениями и использует текущее defaultIncludedInMenu.
Использовать его для discovery; не выдавать за полный состав, граммовку или историческую доступность.

## 8. Что вернуть и как проверить

Паспорт: fetched_at, API/файл, D/период, department/store/size, product GUID, chart IDs, интервалы действия.
Таблица: продукт/ингредиент GUID, тип, исходная единица, выбранная колонка, batch-выход, количество на порцию.
Отдельно: состав блюда/сета, плоский расход GOODS, заявленный выход и статус подтверждения граммовки.
Статусы: confirmed / ambiguous_version / unknown_unit / unresolved_size / missing_recipe / stale_snapshot.
Контроли: одна версия, положительный выход, разрешённые GUID, единицы, нет циклов и двойного разложения.
Для исторического месяца сравнить версии с датами продаж; свежий каталог не заменяет историю.
Старые research/raw/iiko/menu и promo_sets полезны для формы DTO и воспроизводимости старого расчёта.
Их числа, цены, ассортимент и состав не переносить в текущий ответ без нового as-of подтверждения.
Не вызывать save/update/delete техкарт, sync продуктов, импорт инвентаризации или P&L sync.

## Проверенные опорные источники

- `app-spec/integrations/iiko/server-api-endpoints.md`: assemblyCharts, products/list, scales, dictionaries.
- `apps/api/app/models/iiko_product.py`; `apps/api/app/services/iiko_product_sync.py`.
- `apps/api/app/api/v1/routes/warehouse.py::list_products` — внутренний каталог с фильтрами/лимитом.
- `apps/api/app/services/iiko_inventory.py::fetch_products_catalog/_write_products_cache` — скрытая запись кэша.
- `apps/api/app/services/pnl/iiko_sync.py::fetch_goods_metrics` — реальные GET products/getAll, не безопасный sync-wrapper.
- `apps/api/app/services/pnl/goods_classifier.py::_chart_overlaps/build_menu_usage_snapshot` — пределы графа связей.
- `<inventory-skill>/references/api-methods.md:41`; `scripts/iiko_server_inventory.py` (prepared-recipes).
- `<inventory-skill>/scripts/prepare_inventory.py::chart_is_active` и блок 643–745; `scripts/iiko_data.py::product_records`.
