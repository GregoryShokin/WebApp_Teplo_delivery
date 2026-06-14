from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class IikoProduct(Base):
    """Queryable cache of the iiko nomenclature (`/v2/entities/products/list`).

    Powers the line-item product picker on warehouse invoices: searchable by name/code,
    filtered by ``type`` (the picker defaults to GOODS — purchasable raw goods, not dishes
    or modifiers). ``unit`` is the human-readable measure («кг»/«л»/«шт»/«порц») resolved
    from ``main_unit_guid`` (iiko returns the unit only as a GUID; the catalogue has just a
    handful of distinct ones, mapped once via the ``iiko.unit_guid_map`` setting). Deleted
    iiko products are kept (flagged ``deleted``) because their GUID may live in historical
    invoice lines. Refreshed by a periodic sync job, never edited by hand.
    """

    __tablename__ = "iiko_product"
    __table_args__ = (
        Index("uq_iiko_product_iiko_id", "iiko_id", unique=True),
        Index("ix_iiko_product_type", "type"),
        Index("ix_iiko_product_name_lower", func.lower(text("name"))),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Stable iiko product GUID — the value pushed back as <product> in invoice items.
    iiko_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # iiko product type: GOODS / DISH / PREPARED / MODIFIER / SERVICE.
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    # Raw measure-unit GUID from iiko; kept so a newly-seen unit can be mapped later.
    main_unit_guid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Resolved unit label («кг» / «л» / «шт» / «порц»); NULL until the GUID is mapped.
    unit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
