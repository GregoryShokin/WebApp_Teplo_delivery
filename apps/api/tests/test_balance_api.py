"""HTTP-контур модуля «Баланс»: права, граница «предпросмотр — снимок», ручной ввод.

Расчётная часть покрыта отдельно (``test_balance_assembly``, ``test_balance_snapshot``,
``test_balance_reconcile``). Здесь — только то, что добавляет слой API: кого пускают, что
отвечают на невозможный запрос и какую форму получает страница.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

sys.path.append(str(Path(__file__).parent / "counterparties"))

from cp_helpers import admin_headers, headers_for  # noqa: E402

BASE = "/api/v1/balance"
JULY_END = "2026-07-31"


def _admin(factory) -> dict[str, str]:
    return asyncio.run(admin_headers(factory))


def _cashier(factory) -> dict[str, str]:
    return asyncio.run(headers_for(factory, "cashier-balance@teplo.local", ["cashier"]))


def _line(view: dict, code: str) -> dict:
    return next(item for item in view["lines"] if item["line_code"] == code)


def test_preview_is_closed_to_those_without_the_right(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Баланс — сводка всего бизнеса: кассир видеть её не должен."""
    response = client.get(
        f"{BASE}/preview", params={"as_of": JULY_END}, headers=_cashier(async_session_factory)
    )
    assert response.status_code == 403, response.text


def test_preview_returns_the_lines_and_writes_nothing(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Предпросмотр — рабочая поверхность: он отвечает «сколько получается» и не подписывает.

    Отдельная ручка нужна именно поэтому: смешай её со снимком — и каждый заход владельца на
    страницу переписывал бы подписанную цифру.
    """
    headers = _admin(async_session_factory)
    response = client.get(f"{BASE}/preview", params={"as_of": JULY_END}, headers=headers)
    assert response.status_code == 200, response.text

    view = response.json()
    assert view["status"] == "preview"
    assert view["lines"], "предпросмотр без строк — это пустая страница, а не баланс"
    assert _line(view, "equity_plug")["status"] == "plug"

    # Ничего не записано: снимка на эту дату по-прежнему нет.
    assert client.get(f"{BASE}/snapshots/{JULY_END}", headers=headers).status_code == 404


def test_a_date_before_the_start_of_accounting_is_answered_not_crashed(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """До 01.07.2026 баланса нет — и ответ должен это объяснять, а не падать пятисоткой."""
    response = client.get(
        f"{BASE}/preview", params={"as_of": "2026-06-30"}, headers=_admin(async_session_factory)
    )
    assert response.status_code == 422, response.text
    assert "01.07.2026" in response.json()["detail"]


def test_the_snapshot_survives_the_whole_round_trip(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Заморозка → чтение → подпись → отказ пересобрать: полный путь месяца одним тестом."""
    headers = _admin(async_session_factory)

    frozen = client.post(f"{BASE}/snapshots", json={"as_of": JULY_END}, headers=headers)
    assert frozen.status_code == 200, frozen.text
    assert frozen.json()["status"] == "draft"
    assert frozen.json()["method"] == "inventory"

    read_back = client.get(f"{BASE}/snapshots/{JULY_END}", headers=headers)
    assert read_back.status_code == 200, read_back.text
    assert read_back.json()["asset_total"] == frozen.json()["asset_total"]

    final = client.post(f"{BASE}/snapshots/{JULY_END}/finalize", json={}, headers=headers)
    assert final.status_code == 200, final.text
    assert final.json()["status"] == "final"
    assert final.json()["finalized_at"] is not None

    again = client.post(f"{BASE}/snapshots", json={"as_of": JULY_END}, headers=headers)
    assert again.status_code == 409, again.text


def test_freezing_is_closed_to_readers(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Подпись месяца — не то действие, которое разрешают всем читателям отчёта."""
    response = client.post(
        f"{BASE}/snapshots", json={"as_of": JULY_END}, headers=_cashier(async_session_factory)
    )
    assert response.status_code == 403, response.text


def test_a_register_backed_line_refuses_a_hand_entered_number(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Подменить руками величину, которую считает регистр, нельзя.

    Разреши это — и расхождение между балансом и регистром станет невидимым: цифра сойдётся
    там, где данные разошлись, и искать причину будет уже негде.
    """
    response = client.put(
        f"{BASE}/manual/{JULY_END}/money_bank",
        json={"amount": "100000.00", "reason": "хочу так"},
        headers=_admin(async_session_factory),
    )
    assert response.status_code == 422, response.text
    assert "из регистра" in response.json()["detail"]


def test_a_hand_entered_number_needs_a_reason_and_then_shows_up(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Цифра без основания в балансе неотличима от опечатки, поэтому основание обязательно."""
    headers = _admin(async_session_factory)

    without_reason = client.put(
        f"{BASE}/manual/{JULY_END}/ar_partner",
        json={"amount": "18400.00", "reason": ""},
        headers=headers,
    )
    assert without_reason.status_code == 422, without_reason.text

    accepted = client.put(
        f"{BASE}/manual/{JULY_END}/ar_partner",
        json={"amount": "18400.00", "reason": "Расписка №4 от 31.07"},
        headers=headers,
    )
    assert accepted.status_code == 200, accepted.text

    view = client.get(f"{BASE}/preview", params={"as_of": JULY_END}, headers=headers).json()
    line = _line(view, "ar_partner")
    assert line["amount"] == "18400.00"
    assert line["status"] == "manual"
    assert "Расписка" in line["source_note"]


def test_drift_is_empty_while_nothing_has_moved(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Пустой дрейф — это ответ «перенесённые в отчётность числа всё ещё верны»."""
    headers = _admin(async_session_factory)
    client.post(f"{BASE}/snapshots", json={"as_of": JULY_END}, headers=headers)

    response = client.get(f"{BASE}/snapshots/{JULY_END}/drift", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == []


def test_the_catalog_explains_why_a_line_is_empty(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Справочник нужен ровно затем, чтобы пустая строка читалась как осознанная, а не забытая."""
    response = client.get(f"{BASE}/catalog", headers=_admin(async_session_factory))
    assert response.status_code == 200, response.text

    by_code = {item["code"]: item for item in response.json()}
    assert by_code["debt_overdraft"]["fill"] == "zero_confirmed"
    assert by_code["debt_overdraft"]["note"]
    assert by_code["ar_owners"]["fill"] == "manual"
    assert by_code["equity_charter"]["fill"] == "not_applicable"


def test_the_snapshot_list_is_newest_first(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Список снимков — журнал закрытых месяцев; свежий месяц владельцу нужен первым."""
    headers = _admin(async_session_factory)
    client.post(f"{BASE}/snapshots", json={"as_of": JULY_END}, headers=headers)
    client.post(f"{BASE}/snapshots", json={"as_of": "2026-08-31"}, headers=headers)

    rows = client.get(f"{BASE}/snapshots", headers=headers).json()
    assert [row["as_of"] for row in rows][:2] == ["2026-08-31", JULY_END]
