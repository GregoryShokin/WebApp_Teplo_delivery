from fastapi import APIRouter

from app.api.v1.payroll_config import router as payroll_config_router
from app.api.v1.routes.access_control import router as access_control_router
from app.api.v1.routes.accumulation_fund import router as accumulation_fund_router
from app.api.v1.routes.auth import router as auth_router
from app.api.v1.routes.couriers import router as couriers_router
from app.api.v1.routes.dds import router as dds_router
from app.api.v1.routes.deposits import router as deposits_router
from app.api.v1.routes.employees import router as employees_router
from app.api.v1.routes.health import router as health_router
from app.api.v1.routes.integrations import router as integrations_router
from app.api.v1.routes.inventory import router as inventory_router
from app.api.v1.routes.payroll import router as payroll_router
from app.api.v1.routes.payroll_adjustments import router as payroll_adjustments_router
from app.api.v1.routes.settings import router as settings_router
from app.api.v1.routes.shift_schedule import router as shift_schedule_router
from app.api.v1.routes.shifts import router as shifts_router
from app.api.v1.routes.vacations import router as vacations_router

api_router = APIRouter()
api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(access_control_router, prefix="/access-control", tags=["access-control"])
api_router.include_router(health_router, tags=["health"])
api_router.include_router(couriers_router, prefix="/couriers", tags=["couriers"])
api_router.include_router(deposits_router, prefix="/deposits", tags=["deposits"])
api_router.include_router(dds_router, prefix="/dds", tags=["dds"])
api_router.include_router(employees_router, prefix="/employees", tags=["employees"])
api_router.include_router(integrations_router, prefix="/integrations", tags=["integrations"])
api_router.include_router(inventory_router, prefix="/inventory", tags=["inventory"])
api_router.include_router(accumulation_fund_router, prefix="/payroll/fund", tags=["payroll"])
api_router.include_router(payroll_router, prefix="/payroll", tags=["payroll"])
api_router.include_router(payroll_adjustments_router, prefix="/payroll", tags=["payroll"])
api_router.include_router(payroll_config_router, prefix="/payroll/config", tags=["payroll-config"])
api_router.include_router(settings_router, prefix="/settings", tags=["settings"])
api_router.include_router(shift_schedule_router, prefix="/schedule", tags=["schedule"])
api_router.include_router(shifts_router, prefix="/shifts", tags=["shifts"])
api_router.include_router(vacations_router, prefix="/vacations", tags=["vacations"])
