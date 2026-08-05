"""Вернуть в разбор карт-операции, ошибочно уехавшие на IHC.ru (инцидент 03.08.2026).

Почему не SQL. На каждой такой проводке висит автопредоплата (открытая дебиторка на IHC.ru),
и снимать её должен код: ``_clear_operation_cashflow`` тянет за собой
``_drop_untouched_bank_prepayments`` и не трогает предоплаты, которых человек уже касался.
Ручной DELETE оставил бы дебиторку сиротой.

Что делает: для перечисленных операций удаляет проводку ДДС и связанную автопредоплату,
ставит статус ``needs_review`` и заводит кейс «требует разбора» — операция возвращается во
вкладку «Требуют разбора», где владелец разметит её сам. Деньги не двигаются: баланс
кошелька в этой модели считается по выписке, а не по проводкам.

Запуск на проде (из /opt/teplo/deploy):

    docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T api \\
        python /app/deploy/dds-rules/2026-08-05_card_merchant_reopen_ops.py

Идемпотентен: уже вернувшуюся в разбор операцию пропускает.
"""

from __future__ import annotations

import asyncio
from datetime import date

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models import BankOperation
from app.services.banking.classifier import (
    _clear_operation_cashflow,
    _operation_review_payload,
    create_or_update_reconciliation_case,
)

# Операции T-Банка, у которых статью и контрагента проставило сломанное правило по ИНН эквайера.
# Отбор по тексту мерчанта + дате: правило действовало с 03.08 до починки, и под него попали
# только эти три назначения (остальные карт-оплаты перехватили правила меньшего приоритета).
AFFECTED_PURPOSES = (
    "Оплата в OZON Moskva RUS",
    "Оплата в MAGNIT MM BEREGOVOJ Volgodonsk RUS",
    "Оплата в MAGAZIN MAGISTR Volgodonsk RUS",
)
# Именно date, а не строка: asyncpg сравнивает типы строго и на varchar даёт
# «operator does not exist: date >= character varying».
AFFECTED_FROM = date(2026, 8, 3)


async def main() -> None:
    async with AsyncSessionLocal() as session:
        operations = (
            await session.scalars(
                select(BankOperation).where(
                    BankOperation.counterparty_inn_raw == "7710140679",
                    BankOperation.direction == "out",
                    BankOperation.operation_date >= AFFECTED_FROM,
                    BankOperation.payment_purpose.in_(AFFECTED_PURPOSES),
                )
            )
        ).all()
        if not operations:
            print("Нечего возвращать: подходящих операций нет")
            return
        for operation in operations:
            if operation.classification_status == "needs_review":
                print(f"— уже в разборе: {operation.payment_purpose} {operation.amount}")
                continue
            await _clear_operation_cashflow(session, operation)
            operation.classification_status = "needs_review"
            await create_or_update_reconciliation_case(
                session,
                kind="unclassified_operation",
                provider=operation.provider,
                bank_operation_id=operation.id,
                payload={
                    **_operation_review_payload(operation),
                    "reason": "card_merchant_rule_rollback",
                },
            )
            print(f"✓ вернул в разбор: {operation.payment_purpose} {operation.amount}")
        await session.commit()


if __name__ == "__main__":
    asyncio.run(main())
