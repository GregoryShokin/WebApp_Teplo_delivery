from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PermissionRead(BaseModel):
    code: str
    description: str


class PermissionModuleRead(BaseModel):
    module: str
    permissions: list[PermissionRead]


class RoleAccessRead(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    is_editable: bool
    permission_codes: list[str]


class RolePermissionsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    permission_codes: list[str] = Field(default_factory=list)


class AccessUserRead(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    is_active: bool
    role_codes: list[str]


class AccessUserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    full_name: str
    password: str
    is_active: bool = True


class AccessUserPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str | None = None
    is_active: bool | None = None


class UserRoleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role_code: str


class AccessAuditEventRead(BaseModel):
    type: str
    actor_email: str | None = None
    user_email: str | None = None
    role_code: str
    permission_code: str | None = None
    action: str
    created_at: datetime
