"""Автоматическая первичная разметка новой номенклатуры для ОПиУ.

Источник определяется не по словам в названии, а по фактам iiko: товарные потоки месяца и
рекурсивная связь ингредиента с блюдами через техкарты. Модель получает уже ограниченный
набор допустимых статей и может отказаться от решения; отказ, низкая уверенность или
несовместимая статья всегда оставляют строку владельцу, а не создают молчаливый расход.
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.pnl import (
    PnlIikoProductObservation,
    PnlProductMonthlyDecision,
    PnlProductWhitelist,
)
from app.services.anthropic_client import LlmCallError, call_tool
from app.services.pnl.revision_products import (
    load_revision_product_guids,
    normalize_iiko_product_guid,
)

SOURCE_INVENTORY = "inventory"
SOURCE_INVOICE = "incoming_invoice"
STATUS_STOCKED = "stocked"
WORKUP_LINE_CODE = "goods_workup"

DECISION_TARGETS: dict[str, tuple[str, str, str | None]] = {
    "stocked": (SOURCE_INVENTORY, STATUS_STOCKED, None),
    "shop_maintenance": (SOURCE_INVOICE, "include", "shop_maintenance"),
    "aux_goods": (SOURCE_INVOICE, "include", "aux_goods"),
}
INVENTORY_DECISIONS = frozenset({"stocked"})
INVOICE_DECISIONS = frozenset({"stocked", "shop_maintenance", "aux_goods", "workup"})
ALL_DECISIONS = INVENTORY_DECISIONS | INVOICE_DECISIONS
SELLABLE_TYPES = frozenset({"DISH", "MODIFIER"})


@dataclass(slots=True, frozen=True)
class MenuProduct:
    guid: str
    name: str
    product_type: str
    active: bool
    deleted: bool
    place_type: str | None


@dataclass(slots=True, frozen=True)
class ProductDishUsage:
    active_dishes: tuple[str, ...] = ()
    inactive_dishes: tuple[str, ...] = ()

    @property
    def only_inactive(self) -> bool:
        return not self.active_dishes and bool(self.inactive_dishes)


@dataclass(slots=True)
class MenuUsageSnapshot:
    products: dict[str, MenuProduct] = field(default_factory=dict)
    usage: dict[str, ProductDishUsage] = field(default_factory=dict)


@dataclass(slots=True)
class AutoClassificationCandidate:
    product_guid: str
    product_name: str
    product_code: str | None
    observed_sources: tuple[str, ...]
    preferred_source: str | None
    allowed_decisions: frozenset[str]
    usage: ProductDishUsage
    product_type: str | None
    place_type: str | None
    source_reason: str


@dataclass(slots=True)
class AutoClassificationResult:
    workup: int = 0
    classified: int = 0
    review: int = 0
    unresolved: int = 0
    model_errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return self.workup + self.classified + self.review

    def summary(self) -> str:
        return (
            "авторазметка товаров: "
            f"проработка={self.workup}, классифицировано={self.classified}, "
            f"ручная проверка={self.review + self.unresolved}"
        )


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _clean(value).casefold() in {"true", "1", "yes", "active"}


def _records(payload: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(payload, (bytes, str)):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _iso_date(value: Any) -> date | None:
    text = _clean(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _chart_overlaps(chart: dict[str, Any], month_start: date, month_end: date) -> bool:
    starts = _iso_date(chart.get("dateFrom"))
    ends = _iso_date(chart.get("dateTo"))
    return (starts is None or starts <= month_end) and (ends is None or ends >= month_start)


def _modifier_guids(items: Any) -> set[str]:
    result: set[str] = set()
    if not isinstance(items, list):
        return result
    queue = deque(item for item in items if isinstance(item, dict))
    while queue:
        item = queue.popleft()
        if _truthy(item.get("deleted")):
            continue
        guid = _clean(item.get("modifier"))
        if guid:
            result.add(guid)
        children = item.get("childModifiers")
        if isinstance(children, list):
            queue.extend(child for child in children if isinstance(child, dict))
    return result


def build_menu_usage_snapshot(
    products_payload: Any,
    charts_payload: Any,
    *,
    month_start: date,
    month_end: date,
    product_guids: set[str],
) -> MenuUsageSnapshot:
    """Развернуть ингредиент → полуфабрикат → блюдо, включая модификаторы блюда."""
    product_rows = _records(products_payload, "products", "items", "rows", "data")
    products: dict[str, MenuProduct] = {}
    product_rows_by_guid: dict[str, dict[str, Any]] = {}
    for row in product_rows:
        guid = _clean(row.get("id") or row.get("product") or row.get("productId"))
        if not guid:
            continue
        product_rows_by_guid[guid] = row
        products[guid] = MenuProduct(
            guid=guid,
            name=_clean(row.get("name") or row.get("productName")) or guid,
            product_type=_clean(row.get("type") or row.get("productType")).upper(),
            active=_truthy(row.get("defaultIncludedInMenu")),
            deleted=_truthy(row.get("deleted") or row.get("isDeleted")),
            place_type=_clean(row.get("placeType")) or None,
        )

    parents_by_component: dict[str, set[str]] = defaultdict(set)
    chart_rows = [
        *_records(charts_payload, "assemblyCharts"),
        *_records(charts_payload, "preparedCharts"),
    ]
    for chart in chart_rows:
        if not _chart_overlaps(chart, month_start, month_end):
            continue
        assembled_guid = _clean(chart.get("assembledProductId"))
        if not assembled_guid:
            continue
        for item in chart.get("items") or []:
            if not isinstance(item, dict):
                continue
            component_guid = _clean(item.get("productId"))
            if component_guid:
                parents_by_component[component_guid].add(assembled_guid)

    # Модификатор может иметь собственную техкарту и входить в блюдо не как items[], а через
    # products[].modifiers. Добавляем это ребро, иначе ингредиенты соусов/топпингов потеряют
    # связь с продаваемым блюдом.
    for parent_guid, row in product_rows_by_guid.items():
        for modifier_guid in _modifier_guids(row.get("modifiers")):
            parents_by_component[modifier_guid].add(parent_guid)

    usage: dict[str, ProductDishUsage] = {}
    for product_guid in product_guids:
        active: set[str] = set()
        inactive: set[str] = set()
        product = products.get(product_guid)
        if product is not None and product.product_type in SELLABLE_TYPES:
            if product.active and not product.deleted:
                active.add(product.name)
            else:
                inactive.add(product.name)
        seen = {product_guid}
        queue = deque([product_guid])
        while queue:
            component_guid = queue.popleft()
            for parent_guid in parents_by_component.get(component_guid, ()):
                if parent_guid in seen:
                    continue
                seen.add(parent_guid)
                parent = products.get(parent_guid)
                if parent is not None and parent.product_type in SELLABLE_TYPES:
                    if parent.active and not parent.deleted:
                        active.add(parent.name)
                    else:
                        inactive.add(parent.name)
                queue.append(parent_guid)
        usage[product_guid] = ProductDishUsage(
            active_dishes=tuple(sorted(active)[:12]),
            inactive_dishes=tuple(sorted(inactive)[:12]),
        )
    return MenuUsageSnapshot(products=products, usage=usage)


def _candidate_payload(candidate: AutoClassificationCandidate) -> dict[str, Any]:
    return {
        "product_guid": candidate.product_guid,
        "name": candidate.product_name,
        "code": candidate.product_code,
        "iiko_type": candidate.product_type,
        "place_type_guid": candidate.place_type,
        "observed_sources": list(candidate.observed_sources),
        "preferred_source": candidate.preferred_source,
        "source_reason": candidate.source_reason,
        "active_dishes": list(candidate.usage.active_dishes),
        "inactive_dishes": list(candidate.usage.inactive_dishes),
        "allowed_decisions": sorted(candidate.allowed_decisions),
    }


CLASSIFICATION_TOOL: dict[str, Any] = {
    "name": "classify_pnl_goods",
    "description": "Классифицировать новую номенклатуру iiko для товарного леджера ОПиУ.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "product_guid": {"type": "string"},
                        "decision": {
                            "type": "string",
                            "enum": [*sorted(ALL_DECISIONS), "manual_review"],
                        },
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "reason": {"type": "string"},
                    },
                    "required": ["product_guid", "decision", "confidence", "reason"],
                },
            }
        },
        "required": ["decisions"],
    },
}

CLASSIFICATION_SYSTEM = """Ты классификатор номенклатуры ресторана для управленческого ОПиУ.
Решай только по переданным данным и только из allowed_decisions каждой строки.

Значения решений:
- stocked: товар хранится на складе и должен учитываться по формуле остаток на начало + приходы
  − остаток на конец. Сюда относятся пищевое сырьё, ингредиенты, полуфабрикаты и упаковка,
  которую пересчитывают на складе, включая коробки для пиццы.
- aux_goods: расходные вспомогательные товары по факту закупки — перчатки, бумажные полотенца,
  салфетки, бытовая химия и аналогичные регулярно расходуемые материалы.
- shop_maintenance: содержание и оснащение торговой точки, а не пища и не расходная упаковка.
- workup: новый ингредиент или товар для тестового блюда/проработки. Он признаётся расходом по
  приходной накладной только в текущем месяце; в следующем месяце решение нужно принять снова.
- manual_review: данных недостаточно или возможны несколько экономически разных трактовок.

Название может быть неоднозначным. Если уверенность ниже 0.8, выбирай manual_review. Не меняй
источник вопреки allowed_decisions и верни ровно одно решение для каждого product_guid."""


def _observation_name(observations: dict[str, Any], attribute: str) -> str | None:
    for observation in observations.values():
        value = getattr(observation, attribute, None)
        if value:
            return str(value)
    return None


def _review_rule(candidate: AutoClassificationCandidate, note: str) -> PnlProductWhitelist | None:
    if candidate.preferred_source is None:
        return None
    return PnlProductWhitelist(
        iiko_product_guid=candidate.product_guid,
        source_kind=candidate.preferred_source,
        line_code=None,
        include_status="requires_owner_review",
        product_name=candidate.product_name,
        product_code=candidate.product_code,
        note=note[:2000],
        updated_by_user_id=None,
    )


def _decision_rows(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = payload.get("decisions")
    if not isinstance(rows, list):
        return {}
    return {
        _clean(row.get("product_guid")): row
        for row in rows
        if isinstance(row, dict) and _clean(row.get("product_guid"))
    }


async def auto_classify_new_goods(
    session: AsyncSession,
    *,
    month_start: date,
    month_end: date,
    observations: Sequence[Any],
    products_payload: Any,
    charts_payload: Any,
    settings: Settings,
) -> AutoClassificationResult:
    """Разметить только новые GUID, не трогая ни одно решение владельца."""
    result = AutoClassificationResult()
    if not settings.pnl_goods_auto_classification_enabled:
        return result

    observations_by_guid: dict[str, dict[str, Any]] = defaultdict(dict)
    for observation in observations:
        guid = _clean(getattr(observation, "iiko_product_guid", None))
        source_kind = _clean(getattr(observation, "source_kind", None))
        if guid and source_kind in {SOURCE_INVENTORY, SOURCE_INVOICE}:
            observations_by_guid[guid][source_kind] = observation
    product_guids = set(observations_by_guid)
    if not product_guids:
        return result
    revision_product_guids = await load_revision_product_guids(session)

    existing_rules = (
        (
            await session.execute(
                select(PnlProductWhitelist).where(
                    PnlProductWhitelist.iiko_product_guid.in_(product_guids)
                )
            )
        )
        .scalars()
        .all()
    )
    decided_guids = {rule.iiko_product_guid for rule in existing_rules}
    monthly_decisions = (
        (
            await session.execute(
                select(PnlProductMonthlyDecision).where(
                    PnlProductMonthlyDecision.iiko_product_guid.in_(product_guids)
                )
            )
        )
        .scalars()
        .all()
    )
    current_monthly = {
        item.iiko_product_guid for item in monthly_decisions if item.period_month == month_start
    }
    previous_workup = {
        item.iiko_product_guid for item in monthly_decisions if item.period_month < month_start
    }
    first_seen = dict(
        (
            await session.execute(
                select(
                    PnlIikoProductObservation.iiko_product_guid,
                    func.min(PnlIikoProductObservation.period_month),
                )
                .where(PnlIikoProductObservation.iiko_product_guid.in_(product_guids))
                .group_by(PnlIikoProductObservation.iiko_product_guid)
            )
        ).all()
    )
    snapshot = build_menu_usage_snapshot(
        products_payload,
        charts_payload,
        month_start=month_start,
        month_end=month_end,
        product_guids=product_guids,
    )

    candidates: list[AutoClassificationCandidate] = []
    for product_guid in sorted(product_guids - decided_guids - current_monthly):
        product_observations = observations_by_guid[product_guid]
        sources = tuple(sorted(product_observations))
        usage = snapshot.usage.get(product_guid, ProductDishUsage())
        catalogue_product = snapshot.products.get(product_guid)
        name = (
            _observation_name(product_observations, "product_name")
            or (catalogue_product.name if catalogue_product is not None else None)
            or product_guid
        )
        code = _observation_name(product_observations, "product_code")

        if normalize_iiko_product_guid(product_guid) in revision_product_guids:
            session.add(
                PnlProductWhitelist(
                    iiko_product_guid=product_guid,
                    source_kind=SOURCE_INVENTORY,
                    line_code=None,
                    include_status=STATUS_STOCKED,
                    product_name=name,
                    product_code=code,
                    note=(
                        "Автоматически: товар входит в активный список складского учёта. "
                        "Расход считается по остаткам на границах месяца."
                    ),
                    updated_by_user_id=None,
                )
            )
            result.classified += 1
            continue

        if SOURCE_INVENTORY in sources:
            # Сам факт появления в складских остатках определяет контур без LLM.
            # Явное пользовательское правило уже отфильтровано через decided_guids
            # и по-прежнему может перенести такой товар в прямые расходы.
            session.add(
                PnlProductWhitelist(
                    iiko_product_guid=product_guid,
                    source_kind=SOURCE_INVENTORY,
                    line_code=None,
                    include_status=STATUS_STOCKED,
                    product_name=name,
                    product_code=code,
                    note=(
                        "Автоматически: товар найден в складских остатках iiko и "
                        "учитывается по формуле начало + приход − конец."
                    ),
                    updated_by_user_id=None,
                )
            )
            result.classified += 1
            continue

        if usage.only_inactive:
            inactive_names = ", ".join(usage.inactive_dishes[:4])
            seen_before_month = first_seen.get(product_guid, month_start) < month_start
            if product_guid in previous_workup or seen_before_month:
                source = SOURCE_INVOICE if SOURCE_INVOICE in sources else sources[0]
                candidate = AutoClassificationCandidate(
                    product_guid=product_guid,
                    product_name=name,
                    product_code=code,
                    observed_sources=sources,
                    preferred_source=source,
                    allowed_decisions=(
                        INVOICE_DECISIONS if source == SOURCE_INVOICE else INVENTORY_DECISIONS
                    ),
                    usage=usage,
                    product_type=(catalogue_product.product_type if catalogue_product else None),
                    place_type=(catalogue_product.place_type if catalogue_product else None),
                    source_reason=(
                        "ранее товар уже проходил месячную Проработку"
                        if product_guid in previous_workup
                        else "товар встречался до текущего месяца"
                    ),
                )
                history_reason = (
                    "в прошлом месяце товар уже был в «Проработке»"
                    if product_guid in previous_workup
                    else "товар не новый: он уже встречался в более раннем месяце"
                )
                review = _review_rule(
                    candidate,
                    f"Автоматически выбран источник, но не расход: {history_reason}. Сейчас "
                    "он по-прежнему встречается только в "
                    f"выключенных блюдах ({inactive_names}); требуется постоянное решение.",
                )
                if review is not None:
                    session.add(review)
                    result.review += 1
                else:
                    result.unresolved += 1
                continue
            if SOURCE_INVOICE not in sources:
                result.unresolved += 1
                continue
            session.add(
                PnlProductMonthlyDecision(
                    period_month=month_start,
                    iiko_product_guid=product_guid,
                    source_kind=SOURCE_INVOICE,
                    decision_kind="workup",
                    note=(
                        "Автоматически: товар входит только в выключенные блюда "
                        f"({inactive_names}). Учтён по накладным в Проработке только за "
                        f"{month_start:%m.%Y}; в следующем месяце потребуется новое решение."
                    ),
                    updated_by_user_id=None,
                )
            )
            result.workup += 1
            continue

        if usage.active_dishes:
            session.add(
                PnlProductWhitelist(
                    iiko_product_guid=product_guid,
                    source_kind=SOURCE_INVENTORY,
                    line_code=None,
                    include_status=STATUS_STOCKED,
                    product_name=name,
                    product_code=code,
                    note=(
                        "Автоматически: товар входит хотя бы в одно активное блюдо "
                        f"({', '.join(usage.active_dishes[:4])}) и учитывается по складским "
                        "остаткам на границах месяца."
                    )[:2000],
                    updated_by_user_id=None,
                )
            )
            result.classified += 1
            continue
        elif sources == (SOURCE_INVOICE,):
            preferred_source = SOURCE_INVOICE
            allowed = INVOICE_DECISIONS
            source_reason = "товар встретился только в приходных накладных"
        elif sources == (SOURCE_INVENTORY,):
            preferred_source = SOURCE_INVENTORY
            allowed = INVENTORY_DECISIONS
            source_reason = "товар встретился только в инвентаризации iiko"
        else:
            preferred_source = SOURCE_INVOICE if SOURCE_INVOICE in sources else None
            allowed = ALL_DECISIONS
            source_reason = (
                "товар встретился и в остатках, и в приходных накладных, но не связан "
                "с активными блюдами"
            )
        candidates.append(
            AutoClassificationCandidate(
                product_guid=product_guid,
                product_name=name,
                product_code=code,
                observed_sources=sources,
                preferred_source=preferred_source,
                allowed_decisions=frozenset(allowed),
                usage=usage,
                product_type=(catalogue_product.product_type if catalogue_product else None),
                place_type=(catalogue_product.place_type if catalogue_product else None),
                source_reason=source_reason,
            )
        )

    batch_size = max(1, settings.pnl_goods_auto_classification_batch_size)
    for offset in range(0, len(candidates), batch_size):
        batch = candidates[offset : offset + batch_size]
        prompt = json.dumps(
            {"candidates": [_candidate_payload(candidate) for candidate in batch]},
            ensure_ascii=False,
        )
        try:
            payload = await call_tool(
                settings,
                tool=CLASSIFICATION_TOOL,
                model=settings.pnl_goods_classification_model,
                prompt=prompt,
                system=CLASSIFICATION_SYSTEM,
                max_tokens=4096,
            )
            decisions = _decision_rows(payload)
            model_error = None
        except LlmCallError as exc:
            decisions = {}
            model_error = str(exc)
            result.model_errors.append(model_error)

        for candidate in batch:
            row = decisions.get(candidate.product_guid)
            decision = _clean(row.get("decision")) if row is not None else "manual_review"
            reason = _clean(row.get("reason")) if row is not None else ""
            try:
                confidence = float(row.get("confidence", 0)) if row is not None else 0.0
            except (TypeError, ValueError):
                confidence = 0.0
            valid = (
                decision in candidate.allowed_decisions
                and confidence >= settings.pnl_goods_classification_min_confidence
            )
            if valid:
                if decision == "workup":
                    session.add(
                        PnlProductMonthlyDecision(
                            period_month=month_start,
                            iiko_product_guid=candidate.product_guid,
                            source_kind=SOURCE_INVOICE,
                            decision_kind="workup",
                            note=(
                                f"Автоматически · {settings.pnl_goods_classification_model} · "
                                f"уверенность {confidence:.0%}. {candidate.source_reason}. "
                                f"{reason} Решение действует только за {month_start:%m.%Y}; "
                                "в следующем месяце товар будет задан повторно."
                            )[:2000],
                            updated_by_user_id=None,
                        )
                    )
                    result.workup += 1
                    continue
                source_kind, status, line_code = DECISION_TARGETS[decision]
                session.add(
                    PnlProductWhitelist(
                        iiko_product_guid=candidate.product_guid,
                        source_kind=source_kind,
                        line_code=line_code,
                        include_status=status,
                        product_name=candidate.product_name,
                        product_code=candidate.product_code,
                        note=(
                            f"Автоматически · {settings.pnl_goods_classification_model} · "
                            f"уверенность {confidence:.0%}. {candidate.source_reason}. "
                            f"{reason}"
                        )[:2000],
                        updated_by_user_id=None,
                    )
                )
                result.classified += 1
                continue

            fallback_reason = (
                f"Модель недоступна: {model_error}"
                if model_error
                else reason or "Модель не дала уверенного совместимого решения"
            )
            review = _review_rule(
                candidate,
                f"Источник выбран автоматически: {candidate.source_reason}. Статья требует "
                f"ручной проверки. {fallback_reason}",
            )
            if review is None:
                result.unresolved += 1
            else:
                session.add(review)
                result.review += 1

    await session.flush()
    return result
