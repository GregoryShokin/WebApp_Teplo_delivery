"""Дом фоновых джоб — процесс-планировщик, а не веб-воркеры.

Прод поднимает uvicorn с ``--workers 2``, и пока планировщик стартовал в lifespan приложения,
каждая джоба шла ДВАЖДЫ одновременно: два ``counterparty_invoice_sync`` каждые 15 минут выбирали
лимит частоты iiko Cloud (четверть прогонов падала с HTTP 429), а ручной пуш накладной в это окно
получал ``TOO_MANY_REQUESTS`` и садился в ``failed``. Тесты закрепляют новое разделение: веб молчит,
``scheduler_runner`` поднимает оба планировщика.
"""

from __future__ import annotations

import pytest

from app import main, scheduler_runner
from app.core.config import Settings


class _FakeScheduler:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def shutdown(self, wait: bool = True) -> None:  # noqa: FBT001, FBT002 — сигнатура apscheduler
        self.stopped += 1


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)


def _mute_startup_warmup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Прогрев реестров при старте к теме теста не относится — глушим, чтобы не ходить в БД."""

    async def _noop(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(main.position_registry, "refresh_position_registry", _noop)
    monkeypatch.setattr(main.iiko_location, "warm_iiko_location_cache", _noop)


async def test_api_lifespan_does_not_start_jobs_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mute_startup_warmup(monkeypatch)
    fake = _FakeScheduler()
    monkeypatch.setattr(main, "get_scheduler", lambda: fake)
    monkeypatch.setattr(main, "get_settings", lambda: _settings())

    async with main.lifespan(None):
        pass

    assert fake.started == 0


async def test_api_lifespan_starts_jobs_when_explicitly_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Стенду без контейнера-планировщика прежнее поведение возвращает флаг (при одном воркере)."""
    _mute_startup_warmup(monkeypatch)
    fake = _FakeScheduler()
    monkeypatch.setattr(main, "get_scheduler", lambda: fake)
    monkeypatch.setattr(main, "get_settings", lambda: _settings(api_runs_background_jobs=True))

    async with main.lifespan(None):
        pass

    assert (fake.started, fake.stopped) == (1, 1)


def test_scheduler_runner_starts_both_schedulers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Оба планировщика живут в одном процессе: asyncio-шный (банк, курьеры) и джобовый
    (синк накладных, сотрудники, СБИС, ночные начисления)."""
    async_scheduler, jobs_scheduler = _FakeScheduler(), _FakeScheduler()
    monkeypatch.setattr(scheduler_runner, "scheduler", async_scheduler)
    monkeypatch.setattr(scheduler_runner, "get_scheduler", lambda: jobs_scheduler)

    started = scheduler_runner.start_all()

    assert (async_scheduler.started, jobs_scheduler.started) == (1, 1)
    assert started == [async_scheduler, jobs_scheduler]
