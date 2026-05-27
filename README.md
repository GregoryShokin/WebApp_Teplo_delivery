# Документация контроля бизнеса

Рабочая база для бизнеса доставки пиццы и роллов на iiko: финансы, маркетинг, операционные метрики, узкие места, задачи и стратегия.

Стартовая точка: [индекс документации](docs/business-control/00-index.md).

## Монорепо приложения

Первый инженерный каркас веб-приложения находится в `apps/`:

- `apps/api` - FastAPI backend, SQLAlchemy-модели, Alembic, APScheduler.
- `apps/web` - React + TypeScript + Vite frontend.
- `scripts/` - существующие Python-интеграции и агенты; переносить в backend постепенно.
- `docker-compose.yml` - dev-окружение с PostgreSQL 16, API и Web.

Решение по стеку: [docs/development/00-stack-decision.md](docs/development/00-stack-decision.md).

Быстрый старт для dev после установки зависимостей:

```bash
docker compose up postgres
cd apps/api && uvicorn app.main:app --reload
npm --workspace apps/web run dev
```
