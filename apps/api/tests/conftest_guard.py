from __future__ import annotations

import os
from urllib.parse import urlparse

DEV_DB_NAMES = {"teplo", "teplo_dev", "teplo_prod"}


def require_test_database_url() -> str:
    database_url = os.environ.get("TEPLO_TEST_DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "TEPLO_TEST_DATABASE_URL не задан. Тесты не могут работать без выделенной "
            "test-БД.\n"
            "Установи переменную, например:\n"
            "  export TEPLO_TEST_DATABASE_URL="
            "'postgresql+asyncpg://teplo:teplo@localhost:5432/teplo_test'\n"
            "Или используй docker-compose окружение."
        )

    validate_test_database_url(database_url)
    return database_url


def validate_test_database_url(database_url: str) -> str:
    parsed = urlparse(database_url.replace("+asyncpg", ""))
    db_name = parsed.path.lstrip("/")
    if not db_name:
        raise RuntimeError(
            "TEPLO_TEST_DATABASE_URL не содержит имя БД. Используй отдельную test-БД, "
            "например 'teplo_test'."
        )

    if db_name in DEV_DB_NAMES:
        raise RuntimeError(
            f"TEPLO_TEST_DATABASE_URL указывает на dev/prod БД '{db_name}'. "
            "Это запрещено: тесты выполняют downgrade('base') и могут уничтожить данные.\n"
            "Используй отдельную БД, например 'teplo_test'."
        )

    return db_name


TEST_DATABASE_URL = require_test_database_url()
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
