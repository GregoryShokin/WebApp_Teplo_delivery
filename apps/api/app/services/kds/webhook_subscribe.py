"""Авто-подписка на вебхук iikoCloud ``DeliveryOrderUpdate`` (idempotent, best-effort).

Вызывается одноразово при старте планировщика. Если endpoint/схема фильтра отличаются
в конкретной версии iiko — НЕ роняем старт: логируем, владелец донастроит подписку в
кабинете iiko (URL = ``iiko_webhook_public_url``, authToken = ``iiko_webhook_token``).
Сверочный поллинг ``kds_reconcile`` всё равно наполняет очередь.
"""

from __future__ import annotations

import logging
from typing import Any

import anyio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.services.iiko_sync import _load_source_credential_env
from app.services.kds.iiko_cloud_client import build_cloud_client, discover_organization_id
from app.services.kds.sync_service import ORG_ID_SETTING_KEY, read_org_id
from app.services.settings_service import write_setting

logger = logging.getLogger(__name__)

_ORDER_STATUSES = [
    "Unconfirmed",
    "WaitCooking",
    "ReadyForCooking",
    "CookingStarted",
    "CookingCompleted",
    "Waiting",
    "OnWay",
    "Delivered",
    "Closed",
    "Cancelled",
]


def _subscribe_blocking(
    org_id: str | None,
    webhook_uri: str,
    auth_token: str,
) -> tuple[str, dict[str, Any]]:
    client = build_cloud_client()
    resolved = org_id or discover_organization_id(client)
    if not resolved:
        raise RuntimeError("iikoCloud organizationId не определён")
    body = {
        "organizationId": resolved,
        "webHooksUri": webhook_uri,
        "authToken": auth_token or "",
        "webHooksFilter": {
            "deliveryOrderFilter": {
                "orderStatuses": _ORDER_STATUSES,
                "itemStatuses": [],
                "errors": True,
            }
        },
    }
    response = client.post("/api/1/webhooks/update_webhook_settings", body)
    return resolved, response


async def ensure_webhook_subscription(session: AsyncSession) -> dict[str, Any]:
    settings = get_settings()
    if not settings.kds_webhook_autosubscribe_enabled:
        return {"status": "disabled"}

    await _load_source_credential_env(session)
    org_id = await read_org_id(session)
    try:
        resolved, _response = await anyio.to_thread.run_sync(
            _subscribe_blocking,
            org_id or None,
            settings.iiko_webhook_public_url,
            settings.iiko_webhook_token or "",
        )
    except Exception as exc:  # noqa: BLE001 - старт не должен падать из-за iiko
        logger.warning(
            "KDS webhook autosubscribe failed (настройте подписку в кабинете iiko вручную): %s",
            exc,
        )
        return {"status": "error", "error": str(exc)[:200]}

    if not org_id and resolved:
        try:
            await write_setting(session, ORG_ID_SETTING_KEY, resolved, changed_by_user_id=None)
        except Exception:  # noqa: BLE001
            logger.exception("failed to persist iikoCloud org id from autosubscribe")
    logger.info("KDS webhook subscription ensured for org %s", resolved)
    return {"status": "ok", "organization_id": resolved}
