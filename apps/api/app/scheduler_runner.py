"""Единственный процесс, где живут ФОНОВЫЕ задачи (контейнер ``scheduler``).

Планировщиков в проекте два, и оба поднимаются здесь:

* ``app.scheduler`` — ``AsyncIOScheduler`` (банк, курьеры, ведомости): всегда жил в этом процессе;
* ``app.jobs.scheduler`` — ``BackgroundScheduler`` (синк накладных/сотрудников/СБИС, ночные
  начисления): раньше стартовал в lifespan веб-приложения. Прод запускает uvicorn с
  ``--workers 2``, поэтому каждая его джоба шла ДВАЖДЫ параллельно: два
  ``counterparty_invoice_sync`` каждые 15 минут выбирали лимит iiko Cloud, четверть прогонов
  падала с HTTP 429, а ручной пуш накладной в это окно получал ``TOO_MANY_REQUESTS``. Один
  процесс на джобу — и дублей нет по построению (флаг ``API_RUNS_BACKGROUND_JOBS`` возвращает
  прежнее поведение стенду без этого контейнера).
"""

from __future__ import annotations

import asyncio
import logging
import signal

from app.jobs.scheduler import get_scheduler
from app.scheduler import scheduler

logging.basicConfig(level=logging.INFO)


def start_all() -> list:
    """Поднять оба планировщика; вернуть их в порядке запуска (гасить — в обратном)."""
    jobs_scheduler = get_scheduler()
    scheduler.start()
    jobs_scheduler.start()
    return [scheduler, jobs_scheduler]


async def main() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)
    started = start_all()
    try:
        await stop_event.wait()
    finally:
        for started_scheduler in reversed(started):
            started_scheduler.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(main())
