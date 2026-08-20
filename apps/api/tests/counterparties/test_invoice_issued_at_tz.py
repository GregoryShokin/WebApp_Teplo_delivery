"""Время накладной и чека — московское, а не гринвичское.

Поле ввода ``<input type="datetime-local">`` шлёт строку без зоны, колонка ``issued_at`` —
``timestamptz``, контейнер API живёт в UTC. Без локализации набранные оператором московские
цифры уезжали в базу как гринвичские: накладная 515287 (прод, 20.08.2026) создана в 15:17 МСК,
а «выписана» в 18:16 — на три часа позже собственного создания.

Проверки чистые: pydantic-схемы и подготовка пуша, без БД и сети.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.api.v1.routes.warehouse import InvoiceCreate, InvoiceUpdate, ReturnCreate
from app.schemas.kassa import ChequeCreate
from app.services.clock import as_moscow
from app.services.warehouse_invoice_push import _to_moscow_wall_clock

MSK = ZoneInfo("Europe/Moscow")
TYPED = "2026-08-20T15:16"  # ровно то, что отдаёт браузерное поле datetime-local


def test_as_moscow_localizes_naive() -> None:
    """Наивное значение — московское стенное время, инстант получается на 3 часа раньше UTC."""
    localized = as_moscow(datetime(2026, 8, 20, 15, 16))

    assert localized.tzinfo is not None
    assert localized.utcoffset().total_seconds() == 3 * 3600
    assert localized.astimezone(UTC) == datetime(2026, 8, 20, 12, 16, tzinfo=UTC)


def test_as_moscow_keeps_explicit_offset() -> None:
    """Клиент вправе прислать зону явно — её не переписываем."""
    aware = datetime(2026, 8, 20, 15, 16, tzinfo=UTC)

    assert as_moscow(aware) is aware


def _line() -> dict:
    return {"name": "Огурцы", "quantity": Decimal("10"), "price": Decimal("120")}


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            InvoiceCreate(
                counterparty_id="00000000-0000-0000-0000-000000000001",
                issued_at=TYPED,
                lines=[_line()],
            ),
            id="создание накладной",
        ),
        pytest.param(
            InvoiceUpdate(issued_at=TYPED, lines=[_line()]),
            id="правка накладной",
        ),
        pytest.param(
            ReturnCreate(
                loan_id="00000000-0000-0000-0000-000000000002",
                issued_at=TYPED,
                returns=[{"amount": Decimal("100")}],
            ),
            id="возврат по бартеру",
        ),
        pytest.param(
            ChequeCreate(
                counterparty_id="00000000-0000-0000-0000-000000000003",
                issued_at=TYPED,
                cash_amount=Decimal("100"),
            ),
            id="чек Кассы",
        ),
    ],
)
def test_payload_reads_typed_time_as_moscow(payload) -> None:
    """Все формы, где дату набирает человек, кладут в issued_at московский инстант."""
    assert payload.issued_at.utcoffset().total_seconds() == 3 * 3600
    assert payload.issued_at.astimezone(UTC) == datetime(2026, 8, 20, 12, 16, tzinfo=UTC)
    # Дата документа берётся как .date() от этого значения — она обязана остаться набранной.
    assert payload.issued_at.date() == date(2026, 8, 20)


def test_push_sends_moscow_wall_clock() -> None:
    """В iiko уходит московское стенное время, а не цифры UTC.

    До починки ввода здесь просто снимали tz, и это работало лишь потому, что ``issued_at``
    хранился неверно. С правильным инстантом «снять tz» отправило бы документ на три часа назад.
    """
    stored = datetime(2026, 8, 20, 12, 16, tzinfo=UTC)  # = 15:16 МСК

    assert _to_moscow_wall_clock(stored) == datetime(2026, 8, 20, 15, 16)


def test_push_keeps_naive_as_is() -> None:
    """Наивный фолбэк от ``invoice_date`` уже московский — второй раз не переводим."""
    naive = datetime(2026, 8, 20, 0, 0)

    assert _to_moscow_wall_clock(naive) == naive


def test_push_date_does_not_slip_to_previous_day() -> None:
    """Полночь по Москве не должна съезжать на вчера при переводе в стенное время.

    Именно на этом строился прод-баг: полночь, ушедшая в iiko, возвращалась как 21:00
    предыдущего дня.
    """
    midnight_msk = datetime(2026, 8, 20, 0, 0, tzinfo=MSK)

    assert _to_moscow_wall_clock(midnight_msk).date() == date(2026, 8, 20)
