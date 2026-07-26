"""Защита от двойного клика: единственность слота гарантирует БД, код её переживает.

Витрины налогов открываются в двух вкладках, а кнопки «Продвинуть готовые» и «Отправить
в банк» люди нажимают дважды. Схема «select → пусто → insert» от этого не защищает:
параллельные запросы оба видят пусто и оба пишут — появляются два обязательства на один
платёж (двойной счёт в долге и вычете).

Поэтому единственность слота держат ЧАСТИЧНЫЕ УНИКАЛЬНЫЕ ИНДЕКСЫ (миграция 0215), а этот
модуль превращает проигранную гонку в нормальный результат вместо 500-й: вставка идёт в
SAVEPOINT, при конфликте — перечит выигравшей строки и работа с ней.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


async def insert_or_reread[T](
    session: AsyncSession,
    instance: T,
    *,
    reread: Callable[[], Awaitable[T | None]],
) -> tuple[T, bool]:
    """Вставить строку; при конфликте уникального слота вернуть выигравшую чужую.

    Возвращает ``(строка, создана_ли_нами)``. SAVEPOINT не даёт ``IntegrityError``
    отравить внешнюю транзакцию — вызывающая ручка продолжает работу штатно.
    Конфликт НЕ по нашему слоту (перечит ничего не нашёл) пробрасывается наружу:
    прятать чужую ошибку целостности нельзя.
    """
    try:
        async with session.begin_nested():
            session.add(instance)
            await session.flush()
    except IntegrityError:
        if instance in session:
            session.expunge(instance)
        existing = await reread()
        if existing is None:
            raise
        return existing, False
    return instance, True


async def lock_row(session: AsyncSession, instance: object) -> None:
    """Взять строку под ``SELECT … FOR UPDATE`` — сериализовать повторные клики.

    Второй запрос ждёт коммита первого и видит уже изменённое состояние (например,
    интейк со статусом ``promoted``), поэтому отрабатывает как «уже сделано», а не
    делает работу второй раз. Для ещё не сохранённой строки (в тестах — созданной в
    той же транзакции) конкуренции нет, блокировка не нужна.
    """
    from sqlalchemy import inspect as sa_inspect

    if sa_inspect(instance).persistent:
        await session.refresh(instance, with_for_update=True)
