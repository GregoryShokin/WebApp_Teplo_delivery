"""Переоценка основного средства по свободному тексту менеджера.

Решение владельца 2026-07-30: менеджер открывает карточку и пишет, что с объектом —
«сломался компрессор», «поцарапана дверь, холод не держит». В фоне модель оценивает, насколько
это меняет стоимость. Владелец подтверждает или отклоняет.

Почему модель, а не правила. Классификацию покупки владелец сознательно оставил на правилах —
там сравнение с порогом и больше ничего. Здесь вход другой: свободный текст, из которого надо
понять, что сломалось, насколько это бьёт по стоимости и надо ли её вообще трогать.

МОДЕЛЬ НЕ СЧИТАЕТ ДЕНЬГИ. Это правило проекта, а не осторожность: «LLM ненадёжна в арифметике,
её дело — интерпретация и приоритизация, не вычисления» (ИИ-ревьюер налогов, и там же тест,
который стережёт формулировку в промпте). Поэтому остаточную стоимость, накопленный износ и
возраст объекта считает КОД и кладёт в промпт готовыми, а у модели просит только ДОЛЮ потери
стоимости — 0.0 если поломка на цену не влияет, 1.0 если объект превратился в лом. Сумму из
доли снова считает код.

АВТОПРИМЕНЕНИЯ НЕТ. Уверенность модели сохраняется и показывается, но порога, при котором
стоимость меняется сама, не существует — как и у ИИ-ревьюера налогов. Стоимость актива меняет
человек. Самооценка модели для этого не годится в принципе.

ОШИБКА МАТЕРИАЛИЗУЕТСЯ В СТРОКЕ. Вызов фоновый, но результата ждёт владелец. Бросить наружу —
значит оставить его перед вечным «ожидание»; проглотить — потерять сообщение менеджера о
поломке. Поэтому неудача пишется в саму запись: статус ``failed`` и человеческая причина.
Свидетельство о поломке остаётся в истории в любом случае.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models import AssetCategory, AssetConditionReport, FixedAsset
from app.services.anthropic_client import LlmCallError, call_tool
from app.services.fixed_assets import accumulated_depreciation, resolve_useful_life

logger = logging.getLogger(__name__)

CallTool = Callable[..., Awaitable[dict[str, Any]]]

# Что именно случилось с объектом. Enum берём из питоновской константы, чтобы словарь домена
# не разошёлся с промптом — приём из ИИ-ревьюера налогов.
IMPACT_KINDS = (
    "breakdown",  # поломка узла: объект не работает или работает хуже
    "wear",  # износ, естественное старение
    "damage",  # повреждение, не влияющее на работу (косметика)
    "improvement",  # объекту стало лучше: замена узла, доработка
    "none",  # на стоимость не влияет
)

_SYSTEM = """Ты оцениваешь состояние оборудования общепита для управленческого учёта.

Тебе дают карточку основного средства с уже посчитанными цифрами и сообщение менеджера о том,
что с объектом произошло. Твоя работа — понять, насколько это меняет РЫНОЧНУЮ стоимость
объекта, и объяснить решение бухгалтеру.

Правила:
* НЕ вычисляй суммы. Все деньги уже посчитаны и даны тебе готовыми. От тебя нужна только ДОЛЯ
  потери стоимости от текущей остаточной — число от 0 до 1.
* Косметика (царапина, вмятина на корпусе, потёртость) почти не влияет на стоимость рабочего
  оборудования: доля 0 или близко к нулю.
* Отказ узла, без которого объект не выполняет функцию (компрессор у холодильника, ТЭН у
  печи), обрушивает стоимость до цены оставшегося железа: доля 0.7-0.9.
* Если из сообщения непонятно, что случилось, или его нельзя связать со стоимостью — ставь
  долю 0 и needs_human=true. Придумывать нельзя.
* Если объекту стало ЛУЧШЕ (заменили узел на новый, доработали), доля отрицательной не
  бывает: поставь 0, вид воздействия improvement и опиши это в обосновании — решение о
  повышении стоимости принимает человек.
* Обоснование пиши по-русски, для человека, одним-двумя предложениями. Ссылайся на то, что
  написал менеджер, а не на общие соображения."""

_TOOL: dict[str, Any] = {
    "name": "assess_asset_condition",
    "description": "Оценить, как описанное менеджером состояние влияет на стоимость объекта.",
    "input_schema": {
        "type": "object",
        "properties": {
            "impact_kind": {
                "type": "string",
                "enum": list(IMPACT_KINDS),
                "description": "Что произошло с объектом.",
            },
            "value_loss_share": {
                "type": "string",
                "description": (
                    "Доля потери ОСТАТОЧНОЙ стоимости, число от 0 до 1 строкой. "
                    "0 — на стоимость не влияет, 1 — объект превратился в лом."
                ),
            },
            "reasoning": {
                "type": "string",
                "description": "Обоснование по-русски, 1-2 предложения, для бухгалтера.",
            },
            "confidence": {
                "type": "number",
                "description": "Насколько уверен в оценке, от 0 до 1.",
            },
            "needs_human": {
                "type": "boolean",
                "description": "Из сообщения нельзя сделать вывод — нужен человек.",
            },
        },
        "required": ["impact_kind", "value_loss_share", "reasoning", "confidence", "needs_human"],
    },
}


def _decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, AttributeError, TypeError, ValueError):
        return None


async def build_prompt(session: AsyncSession, report: AssetConditionReport) -> str:
    """Собрать карточку объекта с ГОТОВЫМИ цифрами и сообщение менеджера.

    Все деньги считаются здесь. Модели остаётся интерпретация текста — ровно то, в чём она
    сильна, и ничего из того, в чём ненадёжна.
    """
    asset = await session.get(FixedAsset, report.asset_id)
    if asset is None:
        raise LlmCallError("Объект не найден")

    accrued = await accumulated_depreciation(session, asset.id)
    initial = Decimal(str(asset.initial_cost))
    residual = max(initial - accrued, Decimal("0.00"))
    life = await resolve_useful_life(session, asset)
    category = await session.get(AssetCategory, asset.category_id) if asset.category_id else None

    months_used = 0
    if asset.commissioned_on is not None:
        today = datetime.now(UTC).date()
        months_used = max(
            0,
            (today.year - asset.commissioned_on.year) * 12
            + (today.month - asset.commissioned_on.month),
        )

    wear = f"{(accrued / initial * 100):.0f}%" if initial > 0 else "не определён"

    return (
        "КАРТОЧКА ОБЪЕКТА\n"
        f"Наименование: {asset.name}\n"
        f"Бренд и модель: {asset.brand_model or 'не указаны'}\n"
        f"Категория: {category.name if category else 'не задана'}\n"
        f"Первоначальная стоимость: {initial:.2f} ₽\n"
        f"Начислено амортизации: {accrued:.2f} ₽\n"
        f"ОСТАТОЧНАЯ СТОИМОСТЬ: {residual:.2f} ₽\n"
        f"Износ: {wear}\n"
        f"Срок полезного использования: {life or 'не задан'} мес\n"
        f"В эксплуатации: {months_used} мес\n"
        f"Текущий статус: {asset.status}\n\n"
        "СООБЩЕНИЕ МЕНЕДЖЕРА\n"
        f"{report.message}\n\n"
        "Оцени, как это влияет на остаточную стоимость. Суммы НЕ вычисляй — верни только долю."
    )


def apply_model_answer(
    report: AssetConditionReport, payload: dict[str, Any], *, model: str
) -> None:
    """Разобрать ответ модели и записать предложение в строку.

    Каждое поле приводим вручную с дефолтом — pydantic к ответу модели в проекте не применяют:
    у модели нет обязанности соблюдать схему, а падение разбора не должно терять сообщение
    менеджера.
    """
    share = _decimal(payload.get("value_loss_share"))
    kind = str(payload.get("impact_kind") or "").strip()
    reasoning = str(payload.get("reasoning") or "").strip()
    needs_human = bool(payload.get("needs_human"))
    confidence = _decimal(payload.get("confidence"))

    report.model = model
    report.proposed_reason = reasoning or None
    if confidence is not None and Decimal("0") <= confidence <= Decimal("1"):
        report.confidence = confidence

    unusable = (
        kind not in IMPACT_KINDS
        or share is None
        or share < 0
        or share > 1
        or needs_human
        or kind in ("none", "improvement")
    )
    if unusable:
        # Снижения нет — но запись всё равно доходит до владельца: сообщение менеджера о
        # поломке ценно само по себе, даже когда модель не смогла оценить его в деньгах.
        report.proposed_cost = None
        report.status = "proposed"
        if not reasoning:
            report.proposed_reason = "Модель не смогла связать сообщение со стоимостью объекта"
        return

    residual = Decimal(str(report.cost_before))
    # Сумму считает КОД: модель дала только долю.
    proposed = (residual * (Decimal("1") - share)).quantize(Decimal("0.01"))
    report.proposed_cost = max(proposed, Decimal("0.00"))
    report.status = "proposed"


async def process_report(
    session: AsyncSession,
    report: AssetConditionReport,
    *,
    settings: Settings | None = None,
    call: CallTool | None = None,
) -> AssetConditionReport:
    """Спросить модель по одной записи и записать исход. Наружу не бросает.

    ``call`` инъектируется ради тестов: контур, двигающий стоимость активов, обязан
    проверяться без сети — образец взят у ИИ-ревьюера налогов.
    """
    settings = settings or get_settings()
    caller = call or call_tool

    try:
        prompt = await build_prompt(session, report)
        payload = await caller(
            settings,
            tool=_TOOL,
            prompt=prompt,
            model=settings.fixed_asset_ai_model,
            system=_SYSTEM,
            max_tokens=1024,
        )
        apply_model_answer(report, payload, model=settings.fixed_asset_ai_model)
        report.error = None
    except LlmCallError as exc:
        report.status = "failed"
        report.error = str(exc)[:1000]
    except Exception as exc:  # noqa: BLE001 - фоновый прогон не имеет права ронять джобу
        logger.warning("переоценка ОС: разбор сообщения не удался", exc_info=True)
        report.status = "failed"
        report.error = str(exc)[:1000]

    await session.flush()
    return report


async def process_pending(
    session: AsyncSession,
    *,
    limit: int = 20,
    settings: Settings | None = None,
    call: CallTool | None = None,
) -> dict[str, int]:
    """Обработать накопившиеся сообщения — по одной транзакции на запись.

    Коммит на КАЖДУЮ единицу работы: сбой на одном объекте не откатывает уже разобранные.
    Упавшая запись получает статус ``failed`` и повторно НЕ берётся — иначе непонятное
    сообщение крутило бы платный вызов модели в цикле до конца времён.
    """
    pending = (
        await session.scalars(
            select(AssetConditionReport)
            .where(AssetConditionReport.status == "pending")
            .order_by(AssetConditionReport.created_at)
            .limit(limit)
        )
    ).all()

    counts = {"processed": 0, "proposed": 0, "failed": 0}
    for report in pending:
        await process_report(session, report, settings=settings, call=call)
        await session.commit()
        counts["processed"] += 1
        counts["proposed" if report.status == "proposed" else "failed"] += 1
    return counts


async def submit_report(
    session: AsyncSession,
    *,
    asset_id: uuid.UUID,
    message: str,
    user_id: uuid.UUID | None,
) -> AssetConditionReport:
    """Принять сообщение менеджера. Модель спросит фоновая джоба.

    Стоимость на момент обращения фиксируем здесь: пока запись ждёт своей очереди, объект
    может успеть самортизироваться, и предложение считалось бы от другой базы.
    """
    asset = await session.get(FixedAsset, asset_id)
    if asset is None:
        raise LlmCallError("Объект не найден")

    accrued = await accumulated_depreciation(session, asset.id)
    residual = max(Decimal(str(asset.initial_cost)) - accrued, Decimal("0.00"))

    report = AssetConditionReport(
        asset_id=asset.id,
        message=message.strip(),
        status="pending",
        cost_before=residual,
        reported_by_user_id=user_id,
    )
    session.add(report)
    await session.flush()
    return report


async def decide_report(
    session: AsyncSession,
    *,
    report: AssetConditionReport,
    accept: bool,
    user_id: uuid.UUID | None,
) -> AssetConditionReport:
    """Решение владельца: применить предложение модели или отклонить.

    Применение меняет ПЕРВОНАЧАЛЬНУЮ стоимость, а не остаточную: остаточная — производная от
    начислений, отдельно её хранить негде. Чтобы объект после переоценки имел ровно ту
    остаточную, которую предложила модель, новой первоначальной становится «предложенная плюс
    уже начисленный износ». График дальше поедет от неё.
    """
    if report.status != "proposed":
        raise LlmCallError("Это предложение уже обработано")

    report.decided_by_user_id = user_id
    report.decided_at = datetime.now(UTC)

    if not accept or report.proposed_cost is None:
        report.status = "dismissed"
        await session.flush()
        return report

    asset = await session.get(FixedAsset, report.asset_id)
    if asset is None:
        raise LlmCallError("Объект не найден")

    accrued = await accumulated_depreciation(session, asset.id)
    asset.initial_cost = (Decimal(str(report.proposed_cost)) + accrued).quantize(Decimal("0.01"))
    asset.review_status = "ok"
    asset.review_reason = None
    report.status = "applied"

    # Хранимые остатки прошлых месяцев считались от старой базы — пересчитываем хвост.
    from app.services.fixed_assets import recompute_residuals

    await recompute_residuals(session, asset.id)
    await session.flush()
    return report
