"""Чистая раскладка пула ЗП (``allocate_pool``): меньшие первыми, граничный сплит, overflow.

Money-path-алгоритм тестируется изолированно, без БД.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from app.services.payroll_reserves import (
    EmployeeShare,
    PoolAllocation,
    allocate_pool,
    allocated_total,
)

# Детерминированные id, отсортированные лексикографически (для проверки тай-брейка).
E1 = uuid.UUID("00000000-0000-0000-0000-000000000001")
E2 = uuid.UUID("00000000-0000-0000-0000-000000000002")
E3 = uuid.UUID("00000000-0000-0000-0000-000000000003")
E4 = uuid.UUID("00000000-0000-0000-0000-000000000004")


def _shares(*pairs: tuple[uuid.UUID, str]) -> list[EmployeeShare]:
    return [EmployeeShare(eid, Decimal(rem)) for eid, rem in pairs]


def _as_dict(allocs: list[PoolAllocation]) -> dict[uuid.UUID, Decimal]:
    return {a.employee_id: a.amount for a in allocs}


def test_pool_covers_everyone_fully() -> None:
    shares = _shares((E1, "1000"), (E2, "2000"), (E3, "3000"))
    allocs = allocate_pool(Decimal("6000"), shares)
    assert _as_dict(allocs) == {
        E1: Decimal("1000.00"),
        E2: Decimal("2000.00"),
        E3: Decimal("3000.00"),
    }
    assert allocated_total(allocs) == Decimal("6000.00")


def test_smaller_first_boundary_gets_split_rest_zero() -> None:
    # Пул 4500: гасит 1000 и 2000 полностью, третьему (3000) остаётся 1500 — сплит.
    shares = _shares((E3, "3000"), (E1, "1000"), (E2, "2000"))
    allocs = allocate_pool(Decimal("4500"), shares)
    d = _as_dict(allocs)
    assert d[E1] == Decimal("1000.00")
    assert d[E2] == Decimal("2000.00")
    assert d[E3] == Decimal("1500.00")  # граничный: частично
    assert allocated_total(allocs) == Decimal("4500.00")


def test_boundary_after_exhaustion_gets_nothing() -> None:
    # Пул 3000: ровно на двоих меньших; третий не попадает вовсе (0, отсутствует в списке).
    shares = _shares((E1, "1000"), (E2, "2000"), (E3, "3000"))
    allocs = allocate_pool(Decimal("3000"), shares)
    d = _as_dict(allocs)
    assert d == {E1: Decimal("1000.00"), E2: Decimal("2000.00")}
    assert E3 not in d


def test_boundary_override_absorbs_remainder() -> None:
    # Пользователь назначает граничным E1 (маленькая ЗП) — он уходит в конец очереди:
    # сначала полностью гасим E2(2000) и E3(3000)=5000, E1 забирает остаток 1000 из 6000.
    shares = _shares((E1, "1500"), (E2, "2000"), (E3, "3000"))
    allocs = allocate_pool(Decimal("6000"), shares, boundary_override=E1)
    d = _as_dict(allocs)
    assert d[E2] == Decimal("2000.00")
    assert d[E3] == Decimal("3000.00")
    assert d[E1] == Decimal("1000.00")  # граничный получил остаток (сплит), хотя ЗП меньше


def test_boundary_override_gets_zero_when_pool_too_small() -> None:
    # Пул мал: другие (E2,E3) съедают весь пул, назначенный граничным E1 получает 0.
    shares = _shares((E1, "1000"), (E2, "2000"), (E3, "3000"))
    allocs = allocate_pool(Decimal("4000"), shares, boundary_override=E1)
    d = _as_dict(allocs)
    assert d[E2] == Decimal("2000.00")
    assert d[E3] == Decimal("2000.00")  # E3 — фактический граничный (частично)
    assert E1 not in d  # назначенный граничным не дошёл — уйдёт в другой пул/долг


def test_selected_subset_only() -> None:
    # Режим «выбрал конкретных»: раскладываем только по E1 и E3.
    shares = _shares((E1, "1000"), (E2, "2000"), (E3, "3000"))
    allocs = allocate_pool(Decimal("10000"), shares, selected_ids={E1, E3})
    d = _as_dict(allocs)
    assert d == {E1: Decimal("1000.00"), E3: Decimal("3000.00")}
    assert E2 not in d


def test_selected_subset_with_boundary_split() -> None:
    shares = _shares((E1, "1000"), (E2, "2000"), (E3, "3000"), (E4, "4000"))
    # Выбраны E2,E3,E4 (Σ=9000); пул 5000 → E2 полн., E3 частично 3000, E4=0.
    allocs = allocate_pool(Decimal("5000"), shares, selected_ids={E2, E3, E4})
    d = _as_dict(allocs)
    assert d[E2] == Decimal("2000.00")
    assert d[E3] == Decimal("3000.00")
    assert E4 not in d
    assert allocated_total(allocs) == Decimal("5000.00")


def test_zero_and_negative_pool_empty() -> None:
    shares = _shares((E1, "1000"))
    assert allocate_pool(Decimal("0"), shares) == []
    assert allocate_pool(Decimal("-50"), shares) == []


def test_skips_non_positive_remaining() -> None:
    shares = [
        EmployeeShare(E1, Decimal("0")),
        EmployeeShare(E2, Decimal("-100")),
        EmployeeShare(E3, Decimal("500")),
    ]
    allocs = allocate_pool(Decimal("10000"), shares)
    assert _as_dict(allocs) == {E3: Decimal("500.00")}


def test_tiebreak_equal_remaining_by_id() -> None:
    # Равные остатки: порядок детерминирован по id (E1 раньше E2). Пул на 1.5 сотрудника.
    shares = _shares((E2, "1000"), (E1, "1000"))
    allocs = allocate_pool(Decimal("1500"), shares)
    # E1 (меньший id) гасится полностью, E2 получает остаток 500.
    assert allocs[0].employee_id == E1 and allocs[0].amount == Decimal("1000.00")
    assert allocs[1].employee_id == E2 and allocs[1].amount == Decimal("500.00")


def test_kopeck_quantization() -> None:
    # remaining округляются до копейки (ROUND_HALF_UP): 1000.005→1000.01, 2000.994→2000.99.
    shares = _shares((E1, "1000.005"), (E2, "2000.994"))
    allocs = allocate_pool(Decimal("3001.00"), shares)
    d = _as_dict(allocs)
    # E1 гасится полностью (1000.01), E2 забирает остаток пула 2000.99 (ровно его remaining).
    assert d[E1] == Decimal("1000.01")
    assert d[E2] == Decimal("2000.99")
    assert allocated_total(allocs) == Decimal("3001.00")


def test_kopeck_boundary_split_leaves_no_pool_dust() -> None:
    # Пул на копейку меньше суммы: граничный недополучает ровно копейку, дребезга пула нет.
    shares = _shares((E1, "1000.00"), (E2, "2000.00"))
    allocs = allocate_pool(Decimal("2999.99"), shares)
    d = _as_dict(allocs)
    assert d[E1] == Decimal("1000.00")
    assert d[E2] == Decimal("1999.99")
    assert allocated_total(allocs) == Decimal("2999.99")


def test_exact_pool_equals_total_no_leftover() -> None:
    shares = _shares((E1, "1234.56"), (E2, "8765.44"))
    allocs = allocate_pool(Decimal("10000.00"), shares)
    assert allocated_total(allocs) == Decimal("10000.00")
    assert _as_dict(allocs) == {E1: Decimal("1234.56"), E2: Decimal("8765.44")}
