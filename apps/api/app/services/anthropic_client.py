"""Один структурированный вызов Claude — общий для всех сервисов проекта.

До этого модуля блок создания клиента жил в двух местах ДОСЛОВНО — байт в байт: в ИИ-ревьюере
налогов и в распознавании счетов. Третья копия (переоценка основных средств) закрепила бы
дублирование навсегда, а цена расхождения высокая: клиент, созданный без заголовка релея, на
проде получит 403 и покажет владельцу «дело в ключе». Ровно ту ошибку, ради которой написан
``call_failure_reason``.

ПРО РЕЛЕЙ. С российского IP Anthropic отвечает 403 «Request not allowed» даже на верный ключ
(проверено на проде 27.07.2026). Базовый URL SDK берёт из ``ANTHROPIC_BASE_URL`` сам, а секрет
уходит заголовком: ключ остаётся у нас, релей его не хранит и без секрета никого не пускает.

ПРО СТРУКТУРНЫЙ ОТВЕТ. Канон проекта — forced tool_use, а не «попроси JSON текстом»:
описываем один инструмент и требуем его вызвать. Ответ забираем из первого блока ``tool_use``.
Гарантии всё равно нет — при обрыве или упоре в ``max_tokens`` блок не придёт или придёт
обрезанным, поэтому «модель не вернула структурированный ответ» обрабатывается как штатный
исход, а не как невозможный.

Импорт ``anthropic`` ЛЕНИВЫЙ, внутри функции: до пересборки образа пакета может не быть, и на
этом же держится подмена SDK в тестах.
"""

from __future__ import annotations

from typing import Any

from app.core.config import Settings


class LlmCallError(RuntimeError):
    """Вызов модели не состоялся. Текст пригоден для показа человеку."""


async def call_tool(
    settings: Settings,
    *,
    tool: dict[str, Any],
    model: str,
    prompt: str | None = None,
    content: list[dict[str, Any]] | None = None,
    system: str | None = None,
    max_tokens: int = 2048,
) -> dict[str, Any]:
    """Спросить модель и вернуть заполненные ею поля инструмента.

    ``prompt`` — простой текстовый вопрос; ``content`` — готовые блоки, когда в сообщение
    нужно вложить документ (распознавание счёта шлёт PDF в base64).
    """
    if not settings.anthropic_api_key:
        raise LlmCallError("Модель не настроена: добавьте ANTHROPIC_API_KEY в окружение.")
    try:
        from anthropic import AsyncAnthropic
    except Exception as exc:  # noqa: BLE001 - до пересборки образа пакета может не быть
        raise LlmCallError("Пакет anthropic недоступен в этом окружении.") from exc

    client = AsyncAnthropic(
        api_key=settings.anthropic_api_key,
        timeout=settings.anthropic_timeout_seconds,
        max_retries=settings.anthropic_max_retries,
        default_headers=(
            {"x-relay-secret": settings.anthropic_relay_secret}
            if settings.anthropic_relay_secret
            else None
        ),
    )
    if content is None and prompt is None:
        raise LlmCallError("Нечего спрашивать: не задан ни текст, ни содержимое сообщения.")
    request: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "tools": [tool],
        "tool_choice": {"type": "tool", "name": str(tool["name"])},
        "messages": [{"role": "user", "content": content if content is not None else prompt}],
    }
    if system is not None:
        request["system"] = system

    try:
        message = await client.messages.create(**request)
    except Exception as exc:  # noqa: BLE001 - сеть и лимиты не должны ронять вызывающего
        raise LlmCallError(call_failure_reason(exc)) from exc

    for block in message.content:
        if getattr(block, "type", None) == "tool_use":
            return dict(block.input)  # type: ignore[arg-type]
    raise LlmCallError("Модель не вернула структурированный ответ.")


def call_failure_reason(exc: Exception) -> str:
    """Человеческая причина отказа вместо сырого текста исключения.

    Отдельно ловим региональную блокировку: с российского IP Anthropic отвечает 403 «Request
    not allowed» ДАЖЕ на верный ключ (проверено на проде 27.07.2026 — 403 приходит и на
    заведомо неправильный ключ, то есть дело не в нём). Без этого владелец видел
    «Не удалось получить ответ модели: Error code: 403…» и шёл искать проблему в ключе.
    """
    text = str(exc)
    if "403" in text and "not allowed" in text.casefold():
        return (
            "Anthropic не обслуживает запросы с IP этого сервера (403). Ключ тут ни при чём: "
            "нужен выход через разрешённый регион — задайте HTTPS_PROXY в окружении API "
            "либо запускайте разбор с машины, у которой доступ есть."
        )
    if "401" in text or "authentication" in text.casefold():
        return "Ключ ANTHROPIC_API_KEY не принят (401): проверьте значение и срок действия."
    if "429" in text:
        return "Лимит запросов к модели исчерпан (429) — попробуйте позже."
    return f"Не удалось получить ответ модели: {exc}"
