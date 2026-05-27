.PHONY: help db-up db-down api-dev web-dev api-test api-lint web-lint web-format

help:
	@printf "Teplo development commands:\n"
	@printf "  make db-up       Start PostgreSQL 16 with Docker Compose\n"
	@printf "  make db-down     Stop Docker Compose services\n"
	@printf "  make api-dev     Run FastAPI locally\n"
	@printf "  make web-dev     Run Vite locally\n"
	@printf "  make api-test    Run backend tests\n"
	@printf "  make api-lint    Run ruff checks\n"
	@printf "  make web-lint    Run frontend lint\n"

db-up:
	docker compose up -d postgres

db-down:
	docker compose down

api-dev:
	cd apps/api && uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

web-dev:
	npm --workspace apps/web run dev

api-test:
	cd apps/api && pytest

api-lint:
	cd apps/api && ruff check .

web-lint:
	npm --workspace apps/web run lint

web-format:
	npm --workspace apps/web run format
