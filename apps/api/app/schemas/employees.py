from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EmployeeStatus = str
EmployeeChangeSource = Literal["app", "iiko_sync", "system_migration"]
EmployeeChangeStatus = Literal["success", "error", "requires_review", "skipped"]


class EmployeeRoleAssignmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    payroll_role: str
    category: str
    is_primary: bool
    is_substitute: bool = False
    effective_from: date
    effective_to: date | None = None
    is_pending: bool
    created_at: datetime
    updated_at: datetime


class EmployeeRoleAssignmentCreate(BaseModel):
    payroll_role: str = Field(max_length=64)
    category: str = Field(max_length=64)
    is_primary: bool = False
    is_substitute: bool = False
    effective_from: date | None = None
    comment: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_substitute_primary(self) -> EmployeeRoleAssignmentCreate:
        if self.is_substitute and self.is_primary:
            raise ValueError("Подменная роль не может быть основной")
        return self


class EmployeeRoleAssignmentPatch(BaseModel):
    payroll_role: str | None = Field(default=None, max_length=64)
    category: str | None = Field(default=None, max_length=64)
    is_primary: bool | None = None
    is_substitute: bool | None = None
    effective_from: date | None = None
    comment: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_substitute_primary(self) -> EmployeeRoleAssignmentPatch:
        if self.is_substitute is True and self.is_primary is True:
            raise ValueError("Подменная роль не может быть основной")
        return self


class EmployeePositionEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    position: str
    effective_from: date
    effective_to: date | None = None
    comment: str | None = None
    created_at: datetime
    updated_at: datetime


class EmployeeAllowanceEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    allowance_type: str
    is_enabled: bool
    effective_from: date
    effective_to: date | None = None
    comment: str | None = None
    created_at: datetime
    updated_at: datetime


class EmployeePendingIikoActionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    action_type: str
    status: str
    effective_on: date
    payload: dict[str, Any] = Field(default_factory=dict)
    attempts: int = 0
    last_error: str | None = None
    related_entity_type: str | None = None
    related_entity_id: uuid.UUID | None = None
    applied_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class EmployeeNoticeInfo(BaseModel):
    notice_date: date
    days_since: int
    will_trigger_full_payout: bool


class EmployeeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str
    iiko_id: str
    position: str | None = None
    category: str | None = None
    default_cooking_station: str | None = None
    is_senior: bool = False
    is_deputy_senior: bool = False
    status: EmployeeStatus
    hire_date: date | None = None
    tenure_started_at: date | None = None
    fire_date: date | None = None
    fire_reason: str | None = None
    requires_role_review: bool = False
    role_review_payload: dict[str, Any] | None = None
    pin_assumed_from_iiko: bool = False
    pin_set_at: datetime | None = None
    iiko_sync_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    assignments: list[EmployeeRoleAssignmentRead] = Field(default_factory=list)
    active_notice: EmployeeNoticeInfo | None = None


class DepositDismissAction(StrEnum):
    PAYOUT_FULL = "payout_full"
    PAYOUT_PARTIAL = "payout_partial"
    WRITE_OFF = "write_off"
    NONE = "none"


class EmployeeNoticeRequest(BaseModel):
    notice_date: date = Field(default_factory=date.today)
    comment: str | None = Field(default=None, max_length=1000)

    @field_validator("comment")
    @classmethod
    def strip_comment(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.strip()
        return normalized or None


class EmployeeNoticeCancelRequest(BaseModel):
    comment: str | None = Field(default=None, max_length=1000)

    @field_validator("comment")
    @classmethod
    def strip_comment(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.strip()
        return normalized or None


class EmployeeHireDateRequest(BaseModel):
    hire_date: date
    comment: str | None = Field(default=None, max_length=1000)

    @field_validator("comment")
    @classmethod
    def strip_comment(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.strip()
        return normalized or None


class EmployeeNoticeActionRead(BaseModel):
    event_id: uuid.UUID
    effective_from: date
    days_until_today: int


class EmployeeDismissRequest(BaseModel):
    fire_date: date | None = None
    reason_id: uuid.UUID | None = None
    reason_code: str | None = Field(default=None, max_length=64)
    reason_label: str | None = Field(default=None, max_length=160)
    comment: str | None = Field(default=None, max_length=1000)
    reason: str | None = Field(default=None, max_length=1000)
    deposit_action: DepositDismissAction = DepositDismissAction.NONE
    deposit_payout_amount: Decimal | None = Field(default=None, ge=0)
    deposit_comment: str | None = Field(default=None, max_length=1000)

    @field_validator("reason_code", "reason_label", "comment", "reason")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.strip()
        return normalized or None

    @field_validator("deposit_comment")
    @classmethod
    def strip_optional_deposit_text(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_deposit_action(self) -> EmployeeDismissRequest:
        if self.deposit_action == DepositDismissAction.PAYOUT_PARTIAL:
            if self.deposit_payout_amount is None or self.deposit_payout_amount <= 0:
                raise ValueError("Для частичной выплаты укажите сумму больше 0")
            return self
        if self.deposit_payout_amount is not None:
            raise ValueError("Сумма выплаты допустима только для частичной выплаты депозита")
        return self


class EmployeeCreateRoleRequest(BaseModel):
    payroll_role: str = Field(min_length=1, max_length=64)
    category: str = Field(min_length=1, max_length=64)
    is_primary: bool = False


class EmployeeCreateRequest(BaseModel):
    full_name: str = Field(min_length=1, max_length=255)
    pin_code: str
    iiko_role_id: str = Field(min_length=1, max_length=128)
    category: str | None = Field(default=None, max_length=64)
    roles: list[EmployeeCreateRoleRequest] = Field(default_factory=list)
    is_senior: bool = False
    is_deputy_senior: bool = False

    @field_validator("full_name")
    @classmethod
    def validate_full_name(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized.split()) < 2:
            raise ValueError("Укажите минимум два слова в ФИО")
        return normalized

    @field_validator("pin_code")
    @classmethod
    def validate_pin_code(cls, value: str) -> str:
        if not value.isdigit() or len(value) != 4:
            raise ValueError("ПИН-код должен состоять из 4 цифр")
        return value

    @model_validator(mode="after")
    def validate_roles(self) -> EmployeeCreateRequest:
        if not self.roles:
            return self
        primary_count = sum(1 for role in self.roles if role.is_primary)
        if primary_count != 1:
            raise ValueError("Ровно одна роль должна быть основной")
        payroll_roles = [role.payroll_role for role in self.roles]
        if len(set(payroll_roles)) != len(payroll_roles):
            raise ValueError("Роли не должны повторяться")
        return self


class EmployeePinChangeRequest(BaseModel):
    pin_code: str

    @field_validator("pin_code")
    @classmethod
    def validate_pin_code(cls, value: str) -> str:
        if not value.isdigit() or len(value) != 4:
            raise ValueError("ПИН-код должен состоять из 4 цифр")
        return value


class EmployeePatchRoleAssignment(BaseModel):
    id: uuid.UUID | None = None
    payroll_role: str = Field(min_length=1, max_length=64)
    category: str = Field(min_length=1, max_length=64)
    is_primary: bool = False


class IikoEmployeeRoleRead(BaseModel):
    id: str
    name: str
    code: str | None = None
    deleted: bool = False


class EmployeeDismissalReasonRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    label: str
    requires_comment: bool = False
    is_system: bool = False
    is_active: bool = True
    sort_order: int = 0
    created_at: datetime
    updated_at: datetime


class EmployeeDismissalReasonCreate(BaseModel):
    code: str | None = Field(default=None, min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=160)
    requires_comment: bool = False
    is_active: bool = True
    sort_order: int = Field(default=0, ge=0)

    @field_validator("code")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.strip()
        return normalized or None

    @field_validator("label")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Значение обязательно")
        return normalized


class EmployeeDismissalReasonUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=160)
    requires_comment: bool | None = None
    is_active: bool | None = None
    sort_order: int | None = Field(default=None, ge=0)

    @field_validator("label")
    @classmethod
    def strip_optional_required_text(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("Значение обязательно")
        return normalized


class EmployeeChangeEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID | None = None
    changed_at: datetime
    effective_from: date | None = None
    effective_to: date | None = None
    change_type: str
    source: EmployeeChangeSource
    actor_user_id: uuid.UUID | None = None
    actor_label: str | None = None
    status: EmployeeChangeStatus
    summary: str
    before_value: dict[str, Any] | None = None
    after_value: dict[str, Any] | None = None
    diff: dict[str, Any] | None = None
    reason_id: uuid.UUID | None = None
    reason: str | None = None
    reason_code: str | None = None
    reason_label: str | None = None
    comment: str | None = None
    related_agent_run_id: uuid.UUID | None = None
    related_agent_action_id: uuid.UUID | None = None
    related_entity_type: str | None = None
    related_entity_id: uuid.UUID | None = None
    payroll_impact: bool = False
    payroll_impact_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class EmployeeChangeEventFilter(BaseModel):
    employee_id: uuid.UUID | None = None
    changed_from: datetime | None = None
    changed_to: datetime | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    change_type: str | None = None
    source: EmployeeChangeSource | None = None
    actor: str | None = None
    status: EmployeeChangeStatus | None = None
    only_errors: bool = False
    only_requires_review: bool = False
    include_system_migrations: bool = False


class EmployeePatch(BaseModel):
    full_name: str | None = Field(default=None, max_length=255)
    position: str | None = Field(default=None, max_length=160)
    category: str | None = Field(default=None, max_length=160)
    default_cooking_station: str | None = Field(default=None, max_length=160)
    is_senior: bool | None = None
    is_deputy_senior: bool | None = None
    requires_role_review: bool | None = None
    hire_date: date | None = None
    fire_date: date | None = None
    pin_code: str | None = None
    roles: list[EmployeePatchRoleAssignment] | None = None
    effective_from: date | None = None
    comment: str | None = Field(default=None, max_length=1000)
    transfer_from_existing: bool = False

    @field_validator("full_name")
    @classmethod
    def validate_full_name(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.strip()
        if len(normalized.split()) < 2:
            raise ValueError("Укажите минимум фамилию и имя")
        return normalized

    @field_validator("comment")
    @classmethod
    def strip_comment(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.strip()
        return normalized or None

    @field_validator("pin_code")
    @classmethod
    def validate_pin_code(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not value.isdigit() or len(value) != 4:
            raise ValueError("ПИН-код должен состоять из 4 цифр")
        return value

    @model_validator(mode="after")
    def validate_roles(self) -> EmployeePatch:
        if self.roles is None:
            return self
        if not self.roles:
            return self
        primary_count = sum(1 for role in self.roles if role.is_primary)
        if primary_count != 1:
            raise ValueError("Ровно одна роль должна быть основной")
        payroll_roles = [role.payroll_role for role in self.roles]
        if len(set(payroll_roles)) != len(payroll_roles):
            raise ValueError("Роли не должны повторяться")
        return self


class SyncResultRead(BaseModel):
    created: int
    updated: int
    deactivated: int


class ErrorDetail(BaseModel):
    detail: str | dict[str, Any]
