"""Общие аннотированные типы для pydantic-схем API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator

from app.services.clock import as_moscow

# Дата-время, набранное человеком в форме. Браузерный ``<input type="datetime-local">`` шлёт
# строку БЕЗ зоны, а колонки у нас ``timestamptz`` и контейнер API живёт в UTC — без этой
# аннотации набранное значение уезжает в базу как гринвичское и витрина показывает его на три
# часа позже (см. ``as_moscow``). Вешать на поля, которые заполняет оператор, а не сервер.
MoscowDateTime = Annotated[datetime, AfterValidator(as_moscow)]
