# MVP P0-P4 smoke test report

Дата: 2026-05-27

## Итог

✅ P2 не был отдельным commit'ом, но базовая реализация Auth + Settings + audit history уже была в дереве.  
✅ Недостающие P2 test artifacts дозаполнены: добавлены `apps/api/tests/test_auth.py` и `apps/api/tests/test_settings.py`.  
✅ Clean Docker Compose smoke пройден после точечных фиксов dev/API запуска.  
✅ Все 4 миграции накатываются на чистую БД.  
✅ Backend tests: 41 passed, 3 skipped.  
✅ Frontend routes `/login`, `/staff`, `/settings`, `/payroll/runs` открываются без console warning/error.

## P2 audit

| Пункт | Статус | Детали |
| --- | --- | --- |
| `apps/api/app/auth/` | ✅ | Есть JWT utilities, bcrypt hashing/verify, `current_user`, `require_role`. |
| Auth endpoints | ✅ | `POST /api/v1/auth/login`, `POST /api/v1/auth/refresh`, `POST /api/v1/auth/logout`. |
| Roles | ✅ | Seed содержит `admin`, `owner`, `finance_manager`, `manager`, `accountant`. |
| Settings routes | ✅ | `GET /settings`, `GET /settings/{key}`, `PUT /settings/{key}`, `GET /settings/{key}/history`. |
| Settings service | ✅ | Read/write/history есть, запись создаёт `app_setting_history`. |
| Critical settings owner-only | ✅ | `balance_close_deadline`, `fixed_asset_threshold`, `repair_vs_modernization_pct` и namespace-варианты закрыты на owner. |
| P2 tests | ✅ | Был общий `test_auth_settings.py`; добавлены отдельные `test_auth.py` и `test_settings.py`. |
| Frontend routes | ✅ | `apps/web/src/routes/login.tsx`, `apps/web/src/routes/settings.tsx` есть и smoke-пройдены. |

## Bugs found

| Priority | Статус | Описание |
| --- | --- | --- |
| P1 | ✅ fixed | API контейнер падал при старте: `JWT_SECRET_KEY` в `apps/docker-compose.yml` был короче `min_length=32`. |
| P1 | ✅ fixed | `apps/Makefile` не содержал `migrate` и `test`, хотя smoke-инструкция на них опирается. |
| P2 | ✅ fixed | `GET /api/v1/employees` без trailing slash давал 307, теперь `/employees` и `/employees/` оба возвращают 200. |
| P2 | ✅ fixed | Отдельных P2 test artifacts `test_auth.py` / `test_settings.py` не было; добавлены проверки refresh/logout/settings history. |
| P3 | ⚠️ skipped | `POST /api/v1/employees/sync` не прогонялся в clean smoke: iiko credentials не настроены, а HTTP endpoint не имеет mock-mode. |

## Commands and results

| Команда / проверка | Результат |
| --- | --- |
| `(cd apps && docker compose down -v)` | ✅ clean Postgres volume removed |
| `(cd apps && docker compose up -d)` | ✅ postgres/api/web started |
| `TEPLO_ADMIN_PASSWORD=admin-password-for-smoke make -C apps migrate` | ✅ applied `0001_core_domain` → `0004_payroll` |
| `make -C apps test` | ✅ 41 passed, 3 skipped |
| `POST /api/v1/auth/login` | ✅ 200, admin token получен |
| `GET /api/v1/settings` | ✅ 200 |
| `GET /api/v1/employees` | ✅ 200, `[]` |
| `POST /api/v1/employees/sync` | ⚠️ skipped: no iiko credentials/mock endpoint |
| `PATCH /api/v1/employees/{id}` with `full_name` | ✅ 400, `full_name is synchronized from iiko` |
| `GET /api/v1/payroll/runs` | ✅ 200, `[]` |
| Frontend `/login` | ✅ форма рендерится |
| Frontend login → `/settings` | ✅ login successful |
| Frontend `/staff` | ✅ route opens |
| Frontend `/settings` | ✅ route opens |
| Frontend `/payroll/runs` | ✅ route opens |
| Browser console | ✅ no warning/error; only Vite debug and React DevTools info |

## How to run locally

```bash
(cd apps && docker compose down -v)
(cd apps && docker compose up -d)
TEPLO_ADMIN_PASSWORD=admin-password-for-smoke make -C apps migrate
make -C apps test
```

Admin login for this smoke run:

```text
email: admin@teplo.local
password: admin-password-for-smoke
```

Notes:

- Pass `TEPLO_ADMIN_PASSWORD` when running migrations on a clean DB. Without it, migration `0002_seed_reference` generates a random admin password.
- `apps/Makefile` defaults to `../.venv-api/bin/python` from `apps/`. Override with `make -C apps API_PYTHON=python test` if using an activated environment.
- First clean `(cd apps && docker compose up -d)` can be slow because Docker builds API/Web images and downloads dependencies.

## Recommendations before next module

1. Add a deterministic iiko mock/smoke mode for `POST /api/v1/employees/sync`.
2. Keep Auth/Settings as accepted P2 baseline and do not start DDS/Balance until this smoke stays green after a fresh clone.
3. Consider consolidating RBAC dependencies before adding more finance modules: Settings uses `app.auth.current_user`, while staff/payroll use `app.api.deps.get_current_actor`.
