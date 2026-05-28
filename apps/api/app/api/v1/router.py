from fastapi import APIRouter

from app.api.v1.payroll_config import router as payroll_config_router
from app.api.v1.routes.auth import router as auth_router
from app.api.v1.routes.employees import router as employees_router
from app.api.v1.routes.health import router as health_router
from app.api.v1.routes.integrations import router as integrations_router
from app.api.v1.routes.payroll import router as payroll_router
from app.api.v1.routes.settings import router as settings_router
from app.api.v1.routes.shifts import router as shifts_router

api_router = APIRouter()
api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(health_router, tags=["health"])
api_router.include_router(employees_router, prefix="/employees", tags=["employees"])
api_router.include_router(integrations_router, prefix="/integrations", tags=["integrations"])
api_router.include_router(payroll_router, prefix="/payroll", tags=["payroll"])
api_router.include_router(payroll_config_router, prefix="/payroll/config", tags=["payroll-config"])
api_router.include_router(settings_router, prefix="/settings", tags=["settings"])
api_router.include_router(shifts_router, prefix="/shifts", tags=["shifts"])
