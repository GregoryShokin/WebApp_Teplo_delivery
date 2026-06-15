"""Phase 4: paying a warehouse invoice — split across bank + cash sources, with the
«персонал» part booked to its own DDS article.

Built ON TOP of the existing counterparty payment primitives (``pay_invoice_from_wallet``,
``confirm_invoice_match``, allocations, ``_recompute_status``) — this module only adds the
orchestration that those primitives lack: a single transaction over N allocations, and
splitting the production vs «персонал» amount onto two DDS articles.

Production part (``amount − staff_amount``) → ``payment_to_supplier``; «персонал» part
(``staff_amount``) → ``supplier_staff_payment`` (configurable via the
``warehouse.staff_payment_article_code`` setting).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BankOperation, DdsArticle, SupplierInvoice, Wallet
from app.services.counterparty_bank_match import (
    WAREHOUSE_AMOUNT_TOLERANCE_PCT,
    WAREHOUSE_TIGHT_WINDOW_MINUTES,
    WAREHOUSE_WINDOW_HOURS,
    _apply_bank_allocation,
    assert_bank_matchable,
)
from app.services.counterparty_matching import (
    _invoice_remaining,
    _op_already_allocated,
    _recompute_status,
)
from app.services.counterparty_payments import DEFAULT_SUPPLIER_ARTICLE_CODE, _apply_wallet_payment
from app.services.settings_service import SettingNotFoundError, get_setting_model

STAFF_ARTICLE_SETTING_KEY = "warehouse.staff_payment_article_code"
DEFAULT_STAFF_ARTICLE_CODE = "supplier_staff_payment"
WINDOW_HOURS_SETTING_KEY = "warehouse.match_window_hours"
TOLERANCE_PCT_SETTING_KEY = "warehouse.match_amount_tolerance_pct"
TIGHT_MINUTES_SETTING_KEY = "warehouse.match_tight_window_minutes"


class WarehousePaymentError(RuntimeError):
    """Domain error for warehouse payment/split (maps to HTTP 409/422)."""


@dataclass
class BankPart:
    """One bank source of a split payment. ``amount=None`` → the whole operation (MVP rule:
    one operation settles one invoice, не дробится на несколько накладных)."""

    bank_operation_id: uuid.UUID
    amount: Decimal | None = None


@dataclass
class CashPart:
    """One cash/wallet source of a split payment. ``article_id=None`` → production article."""

    wallet_id: uuid.UUID
    amount: Decimal
    operation_date: date
    article_id: uuid.UUID | None = None
    comment: str | None = None


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _production_amount(invoice: SupplierInvoice) -> Decimal:
    """Goods part of the invoice = full amount minus the «персонал» part."""
    return _money(invoice.amount) - _money(invoice.staff_amount)


async def _resolve_article_id(session: AsyncSession, code: str) -> uuid.UUID:
    article_id = await session.scalar(select(DdsArticle.id).where(DdsArticle.code == code))
    if article_id is None:
        raise WarehousePaymentError(f"Статья ДДС «{code}» не настроена")
    return article_id


async def _staff_article_code(session: AsyncSession) -> str:
    """Configured staff DDS article code, falling back to the seeded default. Uses
    ``get_setting_model`` (raw value) inside try/except — ``get_setting`` would raise on a
    missing key and return a serialized dict, not the bare value."""
    try:
        setting = await get_setting_model(session, STAFF_ARTICLE_SETTING_KEY)
    except SettingNotFoundError:
        return DEFAULT_STAFF_ARTICLE_CODE
    value = setting.value
    if isinstance(value, str) and value.strip():
        return value.strip()
    return DEFAULT_STAFF_ARTICLE_CODE


async def _setting_number(session: AsyncSession, key: str, default: Any) -> Any:
    try:
        setting = await get_setting_model(session, key)
    except SettingNotFoundError:
        return default
    value = setting.value
    return value if isinstance(value, int | float) else default


async def resolve_match_params(
    session: AsyncSession,
    *,
    window_hours: int | None = None,
    amount_tolerance_pct: Decimal | float | None = None,
    tight_window_minutes: int | None = None,
) -> tuple[int, Decimal, int]:
    """Resolve invoice↔bank match params: explicit args win, else the configured settings,
    else the module defaults. Keeps the «Настройки» values live without a hard dependency."""
    window = (
        window_hours
        if window_hours is not None
        else int(await _setting_number(session, WINDOW_HOURS_SETTING_KEY, WAREHOUSE_WINDOW_HOURS))
    )
    if amount_tolerance_pct is not None:
        tol = Decimal(str(amount_tolerance_pct))
    else:
        raw_tol = await _setting_number(
            session, TOLERANCE_PCT_SETTING_KEY, WAREHOUSE_AMOUNT_TOLERANCE_PCT
        )
        tol = Decimal(str(raw_tol))
    if tight_window_minutes is not None:
        tight = tight_window_minutes
    else:
        raw_tight = await _setting_number(
            session, TIGHT_MINUTES_SETTING_KEY, WAREHOUSE_TIGHT_WINDOW_MINUTES
        )
        tight = int(raw_tight)
    return window, tol, tight


async def resolve_production_article_id(session: AsyncSession) -> uuid.UUID:
    return await _resolve_article_id(session, DEFAULT_SUPPLIER_ARTICLE_CODE)


async def resolve_staff_article_id(session: AsyncSession) -> uuid.UUID:
    return await _resolve_article_id(session, await _staff_article_code(session))


async def build_staff_split_cash_parts(
    session: AsyncSession,
    invoice: SupplierInvoice,
    *,
    wallet_id: uuid.UUID,
    operation_date: date,
    comment: str | None = None,
) -> list[CashPart]:
    """Split a fully-cash payment of one invoice into production + «персонал» parts, each on
    its own DDS article. Used when the manager pays from the cash desk with ``split_staff``."""
    production = _production_amount(invoice)
    staff = _money(invoice.staff_amount)
    parts: list[CashPart] = []
    if production > 0:
        parts.append(
            CashPart(
                wallet_id=wallet_id,
                amount=production,
                operation_date=operation_date,
                article_id=await resolve_production_article_id(session),
                comment=comment,
            )
        )
    if staff > 0:
        parts.append(
            CashPart(
                wallet_id=wallet_id,
                amount=staff,
                operation_date=operation_date,
                article_id=await resolve_staff_article_id(session),
                comment=comment,
            )
        )
    return parts


async def pay_invoice_split(
    session: AsyncSession,
    *,
    invoice_id: uuid.UUID,
    bank_parts: list[BankPart] | None = None,
    cash_parts: list[CashPart] | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> SupplierInvoice:
    """Pay one payable invoice from N sources (bank operations + cash/wallet parts) in a
    SINGLE transaction. All parts are validated BEFORE any write, so an occupied operation
    or an over-payment aborts the whole split with nothing persisted. Production vs «персонал»
    splitting is expressed by the cash parts' ``article_id`` (``build_staff_split_cash_parts``)."""
    bank_parts = bank_parts or []
    cash_parts = cash_parts or []
    if not bank_parts and not cash_parts:
        raise WarehousePaymentError("Не указано ни одного источника оплаты")

    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None:
        raise WarehousePaymentError("Накладная не найдена")
    if invoice.payment_status == "void":
        raise WarehousePaymentError("Накладная аннулирована")
    if invoice.direction != "payable":
        raise WarehousePaymentError("Доходную накладную нельзя оплатить со счёта")
    if invoice.barter_role is not None:
        raise WarehousePaymentError("Бартерную накладную нельзя оплатить деньгами")

    remaining = await _invoice_remaining(session, invoice)

    # --- validate everything before writing anything --------------------------
    resolved_bank: list[tuple[BankOperation, Decimal]] = []
    seen_ops: set[uuid.UUID] = set()
    total = Decimal("0.00")
    for part in bank_parts:
        if part.bank_operation_id in seen_ops:
            raise WarehousePaymentError("Одна банковская операция указана дважды")
        seen_ops.add(part.bank_operation_id)
        operation = await session.get(BankOperation, part.bank_operation_id)
        if operation is None:
            raise WarehousePaymentError("Банковская операция не найдена")
        # Same guard as confirm: out, not card-noise, not an internal transfer.
        assert_bank_matchable(invoice, operation)
        if await _op_already_allocated(session, operation.id):
            raise WarehousePaymentError("Банковская операция уже использована")
        op_amount = _money(abs(operation.amount))
        amount = _money(part.amount) if part.amount is not None else op_amount
        if amount <= 0:
            raise WarehousePaymentError("Сумма банковской части должна быть больше нуля")
        # «Операция = одна накладная целиком»: не дробим операцию, не аллоцируем сверх неё.
        if amount > op_amount:
            raise WarehousePaymentError("Сумма банковской части превышает сумму операции")
        resolved_bank.append((operation, amount))
        total += amount

    resolved_cash: list[tuple[Wallet, Decimal, uuid.UUID, date, str | None]] = []
    for part in cash_parts:
        amount = _money(part.amount)
        if amount <= 0:
            raise WarehousePaymentError("Сумма наличной части должна быть больше нуля")
        wallet = await session.get(Wallet, part.wallet_id)
        if wallet is None or wallet.status != "active":
            raise WarehousePaymentError("Счёт не найден или неактивен")
        article_id = part.article_id or await resolve_production_article_id(session)
        if await session.get(DdsArticle, article_id) is None:
            raise WarehousePaymentError("Статья ДДС не найдена")
        resolved_cash.append((wallet, amount, article_id, part.operation_date, part.comment))
        total += amount

    total = _money(total)
    if total <= 0:
        raise WarehousePaymentError("Сумма оплаты должна быть больше нуля")
    if total > remaining:
        raise WarehousePaymentError(
            f"Сумма частей {total} превышает остаток накладной {remaining}"
        )

    # --- apply in one transaction --------------------------------------------
    try:
        for operation, amount in resolved_bank:
            await _apply_bank_allocation(
                session,
                invoice=invoice,
                operation=operation,
                amount=amount,
                actor_user_id=actor_user_id,
            )
        for wallet, amount, article_id, op_date, comment in resolved_cash:
            await _apply_wallet_payment(
                session,
                invoice=invoice,
                wallet=wallet,
                amount=amount,
                operation_date=op_date,
                article_id=article_id,
                comment=comment,
                actor_user_id=actor_user_id,
            )
        await _recompute_status(session, invoice)
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    await session.refresh(invoice)
    return invoice
