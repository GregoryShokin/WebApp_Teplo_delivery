"""Модуль «Налоги»: расчёт УСН «Доходы» 6%, вычетов и задолженности перед бюджетом."""

from app.services.taxes.engine import (
    ClaimPolicy,
    TaxComputationError,
    TaxInputs,
    TaxState,
    YearConfig,
    compute_tax_state,
    period_code_for,
    period_end_date,
    rub,
)

__all__ = [
    "ClaimPolicy",
    "TaxComputationError",
    "TaxInputs",
    "TaxState",
    "YearConfig",
    "compute_tax_state",
    "period_code_for",
    "period_end_date",
    "rub",
]
