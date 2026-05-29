# Stack decision for Teplo

Дата фиксации: 2026-05-27.

Статус: default-решение для первого скелета монорепо. PostgreSQL уже зафиксирован архитектурным решением 30; остальные пункты выбраны как минимально сложный self-hosted стек под 5-10 пользователей, read-heavy нагрузку и постепенный перенос Python-интеграций из `research/scripts/`.

## Контекст выбора

«Тепло» заменяет Google Sheets для управленческого учета малого бизнеса: зарплата, ДДС, ОПиУ/P&L, баланс, платежный календарь, ОС, налоги и интеграции. Система self-hosted, не SaaS. Нагрузка read-heavy: отчеты и сверки читаются чаще, чем меняются; записи появляются при ручных корректировках, закрытии периодов и ETL из iiko, банков, Mail.ru, Mango, СБИС и Telegram OCR.

Важные ограничения:

- PostgreSQL 16 - целевая БД.
- Raw/private данные с ПДн, назначениями платежей, cookies, OCR text и вложениями не смешиваются с processed/domain.
- Audit trail централизованный и immutable: доменное значение ссылается на `source_reference`, дальше на `source_snapshot` / `agent_run` / `manual_action`.
- Существующие интеграции уже написаны как Python-скрипты в `research/scripts/`, поэтому первый backend должен переиспользовать этот язык и рантайм.
- AI-агенты являются частью инфраструктуры, а не внешним экспериментом: нужны `data_source`, `source_credential`, `agent_run`, `agent_action`, `parsed_document`, `source_snapshot`, `credential_event`.

## Default stack

| Слой | Default | Почему | Trade-offs | Альтернативы |
| --- | --- | --- | --- | --- |
| Backend | **Python + FastAPI + SQLAlchemy 2 + Alembic** | Совпадает с существующими `research/scripts/`; удобно подключать iiko/Sber/T-Bank/Mail.ru/Mango/Telegram адаптеры; FastAPI хорошо подходит для typed API и внутреннего back-office. | FastAPI не дает готовую admin-панель и RBAC из коробки; дисциплину доменной модели, миграций и сервисного слоя нужно поддерживать самим. | Django + DRF, если срочно нужна admin-панель; NestJS, если команда станет TypeScript-first. |
| Frontend | **React + TypeScript + Vite + TanStack Query + shadcn/ui** | Быстрый dev loop; typed UI; TanStack Query естественно ложится на read-heavy отчеты и кеширование; shadcn/ui дает локальные компоненты без тяжелого design-system runtime. | Нужно самостоятельно собрать навигацию, формы и таблицы; shadcn/ui требует аккуратной поддержки Tailwind-конвенций. | Next.js, если появится SSR/public cabinet; Vue + Nuxt, если команда предпочтет Vue. |
| Database | **PostgreSQL 16** | Зафиксировано архитектурой; надежные транзакции, JSONB для audit/integration metadata, индексы для отчетов, нормальные миграции. | Требует дисциплины миграций, backup/restore и мониторинга; для локальной разработки нужен контейнер или локальный Postgres. | SQLite только для отдельных unit-тестов; ClickHouse позже как read-optimized аналитическая витрина, если Postgres станет узким местом. |
| Очереди и cron | **APScheduler** | Для одного self-hosted сервера и периодических ETL проще in-process scheduler: меньше сервисов, быстрее старт. | Не подходит для распределенной обработки, тяжелых очередей и гарантированной доставки при нескольких API-инстансах. | Celery + Redis/RabbitMQ позже для тяжелых OCR/ETL; RQ/Dramatiq как промежуточный вариант. |
| Auth | **JWT + bcrypt** | Достаточно для 5-10 пользователей, self-hosted окружения и ролей owner/finance/managing/accountant; без внешнего IdP. | Нужно аккуратно реализовать rotation/expiry, хранение refresh-токенов и аудит входов; JWT сложнее отозвать без server-side state. | Session cookies для более простой server-side invalidation; Keycloak/Authentik позже, если появятся SSO, MFA и сложные политики. |
| Тесты | **pytest + Playwright** | pytest покрывает Python-сервисы, парсеры и ETL; Playwright проверяет реальные back-office сценарии в браузере. | E2E тяжелее и медленнее unit-тестов; нужно держать fixtures без ПДн. | unittest для совсем простых модулей; Cypress как альтернатива Playwright, но Playwright лучше для browser automation-паттернов проекта. |
| Инфраструктура | **Docker Compose для dev** | Один командный запуск Postgres + API + Web; подходит для self-hosted разработки и воспроизводимых окружений. | Compose не является полноценным production-планом: backup, TLS, systemd, deploy, secrets и monitoring нужно описать отдельно. | Bare metal + systemd для первого prod; Kubernetes только если появится много сервисов и операторская команда. |
| Lint/format | **ruff + eslint + prettier** | ruff быстро закрывает Python lint/format; eslint/prettier стандартны для React/TypeScript. | Две экосистемы форматирования; правила нужно синхронизировать в CI. | Black + isort + flake8 вместо ruff; Biome вместо eslint/prettier, если frontend станет большим и хочется один инструмент. |

## Монорепо

Первый каркас:

```text
apps/
  api/      FastAPI backend, SQLAlchemy models, Alembic migrations, APScheduler jobs
  web/      React/Vite frontend
research/scripts/   существующие Python-интеграции и агенты; переносить постепенно
docs/      продуктовая и инженерная документация
infra/     заготовки инфраструктуры
tests/     существующие тесты scripts; новые app-тесты живут рядом с app
```

Граница ответственности:

- `apps/api` владеет доменной моделью, auth, API, audit trail и orchestration hooks.
- `research/scripts/` пока остаются рабочими интеграциями. Новые адаптеры в `apps/api/app/integrations` должны вызывать или переиспользовать их логику без копирования секретов в код.
- `apps/web` не хранит бизнес-логику расчетов; он показывает состояния, отчеты, формы ручного подтверждения и audit evidence.
- `research/private/` остается private-хранилищем raw artifacts; в БД и processed-слое хранятся ссылки, hashes, статусы качества и нормализованные поля.

## Что не решаем в первом скелете

- Production deployment: нужен отдельный документ с TLS, backup/restore, secrets, firewall, systemd или container runtime, monitoring и регламентом обновлений.
- Полный RBAC: в скелете есть место под роли, но конкретные permission matrix надо фиксировать вместе с UX модулей.
- Перенос всех `research/scripts/` внутрь backend: миграция должна идти по одному источнику, с audit trail и fixtures.
- CI/CD: после стабилизации структуры стоит добавить проверки `ruff`, `pytest`, `eslint`, `prettier`, `playwright`.
