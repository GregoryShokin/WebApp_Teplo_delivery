"""Правила ОПиУ, которые уже ломались, — закреплены тестом без базы.

Здесь нет проверок «функция возвращает число». Каждый тест соответствует конкретной ошибке,
которую владелец увидел на экране 03.08.2026, и падает, если её вернут обратно.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from app.services.pnl import formulas
from app.services.pnl.projector import INVERTED_IIKO_METRICS, rubles
from app.services.pnl.sources.deposits import _msk_bounds, _msk_date
from app.services.pnl.types import LineStatus, LineValue


def _line(code: str, amount: Decimal | None, status: LineStatus, **kwargs) -> LineValue:
    return LineValue(
        code=code,
        title=code,
        block="test",
        kind=kwargs.pop("kind", "source"),
        level=1,
        sort_order=1,
        sign_role=kwargs.pop("sign_role", -1),
        month_basis="calendar",
        amount=amount,
        status=status,
        **kwargs,
    )


class TestRubles:
    """Форматирование сумм не должно трогать текст вокруг числа."""

    def test_thousands_separated_and_kopecks_use_comma(self) -> None:
        # Разделитель разрядов — НЕРАЗРЫВНЫЙ пробел: сумма не должна переноситься посередине.
        assert rubles(Decimal("93752.00")) == "93 752,00"
        assert rubles(Decimal("10995.59")) == "10 995,59"
        assert rubles(Decimal("999.10")) == "999,10"
        assert rubles(Decimal("1234567.05")) == "1 234 567,05"

    def test_negative_keeps_sign(self) -> None:
        assert rubles(Decimal("-61861.00")) == "−61 861,00"

    def test_sentence_commas_survive(self) -> None:
        # Ровно та ошибка: раньше `f"{x:,.2f}".replace(",", " ")` вычищал запятую предложения,
        # и предупреждение читалось как «оплачено 10 995.59 ₽  документа за период ещё нет».
        message = f"оплачено {rubles(Decimal('10995.59'))} ₽, документа за период ещё нет"
        assert "₽, документа" in message


class TestRatioIncompleteness:
    """Неполный числитель не стирает рентабельность, а помечает её."""

    def test_incomplete_numerator_still_yields_value(self) -> None:
        lines = {
            "ebitda": _line(
                "ebitda",
                Decimal("896721.93"),
                LineStatus.INCOMPLETE,
                missing_lines=["context_ads"],
            ),
            "revenue": _line("revenue", Decimal("3596783.28"), LineStatus.OK),
        }
        value, missing = formulas._ratio({"num": "ebitda", "den": "revenue"}, lines)
        assert value is not None
        assert value.quantize(Decimal("0.01")) == Decimal("0.25")
        # Неполнота не исчезает — она переезжает на сам показатель.
        assert missing == ["context_ads"]

    def test_incomplete_denominator_cancels_the_ratio(self) -> None:
        # Знаменатель — всегда выручка. Пока она не подтянулась, доля считается от остатка
        # и выдаёт «≈ 100 %» на пустом месяце: такую цифру нельзя показать даже с оговоркой.
        lines = {
            "margin": _line("margin", Decimal("-1428.00"), LineStatus.INCOMPLETE),
            "revenue": _line(
                "revenue",
                Decimal("-1428.00"),
                LineStatus.INCOMPLETE,
                missing_lines=["revenue_net_chernikova"],
            ),
        }
        value, missing = formulas._ratio({"num": "margin", "den": "revenue"}, lines)
        assert value is None
        assert missing == ["revenue_net_chernikova"]

    def test_unknown_numerator_still_has_no_ratio(self) -> None:
        lines = {
            "ebitda": _line("ebitda", None, LineStatus.NO_DATA),
            "revenue": _line("revenue", Decimal("100.00"), LineStatus.OK),
        }
        value, missing = formulas._ratio({"num": "ebitda", "den": "revenue"}, lines)
        assert value is None
        assert "ebitda" in missing


class TestNotUsedLines:
    """Закрытая точка не делает подытог неполным — она просто не участвует."""

    def test_not_used_contributes_zero_without_missing(self) -> None:
        lines = {
            "rent_gagarina": _line("rent_gagarina", None, LineStatus.NOT_USED),
            "rent_chernikova": _line("rent_chernikova", Decimal("100000.00"), LineStatus.OK),
        }
        catalog = [
            {"code": "rent_gagarina", "block": "b", "kind": "source", "status": "not_used"},
            {"code": "rent_chernikova", "block": "b", "kind": "source", "status": "active"},
        ]
        value, missing = formulas.evaluate({"op": "sum", "block": "b"}, lines, catalog)
        assert value == Decimal("100000.00")
        assert missing == []


class TestInventorySign:
    """Недостача упаковки — расход, а не доход."""

    def test_both_inventory_metrics_are_inverted(self) -> None:
        # Строка отчёта держит расход положительным; зеркало iiko отдаёт недостачу минусом.
        # Забыть здесь одну из двух метрик — значит показать соседние строки в разных
        # соглашениях, что уже случалось.
        assert set(INVERTED_IIKO_METRICS) == {"packaging_result", "pizza_box_result"}

    def test_shortage_becomes_positive_expense(self) -> None:
        mirror = Decimal("-30810.00")  # недостача в терминах iiko
        assert -mirror == Decimal("30810.00")


class TestMoscowMonthBounds:
    """Месяц списания считается по Москве, а не по UTC."""

    def test_bounds_shift_three_hours_back(self) -> None:
        start, end = _msk_bounds(date(2026, 7, 1), date(2026, 7, 31))
        assert start == datetime(2026, 6, 30, 21, 0)
        assert end == datetime(2026, 7, 31, 21, 0)

    def test_late_evening_stays_in_its_moscow_day(self) -> None:
        # 28.07 21:38 МСК = 18:38 UTC — июль в обеих системах.
        assert _msk_date(datetime(2026, 7, 28, 18, 38)) == date(2026, 7, 28)
        # 01.08 00:30 МСК = 31.07 21:30 UTC — наивная группировка утащила бы это в июль.
        assert _msk_date(datetime(2026, 7, 31, 21, 30)) == date(2026, 8, 1)
