"""Клиент API СБИС (Saby) ЭДО — JSON-RPC 2.0 поверх HTTPS.

Авторизация — сервисная (OAuth 2 для внешних приложений): POST на ``sbis_auth_url``
с ``{app_client_id, secret_key}`` возвращает токен, который передаётся заголовком
``X-SBISAccessToken`` в каждом запросе (докам защищённый ключ app_secret не нужен —
проверено на живом кабинете). Токен кэшируется в памяти процесса и переполучается
на HTTP 401. Лимиты СБИС: ≤5 параллельных потоков, ≤5 активных сессий на приложение
(новая сверх лимита гасит самую старую — протухший кэш безопасен).

Печатная форма PDF генерируется на стороне СБИС асинхронно: первый GET может вернуть
JSON-ошибку «Итоговый файл ещё формируется» — поэтому ``download_pdf`` ретраит.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import date
from typing import Any

import httpx

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

INCOMING_REGISTRY = "Входящие"
_LIST_METHOD = "СБИС.СписокДокументовПоСобытиям"
_PAGE_SIZE = 200
_USER_AGENT = "TeploApp/1.0"

# Токен живёт в памяти процесса: джобы планировщика крутятся в свежих event loop'ах
# (asyncio.run на каждый тик), поэтому примитивы asyncio для кэша не годятся —
# обычный threading.Lock вокруг словаря (внутри критической секции нет await).
_token_lock = threading.Lock()
_token_cache: dict[str, str] = {}


class SbisError(Exception):
    """Базовая ошибка обмена со СБИС."""


class SbisCredentialsError(SbisError):
    """Не заданы SBIS_APP_CLIENT_ID / SBIS_SECRET_KEY."""


class SbisApiError(SbisError):
    """СБИС ответил ошибкой (HTTP или JSON-RPC error)."""


class SbisFileNotReadyError(SbisApiError):
    """Печатная форма ещё формируется на стороне СБИС — нужно повторить запрос."""


def _format_date(value: date) -> str:
    return value.strftime("%d.%m.%Y")


class SbisClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    @property
    def configured(self) -> bool:
        return bool(self._settings.sbis_app_client_id and self._settings.sbis_secret_key)

    def _require_credentials(self) -> tuple[str, str]:
        client_id = self._settings.sbis_app_client_id
        secret_key = self._settings.sbis_secret_key
        if not client_id or not secret_key:
            raise SbisCredentialsError(
                "СБИС не настроен: заполните SBIS_APP_CLIENT_ID и SBIS_SECRET_KEY"
            )
        return client_id, secret_key

    async def _fetch_token(self, client: httpx.AsyncClient, *, force: bool = False) -> str:
        client_id, secret_key = self._require_credentials()
        if not force:
            with _token_lock:
                cached = _token_cache.get(client_id)
            if cached:
                return cached
        response = await client.post(
            self._settings.sbis_auth_url,
            json={"app_client_id": client_id, "secret_key": secret_key},
            headers={"User-Agent": _USER_AGENT},
        )
        if response.status_code != 200:
            raise SbisApiError(
                f"Авторизация СБИС не удалась: HTTP {response.status_code} "
                f"{response.text[:300]}"
            )
        payload = response.json()
        token = payload.get("token") or payload.get("access_token")
        if not token:
            raise SbisApiError(f"Авторизация СБИС: в ответе нет токена ({str(payload)[:300]})")
        with _token_lock:
            _token_cache[client_id] = token
        return token

    async def _rpc(self, client: httpx.AsyncClient, method: str, params: dict[str, Any]) -> Any:
        body = json.dumps(
            {"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
            ensure_ascii=False,
        ).encode("utf-8")
        token = await self._fetch_token(client)
        for attempt in (1, 2):
            response = await client.post(
                self._settings.sbis_service_url,
                content=body,
                headers={
                    "Content-Type": "application/json-rpc;charset=utf-8",
                    "User-Agent": _USER_AGENT,
                    "X-SBISAccessToken": token,
                },
            )
            # Протухший/вытесненный токен → одно переполучение и повтор.
            if response.status_code == 401 and attempt == 1:
                token = await self._fetch_token(client, force=True)
                continue
            break
        if response.status_code != 200:
            raise SbisApiError(
                f"СБИС {method}: HTTP {response.status_code} {response.text[:300]}"
            )
        payload = response.json()
        if "error" in payload:
            error = payload["error"] or {}
            raise SbisApiError(
                f"СБИС {method}: {error.get('message')} ({error.get('details')})"
            )
        return payload.get("result")

    def _client(self) -> httpx.AsyncClient:
        # trust_env=False: контейнерный HTTPS_PROXY (туннель для iiko) не должен
        # перехватывать СБИС — прямой egress к online.sbis.ru работает и в dev, и на проде.
        return httpx.AsyncClient(
            timeout=self._settings.sbis_http_timeout_seconds, trust_env=False
        )

    async def list_incoming_documents(
        self, date_from: date, date_to: date | None = None
    ) -> list[dict[str, Any]]:
        """Реестр «Входящие» за окно дат — все страницы, элементы «Реестра» как есть."""
        items: list[dict[str, Any]] = []
        async with self._client() as client:
            page = 0
            while True:
                filter_payload: dict[str, Any] = {
                    "ДатаС": _format_date(date_from),
                    "ТипРеестра": INCOMING_REGISTRY,
                    "Навигация": {"РазмерСтраницы": str(_PAGE_SIZE), "Страница": str(page)},
                }
                if date_to is not None:
                    filter_payload["ДатаПо"] = _format_date(date_to)
                result = await self._rpc(client, _LIST_METHOD, {"Фильтр": filter_payload})
                registry = (result or {}).get("Реестр") or []
                items.extend(registry)
                if len(registry) < _PAGE_SIZE:
                    return items
                page += 1

    async def _get_raw(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        """GET по ссылке СБИС с токеном и одним переполучением на 401."""
        token = await self._fetch_token(client)
        response: httpx.Response | None = None
        for attempt in (1, 2):
            response = await client.get(
                url,
                headers={"User-Agent": _USER_AGENT, "X-SBISAccessToken": token},
                follow_redirects=True,
            )
            if response.status_code == 401 and attempt == 1:
                token = await self._fetch_token(client, force=True)
                continue
            break
        assert response is not None
        return response

    async def download_file(self, url: str) -> bytes:
        """Скачать файл по ссылке из реестра (XML вложения и т.п.) с текущим токеном."""
        async with self._client() as client:
            response = await self._get_raw(client, url)
        if response.status_code != 200:
            raise SbisApiError(f"СБИС файл: HTTP {response.status_code} {response.text[:200]}")
        return response.content

    @staticmethod
    def _not_ready_message(content: bytes) -> str | None:
        """Текст «файл ещё формируется» из JSON-RPC ответа сервиса печати, иначе None."""
        try:
            payload = json.loads(content)
        except ValueError:
            return None
        message = str(((payload or {}).get("error") or {}).get("message") or "")
        return message if "формируется" in message else None

    async def download_pdf(self, url: str, *, attempts: int = 6, delay_seconds: float = 3) -> bytes:
        """Печатная форма PDF: генерится на стороне СБИС асинхронно — ретраим, пока сервис
        отвечает «файл ещё формируется» (это приходит и с HTTP 200, и с HTTP 500)."""
        async with self._client() as client:
            for _ in range(attempts):
                response = await self._get_raw(client, url)
                content = response.content
                if response.status_code == 200 and content.startswith(b"%PDF"):
                    return content
                if self._not_ready_message(content) is not None:
                    await asyncio.sleep(delay_seconds)
                    continue
                raise SbisApiError(f"СБИС PDF: HTTP {response.status_code} {content[:200]!r}")
        raise SbisFileNotReadyError("СБИС PDF: файл так и не сформировался — повторите позже")
