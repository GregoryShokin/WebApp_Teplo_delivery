"""Справочник привязок iiko для карточки помещения.

Оператор не должен вводить UUID руками: организации берём из iiko Cloud
(``/api/1/organizations``), подразделения и склады — из iiko Server RMS
(``/corporation/departments`` и ``/corporation/stores``, XML ``corporateItemDto``). Помещение
получает выбор из списка, а ``organizationId``/``departmentId``/``storeId`` подставляются сами.

iiko привязан к IP прода, а на превью-стенде замокан (``IIKO_SERVER_BASE_URL=…invalid``), поэтому
живой справочник доступен только на проде. Чтобы UX карточки помещения можно было показать и на
стенде, при недоступности iiko ВНЕ production отдаём демо-набор (``source='mock'``). На проде
демо-режима нет: не смогли получить — ``source='unavailable'`` и пустые списки (фронт покажет
ручной ввод как запасной путь).

Транспорт синхронный (urllib у Cloud и resto-клиента), поэтому весь сбор гоняем в треде. Ни одна
ветка не должна ронять процесс: resto-клиент бросает ``SystemExit`` (BaseException!) при пустом
``IIKO_SERVER_BASE_URL`` — ловим и его.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import PRODUCTION_ENVIRONMENTS, get_settings
from app.services import iiko_sync
from app.services.iiko_cloud_client import iiko_cloud_call

_CACHE_TTL_SECONDS = 600.0


@dataclass
class IikoDirectory:
    source: str  # 'live' | 'mock' | 'unavailable'
    organizations: list[dict[str, str]] = field(default_factory=list)
    departments: list[dict[str, str]] = field(default_factory=list)
    stores: list[dict[str, str]] = field(default_factory=list)


_cache: tuple[float, IikoDirectory] | None = None


def _cache_get() -> IikoDirectory | None:
    if _cache is None:
        return None
    stamped_at, value = _cache
    if time.monotonic() - stamped_at > _CACHE_TTL_SECONDS:
        return None
    return value


def _cache_set(value: IikoDirectory) -> None:
    global _cache
    _cache = (time.monotonic(), value)


def _safe(fetch: Callable[[], list[dict[str, str]]]) -> list[dict[str, str]]:
    """Любая ветка справочника падает независимо: сеть, протухший токен, нет креды и даже
    ``SystemExit`` из resto-клиента не должны валить остальные источники и процесс."""
    try:
        return fetch()
    except BaseException:  # noqa: BLE001 — включая SystemExit из IikoClient.__init__
        return []


def _fetch_organizations() -> list[dict[str, str]]:
    status, body = iiko_cloud_call("/api/1/organizations", {})
    if status != 200 or not isinstance(body, dict):
        return []
    rows = body.get("organizations") or []
    return [
        {"id": str(item["id"]), "name": str(item.get("name") or item["id"])}
        for item in rows
        if isinstance(item, dict) and item.get("id")
    ]


def _fetch_resto_items(endpoint: str) -> list[dict[str, str]]:
    module = iiko_sync._load_export_employees_module()
    module.load_local_env()
    client = module.IikoClient()  # SystemExit при пустом base_url — ловится в _safe
    _status, data = client.request(endpoint, params={"includeDeleted": "false"})
    items = module.parse_xml_items(data, "corporateItemDto")
    result: list[dict[str, str]] = []
    for item in items:
        item_id = item.get("id")
        if not item_id or str(item.get("deleted", "")).lower() == "true":
            continue
        result.append({"id": str(item_id), "name": str(item.get("name") or item_id)})
    return result


def _demo_directory() -> IikoDirectory:
    """Данные под живой стенд без iiko — чтобы показать выбор организации/подразделения/складов.
    На проде не используется."""
    return IikoDirectory(
        source="mock",
        organizations=[
            {"id": "5c7e51f9-93c6-450c-9299-440ac1c889e8", "name": "Foodmarket Тепло Черникова"}
        ],
        departments=[{"id": "d8d4a22e-3abd-4f02-b82d-7d4712f32729", "name": "Черникова"}],
        stores=[
            {"id": "demo-store-main", "name": "Основной склад Черникова"},
            {"id": "demo-store-kitchen", "name": "Кухня"},
            {"id": "demo-store-bar", "name": "Бар"},
        ],
    )


def _collect_sync() -> IikoDirectory:
    cached = _cache_get()
    if cached is not None:
        return cached

    organizations = _safe(_fetch_organizations)
    departments = _safe(lambda: _fetch_resto_items("/corporation/departments"))
    stores = _safe(lambda: _fetch_resto_items("/corporation/stores"))

    if organizations or departments or stores:
        live = IikoDirectory("live", organizations, departments, stores)
        _cache_set(live)
        return live

    if get_settings().environment.casefold() not in PRODUCTION_ENVIRONMENTS:
        return _demo_directory()
    return IikoDirectory("unavailable")


async def get_iiko_directory(session: AsyncSession) -> IikoDirectory:
    """Справочник для карточки помещения. Креды iiko живут в vault — гидратируем их в окружение
    ПЕРЕД синхронными вызовами (как и остальной iiko-контур)."""
    await iiko_sync._load_source_credential_env(session)
    return await run_in_threadpool(_collect_sync)
