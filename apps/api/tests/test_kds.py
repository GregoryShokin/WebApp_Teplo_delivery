from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.api.v1.routes.kds import get_queue
from app.models import KdsOrder, KdsOrderEvent
from app.services.kds import dashboard_service, event_service, sync_service
from app.services.kds.event_service import TapEventInput

MSK = ZoneInfo("Europe/Moscow")


def _tap(order_id: str, event_type: str, when: datetime, cid: str) -> TapEventInput:
    return TapEventInput(
        client_event_id=cid,
        iiko_order_id=order_id,
        event_type=event_type,
        effective_at=when,
    )


async def test_record_events_idempotent_and_fold(async_session_factory) -> None:
    order_id = "TST-FOLD-1"
    t_ready = datetime(2026, 6, 21, 13, 30, tzinfo=UTC)
    t_waiting = datetime(2026, 6, 21, 13, 40, tzinfo=UTC)

    async with async_session_factory() as session:
        r1 = await event_service.record_events(
            session, [_tap(order_id, "ready", t_ready, "c-ready")], actor_user_id=None
        )
        assert [x.status for x in r1] == ["applied"]

        # повтор того же client_event_id — дубликат, не задвоение
        r2 = await event_service.record_events(
            session, [_tap(order_id, "ready", t_ready, "c-ready")], actor_user_id=None
        )
        assert [x.status for x in r2] == ["duplicate"]

        r3 = await event_service.record_events(
            session, [_tap(order_id, "waiting_courier", t_waiting, "c-wait")], actor_user_id=None
        )
        assert [x.status for x in r3] == ["applied"]
        waiting_event_id = uuid.UUID(r3[0].event_id)

    async with async_session_factory() as session:
        order = await session.scalar(select(KdsOrder).where(KdsOrder.iiko_order_id == order_id))
        assert order.ready_at == t_ready
        assert order.waiting_courier_at == t_waiting
        assert order.handed_to_courier_at is None

    # откат «Ждёт курьера» → колонка очищается, ready остаётся
    async with async_session_factory() as session:
        rolled = await event_service.rollback_event(
            session, waiting_event_id, actor_user_id=None
        )
        assert rolled is not None

    async with async_session_factory() as session:
        order = await session.scalar(select(KdsOrder).where(KdsOrder.iiko_order_id == order_id))
        assert order.ready_at == t_ready
        assert order.waiting_courier_at is None
        events = (
            await session.scalars(
                select(KdsOrderEvent).where(KdsOrderEvent.iiko_order_id == order_id)
            )
        ).all()
        # журнал append-only: set ready, set waiting, rollback waiting
        assert len(events) == 3
        assert sorted((e.event_type, e.action) for e in events) == [
            ("ready", "set"),
            ("waiting_courier", "rollback"),
            ("waiting_courier", "set"),
        ]


async def test_offline_buffer_folds_by_effective_at(async_session_factory) -> None:
    order_id = "TST-FOLD-2"
    t_ready = datetime(2026, 6, 21, 13, 30, tzinfo=UTC)
    t_waiting = datetime(2026, 6, 21, 13, 45, tzinfo=UTC)
    # батч с обратным порядком (как при флаше офлайн-буфера) — свёртка по effective_at
    async with async_session_factory() as session:
        results = await event_service.record_events(
            session,
            [
                _tap(order_id, "waiting_courier", t_waiting, "c2-wait"),
                _tap(order_id, "ready", t_ready, "c2-ready"),
            ],
            actor_user_id=None,
        )
        assert all(x.status == "applied" for x in results)

    async with async_session_factory() as session:
        order = await session.scalar(select(KdsOrder).where(KdsOrder.iiko_order_id == order_id))
        assert order.ready_at == t_ready
        assert order.waiting_courier_at == t_waiting


def test_parse_cloud_order_preorder_hot_and_utc() -> None:
    base_order = {
        "number": "501",
        "status": "CookingStarted",
        "whenCreated": "2026-06-21 16:00:00.000",
        "completeBefore": "2026-06-21 16:45:00.000",
        "items": [{"product": {"name": "Сет Филадельфия"}}],
    }
    asap = sync_service.parse_cloud_order("ID-1", base_order, preorder_horizon_minutes=120)
    assert asap.is_preorder is False
    assert asap.has_hot_item is False
    assert asap.order_number == "501"
    # 16:00 MSK -> 13:00 UTC
    assert asap.when_created == datetime(2026, 6, 21, 13, 0, tzinfo=UTC)
    assert asap.is_active is True

    pre_order = {
        **base_order,
        "completeBefore": "2026-06-21 21:00:00.000",  # +5ч
        "items": [{"product": {"name": "2 пиццы за 999"}}],
    }
    pre = sync_service.parse_cloud_order("ID-2", pre_order, preorder_horizon_minutes=120)
    assert pre.is_preorder is True
    assert pre.has_hot_item is True

    closed = sync_service.parse_cloud_order(
        "ID-3", {**base_order, "status": "Closed"}, preorder_horizon_minutes=120
    )
    assert closed.is_active is False


async def test_upsert_out_of_order_guard(async_session_factory) -> None:
    order_id = "TST-OOO-1"
    newer = sync_service.parse_cloud_order(
        order_id,
        {"number": "1", "status": "Delivered", "whenCreated": "2026-06-21 16:00:00.000"},
        preorder_horizon_minutes=120,
    )
    older = sync_service.parse_cloud_order(
        order_id,
        {"number": "1", "status": "CookingStarted", "whenCreated": "2026-06-21 16:00:00.000"},
        preorder_horizon_minutes=120,
    )
    t2 = datetime(2026, 6, 21, 13, 10, tzinfo=UTC)
    t1 = datetime(2026, 6, 21, 13, 5, tzinfo=UTC)

    async with async_session_factory() as session:
        _o, created, applied = await sync_service.upsert_kds_order(session, newer, event_at=t2)
        assert created and applied
        # более старое событие не должно перетереть свежий снимок
        _o, created2, applied2 = await sync_service.upsert_kds_order(session, older, event_at=t1)
        assert created2 is False and applied2 is False
        await session.commit()

    async with async_session_factory() as session:
        order = await session.scalar(select(KdsOrder).where(KdsOrder.iiko_order_id == order_id))
        assert order.status == "Delivered"
        assert order.is_active is False


async def test_dashboard_intervals_segments_and_kpi(async_session_factory) -> None:
    work = date(2026, 6, 21)
    created = datetime(2026, 6, 21, 14, 0, tzinfo=UTC)  # 17:00 MSK (пик)

    async with async_session_factory() as session:
        session.add(
            KdsOrder(
                id=uuid.uuid4(),
                iiko_order_id="DASH-ASAP",
                order_number="900",
                work_date=work,
                status="OnWay",
                when_created=created,
                complete_before=created + timedelta(minutes=50),
                ready_at=created + timedelta(minutes=5),
                waiting_courier_at=created + timedelta(minutes=8),
                handed_to_courier_at=created + timedelta(minutes=12),
                delivered_at=created + timedelta(minutes=30),
                is_preorder=False,
                has_hot_item=True,
                is_active=True,
            )
        )
        session.add(
            KdsOrder(
                id=uuid.uuid4(),
                iiko_order_id="DASH-PRE",
                order_number="901",
                work_date=work,
                status="OnWay",
                when_created=created,
                complete_before=created + timedelta(hours=5),
                ready_at=created + timedelta(minutes=10),
                delivered_at=created + timedelta(hours=6),  # опоздал к слоту
                is_preorder=True,
                has_hot_item=False,
                is_active=True,
            )
        )
        await session.commit()

    async with async_session_factory() as session:
        result = await dashboard_service.compute_dashboard(
            session, date_from=work, date_to=work
        )

    asap_17 = next(
        row for row in result["by_hour"] if row["segment"] == "asap" and row["hour"] == 17
    )
    assert asap_17["queue_cook"]["median"] == 5.0
    assert asap_17["packing"]["median"] == 3.0
    assert asap_17["courier_wait"]["median"] == 4.0
    assert asap_17["road"]["median"] == 18.0

    assert result["kpi"]["median_packing_min"] == 3.0
    # горячий ASAP доставлен в срок → 0% опоздания горячих
    assert result["kpi"]["hot_late_pct"] == 0.0
    assert result["kpi"]["hot_late_n"] == 1

    # пик 16–19 содержит ASAP-заказ
    assert result["peak"]["queue_cook"]["median"] == 5.0

    # предзаказ был готов задолго до слота (готов вовремя), но доставлен с опозданием
    punct = result["preorder_punctuality"]
    assert punct["ready_n"] == 1
    assert punct["ready_on_time_pct"] == 100.0
    assert punct["delivered_on_time_pct"] == 0.0


async def test_queue_shows_cross_midnight_preorder_due_today(async_session_factory) -> None:
    today = datetime.now(MSK).date()
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)
    due_today = datetime.combine(today, time(12, 0), tzinfo=MSK).astimezone(UTC)
    due_yesterday = datetime.combine(yesterday, time(20, 0), tzinfo=MSK).astimezone(UTC)
    due_tomorrow = datetime.combine(tomorrow, time(12, 0), tzinfo=MSK).astimezone(UTC)

    async with async_session_factory() as session:
        # кросс-полуночный предзаказ: создан вчера, срок сегодня → ДОЛЖЕН быть на доске
        session.add(
            KdsOrder(
                id=uuid.uuid4(), iiko_order_id="Q-PRE-TODAY", order_number="1",
                work_date=yesterday, status="CookingStarted", complete_before=due_today,
                is_preorder=True, is_active=True,
            )
        )
        # ASAP сегодня без явного предзаказа-срока, но со слотом сегодня → на доске
        session.add(
            KdsOrder(
                id=uuid.uuid4(), iiko_order_id="Q-ASAP", order_number="2",
                work_date=today, status="CookingCompleted", complete_before=due_today,
                is_preorder=False, is_active=True,
            )
        )
        # зависший вчерашний (срок вчера) → НЕ на доске
        session.add(
            KdsOrder(
                id=uuid.uuid4(), iiko_order_id="Q-STALE", order_number="3",
                work_date=yesterday, status="OnWay", complete_before=due_yesterday,
                is_preorder=False, is_active=True,
            )
        )
        # дальний предзаказ (срок завтра) → не засоряет сегодняшнюю доску
        session.add(
            KdsOrder(
                id=uuid.uuid4(), iiko_order_id="Q-FAR", order_number="4",
                work_date=today, status="CookingStarted", complete_before=due_tomorrow,
                is_preorder=True, is_active=True,
            )
        )
        await session.commit()

    async with async_session_factory() as session:
        response = await get_queue(session)
    ids = {order.iiko_order_id for order in response.orders}
    assert "Q-PRE-TODAY" in ids
    assert "Q-ASAP" in ids
    assert "Q-STALE" not in ids
    assert "Q-FAR" not in ids


async def test_poll_stale_sweep_spares_seen_orders(async_session_factory) -> None:
    today = datetime.now(MSK).date()
    yesterday = today - timedelta(days=1)
    async with async_session_factory() as session:
        # активный вчерашний заказ, который поллинг СНОВА подтвердит (in seen) — не гасим
        session.add(
            KdsOrder(
                id=uuid.uuid4(), iiko_order_id="SWEEP-SEEN", order_number="9",
                work_date=yesterday, status="OnWay", is_active=True,
            )
        )
        # активный вчерашний заказ, которого нет в выдаче iiko — гасим как зависший
        session.add(
            KdsOrder(
                id=uuid.uuid4(), iiko_order_id="SWEEP-GONE", order_number="10",
                work_date=yesterday, status="OnWay", is_active=True,
            )
        )
        await session.commit()
        seen = {"SWEEP-SEEN"}
        from sqlalchemy import update

        stale_query = update(KdsOrder).where(
            KdsOrder.is_active.is_(True), KdsOrder.work_date < today
        )
        if seen:
            stale_query = stale_query.where(KdsOrder.iiko_order_id.notin_(seen))
        await session.execute(stale_query.values(is_active=False))
        await session.commit()

    async with async_session_factory() as session:
        seen_order = await session.scalar(
            select(KdsOrder).where(KdsOrder.iiko_order_id == "SWEEP-SEEN")
        )
        gone_order = await session.scalar(
            select(KdsOrder).where(KdsOrder.iiko_order_id == "SWEEP-GONE")
        )
        assert seen_order.is_active is True
        assert gone_order.is_active is False


def test_parse_webhook_event_time_variants() -> None:
    assert sync_service.parse_webhook_event_time("2026-06-21 16:00:00.000") == datetime(
        2026, 6, 21, 13, 0, tzinfo=UTC
    )
    # epoch миллисекунды
    epoch_ms = 1_700_000_000_000
    assert sync_service.parse_webhook_event_time(epoch_ms) == datetime.fromtimestamp(
        epoch_ms / 1000, tz=UTC
    )
    assert sync_service.parse_webhook_event_time(None) is None
