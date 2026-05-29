# Teplo — управленческая система ресторана

Монорепо включает: приложение, его спецификацию, бизнес-документацию и исследовательский слой.

## Структура

| Раздел | Что внутри |
|---|---|
| [apps/](apps/) | Код приложения: FastAPI backend, React frontend, dev-инфра |
| [app-spec/](app-spec/00-index.md) | Спецификация приложения: архитектура, модули, страницы, сущности БД, ADR, интеграции |
| [business-docs/](business-docs/00-index.md) | Бизнес-логика: методологии финансов, маркетинга, штата, операций |
| [research/](research/00-index.md) | Исследовательский слой: ETL-скрипты, сырые выгрузки, исторические снапшоты, archive |

## Быстрый старт разработчика

```bash
make -C apps db-up
cd apps && npm install
cd apps/api && uvicorn app.main:app --reload
cd apps && npm --workspace web run dev
```

См. также:
- [Стек приложения и обоснование](app-spec/architecture/decisions/stack-decision.md)
- [Архитектура БД](app-spec/architecture/database.md)
- [Видение продукта](app-spec/architecture/vision.md)
