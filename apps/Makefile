API_PYTHON ?= ../.venv-api/bin/python

.PHONY: help db-up db-down migrate test api-dev web-dev api-test api-lint web-lint web-format

help:
	@printf "Teplo development commands:\n"
	@printf "  make db-up       Start PostgreSQL 16 with Docker Compose\n"
	@printf "  make db-down     Stop Docker Compose services\n"
	@printf "  make migrate     Apply API database migrations\n"
	@printf "  make test        Run backend tests\n"
	@printf "  make api-dev     Run FastAPI locally\n"
	@printf "  make web-dev     Run Vite locally\n"
	@printf "  make api-test    Run backend tests\n"
	@printf "  make api-lint    Run ruff checks\n"
	@printf "  make web-lint    Run frontend lint\n"

db-up:
	docker compose up -d postgres

db-down:
	docker compose down

migrate:
	cd api && $(API_PYTHON) -m alembic upgrade head

test: api-test

api-dev:
	cd api && uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

web-dev:
	npm --workspace web run dev

api-test:
	cd api && $(API_PYTHON) -m pytest

# Courier deliveries Phase 1: run from cron/scheduler later via
# POST /api/v1/couriers/sync?mode=hot (or mode=custom&date_from=YYYY-MM-DD&date_to=YYYY-MM-DD).
api-lint:
	cd api && ruff check .

web-lint:
	npm --workspace web run lint

web-format:
	npm --workspace web run format
