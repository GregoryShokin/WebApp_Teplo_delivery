"""Текст с коммунальной платёжки: снимок → строки, которые разберут детерминированные парсеры.

ПОЧЕМУ МОДЕЛЬ ИСПОЛЬЗУЕТСЯ ТОЛЬКО КАК OCR. Соблазн спросить у неё сразу «сумма и период» велик
и неверен: сумму счёта нельзя брать «на глаз». В квитанции Водоканала оплачиваются РОВНО строки
4–7 (остальные — не наша доля), в акте электроэнергии складываются потребление и потери, а
рядом лежит ещё авансовая строка следующего периода. Эти правила уже написаны регексами и
покрыты golden-тестами на реальных фото (``utility_recognition``), они детерминированы и
воспроизводимы. Модель здесь заменяет ТОЛЬКО тот шаг, который на сервере выполнить нечем, —
превращение картинки в текст.

ПОЧЕМУ НЕ TESSERACT. Локальный бот владельца, откуда пришли парсеры, идёт цепочкой
tesseract → апскейл → macOS Vision и выбирает лучший проход по числу недостающих полей: одного
tesseract на телефонных снимках с бликами и перспективой не хватало, часть счетов вытягивала
именно Vision. В Linux-контейнере Vision нет вовсе, и остался бы ровно тот слабый проход.
Объём при этом крошечный — три ресурса, десяток снимков в год, — поэтому вопрос цены вызова не
стоит. Станет потоком в сотни документов — tesseract вернётся первым дешёвым проходом, а
модель останется фолбэком, как в боте.

PDF в модель не отправляем: текстовый слой из него достаёт ``pypdf`` бесплатно и точно. Модель
получает только то, что иначе нечитаемо, — фотографию.
"""

from __future__ import annotations

import base64
import logging
from io import BytesIO

from app.core.config import Settings
from app.services.anthropic_client import LlmCallError, call_tool
from app.services.utility_images import HEIC_MIME_TYPES, to_displayable

logger = logging.getLogger(__name__)

__all__ = ["extract_text"]

# Форматы, которые Anthropic принимает как изображение. HEIC среди них нет — снимок с айфона
# конвертируется в JPEG на входе (``to_displayable``), а не отклоняется: для человека это
# обычная фотография, и просить его пересохранить файл — плохой ответ.
_VISION_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})

_OCR_TOOL = {
    "name": "submit_text",
    "description": "Вернуть распознанный текст документа построчно.",
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Весь распознанный текст документа, строка за строкой.",
            },
            "readable": {
                "type": "boolean",
                "description": "Читается ли документ вообще (false — снимок непригоден).",
            },
        },
        "required": ["text", "readable"],
    },
}

# Просим ТЕКСТ, а не осмысление: любая «помощь» модели здесь вредна. Сумма, которую она решит
# поправить или досчитать, разойдётся с бумагой, а проверять человеку будет нечего.
_OCR_PROMPT = """Перед тобой фотография или скан коммунального документа: счёт за воду,
акт за электроэнергию или подобная платёжка.

Распознай ВЕСЬ текст документа как есть, строка за строкой, сверху вниз.

Требования:
- Переноси числа ровно так, как они напечатаны, включая копейки и разделители разрядов.
- Сохраняй табличные строки одной строкой: номер строки, наименование, количество, цена, сумма.
- Не исправляй, не пересчитывай и не додумывай значения. Не пропускай строки, даже если они
  кажутся неважными.
- Ничего не комментируй и не добавляй пояснений — только текст документа.

Если снимок нечитаем (смазан, обрезан, тёмный), верни readable=false и тот текст, что удалось
разобрать."""


def _pdf_text(content: bytes) -> str | None:
    """Текстовый слой PDF. ``None`` — слоя нет (скан), нужна картинка."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(content))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception:  # noqa: BLE001 — битый или зашифрованный PDF не должен ронять приёмку
        logger.warning("не удалось прочитать PDF коммунальной платёжки", exc_info=True)
        return None
    text = "\n".join(pages).strip()
    return text or None


async def extract_text(
    content: bytes, *, mime: str | None, settings: Settings
) -> tuple[str | None, str]:
    """Достать текст документа. Возвращает ``(текст, каким способом)``.

    Способ возвращается наружу не для красоты: он уходит в ``recognition`` приёмки и объясняет
    человеку, откуда взялись поля, — а при разборе ошибки показывает, чей это промах, парсера
    или распознавания.
    """
    if not content:
        return None, "empty"

    if mime == "application/pdf":
        text = _pdf_text(content)
        if text:
            return text, "pdf_text"
        # Скан без текстового слоя — картинкой его в vision не отправить (тип document для
        # PDF модель принимает, но здесь это уже не OCR-задача): честно говорим, что не смогли.
        return None, "pdf_without_text_layer"

    if mime in HEIC_MIME_TYPES:
        content, mime = to_displayable(content, mime)
    if mime not in _VISION_MEDIA_TYPES:
        return None, "unsupported_media_type"

    try:
        payload = await call_tool(
            settings,
            tool=_OCR_TOOL,
            model=settings.utility_ocr_model,
            max_tokens=4096,
            content=[
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime,
                        "data": base64.b64encode(content).decode(),
                    },
                },
                {"type": "text", "text": _OCR_PROMPT},
            ],
        )
    except LlmCallError as exc:
        # Приёмку не роняем: без распознавания человек введёт сумму руками, глядя на снимок.
        logger.warning("vision-распознавание платёжки не удалось: %s", exc)
        return None, "vision_failed"

    if not payload:
        return None, "vision_empty"
    text = str(payload.get("text") or "").strip()
    if not text:
        return None, "vision_empty"
    return text, "vision" if payload.get("readable") else "vision_unreadable"
