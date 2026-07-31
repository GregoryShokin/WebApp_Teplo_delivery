"""Диалог заведения карточки ОС: что отдано модели, а что удержано кодом.

Модель здесь определяет КАТЕГОРИЮ, а категория задаёт срок амортизации. Цена ошибки не в
кривом названии, а в том, что объект будет амортизироваться не те годы, и заметить это можно
только годы спустя. Поэтому проверяются именно границы, а не «умность» ответов.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AssetCategory
from app.services import asset_intake
from app.services.anthropic_client import LlmCallError
from app.services.asset_intake import (
    AssetIntakeError,
    IntakeTurn,
    build_prompt,
    next_step,
    parse_answer,
)


async def _categories(session: AsyncSession) -> list[AssetCategory]:
    return list((await session.scalars(select(AssetCategory).order_by(AssetCategory.name))).all())


async def test_prompt_carries_categories_with_their_useful_lives(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Категории уходят в промпт СО СРОКАМИ — иначе модель выбирает по звучанию названия.

    Срок здесь не украшение: он единственное, чем «Тепловое оборудование» отличается от
    «Вспомогательного» с точки зрения последствий.
    """
    async with async_session_factory() as session:
        categories = await _categories(session)
        prompt = build_prompt(
            purchase="купили стол", history=[], categories=categories, questions_left=3
        )

    assert "купили стол" in prompt
    assert "Тепловое оборудование — 7 лет" in prompt
    assert "Мебель и предметы интерьера — 7 лет" in prompt
    # Сколько ещё можно спросить — модель должна знать, чтобы не тянуть до бесконечности.
    assert "ещё вопросов: 3" in prompt


async def test_prompt_orders_a_verdict_when_questions_run_out(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Лимит исчерпан — промпт ТРЕБУЕТ карточку, а не ещё один вопрос."""
    async with async_session_factory() as session:
        prompt = build_prompt(
            purchase="стол",
            history=[IntakeTurn(question="Какой стол?", answer="обычный")],
            categories=await _categories(session),
            questions_left=0,
        )
    assert "Вопросы закончились" in prompt
    assert "ready" in prompt
    # Уже заданные вопросы и ответы обязаны быть в промпте, иначе модель спросит то же самое.
    assert "Какой стол? → обычный" in prompt


async def test_invented_category_is_dropped(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Категории вне справочника не существует, и подставлять её нельзя.

    У выдуманной категории нет срока службы, а объект без срока ``accrue_depreciation`` молча
    ПРОПУСКАЕТ. То есть красиво названная карточка никогда бы не амортизировалась. Лучше
    оставить поле пустым и дать человеку выбрать самому.
    """
    async with async_session_factory() as session:
        categories = await _categories(session)
        result = parse_answer(
            {
                "status": "ready",
                "name": "Стол производственный",
                "category_name": "Кухонная мебель из нержавейки",
            },
            categories,
        )
    assert result.category_id is None
    assert result.category_name is None
    assert result.name == "Стол производственный"


async def test_category_matches_by_name_ignoring_case(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Совпадение по имени без учёта регистра: модель пишет как в списке, но не байт в байт."""
    async with async_session_factory() as session:
        categories = await _categories(session)
        target = next(c for c in categories if c.name == "Тепловое оборудование")
        result = parse_answer(
            {"status": "ready", "category_name": "тепловое ОБОРУДОВАНИЕ"}, categories
        )
    assert result.category_id == target.id
    assert result.category_name == "Тепловое оборудование"


async def test_need_more_without_a_question_is_treated_as_ready(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """«Нужно уточнить» без вопроса — тупик: диалог не сдвинется, а человек застрянет."""
    async with async_session_factory() as session:
        result = parse_answer({"status": "need_more"}, await _categories(session))
    assert result.status == "ready"


async def test_question_limit_is_held_by_code_not_by_the_prompt(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Модель продолжает спрашивать после лимита — код всё равно завершает диалог.

    Промпт просит остановиться, но просьба не гарантия. Человек у кассы бросит бесконечный
    опрос и выберет статью попроще — покупка уйдёт мимо баланса, ровно как без всего контура.
    """

    async def _always_asks(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "need_more", "question": "А ещё?", "name": "Стол"}

    monkeypatch.setattr(asset_intake, "call_tool", _always_asks)
    history = [IntakeTurn(question=f"Вопрос {i}", answer="да") for i in range(3)]

    async with async_session_factory() as session:
        result = await next_step(session, purchase="стол", history=history)

    assert result.status == "ready"
    assert result.question is None


async def test_model_failure_surfaces_a_human_reason(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Модель недоступна — говорим об этом словами, а не падаем.

    Интерфейс по этой ошибке возвращается к ручной форме: недоступность модели не повод не
    записать покупку. Контур, защищающий баланс, не должен сам мешать провести платёж.
    """

    async def _fails(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise LlmCallError("Ключ ANTHROPIC_API_KEY не принят (401)")

    monkeypatch.setattr(asset_intake, "call_tool", _fails)

    async with async_session_factory() as session:
        with pytest.raises(AssetIntakeError, match="401"):
            await next_step(session, purchase="рисоварка", history=[])


async def test_empty_purchase_is_refused_before_calling_the_model(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Пустой ввод модели не показываем — платить за пробелы незачем."""

    async def _must_not_be_called(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("модель не должна вызываться на пустом вводе")

    monkeypatch.setattr(asset_intake, "call_tool", _must_not_be_called)

    async with async_session_factory() as session:
        with pytest.raises(AssetIntakeError, match="что купили"):
            await next_step(session, purchase="   ", history=[])


async def test_suggestions_are_trimmed_to_four(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Варианты ответа — для быстрого тапа с планшета, а не список на весь экран."""
    async with async_session_factory() as session:
        result = parse_answer(
            {
                "status": "need_more",
                "question": "Из чего стол?",
                "suggestions": ["нержавейка", "дерево", "пластик", "стекло", "камень", "  "],
            },
            await _categories(session),
        )
    assert result.suggestions == ("нержавейка", "дерево", "пластик", "стекло")
