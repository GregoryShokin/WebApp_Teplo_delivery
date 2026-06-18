"""Юнит-тесты разнесения выплаты по статьям ДДС и каскада наличных.

Чистые функции, без БД: реестр должностей сбрасывается на встроенный канон
(``reset_position_registry_for_tests``), идентичный сиду миграций.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.payroll_payout_allocation import (
    DDS_ARTICLE_ADMIN_PAYROLL,
    DDS_ARTICLE_AUX_PAYROLL,
    DDS_ARTICLE_PRODUCTION_PAYROLL,
    PayoutBucket,
    allocate_cash_cascade,
    build_payout_buckets,
    dds_article_code_for_position,
)
from app.services.position_registry import reset_position_registry_for_tests


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_position_registry_for_tests()
    yield
    reset_position_registry_for_tests()


@pytest.mark.parametrize(
    "position,expected",
    [
        ("Уборщица", DDS_ARTICLE_AUX_PAYROLL),
        ("Посудомойка", DDS_ARTICLE_AUX_PAYROLL),
        ("Менеджер", DDS_ARTICLE_ADMIN_PAYROLL),
        ("Управляющий", DDS_ARTICLE_ADMIN_PAYROLL),
        ("Системный администратор", DDS_ARTICLE_ADMIN_PAYROLL),
        ("Старший курьер", DDS_ARTICLE_ADMIN_PAYROLL),
        ("Повар", DDS_ARTICLE_PRODUCTION_PAYROLL),
        ("Кассир", DDS_ARTICLE_PRODUCTION_PAYROLL),
        ("Курьер", None),
        ("Несуществующая должность", None),
        (None, None),
    ],
)
def test_article_mapping(position, expected):
    assert dds_article_code_for_position(position) == expected


def test_buckets_group_and_priority_order():
    rows = [
        ("Менеджер", Decimal("30000")),
        ("Уборщица", Decimal("15000")),
        ("Управляющий", Decimal("15000")),
        ("Посудомойка", Decimal("10000")),
    ]
    buckets = build_payout_buckets(rows, default_article_code=DDS_ARTICLE_ADMIN_PAYROLL)
    # Вспомогательная корзина (содержание) идёт первой по приоритету наличных.
    assert [b.article_code for b in buckets] == [
        DDS_ARTICLE_AUX_PAYROLL,
        DDS_ARTICLE_ADMIN_PAYROLL,
    ]
    assert buckets[0].total == Decimal("25000.00")  # 15000 уборщица + 10000 посудомойка
    assert buckets[1].total == Decimal("45000.00")  # 30000 менеджер + 15000 управляющий


def test_buckets_skip_nonpositive_and_use_default():
    rows = [
        ("Менеджер", Decimal("0")),  # пропускается
        ("Неизвестная", Decimal("5000")),  # падает в default
    ]
    buckets = build_payout_buckets(rows, default_article_code=DDS_ARTICLE_ADMIN_PAYROLL)
    assert buckets == [PayoutBucket(DDS_ARTICLE_ADMIN_PAYROLL, Decimal("5000.00"))]


def _owner_example_buckets() -> list[PayoutBucket]:
    return [
        PayoutBucket(DDS_ARTICLE_AUX_PAYROLL, Decimal("25000.00")),
        PayoutBucket(DDS_ARTICLE_ADMIN_PAYROLL, Decimal("45000.00")),
    ]


def test_cascade_owner_example():
    # ФОТ 70к (вспом 25к + админ 45к), сплит нал 20к / банк 50к.
    alloc = {a.article_code: a for a in allocate_cash_cascade(_owner_example_buckets(), Decimal("20000"))}
    assert alloc[DDS_ARTICLE_AUX_PAYROLL].cash == Decimal("20000.00")
    assert alloc[DDS_ARTICLE_AUX_PAYROLL].bank == Decimal("5000.00")
    assert alloc[DDS_ARTICLE_ADMIN_PAYROLL].cash == Decimal("0.00")
    assert alloc[DDS_ARTICLE_ADMIN_PAYROLL].bank == Decimal("45000.00")


@pytest.mark.parametrize(
    "cash,aux_cash,aux_bank,admin_cash,admin_bank",
    [
        ("0", "0.00", "25000.00", "0.00", "45000.00"),
        ("20000", "20000.00", "5000.00", "0.00", "45000.00"),
        ("25000", "25000.00", "0.00", "0.00", "45000.00"),
        ("30000", "25000.00", "0.00", "5000.00", "40000.00"),
        ("70000", "25000.00", "0.00", "45000.00", "0.00"),
    ],
)
def test_cascade_edge_cases(cash, aux_cash, aux_bank, admin_cash, admin_bank):
    alloc = {a.article_code: a for a in allocate_cash_cascade(_owner_example_buckets(), Decimal(cash))}
    assert alloc[DDS_ARTICLE_AUX_PAYROLL].cash == Decimal(aux_cash)
    assert alloc[DDS_ARTICLE_AUX_PAYROLL].bank == Decimal(aux_bank)
    assert alloc[DDS_ARTICLE_ADMIN_PAYROLL].cash == Decimal(admin_cash)
    assert alloc[DDS_ARTICLE_ADMIN_PAYROLL].bank == Decimal(admin_bank)


@pytest.mark.parametrize("cash", ["0", "20000", "25000", "30000", "70000", "100000"])
def test_cascade_invariants(cash):
    buckets = _owner_example_buckets()
    alloc = allocate_cash_cascade(buckets, Decimal(cash))
    total_sum = sum(b.total for b in buckets)
    # cash + bank == total для каждой корзины
    for a in alloc:
        assert a.cash + a.bank == a.total
    # суммарно наличными ушло не больше заданного и не больше ФОТ
    assert sum(a.cash for a in alloc) == min(Decimal(cash), total_sum)
    assert sum(a.bank for a in alloc) == total_sum - min(Decimal(cash), total_sum)
