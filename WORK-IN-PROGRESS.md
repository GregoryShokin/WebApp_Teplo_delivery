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

### agent-dds-payroll — ветка `agent/dds-payroll-link`
- worktree: `../Teplo-agent-dds-payroll`
- compose: agent-dds (API 8030 / web 5203 / pg 5462, БД `teplo`, тест `teplo_test_dds`) — изолированный стек
- трогает: связка «журнал ДДС → расчёт ЗП» (привязка операции к сотруднику = «уже выплачено»):
  - back: миграция `0173_dds_employee_payout_offset` (HEAD), `models/payroll.py` (EmployeePayoutOffset
    + offset_amount + payroll_line.employee_payout_offset), `models/__init__.py`,
    `services/payroll_employee_payout_offset.py` (new), `services/payroll_runner.py` +
    `services/payroll_admin.py` (offset-шаг), `services/banking/classifier.py` (атрибуция),
    `services/new_payment.py` (список сотрудников), `api/v1/routes/dds.py`, `schemas/dds.py`
  - front: `routes/dds/OperationReviewDialog.tsx`, `routes/payroll/admin-run-detail.tsx`, `lib/api.ts` (хвост)
- НЕ трогать другим: миграция `0173` (HEAD), offset-секции в `payroll_runner`/`payroll_admin`
- статус: реализовано + протестировано в стеке teplo-dds (159+19+5 тестов, e2e), не влито

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

---

## Shared-ресурсы — кто сейчас держит

- **Миграции alembic** (`alembic upgrade head`): сериализованно, один за раз. Держатель: agent-c (head `0104`)
- **Тестовая БД `teplo_test`**: default-стек. Второй агент → `teplo_test_b` (compose agent-b).
- **Порты**: API 8000 / web 5173 (default); API 8010 / web 5183 (agent-b); API 8020 / web 5193 (agent-c).
