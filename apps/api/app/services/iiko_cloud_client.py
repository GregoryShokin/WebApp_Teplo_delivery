"""Низкоуровневый транспорт iiko Cloud API (``api-ru.iiko.services``).

Единая точка для ВСЕХ Cloud-вызовов: OAuth ``/api/v2/access_token`` → Bearer-токен, обход
банк-прокси и «никогда-не-бросающий» POST. Выделено из :mod:`counterparty_iiko_payment` (метод
``add_payment``, проверен в проде HTTP 201), чтобы новый контур накладных (create/update/post/
unpost/cancel/get) переиспользовал тот же проверенный транспорт.

Креды iiko на проде живут в DB-vault (``SourceCredential``), а НЕ в env контейнера — вызывающий
обязан вызвать :func:`app.services.iiko_sync._load_source_credential_env` ПЕРЕД этими функциями
(он гидратит ``IIKO_CLOUD_*`` в ``os.environ``). ``clientSecret`` хранится С завершающим ``=`` —
без него iiko отвечает 401 «Invalid client secret».
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

IIKO_BASE = "https://api-ru.iiko.services"
ACCESS_TOKEN_PATH = "/api/v2/access_token"

# Организация Cloud — «Foodmarket Тепло Черникова» (из /api/1/organizations).
IIKO_ORGANIZATION_ID = "5c7e51f9-93c6-450c-9299-440ac1c889e8"


def iiko_opener() -> urllib.request.OpenerDirector:
    """Opener с ПУСТЫМ ``ProxyHandler`` — в обход банк-туннеля (``HTTPS_PROXY``), который
    перехватывает ``api-ru.iiko.services`` и рубит соединение. Проверенный рабочий путь."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def iiko_auth_token(
    opener: urllib.request.OpenerDirector,
    *,
    app_id: str | None = None,
    api_login: str | None = None,
    client_secret: str | None = None,
) -> str:
    """Cloud OAuth: ``POST /api/v2/access_token {appId, apiKey, clientSecret}`` → ``token``.

    По умолчанию берёт приложение ``IIKO_CLOUD_*`` из окружения (гидратируется из vault). Аргументы
    позволяют подставить другое marketplace-приложение (напр. отдельный app под накладные).
    """
    body = json.dumps(
        {
            "appId": app_id or os.environ["IIKO_CLOUD_APP_ID"],
            "apiKey": api_login or os.environ["IIKO_CLOUD_API_LOGIN"],
            "clientSecret": client_secret or os.environ["IIKO_CLOUD_CLIENT_SECRET"],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        IIKO_BASE + ACCESS_TOKEN_PATH,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(req, timeout=30) as response:
        return json.loads(response.read())["token"]


def iiko_cloud_call(
    path: str,
    payload: dict,
    *,
    token: str | None = None,
    opener: urllib.request.OpenerDirector | None = None,
    timeout: int = 40,
) -> tuple[int, dict | list]:
    """Синхронный Cloud-вызов (исполнять в треде): auth (если ``token`` не передан) + POST в обход
    прокси. НИКОГДА не бросает — транспорт/таймаут/прокси/протухший токен/нет креды (``KeyError``)
    возвращаются кодом ``0`` с телом-ошибкой, чтобы вызывающий зафиксировал их как ``error``, а не
    ронял поток исключением. ``HTTPError`` → ``(code, json|{"error": raw})``. Терпим к пустому/
    не-JSON телу при 2xx.

    Для серии вызовов передавай общий ``token``/``opener`` (один auth на всю операцию), чтобы не
    дёргать ``access_token`` перед каждым запросом.
    """
    own_opener = opener or iiko_opener()
    try:
        tok = token or iiko_auth_token(own_opener)
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(IIKO_BASE + path, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {tok}")
        with own_opener.open(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            parsed = json.loads(raw) if raw.strip().startswith(("{", "[")) else {"raw": raw}
            return response.status, parsed
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except Exception:  # noqa: BLE001 — тело ошибки не JSON
            return exc.code, {"error": raw[:400]}
    except Exception as exc:  # noqa: BLE001 — сеть/прокси/токен/нет креды: ошибка, но поток не валим
        return 0, {"error": f"{type(exc).__name__}: {exc}"[:400]}
