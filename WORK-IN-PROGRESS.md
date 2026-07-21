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

### agent-payroll-rounding — ветка `agent/payroll-rounding-five`
- worktree: `../Teplo-agent-payroll-rounding`
- compose: agent-b (API 8010 / web 5183 / pg 5442, тест `teplo_test_b`)
- трогает: расчёт итоговой зарплаты сотрудника и связанные payroll-тесты
- НЕ трогать другим: точку финального округления `total_payable` до 5 ₽
- статус: в работе

### agent-c — ветка `agent/c-couriers`
- worktree: `../Teplo-agent-c`
- compose: agent-c (API 8020 / web 5193 / pg 5452, БД `teplo`, тест `teplo_test_c`)
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

---

## Shared-ресурсы — кто сейчас держит

- **Миграции alembic** (`alembic upgrade head`): сериализованно, один за раз. Держатель: agent-c (head `0104`)
- **Тестовая БД `teplo_test`**: default-стек. Второй агент → `teplo_test_b` (compose agent-b).
- **Порты**: API 8000 / web 5173 (default); API 8010 / web 5183 (agent-b); API 8020 / web 5193 (agent-c); API 8040 / web **7153** (agent-payments).
