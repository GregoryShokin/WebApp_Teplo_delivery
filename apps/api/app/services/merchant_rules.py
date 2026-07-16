"""Merchant-правила из карточки контрагента: «списание с картой → контрагент» одним действием.

Кейс владельца (Манго): карт-автосписание не несёт реквизитов — только текст мерчанта
(«Оплата в MANGO-OFFICE.RU MOSKVA RUS»), поэтому связка «списание → контрагент» требует
правила классификации по подстроке назначения. Здесь оно создаётся прямо из карточки
(вместе с включением предоплатной модели), а висящие needs_review-списания с этим
паттерном сразу переклассифицируются — предоплаты по ним создаёт обычный хук
классификатора (``ensure_prepayment_from_bank_transaction``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    BankOperation,
    ClassificationRule,
    Counterparty,
    CounterpartyPayableProfile,
    DdsArticle,
    ReconciliationCase,
)
from app.services.banking.classifier import apply_operation_action, close_reconciliation_case


class MerchantRuleError(RuntimeError):
    """Доменная ошибка создания merchant-правила (маппится в HTTP 409)."""


@dataclass
class MerchantRuleResult:
    rule: ClassificationRule
    # Правило с этим паттерном уже существовало без контрагента — дописали, а не создали.
    updated_existing: bool
    # Сколько висящих needs_review-списаний привязано этим же вызовом.
    backfilled: int


def _escape_like(pattern: str) -> str:
    return pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def list_merchant_rules(
    session: AsyncSession, counterparty_id: uuid.UUID
) -> list[ClassificationRule]:
    """Правила, привязывающие списания к этому контрагенту (для бейджа в карточке)."""
    rules = await session.scalars(
        select(ClassificationRule)
        .where(
            ClassificationRule.counterparty_id == counterparty_id,
            ClassificationRule.purpose_pattern.is_not(None),
        )
        .order_by(ClassificationRule.priority, ClassificationRule.name)
    )
    return list(rules.all())


async def create_merchant_rule(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    purpose_pattern: str,
    article_id: uuid.UUID | None = None,
) -> MerchantRuleResult:
    """Создать правило «назначение содержит X → контрагент» и привязать висящие списания.

    Статья обязательна (правило set_article без статьи возвращает операции в needs_review):
    берём переданную либо статью по умолчанию из профиля контрагента. Если правило с тем же
    паттерном уже есть БЕЗ контрагента (кейс Манго: статью ставило, контрагента нет) —
    дописываем контрагента в него, дубликат не создаём."""
    counterparty = await session.get(Counterparty, counterparty_id)
    if counterparty is None:
        raise MerchantRuleError("Контрагент не найден")

    pattern = " ".join((purpose_pattern or "").split())
    if len(pattern) < 3:
        raise MerchantRuleError("Текст мерчанта слишком короткий — минимум 3 символа")

    resolved_article_id = article_id
    if resolved_article_id is None:
        resolved_article_id = await session.scalar(
            select(CounterpartyPayableProfile.default_dds_article_id).where(
                CounterpartyPayableProfile.counterparty_id == counterparty_id
            )
        )
    if resolved_article_id is None:
        raise MerchantRuleError(
            "У контрагента нет статьи ДДС по умолчанию — укажите её в карточке"
        )
    if await session.get(DdsArticle, resolved_article_id) is None:
        raise MerchantRuleError("Статья ДДС не найдена")

    existing = await session.scalar(
        select(ClassificationRule).where(
            func.lower(ClassificationRule.purpose_pattern) == pattern.casefold()
        )
    )
    updated_existing = False
    if existing is not None:
        if existing.counterparty_id is None:
            existing.counterparty_id = counterparty_id
            rule = existing
            updated_existing = True
        elif existing.counterparty_id == counterparty_id:
            rule = existing
            updated_existing = True
        else:
            raise MerchantRuleError(
                f"Паттерн «{pattern}» уже занят правилом «{existing.name}» другого контрагента"
            )
    else:
        rule = ClassificationRule(
            name=f"Карт-списания: {counterparty.name}",
            priority=100,
            is_active=True,
            direction="out",
            purpose_pattern=pattern,
            action="set_article",
            article_id=resolved_article_id,
            counterparty_id=counterparty_id,
            comment="Создано из карточки контрагента (предоплатная модель по банк-фиду)",
        )
        session.add(rule)
        await session.flush()

    backfilled = await _backfill_pending_operations(session, rule)
    await session.commit()
    await session.refresh(rule)
    return MerchantRuleResult(rule=rule, updated_existing=updated_existing, backfilled=backfilled)


async def _backfill_pending_operations(session: AsyncSession, rule: ClassificationRule) -> int:
    """Переклассифицировать висящие needs_review-списания под свежее правило.

    Ровно тот же путь, что и авто-классификация (``apply_operation_action``), поэтому
    хук предоплатной модели срабатывает сам; открытые кейсы «unclassified_operation»
    закрываем resolved — как это делает ручная разметка."""
    operations = (
        await session.scalars(
            select(BankOperation).where(
                BankOperation.classification_status == "needs_review",
                BankOperation.direction == (rule.direction or "out"),
                BankOperation.payment_purpose.ilike(
                    f"%{_escape_like(rule.purpose_pattern or '')}%", escape="\\"
                ),
            )
        )
    ).all()
    backfilled = 0
    for operation in operations:
        await apply_operation_action(
            session,
            operation,
            action=rule.action,
            article_id=rule.article_id,
            counterparty_id=rule.counterparty_id,
            quality_status="auto",
        )
        if operation.classification_status != "classified":
            continue  # кошелёк не нашёлся и т.п. — кейс остаётся открытым
        backfilled += 1
        case = await session.scalar(
            select(ReconciliationCase).where(
                ReconciliationCase.bank_operation_id == operation.id,
                ReconciliationCase.kind == "unclassified_operation",
                ReconciliationCase.status == "pending",
            )
        )
        if case is not None:
            await close_reconciliation_case(
                session,
                case,
                status="resolved",
                resolution_payload={
                    "action": rule.action,
                    "article_id": str(rule.article_id) if rule.article_id else None,
                    "counterparty_id": (
                        str(rule.counterparty_id) if rule.counterparty_id else None
                    ),
                    "reason": "merchant_rule_backfill",
                    "rule_id": str(rule.id),
                },
            )
    return backfilled
