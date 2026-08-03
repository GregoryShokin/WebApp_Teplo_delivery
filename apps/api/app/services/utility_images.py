"""HEIC → JPEG: снимок с айфона, пригодный и для браузера, и для распознавания.

ЗАЧЕМ. Айфон по умолчанию снимает в HEIC. Через Telegram файл обычно доезжает уже пережатым в
JPEG, но при прямой загрузке из «Файлов» приходит как есть — и упирается сразу в две стены:
браузеры, кроме Safari, HEIC не показывают (человек увидит пустой прямоугольник вместо
квитанции), а vision-модель такой тип не принимает вовсе. Отклонять снимок нельзя: для владельца
это обычная фотография, и объяснение «пересохраните в другом формате» здесь неуместно.

ОРИГИНАЛ ХРАНИМ КАК ПРИНЕСЛИ. Конвертация делается на лету — и при показе, и перед
распознаванием. Так в базе лежит ровно тот файл, который прислал человек (для спора с
арендодателем важен он, а не наша пережатая копия), а неудача конвертации не портит приёмку:
документ останется, просто не покажется.

ЦЕНА. Конвертация идёт на каждый показ, но снимки редкие и мелкие, а альтернатива — вторая
колонка с производным файлом и миграция под неё — дороже и требует следить за согласованностью
двух копий.
"""

from __future__ import annotations

import logging
from io import BytesIO

logger = logging.getLogger(__name__)

__all__ = ["HEIC_MIME_TYPES", "sniff_media_type", "to_displayable"]

# HEIC/HEIF в тех вариантах, которые реально приходят с техники Apple.
HEIC_MIME_TYPES = frozenset({"image/heic", "image/heif"})

# Типы, которые ничего не сообщают о содержимом: их ставят почтовые клиенты, когда им лень
# определять формат. Браузер с таким типом PDF не рисует — он его СКАЧИВАЕТ, и окно разбора
# показывает пустой прямоугольник вместо счёта.
_OPAQUE_MIME_TYPES = frozenset(
    {"", "application/octet-stream", "binary/octet-stream", "application/binary"}
)

# Качество: 88 — визуально неотличимо от оригинала на тексте квитанции, но заметно легче.
# Ниже начинают «плыть» тонкие цифры, а их-то и нужно прочитать.
_JPEG_QUALITY = 88


def sniff_media_type(
    content: bytes, declared: str | None, *, fallback: str = "application/octet-stream"
) -> str:
    """Тип файла по его первым байтам, когда заявленному верить нельзя.

    ЗАЧЕМ. Тип вложения мы берём из письма (``part.get_content_type()``), а отправитель волен
    поставить туда что угодно. 1С-рассылки и часть почтовых шлюзов подписывают PDF как
    ``application/octet-stream`` — и это не косметика: браузер такой ответ не рисует во фрейме,
    а скачивает файл на диск. В окне разбора вместо счёта оставался пустой прямоугольник, а
    в «Загрузках» появлялась копия — ровно то, ради чего окно и открывали, уезжало мимо.

    Содержимое, в отличие от заголовка, не врёт: у всех интересных нам форматов есть сигнатура
    в первых байтах. Заявленный тип оставляем, если он о чём-то говорит, — там может быть
    точность, которой сигнатура не даёт (например, ``image/heif`` против ``image/heic``).

    ``fallback`` — чем назваться, если и заявленный тип пуст, и сигнатура не узнана. Вызывающий
    знает свой канал: у очереди счетов это ``application/pdf`` (там девять из десяти вложений —
    PDF, и показать во фрейме ошибку просмотрщика честнее, чем молча скачать файл).
    """
    if declared and declared.strip().lower() not in _OPAQUE_MIME_TYPES:
        return declared
    head = content[:16]
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    # HEIC/HEIF: контейнер ISO-BMFF, бренд стоит после размера бокса ftyp.
    if content[4:8] == b"ftyp" and content[8:12] in (b"heic", b"heix", b"hevc", b"mif1"):
        return "image/heic"
    return fallback


def to_displayable(content: bytes, mime: str | None) -> tuple[bytes, str]:
    """Вернуть пару «байты, тип», пригодную для браузера и vision.

    Не-HEIC возвращается как есть. Если конвертация невозможна (пакет отсутствует, файл битый),
    возвращается оригинал — вызывающий код сам решит, что с ним делать; ронять приёмку из-за
    формата картинки нельзя.
    """
    if mime not in HEIC_MIME_TYPES:
        return content, mime or "application/octet-stream"
    try:
        # Импорт ленивый: до пересборки образа пакета может не быть, и на этом же держится
        # проверка «что будет, если его нет».
        import pillow_heif
        from PIL import Image

        pillow_heif.register_heif_opener()
        image = Image.open(BytesIO(content))
        # HEIC бывает с альфа-каналом и в цветовых профилях, которые JPEG не принимает.
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        buffer = BytesIO()
        # exif сохраняем: в нём ориентация съёмки, без неё квитанция ляжет боком. Значение
        # берём через ``or``, а не через default у ``get``: ключ в info присутствует и равен
        # None, и Pillow на нём падает — конвертация тихо возвращала оригинал.
        exif = image.info.get("exif") or b""
        image.save(buffer, format="JPEG", quality=_JPEG_QUALITY, exif=exif)
    except Exception:  # noqa: BLE001 — битый снимок не должен закрывать путь к документу
        logger.warning("не удалось конвертировать HEIC в JPEG", exc_info=True)
        return content, mime or "image/heic"
    return buffer.getvalue(), "image/jpeg"
