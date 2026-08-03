from fastapi import APIRouter

from app.api.v1.payroll_config import router as payroll_config_router
from app.api.v1.routes.access_control import router as access_control_router
from app.api.v1.routes.accounting_suppliers import router as accounting_suppliers_router
from app.api.v1.routes.accumulation_fund import router as accumulation_fund_router
from app.api.v1.routes.auth import router as auth_router
from app.api.v1.routes.counterparties import router as counterparties_router
from app.api.v1.routes.couriers import router as couriers_router
from app.api.v1.routes.dds import router as dds_router
from app.api.v1.routes.deposits import router as deposits_router
from app.api.v1.routes.employees import router as employees_router
from app.api.v1.routes.finance_payments import router as finance_payments_router
from app.api.v1.routes.fixed_assets import router as fixed_assets_router
from app.api.v1.routes.health import router as health_router
from app.api.v1.routes.integrations import router as integrations_router
from app.api.v1.routes.inventory import router as inventory_router
from app.api.v1.routes.kassa import router as kassa_router
from app.api.v1.routes.locations import router as locations_router
from app.api.v1.routes.owners import router as owners_router
from app.api.v1.routes.payment_page import router as payment_page_router
from app.api.v1.routes.payroll import router as payroll_router
from app.api.v1.routes.payroll_adjustments import router as payroll_adjustments_router
from app.api.v1.routes.payroll_admin import router as payroll_admin_router
from app.api.v1.routes.payroll_advances import router as payroll_advances_router
from app.api.v1.routes.positions import router as positions_router
from app.api.v1.routes.reports_pnl import router as reports_pnl_router
from app.api.v1.routes.sbis import router as sbis_router
from app.api.v1.routes.settings import router as settings_router
from app.api.v1.routes.shift_schedule import router as shift_schedule_router
from app.api.v1.routes.shifts import router as shifts_router
from app.api.v1.routes.taxes import router as taxes_router
from app.api.v1.routes.utilities import router as utilities_router
from app.api.v1.routes.vacations import router as vacations_router
from app.api.v1.routes.warehouse import router as warehouse_router
from app.api.v1.routes.webhooks import router as webhooks_router

api_router = APIRouter()
api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(
    accounting_suppliers_router, prefix="/accounting/suppliers", tags=["accounting-suppliers"]
)
api_router.include_router(access_control_router, prefix="/access-control", tags=["access-control"])
api_router.include_router(health_router, tags=["health"])
api_router.include_router(couriers_router, prefix="/couriers", tags=["couriers"])
api_router.include_router(counterparties_router, prefix="/counterparties", tags=["counterparties"])
api_router.include_router(deposits_router, prefix="/deposits", tags=["deposits"])
api_router.include_router(dds_router, prefix="/dds", tags=["dds"])
api_router.include_router(employees_router, prefix="/employees", tags=["employees"])
api_router.include_router(integrations_router, prefix="/integrations", tags=["integrations"])
api_router.include_router(inventory_router, prefix="/inventory", tags=["inventory"])
api_router.include_router(accumulation_fund_router, prefix="/payroll/fund", tags=["payroll"])
api_router.include_router(payroll_admin_router, prefix="/payroll/admin", tags=["payroll-admin"])
api_router.include_router(
    payroll_advances_router, prefix="/payroll/advances", tags=["payroll-advances"]
)
api_router.include_router(payroll_router, prefix="/payroll", tags=["payroll"])
api_router.include_router(payroll_adjustments_router, prefix="/payroll", tags=["payroll"])
api_router.include_router(payroll_config_router, prefix="/payroll/config", tags=["payroll-config"])
api_router.include_router(positions_router, prefix="/settings/positions", tags=["positions"])
api_router.include_router(sbis_router, prefix="/sbis", tags=["sbis"])
api_router.include_router(settings_router, prefix="/settings", tags=["settings"])
api_router.include_router(locations_router, prefix="/locations", tags=["locations"])
api_router.include_router(fixed_assets_router, prefix="/fixed-assets", tags=["fixed-assets"])
api_router.include_router(shift_schedule_router, prefix="/schedule", tags=["schedule"])
api_router.include_router(shifts_router, prefix="/shifts", tags=["shifts"])
api_router.include_router(taxes_router, prefix="/taxes", tags=["taxes"])
api_router.include_router(utilities_router, prefix="/accounting/utilities", tags=["utilities"])
api_router.include_router(vacations_router, prefix="/vacations", tags=["vacations"])
api_router.include_router(webhooks_router, prefix="/webhooks", tags=["webhooks"])
api_router.include_router(warehouse_router, prefix="/warehouse", tags=["warehouse"])
api_router.include_router(kassa_router, prefix="/kassa", tags=["kassa"])
api_router.include_router(payment_page_router, prefix="/payment-page", tags=["payment-page"])
api_router.include_router(owners_router, prefix="/owners", tags=["owners"])
api_router.include_router(
    finance_payments_router, prefix="/finance/payments", tags=["finance-payments"]
)
api_router.include_router(reports_pnl_router, prefix="/reports/pnl", tags=["reports"])
