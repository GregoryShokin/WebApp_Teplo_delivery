from __future__ import annotations

import asyncio
import logging

from app.services.iiko_sync import sync_employees

logger = logging.getLogger(__name__)


def run_employee_sync_job() -> None:
    try:
        result = asyncio.run(sync_employees(run_reason="cron"))
    except Exception:
        logger.exception("iiko employee sync job failed")
        raise
    logger.info("iiko employee sync completed: %s", result.as_dict())
