"""Заведение карточки ОС диалогом: сотрудник пишет «купили стол», модель дособирает остальное.

ЗАЧЕМ. Категория карточки задаёт СРОК полезного использования, а срок — всю амортизацию. Стол
из нержавейки на кухню и стол в офис — разные категории и разные сроки; ноутбук, рисоварка и
кондиционер тем более. Заполнять это руками должен человек, который в вопросе не обязан
разбираться: покупку заводит тот, кто платил, а не бухгалтер по основным средствам. Ставка на
его компетентность — это ставка на то, что амортизация поедет молча и вскроется через годы
(решение владельца 2026-07-31).

Поэтому вместо формы — один input и уточняющие вопросы: «что за стол?» → «из нержавеющей
стали» → модель понимает, что это кухонное оборудование, а не офисная мебель, и что размеры
здесь важнее марки. Для рисоварки наоборот: марка и модель обязательны, без них объект не
опознать при следующей инвентаризации.

ГРАНИЦЫ, КОТОРЫЕ МОДЕЛИ НЕ ОТДАНЫ.

* КАТЕГОРИЯ — ТОЛЬКО ИЗ СПРАВОЧНИКА. Список из десяти категорий с их сроками кладётся в
  промпт, а ответ сверяется с ним по имени. Выдуманную категорию отбрасываем: она не имеет
  срока, а объект без срока молча выпадает из начисления амортизации.
* ДЕНЬГИ МОДЕЛЬ НЕ ТРОГАЕТ ВОВСЕ. Стоимость карточки равна сумме платежа, её кладёт код. Это
  то же правило, что в переоценке и в ИИ-ревьюере налогов: дело модели — интерпретация, не
  вычисления.
* ПОСЛЕДНЕЕ СЛОВО ЗА ЧЕЛОВЕКОМ. Диалог заканчивается КАРТОЧКОЙ-ПРЕДЛОЖЕНИЕМ, которую сотрудник
  видит целиком и подтверждает. Это дёшево — он только что сам отвечал на вопросы, — и
  оставляет ошибку модели видимой до записи, а не после.

ЛИМИТ ВОПРОСОВ. Три. Не ради экономии: диалог заводят у кассы с планшета в руках, и на четвёртом
вопросе человек бросит и выберет статью попроще — то есть покупка уйдёт мимо баланса, ровно как
без всего этого контура. Упёрлись в лимит — модель обязана собрать лучшее предположение из того,
что есть, и отдать карточку.

БЕЗ МОДЕЛИ КОНТУР НЕ ВСТАЁТ. Ключа нет, релей лёг, регион не тот — вызов честно отказывает, а
интерфейс возвращается к ручной форме. Платёж должен провестись в любом случае: недоступность
модели не повод не записать покупку.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models import AssetCategory
from app.services.anthropic_client import LlmCallError, call_tool

logger = logging.getLogger(__name__)

# Сколько уточнений модель может задать, прежде чем обязана выдать карточку. См. докстринг.
MAX_QUESTIONS = 3

INTAKE_TOOL: dict[str, Any] = {
    "name": "asset_intake",
    "description": "Уточнить покупку основного средства или собрать карточку.",
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["need_more", "ready"],
                "description": "need_more — нужен ещё один ответ; ready — карточка собрана.",
            },
            "question": {
                "type": "string",
                "description": "Один короткий вопрос сотруднику. Только при status=need_more.",
            },
            "why": {
                "type": "string",
                "description": "Одной фразой: зачем это спрашиваем (что от ответа зависит).",
            },
            "suggestions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "2–4 коротких варианта ответа для быстрого выбора.",
            },
            "name": {"type": "string", "description": "Наименование карточки."},
            "brand": {"type": "string", "description": "Марка (производитель), если уместна."},
            "model": {"type": "string", "description": "Модель, если уместна."},
            "category_name": {
                "type": "string",
                "description": "Категория ТОЧНО из предложенного списка.",
            },
            "specs": {
                "type": "string",
                "description": "Существенные характеристики одной строкой (материал, размеры).",
            },
            "reason": {
                "type": "string",
                "description": "Почему выбрана эта категория — одной фразой для человека.",
            },
        },
        "required": ["status"],
    },
}

SYSTEM_PROMPT = """Ты помогаешь сотруднику ресторана завести карточку основного средства по
факту покупки. Сотрудник в бухгалтерии не разбирается — задавай простые вопросы без терминов.

Твоя задача — определить КАТЕГОРИЮ (от неё зависит срок амортизации) и собрать то, по чему
объект узнают при следующей инвентаризации.

Правила:
1. Категорию выбирай ТОЛЬКО из предложенного списка, слово в слово. Своих не придумывай.
2. Спрашивай ТОЛЬКО то, что меняет категорию или позволяет опознать объект. Не спрашивай цену,
   дату, поставщика, количество — это уже известно.
3. Один вопрос за раз, коротко, по-русски. Давай 2–4 варианта ответа для быстрого выбора.
4. Для техники (рисоварка, холодильник, ноутбук, кондиционер) марка и модель важны — спроси их,
   если не названы. Для мебели важнее материал и размер, марка обычно не нужна.
5. Если из ответа уже всё понятно — не тяни, отдавай карточку (status=ready).
6. Наименование пиши как в накладной: «Стол производственный из нержавеющей стали», а не «стол».
"""


class AssetIntakeError(RuntimeError):
    """Диалог не состоялся. Текст пригоден для показа человеку."""


@dataclass(frozen=True)
class IntakeTurn:
    """Ход диалога: вопрос модели и ответ сотрудника."""

    question: str
    answer: str


@dataclass(frozen=True)
class IntakeResult:
    """Что модель ответила: следующий вопрос либо готовая карточка."""

    status: str
    question: str | None = None
    why: str | None = None
    suggestions: tuple[str, ...] = ()
    name: str | None = None
    brand: str | None = None
    model: str | None = None
    category_id: object | None = None
    category_name: str | None = None
    specs: str | None = None
    reason: str | None = None


def build_prompt(
    *,
    purchase: str,
    history: list[IntakeTurn],
    categories: list[AssetCategory],
    questions_left: int,
) -> str:
    """Собрать промпт: что купили, о чём уже спросили и из каких категорий выбирать.

    Срок службы у каждой категории — не украшение: он объясняет модели, ЧЕМ категории
    отличаются, и удерживает её от выбора «по звучанию названия».
    """
    lines = [f"Сотрудник написал, что купили: «{purchase.strip()}»."]
    if history:
        lines.append("\nУже уточнили:")
        lines.extend(f"— {turn.question} → {turn.answer}" for turn in history)
    lines.append("\nДоступные категории (название — срок службы):")
    lines.extend(
        f"— {category.name} — {category.useful_life_months // 12} лет" for category in categories
    )
    if questions_left <= 0:
        lines.append(
            "\nВопросы закончились. Собери карточку из того, что есть: выбери самую подходящую "
            "категорию и заполни, что известно. status обязан быть ready."
        )
    else:
        lines.append(
            f"\nМожешь задать ещё вопросов: {questions_left}. Если данных уже хватает — "
            f"не спрашивай, отдай карточку."
        )
    return "\n".join(lines)


def _clean(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def parse_answer(payload: dict[str, Any], categories: list[AssetCategory]) -> IntakeResult:
    """Разобрать ответ модели и приземлить категорию на справочник.

    Категория сверяется ПО ИМЕНИ и без учёта регистра. Не нашлась — считаем, что категории нет:
    выдуманная не имеет срока службы, а объект без срока молча выпадает из начисления. Пусть
    лучше человек выберет её сам, чем карточка окажется вечно неамортизируемой.
    """
    status = "ready" if str(payload.get("status") or "").strip() == "ready" else "need_more"
    by_name = {category.name.casefold(): category for category in categories}
    raw_category = _clean(payload.get("category_name"))
    category = by_name.get(raw_category.casefold()) if raw_category else None
    if raw_category and category is None:
        logger.warning(
            "asset_intake: модель предложила категорию вне справочника: %s", raw_category
        )

    suggestions = payload.get("suggestions")
    tidy = (
        tuple(str(item).strip() for item in suggestions if str(item).strip())[:4]
        if isinstance(suggestions, list)
        else ()
    )

    question = _clean(payload.get("question"))
    if status == "need_more" and question is None:
        # Модель сказала «нужно уточнить», но не спросила. Тупик: диалог не сдвинется. Считаем
        # это готовностью — человек всё равно увидит карточку и поправит.
        status = "ready"

    return IntakeResult(
        status=status,
        question=question,
        why=_clean(payload.get("why")),
        suggestions=tidy,
        name=_clean(payload.get("name")),
        brand=_clean(payload.get("brand")),
        model=_clean(payload.get("model")),
        category_id=category.id if category is not None else None,
        category_name=category.name if category is not None else None,
        specs=_clean(payload.get("specs")),
        reason=_clean(payload.get("reason")),
    )


async def next_step(
    session: AsyncSession,
    *,
    purchase: str,
    history: list[IntakeTurn],
    settings: Settings | None = None,
) -> IntakeResult:
    """Один ход диалога: спросить модель, что делать дальше.

    Состояния на сервере нет СОЗНАТЕЛЬНО: клиент присылает всю переписку целиком. Диалог живёт
    внутри одного окна платежа, его бросают на полпути чаще, чем доводят, и хранить такие
    черновики в базе значило бы копить мусор ради ничего.
    """
    if not purchase.strip():
        raise AssetIntakeError("Напишите, что купили.")
    settings = settings or get_settings()
    categories = (await session.scalars(select(AssetCategory).order_by(AssetCategory.name))).all()
    if not categories:
        raise AssetIntakeError(
            "Справочник категорий пуст — без него не определить срок службы объекта."
        )

    prompt = build_prompt(
        purchase=purchase,
        history=history,
        categories=list(categories),
        questions_left=MAX_QUESTIONS - len(history),
    )
    try:
        payload = await call_tool(
            settings,
            tool=INTAKE_TOOL,
            model=settings.fixed_asset_ai_model,
            prompt=prompt,
            system=SYSTEM_PROMPT,
            max_tokens=1024,
        )
    except LlmCallError as exc:
        raise AssetIntakeError(str(exc)) from exc

    result = parse_answer(payload, list(categories))
    # Лимит держит КОД, а не доверие к промпту: модель может продолжать спрашивать бесконечно,
    # а человек у кассы бросит диалог и выберет статью попроще — покупка уйдёт мимо баланса.
    if result.status == "need_more" and len(history) >= MAX_QUESTIONS:
        return IntakeResult(
            status="ready",
            name=result.name or purchase.strip(),
            brand=result.brand,
            model=result.model,
            category_id=result.category_id,
            category_name=result.category_name,
            specs=result.specs,
            reason=result.reason,
        )
    return result
