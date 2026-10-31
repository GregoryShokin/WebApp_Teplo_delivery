"""Починка разноса овердрафта Т-Банка.

Решение владельца 31.10.2026: выдача овердрафта — деньги банка в долг, в ОПиУ она не входит.
Выдачу 30.10.2026 на 36 916,03 ₽ вручную отнесли к статье комиссии: приход в расходной статье
считается возвратом расхода и поднял чистую прибыль. Вместе со статьёй запомнилось и правило
«Предоставление овердрафта → статья комиссии».

Как работает скрипт:

- выдачи овердрафта, стоящие на статье комиссии, переносятся на «Получение овердрафта» той же
  дверью, что ручная переразметка: она проверяет закрытый месяц;
- ошибочное запомненное правило выключается, а не удаляется: след остаётся в списке правил;
- строки овердрафта, ждущие разбора, проходят правила, их вопросы закрываются;
- повторный запуск находит, что чинить нечего.

Запуск:

    python -m app.scripts.repair_overdraft_classification            # только показать
    python -m app.scripts.repair_overdraft_classification --apply    # записать
"""

from __future__ import annotations

import argparse
import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models import (
    BankOperation,
    CashflowTransaction,
    ClassificationRule,
    DdsArticle,
    ReconciliationCase,
)
from app.services.banking.classifier import (
    apply_operation_action,
    close_reconciliation_case,
    run_classification_rules,
)

FEE_ARTICLE_CODE = "overdraft"
DRAW_ARTICLE_CODE = "poluchenie_overdrafta"
DRAW_WORDS = "предоставление овердрафта"
OVERDRAFT_WORD = "овердрафт"
RULE_OFF_NOTE = "Выключено 31.10.2026: выдача овердрафта не комиссия"


def parse_args() -> argparse.Namespace:
    """Разобрать ключи запуска: без ``--apply`` скрипт ничего не пишет."""
    parser = argparse.ArgumentParser(description="Починить разнос овердрафта Т-Банка.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="записать починку; без ключа только показать",
    )
    return parser.parse_args()


def plain_text(value: str | None) -> str:
    """Текст без лишних пробелов и регистра: банк ставит двойные пробелы внутри назначения."""
    return " ".join((value or "").split()).casefold()


async def article_id_by_code(session: AsyncSession, code: str) -> uuid.UUID:
    """Найти статью по коду; без неё чинить нечем, поэтому запуск останавливается."""
    article_id = await session.scalar(select(DdsArticle.id).where(DdsArticle.code == code))
    if article_id is None:
        raise SystemExit(f"Нет статьи с кодом {code}: сначала примените миграции.")
    return article_id


async def move_draws_off_fee_article(session: AsyncSession) -> int:
    """Перенести выдачи овердрафта со статьи комиссии на «Получение овердрафта»."""
    fee_article_id = await article_id_by_code(session, FEE_ARTICLE_CODE)
    draw_article_id = await article_id_by_code(session, DRAW_ARTICLE_CODE)
    rows = (
        await session.execute(
            select(BankOperation, CashflowTransaction)
            .join(
                CashflowTransaction,
                CashflowTransaction.id == BankOperation.cashflow_transaction_id,
            )
            .where(
                BankOperation.provider == "tbank",
                BankOperation.direction == "in",
                CashflowTransaction.article_id == fee_article_id,
            )
            .order_by(BankOperation.operation_date)
        )
    ).all()
    moved = 0
    for operation, transaction in rows:
        if DRAW_WORDS not in plain_text(operation.payment_purpose):
            continue
        print(f"Выдача {operation.operation_date}: {operation.amount} → «Получение овердрафта»")
        await apply_operation_action(
            session,
            operation,
            action="set_article",
            article_id=draw_article_id,
            counterparty_id=transaction.counterparty_id,
            quality_status="owner_review",
        )
        moved += 1
    return moved


async def switch_off_wrong_rules(session: AsyncSession) -> int:
    """Выключить запомненные правила, ведущие выдачу овердрафта в статью комиссии."""
    fee_article_id = await article_id_by_code(session, FEE_ARTICLE_CODE)
    rules = (
        await session.scalars(
            select(ClassificationRule).where(
                ClassificationRule.is_active.is_(True),
                ClassificationRule.article_id == fee_article_id,
                ClassificationRule.direction == "in",
            )
        )
    ).all()
    switched_off = 0
    for rule in rules:
        if DRAW_WORDS not in plain_text(rule.purpose_pattern):
            continue
        print(f"Правило выключается: «{rule.purpose_pattern}»")
        rule.is_active = False
        rule.comment = f"{rule.comment} · {RULE_OFF_NOTE}" if rule.comment else RULE_OFF_NOTE
        switched_off += 1
    return switched_off


async def classify_waiting_lines(session: AsyncSession) -> int:
    """Провести через правила строки овердрафта, ждущие разбора, и закрыть их вопросы.

    Берутся только строки овердрафта: остальная очередь ждёт своего разбора, и общий проход
    правил по ней здесь не нужен.
    """
    waiting = (
        await session.scalars(
            select(BankOperation).where(
                BankOperation.provider == "tbank",
                BankOperation.classification_status == "needs_review",
            )
        )
    ).all()
    operations = [
        operation
        for operation in waiting
        if OVERDRAFT_WORD in plain_text(operation.payment_purpose)
    ]
    if not operations:
        return 0
    await run_classification_rules(session, operations)
    classified = 0
    for operation in operations:
        if operation.classification_status == "needs_review":
            print(f"Осталась в разборе: {operation.operation_date} {operation.amount}")
            continue
        print(f"Разнесена {operation.operation_date}: {operation.amount}")
        classified += 1
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
                resolution_payload={"via": "repair_overdraft_classification"},
            )
    return classified


async def async_main() -> None:
    """Запустить починку; запись идёт одной транзакцией и только с ``--apply``."""
    args = parse_args()
    async with AsyncSessionLocal() as session:
        moved = await move_draws_off_fee_article(session)
        switched_off = await switch_off_wrong_rules(session)
        classified = await classify_waiting_lines(session)
        if args.apply:
            await session.commit()
        else:
            await session.rollback()
    label = "записано" if args.apply else "будет записано"
    print(
        f"Итого, {label}: выдач перенесено {moved}, правил выключено {switched_off}, "
        f"строк разнесено {classified}."
    )


def main() -> None:
    """Точка входа ``python -m app.scripts.repair_overdraft_classification``."""
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
