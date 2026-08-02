"""Слой OCR коммунальной платёжки: что уходит в модель и что делать, когда она не ответила.

Главное, что здесь закреплено, — граница ответственности. Модель отдаёт ТЕКСТ и только его:
сумму из текста достают детерминированные парсеры. Если однажды кто-то решит спросить у модели
сразу «сколько платить», эти тесты должны сломаться — потому что доля в счёте Водоканала
(строки 4–7) и слагаемые акта электроэнергии не выводятся из общего смысла документа, их надо
считать по правилам.

Сети здесь нет: вызов модели подменяется, и проверяется наша обвязка вокруг него.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import get_settings
from app.services import utility_ocr
from app.services.anthropic_client import LlmCallError


def _settings():
    return get_settings()


async def test_pdf_text_layer_is_read_without_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """У PDF с текстовым слоем модель не спрашивают вовсе — pypdf точнее и бесплатен."""
    called = False

    async def _never(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(utility_ocr, "call_tool", _never)
    monkeypatch.setattr(utility_ocr, "_pdf_text", lambda content: "Счёт № 1201\nИтого 9878,79")

    text, how = await utility_ocr.extract_text(
        b"%PDF-1.4 fake", mime="application/pdf", settings=_settings()
    )

    assert how == "pdf_text"
    assert text is not None and "9878,79" in text
    assert called is False


async def test_scanned_pdf_says_so_instead_of_guessing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Скан без текстового слоя — честный отказ, а не пустая строка, выданная за распознанное."""
    monkeypatch.setattr(utility_ocr, "_pdf_text", lambda content: None)

    text, how = await utility_ocr.extract_text(
        b"%PDF-1.4 scan", mime="application/pdf", settings=_settings()
    )

    assert text is None
    assert how == "pdf_without_text_layer"


async def test_photo_goes_to_vision_as_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """Фотография уходит в модель картинкой, а обратно приходит текст — и только текст."""
    captured: dict[str, Any] = {}

    async def _fake(settings: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"text": "1 Водоснабжение 1 234,00\nВсего с НДС 9 878,79", "readable": True}

    monkeypatch.setattr(utility_ocr, "call_tool", _fake)

    text, how = await utility_ocr.extract_text(
        b"\xff\xd8\xff photo", mime="image/jpeg", settings=_settings()
    )

    assert how == "vision"
    assert text is not None and "9 878,79" in text
    blocks = captured["content"]
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"]["media_type"] == "image/jpeg"
    # Инструмент просит именно текст: полей суммы и периода в схеме быть не должно.
    assert set(captured["tool"]["input_schema"]["properties"]) == {"text", "readable"}


async def test_unreadable_photo_is_marked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Смазанный снимок помечается отдельно: человек должен понять, почему поля пустые."""

    async def _fake(settings: Any, **kwargs: Any) -> dict[str, Any]:
        return {"text": "не разобрать", "readable": False}

    monkeypatch.setattr(utility_ocr, "call_tool", _fake)

    _text, how = await utility_ocr.extract_text(
        b"\xff\xd8\xff blur", mime="image/jpeg", settings=_settings()
    )
    assert how == "vision_unreadable"


async def test_model_failure_does_not_break_intake(monkeypatch: pytest.MonkeyPatch) -> None:
    """Модель недоступна — приёмка живёт дальше: сумму введут руками, глядя на снимок.

    Это не мелочь: распознавание удобство, а приём документа — обязанность. Падение внешнего
    сервиса не должно закрывать человеку путь завести долг.
    """

    async def _boom(settings: Any, **kwargs: Any) -> dict[str, Any]:
        raise LlmCallError("Модель не настроена")

    monkeypatch.setattr(utility_ocr, "call_tool", _boom)

    text, how = await utility_ocr.extract_text(
        b"\xff\xd8\xff photo", mime="image/jpeg", settings=_settings()
    )
    assert text is None
    assert how == "vision_failed"


async def test_unsupported_type_is_rejected_early() -> None:
    """Чужой формат отсекается до вызова модели, а не после ошибки от неё."""
    text, how = await utility_ocr.extract_text(
        b"PK\x03\x04", mime="application/zip", settings=_settings()
    )
    assert text is None
    assert how == "unsupported_media_type"


async def test_heic_is_converted_before_vision(monkeypatch: pytest.MonkeyPatch) -> None:
    """Снимок с айфона доезжает до модели: HEIC конвертируется, а не отклоняется.

    Айфон снимает в HEIC по умолчанию, и для владельца это обычная фотография. Просить его
    пересохранить файл — плохой ответ, поэтому формат чиним мы.
    """
    captured: dict[str, Any] = {}

    async def _fake(settings: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"text": "Всего с НДС 9 878,79", "readable": True}

    monkeypatch.setattr(utility_ocr, "call_tool", _fake)
    monkeypatch.setattr(
        utility_ocr, "to_displayable", lambda content, mime: (b"\xff\xd8\xff jpeg", "image/jpeg")
    )

    text, how = await utility_ocr.extract_text(
        b"\x00\x00\x00\x18ftypheic", mime="image/heic", settings=_settings()
    )

    assert how == "vision"
    assert text is not None
    # В модель ушёл именно JPEG — HEIC она не принимает вовсе.
    assert captured["content"][0]["source"]["media_type"] == "image/jpeg"
