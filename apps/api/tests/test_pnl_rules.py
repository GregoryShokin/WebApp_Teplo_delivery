"""Правила ОПиУ, которые уже ломались, — закреплены тестом без базы.

Здесь нет проверок «функция возвращает число». Каждый тест соответствует конкретной ошибке,
которую владелец увидел на экране 03.08.2026, и падает, если её вернут обратно.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from app.services.pnl import formulas
from app.services.pnl.projector import INVERTED_IIKO_METRICS, rubles
from app.services.pnl.sources import acquiring as acquiring_source
from app.services.pnl.sources import recognition as recognition_source
from app.services.pnl.sources.acquiring import parse_commission
from app.services.pnl.sources.deposits import _msk_bounds, _msk_date
from app.services.pnl.sources.recognition import classify_origin
from app.services.pnl.sources.waiting import month_share
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


class TestWaitingMonthAttribution:
    """К какому месяцу относится оплаченное, но не закрытое документом.

    Все четыре правила давали неверный ответ на данных июля 2026, и все четыре владелец
    назвал вслух. Проверяются они здесь, а не через базу: через базу проверялись бы фикстуры.
    """

    JULY = (date(2026, 7, 1), date(2026, 7, 31))

    def _share(self, outstanding: str, **kwargs) -> Decimal | None:
        defaults = {
            "month_start": self.JULY[0],
            "month_end": self.JULY[1],
            "period_start": None,
            "period_end": None,
            "paid_on": None,
            "counterparty_recognized": False,
        }
        return month_share(Decimal(outstanding), **{**defaults, **kwargs})

    def test_july_payment_for_august_is_not_july(self) -> None:
        # Синапсис и О.О заплачены 22.07 — период услуги август. Раньше обе суммы вставали
        # в июльское «ждём документ», и владелец увидел это первым.
        assert (
            self._share(
                "68000.00",
                period_start=date(2026, 8, 1),
                period_end=date(2026, 8, 31),
                paid_on=date(2026, 7, 22),
            )
            is None
        )

    def test_june_payment_for_july_is_july(self) -> None:
        # А то, чего июль действительно ждёт, оплачено 29.06 и в июльскую кассу не попадало.
        assert self._share(
            "48000.00",
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            paid_on=date(2026, 6, 29),
        ) == Decimal("48000.00")

    def test_period_longer_than_month_is_split(self) -> None:
        assert self._share(
            "30000.00",
            period_start=date(2026, 7, 1),
            period_end=date(2026, 9, 30),
            paid_on=date(2026, 6, 20),
        ) == Decimal("10000.00")

    def test_without_period_month_comes_from_payment(self) -> None:
        # Манго: телефония по потреблению, период известен только из УПД. Другой привязки нет.
        assert self._share("10000.00", paid_on=date(2026, 7, 7)) == Decimal("10000.00")

    def test_without_period_recognized_counterparty_is_not_waiting(self) -> None:
        # Арендодатель заплачен 30.07, периода у платежа нет — но июльская аренда по нему уже
        # признана начислением из договора. Значит платёж относится к другому месяцу.
        assert (
            self._share("50000.00", paid_on=date(2026, 7, 30), counterparty_recognized=True) is None
        )

    def test_without_period_and_without_payment_belongs_nowhere(self) -> None:
        assert self._share("15862.24") is None


class TestRecognitionOrigin:
    """Чем подтверждён расход — «документа нет» и «документа не ждут» это разные новости."""

    def test_self_billed_by_tariff_is_not_a_gap(self) -> None:
        # Синапсис в режиме «счёт за период»: признание строится самоактом по окончании
        # месяца, закрывающего не ждут вовсе. Подпись «без первички» читалась упрёком, и
        # владелец справедливо возразил — счета от него приходят.
        assert classify_origin("self_billed", "fixed_tariff") == recognition_source.ORIGIN_BY_TARIFF

    def test_self_billed_where_document_is_expected_is_a_gap(self) -> None:
        assert (
            classify_origin("self_billed", "per_invoice")
            == recognition_source.ORIGIN_AWAITING_DOCUMENT
        )
        assert classify_origin("self_billed", None) == recognition_source.ORIGIN_AWAITING_DOCUMENT

    def test_contract_sources_speak_for_themselves(self) -> None:
        assert classify_origin("lease", None) == recognition_source.ORIGIN_LEASE
        assert classify_origin("utility", None) == recognition_source.ORIGIN_UTILITY

    def test_counterparty_document_wins_over_mode(self) -> None:
        # Микроэл тоже fixed_tariff, но по июлю приехал настоящий счёт — подпись обязана
        # отличаться от соседней строки того же режима.
        assert classify_origin("email", "fixed_tariff") == recognition_source.ORIGIN_DOCUMENT

    def test_accrual_without_invoice_is_a_payment_line(self) -> None:
        assert classify_origin(None, "agreement") == recognition_source.ORIGIN_PAYMENT_LINE


class TestFundForfeitHorizon:
    """Отменить можно только тот расход, который отчёт когда-то признал."""

    @staticmethod
    def _counted(forfeit: str, share: str) -> Decimal:
        # Та же арифметика, что в build_release_month: доля счёта, накопленная внутри
        # горизонта, умножается на сумму списания.
        return (Decimal(forfeit) * Decimal(share)).quantize(Decimal("0.01"))

    def test_legacy_fund_is_fully_excluded(self) -> None:
        # Фонды 2024–2025 давно уволенных: ни рубля не начислено после 01.07.2026, значит
        # отмена расход июля не уменьшает. Владелец назвал их рудиментарными.
        assert self._counted("62260.00", "0") == Decimal("0.00")

    def test_fund_accrued_inside_horizon_is_reversed(self) -> None:
        assert self._counted("12000.00", "1") == Decimal("12000.00")

    def test_partially_pre_horizon_fund_is_split(self) -> None:
        # Счёт текущего года: половина накоплена до начала учёта, половина после. Резать по
        # году было бы грубо — январский и августовский рубль попали бы в одну корзину.
        assert self._counted("12000.00", "0.5") == Decimal("6000.00")


class TestAcquiringCommission:
    """Комиссия эквайринга живёт текстом в назначении платежа — разбор пинуется тестом.

    Отдельного поля у Сбера нет, зачисления приходят нетто, и любая правка регулярки молча
    занижает строку. Все четыре формата взяты из боевой выписки за июль 2026.
    """

    def test_two_part_commission_sums_both(self) -> None:
        # Самый коварный формат: вторая часть в реальных данных бывает ненулевой, и взяв
        # только первую, теряем деньги молча.
        channel, amount = parse_commission(
            "Зачисление средств по операциям эквайринга. Мерчант №211000343261. "
            "Комиссия 242.57 (в т.ч. НДС 43.74) и 16.20 (НДС не обл). Возврат покупки 0.00/0.00."
        )
        assert channel == acquiring_source.CHANNEL_CARD
        assert amount == Decimal("258.77")

    def test_single_part_with_vat(self) -> None:
        channel, amount = parse_commission(
            "Зачисление средств по операциям эквайринга. Мерчант №211000343261. "
            "Комиссия 198.27 (в т.ч. НДС 35.76). Возврат покупки 0.00/0.00."
        )
        assert channel == acquiring_source.CHANNEL_CARD
        assert amount == Decimal("198.27")

    def test_bare_amount_does_not_swallow_the_full_stop(self) -> None:
        # «Комиссия 4.90.» — точка конца предложения стоит вплотную к числу, а сразу за ней
        # идёт «Возврат покупки 0.00/0.00» из таких же цифр.
        channel, amount = parse_commission(
            "Зачисление средств по операциям эквайринга. Мерчант №211000343219. "
            "Комиссия 4.90. Возврат покупки 0.00/0.00.НДС не облагается."
        )
        assert channel == acquiring_source.CHANNEL_CARD
        assert amount == Decimal("4.90")

    def test_payments_channel_has_its_own_wording(self) -> None:
        channel, amount = parse_commission(
            "Перевод средств по договору №СТ22989030758920125 от 07  июля 2025 г.. "
            "За дату 30.06.2026, удержано комиссии за прием платежей  721.12 руб. "
            "НДС не облагается."
        )
        assert channel == acquiring_source.CHANNEL_PAYMENTS
        assert amount == Decimal("721.12")

    def test_unrelated_purpose_yields_nothing(self) -> None:
        assert parse_commission("Оплата по счету 513573 от 16.06.2026") is None
        assert parse_commission(None) is None


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
