"""Сервисные тесты контура временных внештатников (пул iiko-плейсхолдеров).

Гоняются на одноразовой БД (TEPLO_TEST_DATABASE_URL). iiko мокается.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.api.deps import CurrentActor
from app.api.v1.routes import employees as employees_routes
from app.models import (
    AttendanceEntry,
    Employee,
    FreelancerAttendanceCase,
    FreelancerTempCard,
    PayrollPeriod,
)
from app.schemas.employees import EmployeeRead
from app.services import new_payment as new_payment_service
from app.services import shift_schedule_service
from app.services.freelancer import attendance as fa
from app.services.freelancer import pool
from app.services.iiko_sync import IikoEmployeeCreateResult, IikoEmployeeUpdateResult
from app.services.payroll_calculator import (
    base_shift_pay,
    calculate_payroll_lines_from_inputs,
)

pytestmark = pytest.mark.usefixtures("postgres_available")

TODAY = date(2026, 7, 7)


# --------------------------------------------------------------------------- #
# iiko-моки                                                                    #
# --------------------------------------------------------------------------- #
class FakeIiko:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.pin_updates: list[dict[str, Any]] = []
        self._counter = 0

    async def create(self, session: Any, *, full_name: str, role_id: str, pin_code: str):
        self._counter += 1
        iiko_id = f"iiko-ph-{self._counter}-{uuid.uuid4().hex[:6]}"
        self.created.append({"full_name": full_name, "pin_code": pin_code, "iiko_id": iiko_id})
        return IikoEmployeeCreateResult(
            iiko_id=iiko_id,
            full_name=full_name,
            position="Повар",
            role_id=role_id,
            role_code=None,
            is_target_position=True,
            hire_date=None,
        )

    async def update(
        self,
        session: Any,
        *,
        iiko_id: str,
        full_name: str | None = None,
        position: str | None = None,
        pin_code: str | None = None,
    ):
        self.pin_updates.append({"iiko_id": iiko_id, "pin_code": pin_code})
        return IikoEmployeeUpdateResult(
            iiko_id=iiko_id,
            full_name=full_name,
            position=position,
            role_id=None,
            role_code=None,
            hire_date=None,
        )


@pytest.fixture()
def fake_iiko(monkeypatch: pytest.MonkeyPatch) -> FakeIiko:
    fake = FakeIiko()
    monkeypatch.setattr(pool, "create_iiko_employee", fake.create)
    monkeypatch.setattr(pool, "update_iiko_employee", fake.update)
    return fake


# --------------------------------------------------------------------------- #
# Фабрики строк                                                                #
# --------------------------------------------------------------------------- #
def _placeholder(index: int, iiko_id: str) -> Employee:
    return Employee(
        id=uuid.uuid4(),
        full_name=pool.placeholder_display_name(index),
        iiko_id=iiko_id,
        position="Повар",
        is_freelancer_placeholder=True,
        status="active",
        pin_assumed_from_iiko=True,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _real_employee(name: str, *, status: str = "active", placeholder: bool = False) -> Employee:
    return Employee(
        id=uuid.uuid4(),
        full_name=name,
        iiko_id=f"iiko-{uuid.uuid4().hex[:10]}",
        position="Повар",
        category=None,
        status=status,
        is_freelancer_placeholder=placeholder,
        pin_hash="hashed-pin",
        pin_set_at=datetime(2026, 1, 1, tzinfo=UTC),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _freelancer(name: str, rate: Decimal, *, status: str = "active") -> Employee:
    emp = _real_employee(name, status=status)
    emp.iiko_id = pool.synthetic_freelancer_iiko_id()
    emp.category = "freelancer"
    emp.is_freelancer_temp = True
    emp.freelancer_shift_rate = rate
    return emp


# --------------------------------------------------------------------------- #
# 1. Аллокация пула: создание №1→№2→№3, переиспользование по свободному периоду #
# --------------------------------------------------------------------------- #
async def test_pool_allocation_creates_sequential_placeholders(
    async_session_factory, fake_iiko: FakeIiko
) -> None:
    async with async_session_factory() as session:
        # Плейсхолдеров ещё нет — первая аллокация создаёт «Внештат №1».
        alloc1 = await pool.allocate_placeholder(
            session,
            iiko_position="Повар",
            iiko_role_id="role-cook",
            period_from=TODAY,
            period_to=TODAY + timedelta(days=5),
            pin_code="1111",
        )
        assert alloc1.created_new is True
        assert alloc1.placeholder.full_name == "Внештат №1"
        assert alloc1.placeholder.is_freelancer_placeholder is True
        emp1 = _freelancer("Первый Внештат", Decimal("3600"))
        session.add(emp1)
        await session.flush()
        await pool.create_temp_card(
            session,
            employee_id=emp1.id,
            placeholder_id=alloc1.placeholder.id,
            period_from=TODAY,
            period_to=TODAY + timedelta(days=5),
            created_by=None,
        )

        # Пересекающийся период — №1 занят, создаётся «Внештат №2».
        alloc2 = await pool.allocate_placeholder(
            session,
            iiko_position="Повар",
            iiko_role_id="role-cook",
            period_from=TODAY + timedelta(days=2),
            period_to=TODAY + timedelta(days=8),
            pin_code="2222",
        )
        assert alloc2.created_new is True
        assert alloc2.placeholder.full_name == "Внештат №2"
        emp2 = _freelancer("Второй Внештат", Decimal("4200"))
        session.add(emp2)
        await session.flush()
        await pool.create_temp_card(
            session,
            employee_id=emp2.id,
            placeholder_id=alloc2.placeholder.id,
            period_from=TODAY + timedelta(days=2),
            period_to=TODAY + timedelta(days=8),
            created_by=None,
        )

        # Оба заняты на пересекающийся период — создаётся «Внештат №3».
        alloc3 = await pool.allocate_placeholder(
            session,
            iiko_position="Повар",
            iiko_role_id="role-cook",
            period_from=TODAY + timedelta(days=3),
            period_to=TODAY + timedelta(days=9),
            pin_code="3333",
        )
        assert alloc3.created_new is True
        assert alloc3.placeholder.full_name == "Внештат №3"

        # Три iiko-карты созданы через мок.
        assert len(fake_iiko.created) == 3
        assert [c["full_name"] for c in fake_iiko.created] == [
            "Внештат №1",
            "Внештат №2",
            "Внештат №3",
        ]


async def test_pool_reuses_free_placeholder_for_non_overlapping_period(
    async_session_factory, fake_iiko: FakeIiko
) -> None:
    async with async_session_factory() as session:
        placeholder = _placeholder(1, "iiko-ph-1")
        session.add(placeholder)
        emp = _freelancer("Ранний Внештат", Decimal("3600"))
        session.add(emp)
        await session.flush()
        await pool.create_temp_card(
            session,
            employee_id=emp.id,
            placeholder_id=placeholder.id,
            period_from=TODAY,
            period_to=TODAY + timedelta(days=5),
            created_by=None,
        )

        # Непересекающийся более поздний период — №1 свободен, переиспользуем.
        alloc = await pool.allocate_placeholder(
            session,
            iiko_position="Повар",
            iiko_role_id="role-cook",
            period_from=TODAY + timedelta(days=10),
            period_to=TODAY + timedelta(days=15),
            pin_code="9999",
        )
        assert alloc.created_new is False
        assert alloc.placeholder.id == placeholder.id
        # Новой iiko-карты не создаём, ПИН плейсхолдера обновлён.
        assert fake_iiko.created == []
        assert fake_iiko.pin_updates[-1] == {"iiko_id": "iiko-ph-1", "pin_code": "9999"}


# --------------------------------------------------------------------------- #
# 1b. ПИН и плейсхолдер в карточке: хранение + выдача через EmployeeRead        #
# --------------------------------------------------------------------------- #
async def test_create_temp_card_persists_pin_code(async_session_factory) -> None:
    async with async_session_factory() as session:
        placeholder = _placeholder(1, "iiko-ph-1")
        session.add(placeholder)
        emp = _freelancer("Пин Внештат", Decimal("3600"))
        session.add(emp)
        await session.flush()
        card = await pool.create_temp_card(
            session,
            employee_id=emp.id,
            placeholder_id=placeholder.id,
            period_from=TODAY,
            period_to=TODAY + timedelta(days=5),
            created_by=None,
            pin_code="4271",
        )
        assert card.pin_code == "4271"

        # ПИН реально записан в БД (перечитываем свежим select).
        reloaded = await session.scalar(
            select(FreelancerTempCard).where(FreelancerTempCard.id == card.id)
        )
        assert reloaded is not None
        assert reloaded.pin_code == "4271"


async def test_employee_read_exposes_placeholder_name_and_pin(async_session_factory) -> None:
    """Карточка внештатника в EmployeeRead отдаёт имя плейсхолдера и открытый ПИН.

    Грузим сотрудника ровно как list_employees (eager-цепочка role_assignments +
    freelancer_card -> placeholder) и проверяем, что сериализация НЕ требует
    async lazy-load — иначе placeholder_name/assignments упали бы MissingGreenlet.
    """
    from sqlalchemy.orm import selectinload

    async with async_session_factory() as session:
        placeholder = _placeholder(1, "iiko-ph-1")
        session.add(placeholder)
        emp = _freelancer("Видимый Внештат", Decimal("3600"))
        session.add(emp)
        await session.flush()
        await pool.create_temp_card(
            session,
            employee_id=emp.id,
            placeholder_id=placeholder.id,
            period_from=TODAY,
            period_to=TODAY + timedelta(days=5),
            created_by=None,
            pin_code="8899",
        )
        # Сбрасываем идентити-мап, чтобы select реально прогрузил relationship-цепочку.
        session.expunge_all()

        loaded = await session.scalar(
            select(Employee)
            .where(Employee.id == emp.id)
            .options(
                selectinload(Employee.role_assignments),
                selectinload(Employee.freelancer_card).selectinload(FreelancerTempCard.placeholder),
            )
        )
        assert loaded is not None

        payload = EmployeeRead.model_validate(loaded)
        assert payload.freelancer_card is not None
        assert payload.freelancer_card.placeholder_name == "Внештат №1"
        assert payload.freelancer_card.pin_code == "8899"
        assert payload.freelancer_card.placeholder_employee_id == placeholder.id


async def test_pin_gated_by_permission_in_get_employee(async_session_factory) -> None:
    """ПИН внештатника отдаётся только с правом staff.freelancer_pin.read.

    Без права — pin_code=None, но имя плейсхолдера остаётся видимым. Редакция не
    должна затирать ПИН в БД (карточка detach'ится перед занулением).
    """
    async with async_session_factory() as session:
        placeholder = _placeholder(1, "iiko-ph-1")
        session.add(placeholder)
        emp = _freelancer("Гейт Внештат", Decimal("3600"))
        session.add(emp)
        await session.flush()
        await pool.create_temp_card(
            session,
            employee_id=emp.id,
            placeholder_id=placeholder.id,
            period_from=TODAY,
            period_to=TODAY + timedelta(days=5),
            created_by=None,
            pin_code="8899",
        )
        await session.commit()

        # Актор БЕЗ права видеть ПИН (owner-роль даёт доступ к Штату, но право на
        # ПИН отсутствует в наборе permissions).
        actor_without = CurrentActor(
            roles=frozenset({"owner"}),
            user_id=uuid.uuid4(),
            permissions=frozenset({"staff.cooks.read"}),
        )
        loaded_without = await employees_routes._get_employee_or_404(
            session,
            emp.id,
            include_assignments=True,
            actor=actor_without,
        )
        payload_without = EmployeeRead.model_validate(loaded_without)
        assert payload_without.freelancer_card is not None
        assert payload_without.freelancer_card.pin_code is None
        assert payload_without.freelancer_card.placeholder_name == "Внештат №1"

        # ПИН в БД не затёрт редакцией.
        session.expunge_all()
        db_card = await session.scalar(
            select(FreelancerTempCard).where(FreelancerTempCard.employee_id == emp.id)
        )
        assert db_card is not None
        assert db_card.pin_code == "8899"

        # Актор С правом видит ПИН.
        actor_with = CurrentActor(
            roles=frozenset({"owner"}),
            user_id=uuid.uuid4(),
            permissions=frozenset({"staff.cooks.read", "staff.freelancer_pin.read"}),
        )
        loaded_with = await employees_routes._get_employee_or_404(
            session,
            emp.id,
            include_assignments=True,
            actor=actor_with,
        )
        payload_with = EmployeeRead.model_validate(loaded_with)
        assert payload_with.freelancer_card is not None
        assert payload_with.freelancer_card.pin_code == "8899"
        assert payload_with.freelancer_card.placeholder_name == "Внештат №1"


# --------------------------------------------------------------------------- #
# 1c. Плейсхолдер невидим в UI-списках/пикерах (сквозная зачистка)             #
# --------------------------------------------------------------------------- #
async def test_placeholder_excluded_from_schedule_roster(async_session_factory) -> None:
    """График сотрудников не содержит плейсхолдер «Внештат №N», но содержит
    реального внештатника (is_freelancer_temp) под его именем."""
    async with async_session_factory() as session:
        placeholder = _placeholder(1, "iiko-ph-1")  # position='Повар', активен
        real = _freelancer("Реальный Внештат", Decimal("3600"))  # Повар, не placeholder
        session.add_all([placeholder, real])
        await session.flush()

        roster = await shift_schedule_service.list_employees_roster(session)
        ids = {row["id"] for row in roster}
        names = {row["full_name"] for row in roster}
        assert placeholder.id not in ids
        assert "Внештат №1" not in names
        # Реальный внештатник (должность Повар в графике) — виден.
        assert real.id in ids
        assert "Реальный Внештат" in names


async def test_placeholder_excluded_from_new_payment_picker(async_session_factory) -> None:
    """Пикер сотрудников в окне платежа не показывает плейсхолдер, показывает
    реального внештатника."""
    async with async_session_factory() as session:
        placeholder = _placeholder(1, "iiko-ph-1")
        real = _freelancer("Оплатимый Внештат", Decimal("3600"))
        session.add_all([placeholder, real])
        await session.flush()

        rows = await new_payment_service.list_new_payment_employees(session)
        ids = {row["id"] for row in rows}
        assert placeholder.id not in ids
        assert real.id in ids


async def test_placeholder_gets_no_payroll_line(async_session_factory) -> None:
    """Плейсхолдер не получает строку в расчёте ЗП: его явка перекладывается на
    реального внештатника ещё при загрузке, поэтому в набор расчёта попадает
    только реальный внештатник (а плейсхолдер — никогда)."""
    period = PayrollPeriod(
        id=uuid.uuid4(),
        period_type="week",
        start_date=date(2026, 5, 19),
        end_date=date(2026, 5, 25),
        payroll_date=date(2026, 5, 26),
        status="open",
    )
    placeholder = _placeholder(1, "iiko-ph-1")
    real = _freelancer("Расчётный Внештат", Decimal("3600"))
    real.hire_date = date(2024, 1, 1)
    real.tenure_started_at = date(2024, 1, 1)
    # Явка уже переложена на реального внештатника (employee_id = real.id).
    entry = AttendanceEntry(
        id=uuid.uuid4(),
        employee_id=real.id,
        period_id=period.id,
        work_date=date(2026, 5, 20),
        started_at=datetime(2026, 5, 20, 8, tzinfo=UTC),
        ended_at=datetime(2026, 5, 20, 20, tzinfo=UTC),
        minutes_worked=720,
        role="Пиццерист",
        station=None,
        source="manual",
        quality_status="ok",
        notes=None,
    )
    result = calculate_payroll_lines_from_inputs(
        period, uuid.uuid4(), [entry], {real.id: real}, _payroll_settings()
    )
    assert result.blocking_issues == []
    line_employee_ids = {line.employee_id for line in result.lines}
    assert real.id in line_employee_ids
    assert placeholder.id not in line_employee_ids


# --------------------------------------------------------------------------- #
# 2. Обновление/сброс ПИН                                                       #
# --------------------------------------------------------------------------- #
async def test_reset_placeholder_pin_on_archive(async_session_factory, fake_iiko: FakeIiko) -> None:
    async with async_session_factory() as session:
        placeholder = _placeholder(1, "iiko-ph-1")
        session.add(placeholder)
        emp = _freelancer("Истёкший Внештат", Decimal("3600"))
        session.add(emp)
        await session.flush()
        card = await pool.create_temp_card(
            session,
            employee_id=emp.id,
            placeholder_id=placeholder.id,
            period_from=TODAY - timedelta(days=10),
            period_to=TODAY - timedelta(days=1),
            created_by=None,
        )

        await pool.archive_card(session, card, now=datetime(2026, 7, 7, tzinfo=UTC))
        await session.flush()

        assert card.archived_at is not None
        assert emp.status == "inactive"
        assert emp.fire_date == TODAY - timedelta(days=1)
        # ПИН плейсхолдера сброшен (пустой pinCode).
        assert fake_iiko.pin_updates[-1] == {"iiko_id": "iiko-ph-1", "pin_code": ""}


# --------------------------------------------------------------------------- #
# 3. Маппинг явок по дате                                                       #
# --------------------------------------------------------------------------- #
async def test_attendance_remap_inside_period(async_session_factory) -> None:
    async with async_session_factory() as session:
        placeholder = _placeholder(1, "iiko-ph-1")
        session.add(placeholder)
        emp = _freelancer("Внутри Периода", Decimal("3600"))
        session.add(emp)
        await session.flush()
        await pool.create_temp_card(
            session,
            employee_id=emp.id,
            placeholder_id=placeholder.id,
            period_from=TODAY,
            period_to=TODAY + timedelta(days=5),
            created_by=None,
        )
        index = await fa.load_binding_index(session)
        remapped = await fa.remap_attendance_employee(
            session, placeholder, TODAY + timedelta(days=2), index=index
        )
        assert remapped is not None
        assert remapped.id == emp.id


async def test_attendance_outside_period_creates_case(async_session_factory) -> None:
    async with async_session_factory() as session:
        placeholder = _placeholder(1, "iiko-ph-1")
        session.add(placeholder)
        emp = _freelancer("Внутри Периода", Decimal("3600"))
        session.add(emp)
        await session.flush()
        await pool.create_temp_card(
            session,
            employee_id=emp.id,
            placeholder_id=placeholder.id,
            period_from=TODAY,
            period_to=TODAY + timedelta(days=5),
            created_by=None,
        )
        index = await fa.load_binding_index(session)
        out_date = TODAY + timedelta(days=20)
        remapped = await fa.remap_attendance_employee(
            session, placeholder, out_date, index=index, minutes=600
        )
        assert remapped is None
        case = await session.scalar(
            select(FreelancerAttendanceCase).where(
                FreelancerAttendanceCase.placeholder_employee_id == placeholder.id,
                FreelancerAttendanceCase.work_date == out_date,
            )
        )
        assert case is not None
        assert case.status == "open"
        assert case.minutes == 600


async def test_attendance_maps_to_archived_card_inside_period(async_session_factory) -> None:
    async with async_session_factory() as session:
        placeholder = _placeholder(1, "iiko-ph-1")
        session.add(placeholder)
        emp = _freelancer("Архивный Внештат", Decimal("3600"), status="inactive")
        session.add(emp)
        await session.flush()
        card = await pool.create_temp_card(
            session,
            employee_id=emp.id,
            placeholder_id=placeholder.id,
            period_from=TODAY - timedelta(days=10),
            period_to=TODAY - timedelta(days=3),
            created_by=None,
        )
        card.archived_at = datetime(2026, 7, 7, tzinfo=UTC)
        await session.flush()

        # Явка приходит после архивации, но датой ВНУТРИ периода — маппится на карточку.
        index = await fa.load_binding_index(session)
        remapped = await fa.remap_attendance_employee(
            session, placeholder, TODAY - timedelta(days=5), index=index
        )
        assert remapped is not None
        assert remapped.id == emp.id


async def test_creating_card_resolves_open_case(async_session_factory) -> None:
    async with async_session_factory() as session:
        placeholder = _placeholder(1, "iiko-ph-1")
        session.add(placeholder)
        await session.flush()
        work_date = TODAY + timedelta(days=1)
        await fa.upsert_attendance_case(
            session, placeholder_id=placeholder.id, work_date=work_date, minutes=480
        )
        emp = _freelancer("Поздняя Привязка", Decimal("3600"))
        session.add(emp)
        await session.flush()
        card = await pool.create_temp_card(
            session,
            employee_id=emp.id,
            placeholder_id=placeholder.id,
            period_from=TODAY,
            period_to=TODAY + timedelta(days=5),
            created_by=None,
        )
        resolved = await fa.resolve_cases_for_card(session, card)
        assert resolved == 1
        case = await session.scalar(
            select(FreelancerAttendanceCase).where(
                FreelancerAttendanceCase.placeholder_employee_id == placeholder.id
            )
        )
        assert case.status == "resolved"
        assert case.resolved_employee_id == emp.id


# --------------------------------------------------------------------------- #
# 4. Автоархивация по расписанию                                               #
# --------------------------------------------------------------------------- #
async def test_archive_expired_cards_job(async_session_factory, fake_iiko: FakeIiko) -> None:
    async with async_session_factory() as session:
        placeholder = _placeholder(1, "iiko-ph-1")
        session.add(placeholder)
        expired = _freelancer("Истёкший", Decimal("3600"))
        active = _freelancer("Активный", Decimal("3600"))
        session.add_all([expired, active])
        await session.flush()
        await pool.create_temp_card(
            session,
            employee_id=expired.id,
            placeholder_id=placeholder.id,
            period_from=TODAY - timedelta(days=10),
            period_to=TODAY - timedelta(days=1),
            created_by=None,
        )
        placeholder2 = _placeholder(2, "iiko-ph-2")
        session.add(placeholder2)
        await session.flush()
        await pool.create_temp_card(
            session,
            employee_id=active.id,
            placeholder_id=placeholder2.id,
            period_from=TODAY,
            period_to=TODAY + timedelta(days=5),
            created_by=None,
        )

        archived = await pool.archive_expired_cards(
            session, today=TODAY, now=datetime(2026, 7, 7, tzinfo=UTC)
        )
        await session.flush()
        assert archived == 1
        await session.refresh(expired)
        await session.refresh(active)
        assert expired.status == "inactive"
        assert active.status == "active"


# --------------------------------------------------------------------------- #
# 5. Лимит 30 дней                                                              #
# --------------------------------------------------------------------------- #
def test_validate_period_rejects_over_30_days() -> None:
    with pytest.raises(pool.FreelancerError):
        pool.validate_period(TODAY, TODAY + timedelta(days=30))  # 31 день


def test_validate_period_accepts_exactly_30_days() -> None:
    pool.validate_period(TODAY, TODAY + timedelta(days=29))  # 30 дней включительно


def test_validate_period_rejects_reversed() -> None:
    with pytest.raises(pool.FreelancerError):
        pool.validate_period(TODAY, TODAY - timedelta(days=1))


async def test_update_card_period_rejects_extension_beyond_30_days(
    async_session_factory,
) -> None:
    async with async_session_factory() as session:
        placeholder = _placeholder(1, "iiko-ph-1")
        session.add(placeholder)
        emp = _freelancer("Продлеваемый", Decimal("3600"))
        session.add(emp)
        await session.flush()
        card = await pool.create_temp_card(
            session,
            employee_id=emp.id,
            placeholder_id=placeholder.id,
            period_from=TODAY,
            period_to=TODAY + timedelta(days=10),
            created_by=None,
        )
        with pytest.raises(pool.FreelancerError):
            await pool.update_card_period(session, card, period_to=TODAY + timedelta(days=40))
        # В пределах 30 дней от начала — ок.
        updated = await pool.update_card_period(session, card, period_to=TODAY + timedelta(days=20))
        assert updated.period_to == TODAY + timedelta(days=20)


# --------------------------------------------------------------------------- #
# 6. Валидация уникальности имён                                               #
# --------------------------------------------------------------------------- #
async def test_active_duplicate_name_blocks(async_session_factory) -> None:
    async with async_session_factory() as session:
        session.add(_real_employee("Иван Иванов"))
        await session.flush()
        with pytest.raises(pool.DuplicateActiveNameError):
            await pool.ensure_unique_active_name(session, "Иван  Иванов")  # лишний пробел


async def test_archived_duplicate_name_does_not_block(async_session_factory) -> None:
    async with async_session_factory() as session:
        session.add(_real_employee("Пётр Петров", status="inactive"))
        await session.flush()
        # Архивный тёзка не блокирует.
        await pool.ensure_unique_active_name(session, "Пётр Петров")


async def test_placeholder_name_does_not_block(async_session_factory) -> None:
    async with async_session_factory() as session:
        session.add(_real_employee("Внештат №1", placeholder=True))
        await session.flush()
        await pool.ensure_unique_active_name(session, "Внештат №1")


# --------------------------------------------------------------------------- #
# 7. Начисление: ставка/12 × часы, без доплат пт/сб и фонда                     #
# --------------------------------------------------------------------------- #
def _payroll_settings() -> dict[str, Any]:
    return {
        "payroll.role_category_rates": {
            "Пиццерист": {"category_2": 2200, "intern": 2000},
            "Сушист": {"category_2": 2400},
        },
        "payroll.category_rules": {
            "2": {"coeff": 7.5, "deposit_target": 15000, "deposit_withholding": 1000},
            "4": {"coeff": 0, "deposit_target": 7000, "deposit_withholding": 1000},
        },
        "payroll.revenue_percent_tiers": [
            {"from": 50000, "rate": 0.035},
            {"from": 140000, "rate": 0.045},
        ],
        "payroll.allowances": {"senior": 500, "deputy_senior": 300},
        "payroll.weekday_premium": {"amount": 200, "threshold_hours": 8},
        "payroll.fund_rates_by_tenure": [
            {"min_years": 0.5, "rate": 0.05},
            {"min_years": 1.0, "rate": 0.10},
            {"min_years": 1.5, "rate": 0.15},
        ],
        "payroll.mock_daily_revenue": {},
        "payroll.deposit_auto_withholding_enabled": False,
        "payroll.deposit_fund_payment_date": "01-15",
    }


def test_base_shift_pay_freelancer_uses_card_rate() -> None:
    emp = _freelancer("Формула Внештат", Decimal("3600"))
    # 6 часов явки = 360 минут. Ставка/12 × 6 = 3600/12 × 6 = 1800.
    pay = base_shift_pay({}, "pizza", "freelancer", emp, 360)
    assert pay == Decimal("1800")
    # Полная смена 12ч = ставка целиком.
    assert base_shift_pay({}, "pizza", "freelancer", emp, 720) == Decimal("3600")


def test_freelancer_friday_shift_has_no_weekday_premium_or_fund() -> None:
    friday = date(2026, 5, 22)
    assert friday.weekday() == 4
    period = PayrollPeriod(
        id=uuid.uuid4(),
        period_type="week",
        start_date=date(2026, 5, 19),
        end_date=date(2026, 5, 25),
        payroll_date=date(2026, 5, 26),
        status="open",
    )
    emp = Employee(
        id=uuid.uuid4(),
        full_name="Пятничный Внештат",
        iiko_id=pool.synthetic_freelancer_iiko_id(),
        position="Повар",
        category="freelancer",
        status="active",
        is_freelancer_temp=True,
        freelancer_shift_rate=Decimal("3600"),
        hire_date=date(2024, 1, 1),
        tenure_started_at=date(2024, 1, 1),
        pin_hash="hashed-pin",
        pin_set_at=datetime(2026, 1, 1, tzinfo=UTC),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    entry = AttendanceEntry(
        id=uuid.uuid4(),
        employee_id=emp.id,
        period_id=period.id,
        work_date=friday,
        started_at=datetime(2026, 5, 22, 8, tzinfo=UTC),
        ended_at=datetime(2026, 5, 22, 20, tzinfo=UTC),
        minutes_worked=720,  # 12ч
        role="Пиццерист",
        station=None,
        source="manual",
        quality_status="ok",
        notes=None,
    )
    result = calculate_payroll_lines_from_inputs(
        period, uuid.uuid4(), [entry], {emp.id: emp}, _payroll_settings()
    )
    assert result.blocking_issues == []
    line = result.lines[0]
    # Полная смена по ставке 3600, без +200 пт/сб и без фонда.
    assert line.base_pay == Decimal("3600")
    assert line.fund_accrual == Decimal("0")
    assert line.percent_pay == Decimal("0")


# --------------------------------------------------------------------------- #
# 8. Миграционный шаг: поглощение старых карточек                              #
# --------------------------------------------------------------------------- #
async def test_absorb_legacy_marks_old_cards_as_placeholders(async_session_factory) -> None:
    async with async_session_factory() as session:
        legacy1 = _real_employee("Внештат №1")
        legacy2 = _real_employee("Внештат №2")
        regular = _real_employee("Обычный Повар")
        session.add_all([legacy1, legacy2, regular])
        await session.flush()

        # Логика скрипта: пометить карточки «Внештат №N» плейсхолдерами.
        candidates = (
            await session.scalars(
                select(Employee).where(
                    Employee.is_freelancer_placeholder.is_(False),
                    Employee.full_name.ilike("Внештат %"),
                )
            )
        ).all()
        marked = 0
        for emp in candidates:
            if pool.parse_placeholder_index(emp.full_name) is not None:
                emp.is_freelancer_placeholder = True
                marked += 1
        await session.flush()

        assert marked == 2
        await session.refresh(legacy1)
        await session.refresh(regular)
        assert legacy1.is_freelancer_placeholder is True
        assert regular.is_freelancer_placeholder is False
