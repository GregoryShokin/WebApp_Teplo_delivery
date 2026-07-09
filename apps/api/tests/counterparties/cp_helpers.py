"""Shared factories/auth helpers for the «Контрагенты» module tests.

Imported by bare name (``from cp_helpers import ...``) — pytest runs in *prepend*
import mode without ``__init__.py``, so a sibling module in this directory is on
``sys.path`` (same trick the root ``conftest`` uses for ``conftest_guard``).

Factories take an :class:`AsyncSession` and ``flush`` only — the caller decides when
to commit (service tests share the session; API tests must commit so the app's own
session sees the rows).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import create_access_token
from app.models import (
    Account,
    BankOperation,
    Counterparty,
    CounterpartyAlias,
    CounterpartyPayableProfile,
    CounterpartyPaymentDraft,
    CounterpartyRole,
    CounterpartyRoutingRule,
    DdsArticle,
    IikoProduct,
    InvoicePaymentAllocation,
    Organization,
    Role,
    SupplierInvoice,
    User,
    UserRole,
    Wallet,
)

ADMIN_EMAIL = "admin@teplo.local"


# --- auth ---------------------------------------------------------------------


def token_headers(user_id: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(str(user_id))}"}


async def admin_user_id(session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with session_factory() as session:
        admin_id = await session.scalar(select(User.id).where(User.email == ADMIN_EMAIL))
        assert admin_id is not None, "seeded admin user is missing from the test DB"
        return admin_id


async def create_user(
    session_factory: async_sessionmaker[AsyncSession],
    email: str,
    role_codes: Sequence[str],
) -> uuid.UUID:
    """Create (or reuse) a user with the given role codes; mirrors test_access_control_api."""
    async with session_factory() as session:
        existing = await session.scalar(select(User).where(User.email == email))
        if existing is not None:
            return existing.id
        organization_id = await session.scalar(select(Organization.id).limit(1))
        assert organization_id is not None
        roles = (await session.scalars(select(Role).where(Role.code.in_(tuple(role_codes))))).all()
        assert {role.code for role in roles} == set(role_codes), (
            f"missing seeded roles for {role_codes}"
        )
        user = User(
            email=email,
            full_name=email,
            hashed_password="sha256$unused",
            is_active=True,
        )
        session.add(user)
        await session.flush()
        for role in roles:
            session.add(UserRole(user_id=user.id, role_id=role.id, organization_id=organization_id))
        await session.commit()
        return user.id


async def headers_for(
    session_factory: async_sessionmaker[AsyncSession],
    email: str,
    role_codes: Sequence[str],
) -> dict[str, str]:
    return token_headers(await create_user(session_factory, email, role_codes))


async def admin_headers(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, str]:
    return token_headers(await admin_user_id(session_factory))


# --- domain factories ---------------------------------------------------------


def _money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


async def make_counterparty(
    session: AsyncSession,
    *,
    name: str,
    inn: str | None = None,
    cp_type: str = "legal_entity",
    status: str = "active",
    relationship: str = "official",
    role: str | None = "supplier",
    iiko_guid: str | None = None,
    requisites: dict[str, Any] | None = None,
    requisites_verified: bool = False,
    ledger_category_id: uuid.UUID | None = None,
    brand_group: str | None = None,
    payment_delay_days: int | None = None,
    payment_due_day_of_month: int | None = None,
) -> Counterparty:
    """A supplier counterparty + role + payable profile (+ optional iiko alias)."""
    counterparty = Counterparty(name=name, inn=inn, type=cp_type, status=status)
    session.add(counterparty)
    await session.flush()
    if role:
        session.add(CounterpartyRole(counterparty_id=counterparty.id, role=role))
    profile = CounterpartyPayableProfile(
        counterparty_id=counterparty.id,
        internal_name=name,
        relationship=relationship,
        requisites=dict(requisites or {}),
        requisites_verified=requisites_verified,
        requisites_verified_at=datetime.now(tz=UTC) if requisites_verified else None,
        ledger_category_id=ledger_category_id,
        brand_group=brand_group,
        payment_delay_days=payment_delay_days,
        payment_due_day_of_month=payment_due_day_of_month,
    )
    session.add(profile)
    if iiko_guid:
        session.add(
            CounterpartyAlias(counterparty_id=counterparty.id, alias=iiko_guid, source="iiko")
        )
    await session.flush()
    return counterparty


async def make_invoice(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    amount: Decimal | str | float,
    number: str | None = None,
    payment_status: str = "unpaid",
    source: str = "manual",
    direction: str = "payable",
    external_id: str | None = None,
    vat_total: Decimal | str | float = 0,
    vat_breakdown: dict[str, Any] | None = None,
    line_items: list[dict[str, Any]] | None = None,
    invoice_date: date | None = None,
    issued_at: datetime | None = None,
    staff_amount: Decimal | str | float | None = None,
    barter_role: str | None = None,
    due_date: date | None = None,
    draft_id: uuid.UUID | None = None,
) -> SupplierInvoice:
    invoice = SupplierInvoice(
        counterparty_id=counterparty_id,
        source=source,
        direction=direction,
        external_id=external_id,
        number=number,
        amount=_money(amount),
        staff_amount=_money(staff_amount) if staff_amount is not None else Decimal("0.00"),
        vat_total=_money(vat_total),
        vat_breakdown=dict(vat_breakdown or {}),
        line_items=list(line_items or []),
        payment_status=payment_status,
        invoice_date=invoice_date,
        issued_at=issued_at,
        barter_role=barter_role,
        due_date=due_date,
        draft_id=draft_id,
    )
    session.add(invoice)
    await session.flush()
    return invoice


async def make_draft(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    amount: Decimal | str | float,
    status: str = "created",
    document_id: str | None = None,
) -> CounterpartyPaymentDraft:
    draft = CounterpartyPaymentDraft(
        counterparty_id=counterparty_id,
        document_id=document_id or f"teplo-cp-{uuid.uuid4()}",
        amount=_money(amount),
        status=status,
    )
    session.add(draft)
    await session.flush()
    return draft


async def make_account(
    session: AsyncSession,
    *,
    bank_code: str = "tbank",
    account_number: str | None = None,
    legal_entity: str = "ООО Тест",
    bic: str | None = None,
) -> Account:
    """A bank account a wallet can hang off, so the bank-feed reconcile resolves a wallet."""
    account = Account(
        bank_code=bank_code,
        account_number=account_number or f"4070281{uuid.uuid4().hex[:13]}",
        legal_entity=legal_entity,
        bic=bic,
    )
    session.add(account)
    await session.flush()
    return account


async def make_wallet(
    session: AsyncSession,
    *,
    name: str = "Касса",
    wallet_type: str = "cash_safe",
    status: str = "active",
    code: str | None = None,
    account_id: uuid.UUID | None = None,
) -> Wallet:
    wallet = Wallet(
        code=code or f"w-{uuid.uuid4().hex[:8]}",
        name=name,
        type=wallet_type,
        status=status,
        account_id=account_id,
    )
    session.add(wallet)
    await session.flush()
    return wallet


async def make_expense_article(
    session: AsyncSession,
    *,
    code: str = "payment_to_supplier",
    name: str = "Оплата поставщикам",
) -> DdsArticle:
    existing = await session.scalar(select(DdsArticle).where(DdsArticle.code == code))
    if existing is not None:
        return existing
    article = DdsArticle(code=code, name=name, movement_type="outflow", activity_type="operating")
    session.add(article)
    await session.flush()
    return article


async def make_iiko_product(
    session: AsyncSession, *, name: str = "Товар", unit: str = "кг"
) -> IikoProduct:
    """Сопоставимая товарная позиция iiko — товарные строки накладной требуют product_guid."""
    product = IikoProduct(
        iiko_id=str(uuid.uuid4()), name=name, type="GOODS", unit=unit, synced_at=datetime.now(UTC)
    )
    session.add(product)
    await session.flush()
    return product


async def make_bank_operation(
    session: AsyncSession,
    *,
    amount: Decimal | str | float,
    direction: str = "out",
    inn: str | None = None,
    name: str | None = None,
    account: str | None = None,
    operation_date: date | None = None,
    posted_at: datetime | None = None,
    provider: str = "tbank",
    provider_operation_id: str | None = None,
    receiver: dict[str, Any] | None = None,
    raw_payload: dict[str, Any] | None = None,
    category: str | None = None,
    account_id: uuid.UUID | None = None,
    transfer_group_id: uuid.UUID | None = None,
    classification_status: str = "pending",
) -> BankOperation:
    """An outgoing bank op. ``amount`` stays POSITIVE — sign lives in ``direction``
    (matches the T-Bank normalizer: Debit/42000.00 → direction='out', amount=42000.00).
    """
    payload: dict[str, Any] = dict(raw_payload or {})
    if receiver is not None:
        payload["receiver"] = receiver
    if category is not None:
        payload["category"] = category
    operation = BankOperation(
        provider=provider,
        provider_operation_id=provider_operation_id or f"op-{uuid.uuid4()}",
        operation_date=operation_date or date.today(),
        posted_at=posted_at,
        direction=direction,
        amount=_money(amount),
        counterparty_name_raw=name,
        counterparty_inn_raw=inn,
        counterparty_account_raw=account,
        raw_payload=payload,
        account_id=account_id,
        transfer_group_id=transfer_group_id,
        classification_status=classification_status,
    )
    session.add(operation)
    await session.flush()
    return operation


async def add_routing_rule(
    session: AsyncSession,
    *,
    iiko_supplier_guid: str,
    prefix: str,
    counterparty_id: uuid.UUID,
) -> CounterpartyRoutingRule:
    rule = CounterpartyRoutingRule(
        iiko_supplier_guid=iiko_supplier_guid,
        prefix=prefix,
        counterparty_id=counterparty_id,
    )
    session.add(rule)
    await session.flush()
    return rule


async def allocated_total(session: AsyncSession, invoice_id: uuid.UUID) -> Decimal:
    from sqlalchemy import func

    total = await session.scalar(
        select(func.coalesce(func.sum(InvoicePaymentAllocation.amount), 0)).where(
            InvoicePaymentAllocation.invoice_id == invoice_id
        )
    )
    return _money(total)


# --- iiko XML builders --------------------------------------------------------


def suppliers_xml(suppliers: Sequence[dict[str, Any]]) -> str:
    """Build a ``/suppliers`` XML payload. Each dict: id, name, inn, deleted,
    represents_store."""
    parts = ["<employees>"]
    for supplier in suppliers:
        parts.append("<employee>")
        parts.append(f"<id>{supplier['id']}</id>")
        parts.append(f"<name>{supplier.get('name', supplier['id'])}</name>")
        if supplier.get("inn") is not None:
            parts.append(f"<taxpayerIdNumber>{supplier['inn']}</taxpayerIdNumber>")
        parts.append(f"<deleted>{str(supplier.get('deleted', False)).lower()}</deleted>")
        parts.append(
            f"<representsStore>{str(supplier.get('represents_store', False)).lower()}"
            "</representsStore>"
        )
        parts.append("</employee>")
    parts.append("</employees>")
    return "".join(parts)


def _cloud_item(item: dict[str, Any]) -> dict[str, Any]:
    """Позиция Cloud-документа. Принимает те же ключи, что легаси-XML-билдер
    ({sum, vat_percent, vat_sum, product, article, amount}); ``vat_sum`` конвертируется в
    ``sumWithoutVat = sum − vat_sum`` — Cloud не отдаёт ``vatSum``, ingest считает НДС разницей."""
    out: dict[str, Any] = {"sum": item["sum"]}
    if item.get("vat_percent") is not None:
        out["vatPercent"] = item["vat_percent"]
    if item.get("vat_sum") is not None:
        out["sumWithoutVat"] = round(float(item["sum"]) - float(item["vat_sum"]), 2)
    if item.get("product") is not None:
        out["product"] = item["product"]
    if item.get("article") is not None:
        out["productArticle"] = item["article"]
    if item.get("amount") is not None:
        out["amount"] = item["amount"]
    return out


def cloud_invoice_docs(documents: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cloud JSON-документы (incoming) в форме ответа ``incoming_invoice/get``.

    Each dict supports: id, supplier (guid), status (default PROCESSED),
    document_number, transport_invoice_number, incoming_date, due_date,
    items (list of {sum, vat_percent, vat_sum, product, article, amount}).
    """
    docs: list[dict[str, Any]] = []
    for doc in documents:
        out: dict[str, Any] = {
            "documentId": doc["id"],
            "status": doc.get("status", "PROCESSED"),
            "counteragent": doc.get("supplier"),
            "number": doc.get("document_number"),
            "transportInvoiceNumber": doc.get("transport_invoice_number"),
            "incomingDate": doc.get("incoming_date"),
            "dueDate": doc.get("due_date"),
            "items": [_cloud_item(item) for item in doc.get("items", [])],
        }
        docs.append(out)
    return docs


def cloud_outgoing_docs(documents: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cloud JSON-документы (outgoing, our AR) в форме ответа ``outgoing_invoice/get``.
    Партнёр — в ``counteragent`` (в Cloud оба направления используют это поле); дата — ``date``.

    Each dict supports: id, counteragent (guid), status (default PROCESSED),
    document_number, incoming_date, items.
    """
    docs: list[dict[str, Any]] = []
    for doc in documents:
        out: dict[str, Any] = {
            "documentId": doc["id"],
            "status": doc.get("status", "PROCESSED"),
            "counteragent": doc.get("counteragent"),
            "number": doc.get("document_number"),
            "date": doc.get("incoming_date"),
            "items": [_cloud_item(item) for item in doc.get("items", [])],
        }
        docs.append(out)
    return docs
