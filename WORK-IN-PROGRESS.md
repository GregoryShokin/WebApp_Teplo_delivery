# WORK-IN-PROGRESS — доска координации агентов

Заполняй ПЕРЕД началом работы. Правила:

1. `git pull` (или `git fetch && git rebase`) → прочитать эту доску.
2. Объявить свою зону (ветка + какие файлы/папки трогаешь) → `git add WORK-IN-PROGRESS.md && git commit && git push`.
3. Если твоя зона пересекается с чужой активной — НЕ править молча, согласовать.
4. Чужие незакоммиченные файлы не трогать. `git add -A` в общей рабочей папке запрещён — работай в своём worktree.
5. После завершения задачи — убрать свой блок отсюда.

Каждый агент работает в отдельном git worktree (см. `scripts/agent-worktree.sh`),
поэтому незакоммиченные файлы физически изолированы. Эта доска — про намерения и
shared-ресурсы (БД, Docker, миграции, тесты), которые worktree НЕ изолирует.

---

## Активные зоны

<!-- ШАБЛОН — копируй блок ниже
### agent-<имя> — ветка `agent/<задача>`
- worktree: `../Teplo-agent-<имя>`
- compose: default | agent-b (порт API, имя БД)
- трогает: <пути>
- НЕ трогать другим: <критичные файлы>
- статус: <в работе / на ревью>
-->

### agent-depositdedupe — ветка `agent/depositdedupe-deposit-payroll-double-payout`
- worktree: `../Teplo-agent-depositdedupe`
- compose: стенд не поднимаю; отдельная временная test-БД
- трогает: взаимные гарды выдачи депозита через ведомость и отдельный банк-черновик в
  `deposit_schedule.py`, `deposit_bank_draft.py`, `routes/deposits.py`, `routes/employees.py`,
  `payroll_runner.py` и связанных тестах
- НЕ трогать другим: эти депозитные гарды до завершения задачи
- статус: в работе

### agent-balance — ветка `agent/balance-as-of-foundation`
- worktree: `../Teplo-agent-balance`
- compose: стенд не поднимаю; тестовая БД `teplo_test_balance` (контейнер `teplo-postgres`, порт 5432)
- задача: фундамент модуля «Баланс» — научить контуры отдавать остаток НА ДАТУ (сегодня почти всё
  считает «на сейчас») + гигиена данных до первого снимка. Сам модуль баланса в этой ветке НЕ строю.
- трогает:
  - деньги: `app/api/v1/routes/dds.py` (`_wallet_movement_deltas` уезжает в сервис с `as_of`),
    новый `app/services/wallet_balances.py`, копии логики в `app/services/kassa/payouts.py` и
    `app/services/payroll_payouts.py`
  - ОС: `app/services/asset_balance.py` (фильтр «карточка существовала на дату»),
    `app/api/v1/routes/fixed_assets.py` (чтение снимка для закрытых месяцев)
  - люди: `app/api/v1/routes/accounting_suppliers.py` (staff-payable — исторический срез),
    даты у транзакций депозита/накопительного фонда
  - контрагенты: `app/services/counterparty_balance_as_of.py` (бартер, `receivable`, единый предикат)
  - гигиена: `app/services/banking/classifier.py` (prebooked в `apply_operation_split`),
    `app/services/counterparty_registry.py` (замок в `void_invoice`),
    `app/scripts/writeoff_pre_accounting.py`
- НЕ трогать другим: `_wallet_movement_deltas` и его вызовы, `asset_balance.balance_lines`,
  `build_balance_as_of`, `apply_operation_split`
- смежники, с кем сверяюсь: `agent/hourcap-*` (payroll_calculator.py — рядом с моим staff-payable)
- статус: ОС, деньги и гард prebooked сделаны (3 коммита, 2993 зелёных); в работе — люди на дату
- **ВНИМАНИЕ соседям:** формула остатка кошелька уехала из `routes/dds.py` в
  `services/wallet_balance_as_of.py`. Своих копий больше не заводить — их было четыре, и одна
  успела разойтись по смыслу. Нужен остаток на дату — зовите `wallet_balance_as_of(session,
  wallet, as_of=...)`, нужен итог — `build_money_balance_as_of`.
- полный прогон гоняю на своём Postgres: контейнер `teplo-pg-balance`, порт **5512**,
  `max_connections=800` + `fsync=off`. На общем `teplo-postgres` (5432) полный набор даёт
  ~1100 фальшивых падений на исчерпании соединений и идёт 23 минуты вместо 12.

### agent-c — ветка `agent/c-couriers`
- worktree: `../Teplo-agent-c`
- compose: agent-c (web 5203 / api 8030 / pg 5462, БД `teplo`, тест `teplo_test_c`)
- трогает: модуль «Курьеры» — объединённая страница «Смена» (инбокс + депозиты + оценки):
  - back: `app/models/courier_shift_day.py`, `app/services/couriers/shift_day_service.py`,
    `app/api/v1/routes/couriers.py` (shift-day роуты), `app/schemas/couriers.py`,
    `alembic/versions/0104_courier_shift_day.py`, `app/models/__init__.py`
  - front: `routes/couriers/{shift,shift-inbox,deposits,evaluations}.tsx`, `lib/api.ts` (хвост),
    `lib/permissions.ts` (секция `couriers.shift`), `components/layout/AppLayout.tsx` (пункт «Смена»),
    `router.tsx` (раздел `/couriers`)
- НЕ трогать другим: миграция `0104` (head), пункт сайдбара «Смена», секция прав `couriers.shift`
- статус: реализовано, проверено в стеке teplo-c; не закоммичено

### agent-payments — ветка `agent/payments-finance-payments`
- worktree: `../Teplo-agent-payments`
- compose: agent-payments (pg 5472 / api 8040 / web **7153**, БД `teplo`, тест `teplo_test_payments`, проект `teplo-payments`)
- трогает: контур «Платежи» (FEAT-003) — агрегатор исходящих платежей + перенос «Страницы на оплату» в «Финансы» + плавающая FAB-модалка активных платежей:
  - back: новый сервис/роут агрегатора платежей, расширение `DraftRead` (`counterparties.py`),
    возможные ридонли-эндпоинты поверх `CounterpartyPaymentDraft` / `SafeAllocation` / intake
  - front: `routes/payment-page/*` (переезд/переименование в «Финансы → Платежи»),
    новая FAB-модалка активных платежей, `router.tsx`, `AppLayout.tsx` (меню «Финансы»), `lib/permissions.ts`
- НЕ трогать другим: пункт меню «Платежи», секция прав платежей, compose `agent-payments` и порт 7153
- статус: в работе (разведка завершена, уточняю ТЗ)

### agent-finance-workbench — ветка `agent/finance-workbench-proddata-preview`
- worktree: `../Teplo-agent-finance-workbench`
- compose: `teplo-finance-workbench` (web 5203 / api 8050 / pg 5482)
- трогает: дальнейшая доработка финансового функционала на изолированной копии production-БД
- НЕ трогать другим: контейнеры/порты 5203, 8050, 5482 и локальный prod-data snapshot
- статус: долгоживущее превью по запросу пользователя

---

## Shared-ресурсы — кто сейчас держит

- **Миграции alembic** (`alembic upgrade head`): сериализованно, один за раз. Держатель: agent-c (head `0104`)
- **Тестовая БД `teplo_test`**: default-стек. Второй агент → `teplo_test_b` (compose agent-b).
- **Порты** — формула слота k: web `5173+10k` / api `8000+10k` / pg `5432+10k`.
  Занятые слоты (сверено с compose-файлами 26.07):

  | слот | стек (compose) | web | api | pg |
  |------|----------------|-----|-----|-----|
  | 0 | default (`docker-compose.yml`) | 5173 | 8000 | 5432 |
  | 1 | agent-b | 5183 | 8010 | 5442 |
  | 2 | agent-periods | 5193 | 8020 | 5452 |
  | 3 | agent-c | 5203 | 8030 | 5462 |
  | 4 | agent-payments | **7153** (запрос владельца, не 5213) | 8040 | 5472 |
  | 4 | preview-taxes (ветка `agent/tax-taxes`) | 5213 | 8040 | 5472 |
  | 5 | agent-partial | **7163** (не 5223) | 8050 | 5482 |
  | 6 | agent-dds | 5233 | 8060 | 5492 |
  | 7 | preview-ic | 5243 | 8070 | 5502 |

  Слот 4 держат сразу два стенда (agent-payments и preview-taxes) — одновременно не поднять,
  согласуйте очередь. Следующий свободный слот — 8 (web 5253 / api 8080 / pg 5512).

  **Живые контейнеры расходятся с таблицей — сверено `docker ps` 26.07:**

  - `teplo-web-finance-workbench` держит **web 5203**, а по таблице это порт agent-c (слот 3).
    Стенд долгоживущий, на копии прод-БД, порты зафиксированы владельцем — двигать его не надо;
    поднимать agent-c с web 5203 сейчас нельзя, берите свободный слот 8.
    Api/pg этого стенда (8050/5482) — из `agent-partial`, то есть он сидит на двух слотах разом.
  - `teplo-*-taxes` поднят из `apps/docker-compose.preview-taxes.yml`, который **не в git**
    (untracked у `Teplo-agent-tax`) и занимает api 8040 / pg 5472 — порты agent-payments.
  - Контейнер, поднятый до правки compose-файла, сохраняет СТАРЫЙ маппинг портов и старое
    окружение: изменения подхватятся только при пересоздании (`up -d --force-recreate`).

  **iiko: заглушка `IIKO_SERVER_BASE_URL=http://iiko-disabled.invalid` стоит во всех стендах,
  кроме `preview-ic` и основного `docker-compose.yml`.** В общем `../.env` прописан боевой сервер
  iiko, поэтому без заглушки превью умеет писать в боевую систему. У `preview-ic` живой iiko —
  назначение ветки (унификация накладных на iiko Cloud), там это осознанно.
