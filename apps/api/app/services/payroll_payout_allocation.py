"""Разнесение выплаты ведомости по статьям ДДС с каскадным распределением наличных.

Административная ведомость смешанная: вспомогательный персонал (уборщицы/посудомойки)
относится к статье «Содержание торговых точек», остальные (администрация, включая
старшего курьера) — к «Зарплате административного персонала». Наличные гасят статьи
по приоритету (вспомогательная корзина первой), банк добивает остаток каждой корзины.

Производственная ведомость по этим функциям не разносится — там одна статья
(«Зарплата производственного персонала»), и вызывающий код идёт прежним путём.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.services.position_registry import position_info

# Коды статей ДДС из канонического каталога (миграция 0114_dds_articles_catalog).
DDS_ARTICLE_PRODUCTION_PAYROLL = "zarplata_proizvodstvennogo_personala"
DDS_ARTICLE_ADMIN_PAYROLL = "zarplata_administrativnogo_personala"
DDS_ARTICLE_AUX_PAYROLL = "soderzhanie_torgovyh_tochek"

# Приоритет погашения наличными: вспомогательная корзина (содержание торговых точек)
# гасится раньше администрации; производственная статья — в хвосте на случай смешанных
# строк (обычно это отдельная ведомость и сюда не попадает).
_CASH_PRIORITY: tuple[str, ...] = (
    DDS_ARTICLE_AUX_PAYROLL,
    DDS_ARTICLE_ADMIN_PAYROLL,
    DDS_ARTICLE_PRODUCTION_PAYROLL,
)

_CENTS = Decimal("0.01")


def _money(value: object) -> Decimal:
    return Decimal(str(value or 0)).quantize(_CENTS)


def dds_article_code_for_position(position: str | None) -> str | None:
    """Код статьи ДДС для выплаты зарплаты по должности (или None — должность неизвестна
    либо не участвует в этих ведомостях, например обычный курьер из депозитного контура)."""
    info = position_info(position)
    if info is None:
        return None
    if info.permission_group == "auxiliary":
        return DDS_ARTICLE_AUX_PAYROLL
    if info.archetype == "production_percent":
        return DDS_ARTICLE_PRODUCTION_PAYROLL
    if info.permission_group == "administration" or info.gets_admin_oklad:
        return DDS_ARTICLE_ADMIN_PAYROLL
    return None


@dataclass(frozen=True, slots=True)
class PayoutBucket:
    """Корзина выплаты по статье ДДС: сколько всего к выплате по этой статье."""

    article_code: str
    total: Decimal


@dataclass(frozen=True, slots=True)
class BucketAllocation:
    """Корзина, разнесённая на наличную и банковскую части (cash + bank == total)."""

    article_code: str
    total: Decimal
    cash: Decimal
    bank: Decimal


def build_payout_buckets(
    rows: Iterable[tuple[str | None, Decimal]],
    *,
    default_article_code: str,
) -> list[PayoutBucket]:
    """Сгруппировать строки ведомости ``(должность, сумма)`` в корзины по статьям ДДС.

    Должности, которые не маппятся на статью, падают в ``default_article_code``.
    Строки с нулевой/отрицательной суммой пропускаются. Корзины упорядочены по
    приоритету погашения наличными (``_CASH_PRIORITY``); статьи вне приоритета —
    в конце в порядке первого появления.
    """
    sums: dict[str, Decimal] = {}
    first_seen: list[str] = []
    for position, amount in rows:
        amt = _money(amount)
        if amt <= 0:
            continue
        code = dds_article_code_for_position(position) or default_article_code
        if code not in sums:
            sums[code] = Decimal("0.00")
            first_seen.append(code)
        sums[code] += amt

    def sort_key(code: str) -> tuple[int, int]:
        if code in _CASH_PRIORITY:
            return (0, _CASH_PRIORITY.index(code))
        return (1, first_seen.index(code))

    return [PayoutBucket(code, sums[code]) for code in sorted(sums, key=sort_key)]


def allocate_cash_cascade(
    buckets: Sequence[PayoutBucket],
    cash_total: Decimal,
) -> list[BucketAllocation]:
    """Каскадно распределить наличные по корзинам в их порядке, банк — остаток.

    Первая корзина забирает ``min(остаток_наличных, её сумма)`` наличными, следующая —
    из того, что осталось, и так далее. Для каждой корзины ``cash + bank == total``;
    суммарно ``Σ cash == min(cash_total, Σ total)``.
    """
    remaining = _money(cash_total)
    allocations: list[BucketAllocation] = []
    for bucket in buckets:
        cash = bucket.total if bucket.total <= remaining else remaining
        cash = _money(cash)
        bank = _money(bucket.total - cash)
        remaining = _money(remaining - cash)
        allocations.append(
            BucketAllocation(
                article_code=bucket.article_code,
                total=_money(bucket.total),
                cash=cash,
                bank=bank,
            )
        )
    return allocations
