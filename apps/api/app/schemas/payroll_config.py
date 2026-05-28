from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class PayrollRateBase(BaseModel):
    position_group: str
    category: str
    station: str | None = None
    rate_type: str = Field(default="daily", pattern="^(daily|hourly|monthly)$")
    amount: float | None = Field(default=None, ge=0)
    is_active: bool = True
    effective_from: date
    effective_to: date | None = None


class PayrollRateRead(PayrollRateBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime


class PayrollRevenueShareBase(BaseModel):
    position_group: str
    category: str
    percent: float = Field(ge=0)
    effective_from: date
    effective_to: date | None = None


class PayrollRevenueShareRead(PayrollRevenueShareBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime


class PayrollDeductionCategoryBase(BaseModel):
    code: str
    display_name: str
    description: str | None = None
    type: str = Field(pattern="^(fine|withholding|deposit_writeoff)$")
    default_amount: float | None = Field(default=None, ge=0)
    effective_from: date
    effective_to: date | None = None


class PayrollDeductionCategoryRead(PayrollDeductionCategoryBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime


class PayrollSeniorityPremiumBase(BaseModel):
    role: str = Field(pattern="^(senior|deputy_senior)$")
    percent_of_base: float = Field(ge=0)
    effective_from: date
    effective_to: date | None = None


class PayrollSeniorityPremiumRead(PayrollSeniorityPremiumBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
