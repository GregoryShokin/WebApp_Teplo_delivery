"""Тонкая async-обёртка над script-клиентом iikoCloud (iikoTransport).

Переиспользует ``integrations/iiko/scripts/iiko_cloud.py`` (как ``courier_sync`` грузит
``export_orders_delivery``) — не плодим второй клиент. Креды (``IIKO_CLOUD_*``) кладёт в
окружение ``iiko_sync._load_source_credential_env`` ПЕРЕД вызовом. Блокирующие urllib-вызовы
гоняем в треде. iikoCloud публичный → клиент сам обходит HTTPS_PROXY (банк-туннель).
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import Any

import anyio

from app.services.iiko_sync import _candidate_project_roots

_CLOUD_MODULE: ModuleType | None = None


def _load_iiko_cloud_module() -> ModuleType:
    global _CLOUD_MODULE
    if _CLOUD_MODULE is not None:
        return _CLOUD_MODULE
    for root in _candidate_project_roots():
        script_dir = root / "integrations/iiko/scripts"
        if not (script_dir / "iiko_cloud.py").exists():
            continue
        script_dir_str = str(script_dir)
        if script_dir_str not in sys.path:
            sys.path.insert(0, script_dir_str)
        _CLOUD_MODULE = importlib.import_module("iiko_cloud")
        return _CLOUD_MODULE
    raise RuntimeError("integrations/iiko/scripts/iiko_cloud.py is not available")


def build_cloud_client() -> Any:
    """Создаёт и аутентифицирует ``IikoCloud`` (читает IIKO_CLOUD_* из окружения)."""
    module = _load_iiko_cloud_module()
    client = module.IikoCloud()
    client.auth()
    return client


def discover_organization_id(client: Any) -> str | None:
    response = client.post("/api/1/organizations", {"returnAdditionalInfo": False})
    organizations = response.get("organizations") or []
    if not organizations:
        return None
    return organizations[0].get("id")


async def cloud_post(path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Одноразовый аутентифицированный POST в iikoCloud (для редких вызовов)."""

    def _call() -> dict[str, Any]:
        client = build_cloud_client()
        return client.post(path, body or {})

    return await anyio.to_thread.run_sync(_call)
