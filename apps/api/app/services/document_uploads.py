"""Приём файла от человека: определение типа по содержимому и предел размера.

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ. Загрузка документа перестала быть привилегией одного экрана: счёт
приходит почтой и по ЭДО, коммунальная квитанция — фотографией из рук владельца. Правила
приёма при этом одни и те же, а цена ошибки в них не зависит от того, кто грузит.

ТИП — ПО СОДЕРЖИМОМУ, А НЕ ПО ЗАГОЛОВКУ. ``Content-Type`` пишет клиент, и верить ему нельзя.
Иначе SVG со скриптом, представленный как ``image/png``, отдавался бы обратно inline — то есть
исполнялся бы в origin приложения, с его куками и токеном. Отсюда же и белый список: у SVG нет
бинарной сигнатуры, и в него он не попадает вовсе.
"""

from __future__ import annotations

__all__ = ["MAX_UPLOAD_BYTES", "FILE_SIGNATURES", "sniff_mime"]

# Больше 25 МБ фотография квитанции не бывает даже с современного телефона, а лежит файл в
# строке таблицы — раздувать её нечем.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

FILE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"%PDF-", "application/pdf"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def sniff_mime(content: bytes) -> str | None:
    """Тип по сигнатуре. ``None`` — формат не поддержан, грузить нельзя."""
    for signature, mime in FILE_SIGNATURES:
        if content.startswith(signature):
            return mime
    # WEBP и HEIC — контейнерные форматы: сигнатура не в начале файла.
    if len(content) >= 12:
        if content[0:4] == b"RIFF" and content[8:12] == b"WEBP":
            return "image/webp"
        # Бренды HEIF, которые реально ставит техника Apple. mif1/msf1 — «просто HEIF»,
        # остальные — снимок и его последовательности; все они читаются одинаково.
        if content[4:8] == b"ftyp" and content[8:12] in (
            b"heic",
            b"heix",
            b"heim",
            b"heis",
            b"hevc",
            b"hevm",
            b"hevs",
            b"mif1",
            b"msf1",
        ):
            return "image/heic"
    return None
