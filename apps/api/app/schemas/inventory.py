from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class InventoryPositionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=200)
    allocation_group: str | None = None
    swap_group: str | None = Field(default=None, max_length=64)
    iiko_product_guid: str | None = Field(default=None, max_length=64)
    sort_order: int = 100


class InventoryPositionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    allocation_group: str | None = None
    swap_group: str | None = Field(default=None, max_length=64)
    is_active: bool | None = None
    sort_order: int | None = None


class InventoryPositionRead(BaseModel):
    id: uuid.UUID
    code: str
    display_name: str
    allocation_group: str | None = None
    swap_group: str | None = None
    iiko_product_guid: str | None = None
    is_active: bool
    sort_order: int
    created_at: datetime | None = None
    updated_at: datetime | None = None


class IikoProductRead(BaseModel):
    guid: str
    name: str
    code: str | None = None


class InventoryAuditImportIikoPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    business_date: date
    document_id: str | None = Field(default=None, max_length=64)


class InventoryPositionsSyncRead(BaseModel):
    added: int
    updated: int
    total: int


class IikoInventoryCandidateRead(BaseModel):
    document_id: str
    document_num: str
    items_count: int
    total_shortage: str
    matched_active_count: int


class InventoryAuditManualItemPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position_id: uuid.UUID | None = None
    position_code: str | None = Field(default=None, max_length=64)
    product_name_snapshot: str | None = Field(default=None, max_length=200)
    shortage_amount: Decimal = Field(ge=0)


class InventoryAuditCreateManualPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    business_date: date
    items: list[InventoryAuditManualItemPayload] = Field(min_length=1)
    notes: str | None = Field(default=None, max_length=2000)


class InventoryAuditPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notes: str | None = Field(default=None, max_length=2000)


class InventoryAuditEmployeeExclusionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    excluded: bool
    reason: str | None = Field(default=None, max_length=500)


class InventoryAuditItemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position_id: uuid.UUID | None = None
    position_code: str | None = Field(default=None, max_length=64)
    iiko_product_guid: str | None = Field(default=None, max_length=64)
    product_name_snapshot: str | None = Field(default=None, max_length=200)
    shortage_amount: Decimal = Field(ge=0)


class InventoryAuditItemPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position_id: uuid.UUID | None = None
    position_code: str | None = Field(default=None, max_length=64)
    iiko_product_guid: str | None = Field(default=None, max_length=64)
    product_name_snapshot: str | None = Field(default=None, min_length=1, max_length=200)
    shortage_amount: Decimal | None = Field(default=None, ge=0)
    swap_group_override: str | None = Field(default=None, max_length=64)


class InventoryAuditItemRead(BaseModel):
    id: uuid.UUID
    audit_id: uuid.UUID
    position_id: uuid.UUID | None = None
    position_code: str | None = None
    position_display_name: str | None = None
    allocation_group: str | None = None
    is_considered: bool
    amount: str
    swap_group: str | None = None
    swap_group_default: str | None = None
    swap_group_override: str | None = None
    has_swap_group_override: bool = False
    iiko_product_guid: str | None = None
    product_name_snapshot: str
    shortage_amount: str
    created_at: datetime | None = None


class InventoryAuditListRead(BaseModel):
    id: uuid.UUID
    business_date: date
    previous_audit_date: date | None = None
    iiko_document_id: str | None = None
    iiko_document_num: str | None = None
    source: str
    status: str
    total_shortage_amount: str
    total_penalty_amount: str
    employee_count: int
    notes: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    applied_at: datetime | None = None


class InventoryAuditDetailRead(InventoryAuditListRead):
    total_shortage_iiko: str
    total_shortage_considered: str
    items: list[InventoryAuditItemRead]
    swap_groups: list[dict[str, Any]] = Field(default_factory=list)
    items_skipped_count: int
    computation_snapshot: dict[str, Any] | None = None


class PenaltyComputationRead(BaseModel):
    audit_id: uuid.UUID
    total_shortage_amount: str
    total_penalty_amount: str
    period_start: date
    period_end: date
    groups: dict[str, Any]
    swap_groups: list[dict[str, Any]] = Field(default_factory=list)
    employee_penalties: list[dict[str, Any]]
    employee_recipients: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str]
