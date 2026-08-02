"""Переоценка основного средства по свободному тексту менеджера.

Решение владельца 2026-07-30: менеджер открывает карточку и пишет, что с объектом —
«сломался компрессор», «поцарапана дверь, холод не держит». В фоне модель оценивает, насколько
это меняет стоимость. Владелец подтверждает или отклоняет.

ДВА РАЗНЫХ ВОПРОСА (решение владельца 2026-07-31). Обращение бывает двух видов, и предмет
разговора у них разный:

* ``incident`` — ПОЛОМКА у работающего объекта. Вопрос: сколько он теперь стоит. Модель отдаёт
  долю потери остаточной стоимости, код считает рубли.
* ``incident`` с видом ``loss`` — объекта НЕТ: украли, утратили, уничтожен. Вопрос уже не
  «сколько стоит», а «списывать ли»: предложением становится ВЫБЫТИЕ, остаточная стоимость
  уходит убытком, карточка не переписывается. Появился 2026-08-02 по живому случаю: менеджер
  написал про уличную скамью «украли», модель верно ответила «стоимость полностью утрачена» —
  и владельцу не на что было нажать, потому что словаря для утраты не существовало, а без
  него код выбрасывал ответ модели целиком.
* ``purchase`` — купили Б/У. Вопрос: сколько ему осталось РАБОТАТЬ. Денег этот разговор не
  касается вовсе: за б/у уже заплатили меньше, продавец износ учёл, и списать цену второй раз
  значило бы посчитать его дважды. А вот СРОК приходит из категории и молча считает объект
  новым — пароконвектомат 2018 года амортизировался бы ещё семь лет. Модель отдаёт долю
  израсходованного срока, код считает месяцы.

Второй вид появился по подсказке самой модели: на живом прогоне 31.07.2026 она приписала к
оценке б/у пароконвектомата, что карточка показывает объект как новый (0% износа, 0 мес в
эксплуатации), и это не сходится с годом выпуска. Ошибку не видно ни в одной цифре на экране —
сумма, категория и дата верны каждая по отдельности.

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
from app.services.asset_disposal import dispose_asset
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
    "loss",  # объекта физически нет: украли, утратили, уничтожен
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
* ОБЪЕКТА БОЛЬШЕ НЕТ — украли, потеряли, сгорел, разбит вдребезги, увезли на металлолом —
  это impact_kind=loss и доля 1. Речь уже не о том, сколько объект стоит, а о том, что его
  нет: приложение предложит владельцу списать его целиком. Не путай с поломкой: сломанный
  объект стоит на месте и что-то стоит, утраченного объекта не существует.
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


# Потолок доли израсходованного срока. Единица означала бы, что объект — лом; за лом денег не
# платят, а мы разбираем состоявшуюся покупку. Упёршийся в потолок ответ оставляет объекту
# десятую часть срока — этого хватает, чтобы амортизация пошла, а не свернулась в ноль.
MAX_LIFE_USED_SHARE = Decimal("0.9")

_PURCHASE_SYSTEM = """Ты оцениваешь, сколько осталось работать б/у оборудованию общепита,
которое ресторан только что купил.

Тебе дают карточку покупки и то, что сотрудник сказал о состоянии объекта. Твоя работа — понять,
какая ДОЛЯ срока службы у объекта уже израсходована к моменту покупки.

Правила:
* ДЕНЕГ НЕ КАСАЙСЯ. Цена уже уплачена, и износ в ней учтён продавцом — за б/у платят меньше.
  Предлагать скидку значит посчитать износ дважды. От тебя нужен только срок.
* НЕ вычисляй месяцы и годы. Верни ДОЛЮ от 0 до 1: 0 — объект как новый, 0.5 — половина срока
  позади, 0.9 — дорабатывает последнее. Месяцы посчитают без тебя.
* Главный признак — ВОЗРАСТ. Назван год выпуска или сколько лет отработал — считай от него по
  сроку службы категории.
* Второй признак — состояние. Ухоженный объект своего возраста ближе к норме, разбитый —
  хуже; замена крупного узла (компрессор, ТЭН, мотор) срок, наоборот, продлевает.
* Если ни возраста, ни внятного состояния — ставь долю 0 и needs_human=true. Придумывать
  нельзя: заниженный срок завысит расходы, завышенный оставит на балансе мёртвое железо.
* Обоснование пиши по-русски, для человека, одним-двумя предложениями, и ссылайся на то, что
  сказал сотрудник."""

_PURCHASE_TOOL: dict[str, Any] = {
    "name": "assess_used_asset_life",
    "description": "Оценить, какая доля срока службы б/у объекта уже израсходована.",
    "input_schema": {
        "type": "object",
        "properties": {
            "life_used_share": {
                "type": "string",
                "description": (
                    "Доля УЖЕ ИЗРАСХОДОВАННОГО срока службы, число от 0 до 1 строкой. "
                    "0 — объект как новый, 0.5 — половина позади."
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
                "description": "Ни возраста, ни состояния — оценить нечем, нужен человек.",
            },
        },
        "required": ["life_used_share", "reasoning", "confidence", "needs_human"],
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


async def build_purchase_prompt(session: AsyncSession, report: AssetConditionReport) -> str:
    """Собрать карточку ПОКУПКИ б/у: что купили, за сколько и на какой срок рассчитана категория.

    Срок категории здесь — не справка, а знаменатель: доля, которую вернёт модель, берётся
    именно от него. Без него «половина срока позади» не значит ничего.

    Цену показываем, но ответа про неё не просим. Она нужна как признак состояния: холодильник
    за восемь тысяч и такой же за восемьдесят — разные объекты, даже если описаны одинаково.
    """
    asset = await session.get(FixedAsset, report.asset_id)
    if asset is None:
        raise LlmCallError("Объект не найден")

    life = await resolve_useful_life(session, asset)
    category = await session.get(AssetCategory, asset.category_id) if asset.category_id else None
    if not life:
        # Без срока считать долю не от чего. Это не сбой модели, а незаполненная категория —
        # владелец увидит запись и поставит срок сам.
        raise LlmCallError("У объекта не задан срок службы — от чего считать остаток, неизвестно")

    return (
        "КАРТОЧКА ПОКУПКИ\n"
        f"Наименование: {asset.name}\n"
        f"Бренд и модель: {asset.brand_model or 'не указаны'}\n"
        f"Категория: {category.name if category else 'не задана'}\n"
        f"Срок службы для НОВОГО объекта этой категории: {life} мес ({life // 12} лет)\n"
        f"Заплачено: {Decimal(str(asset.initial_cost)):.2f} ₽\n"
        f"Дата покупки: {asset.commissioned_on or 'не указана'}\n"
        f"Сегодня: {datetime.now(UTC).date()}\n\n"
        "ЧТО СКАЗАЛ СОТРУДНИК О СОСТОЯНИИ\n"
        f"{report.message}\n\n"
        "Верни долю уже израсходованного срока. Месяцы и деньги НЕ считай."
    )


def apply_purchase_answer(
    report: AssetConditionReport, payload: dict[str, Any], *, model: str, category_life: int
) -> None:
    """Перевести долю израсходованного срока в остаток месяцев.

    Месяцы считает КОД — то же правило, что и с деньгами в оценке поломки: дело модели
    интерпретация, не арифметика.

    Ноль месяцев не выдаём никогда. Объект, за который заплатили, ещё поработает, а карточка с
    нулевым сроком не амортизируется вовсе — то есть остаётся на балансе навсегда, ровно с той
    же ошибкой, которую этот контур и чинит, только с другого края.
    """
    share = _decimal(payload.get("life_used_share"))
    reasoning = str(payload.get("reasoning") or "").strip()
    needs_human = bool(payload.get("needs_human"))
    confidence = _decimal(payload.get("confidence"))

    report.model = model
    report.proposed_reason = reasoning or None
    report.status = "proposed"
    if confidence is not None and Decimal("0") <= confidence <= Decimal("1"):
        report.confidence = confidence

    unusable = share is None or share <= 0 or share > 1 or needs_human
    if unusable:
        # Предложения нет — но запись доходит до владельца: он знает, что объект б/у, и может
        # поставить срок сам. Молчание было бы хуже: карточка осталась бы «как новая».
        report.proposed_useful_life_months = None
        if not reasoning:
            report.proposed_reason = "Из описания не понять, сколько объект уже отработал"
        return

    capped = min(share, MAX_LIFE_USED_SHARE)
    remaining = int((Decimal(category_life) * (Decimal("1") - capped)).to_integral_value())
    report.proposed_useful_life_months = max(remaining, 1)


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

    # ОБЪЕКТА НЕТ — это выбытие, а не переоценка, и доля потери здесь ни при чём. Считать
    # такое обращение переоценкой в ноль было бы прямым враньём в карточке: применение
    # переоценки переписывает ПЕРВОНАЧАЛЬНУЮ стоимость в размер накопленного износа, то есть
    # подменяет цену покупки. Предлагаем списание, а сумму убытка код возьмёт из остаточной.
    if kind == "loss" and not needs_human:
        report.proposed_disposal = True
        report.proposed_cost = None
        report.status = "proposed"
        if not reasoning:
            report.proposed_reason = "Из сообщения следует, что объекта больше нет"
        return

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


async def _ask_about_purchase(
    session: AsyncSession,
    report: AssetConditionReport,
    *,
    settings: Settings,
    caller: CallTool,
) -> None:
    """Спросить модель об остатке срока у б/у покупки и записать предложение.

    Срок категории вычитывается ДО вызова и передаётся в разбор: доля без знаменателя — просто
    число, а брать его из карточки второй раз значило бы допустить, что за время вызова он
    поменялся.
    """
    asset = await session.get(FixedAsset, report.asset_id)
    if asset is None:
        raise LlmCallError("Объект не найден")
    prompt = await build_purchase_prompt(session, report)
    life = await resolve_useful_life(session, asset)
    payload = await caller(
        settings,
        tool=_PURCHASE_TOOL,
        prompt=prompt,
        model=settings.fixed_asset_ai_model,
        system=_PURCHASE_SYSTEM,
        max_tokens=1024,
    )
    apply_purchase_answer(
        report,
        payload,
        model=settings.fixed_asset_ai_model,
        category_life=int(life or 0),
    )


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
        if report.kind == "purchase":
            await _ask_about_purchase(session, report, settings=settings, caller=caller)
        else:
            payload = await caller(
                settings,
                tool=_TOOL,
                prompt=await build_prompt(session, report),
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
    kind: str = "incident",
) -> AssetConditionReport:
    """Принять сообщение менеджера. Модель спросит фоновая джоба.

    Стоимость на момент обращения фиксируем здесь: пока запись ждёт своей очереди, объект
    может успеть самортизироваться, и предложение считалось бы от другой базы.

    ``kind`` решает, о чём вообще будет разговор: у поломки предмет — стоимость, у покупки б/у —
    остаток срока. Вид задаётся в точке входа, а не выводится из карточки: «объект помечен б/у»
    не значит, что сообщение о нём — про покупку, и через полгода у той же карточки появятся
    обычные поломки.
    """
    asset = await session.get(FixedAsset, asset_id)
    if asset is None:
        raise LlmCallError("Объект не найден")

    accrued = await accumulated_depreciation(session, asset.id)
    residual = max(Decimal(str(asset.initial_cost)) - accrued, Decimal("0.00"))

    report = AssetConditionReport(
        asset_id=asset.id,
        message=message.strip(),
        kind=kind,
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

    У ПОКУПКИ Б/У применяется СРОК, а не стоимость. Цену трогать нечем и незачем: за объект
    заплатили ровно столько, сколько он стоил вместе со своим износом. Срок же в карточке пуст,
    и объект берёт его из категории — то есть считает себя новым. Проставленный срок
    перекрывает категорийный и с этого месяца ведёт нормальную амортизацию.

    У УТРАТЫ применяется ВЫБЫТИЕ. Объекта нет — переоценивать нечего: он уходит из
    внеоборотных активов целиком, а его остаточная стоимость становится убытком. Стоимость в
    карточке при этом не трогается: она остаётся историей — за сколько купили и сколько успели
    самортизировать.
    """
    if report.status != "proposed":
        raise LlmCallError("Это предложение уже обработано")

    report.decided_by_user_id = user_id
    report.decided_at = datetime.now(UTC)

    if report.proposed_disposal:
        if not accept:
            report.status = "dismissed"
            await session.flush()
            return report
        asset = await session.get(FixedAsset, report.asset_id)
        if asset is None:
            raise LlmCallError("Объект не найден")
        await dispose_asset(
            session,
            asset=asset,
            reason=report.message,
            user_id=user_id,
            condition_report_id=report.id,
        )
        report.status = "applied"
        await session.flush()
        return report

    proposal = (
        report.proposed_useful_life_months if report.kind == "purchase" else report.proposed_cost
    )
    if not accept or proposal is None:
        report.status = "dismissed"
        await session.flush()
        return report

    asset = await session.get(FixedAsset, report.asset_id)
    if asset is None:
        raise LlmCallError("Объект не найден")

    if report.kind == "purchase":
        asset.useful_life_months = int(report.proposed_useful_life_months or 0)
        report.status = "applied"
        # Стоимость не менялась — пересчитывать хранимые остатки нечего: они выводятся из
        # первоначальной, а новый срок влияет только на будущие начисления.
        await session.flush()
        return report

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
