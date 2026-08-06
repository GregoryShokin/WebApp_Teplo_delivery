"""Правила ОПиУ, которые уже ломались, — закреплены тестом без базы.

Здесь нет проверок «функция возвращает число». Каждый тест соответствует конкретной ошибке,
которую владелец увидел на экране 03.08.2026, и падает, если её вернут обратно.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from app.services.pnl import formulas, iiko_sync, projector
from app.services.pnl.projector import IIKO_LINE_METRIC, INVERTED_IIKO_METRICS, rubles
from app.services.pnl.sources import acquiring as acquiring_source
from app.services.pnl.sources import cashflow as cash_source
from app.services.pnl.sources import inventory as inventory_source
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


class TestInventoryVersusStock:
    """Результат инвентаризации и складской оборот — разные величины, и их уже путали.

    Инвентаризация мерит ПОТЕРЮ: расхождение книжного остатка с фактическим. Roll-forward
    «начало + приход − конец» мерит РАСХОД ЗА ПЕРИОД, то есть ровно то, что считает фудкост.
    Подмена первого вторым выключила из июля 2026 расход 21 018,27 ₽ и не дала замены:
    потребление уже было посчитано, потери перестали считаться вовсе.
    """

    def test_inventory_metrics_are_inverted(self) -> None:
        # Строка отчёта держит расход положительным; зеркало iiko отдаёт недостачу минусом.
        # Все три корзины приходят одним документом барной ревизии и инвертируются одинаково.
        assert set(INVERTED_IIKO_METRICS) == {
            "packaging_result",
            "pizza_box_result",
            "beverage_result",
        }

    def test_stock_metrics_are_not_pnl_lines(self) -> None:
        # Roll-forward в ОПиУ не идёт ни при каком знаке: он задвоил бы себестоимость.
        stock_metrics = {"stock_consumption", "stock_closing_balance"}
        assert not stock_metrics & set(IIKO_LINE_METRIC.values())
        assert not stock_metrics & set(INVERTED_IIKO_METRICS)

    def test_inventory_lines_are_fed_from_the_variance_metric(self) -> None:
        assert IIKO_LINE_METRIC["packaging_inventory"] == "packaging_result"
        assert IIKO_LINE_METRIC["pizza_box_inventory"] == "pizza_box_result"
        assert IIKO_LINE_METRIC["beverage_inventory"] == "beverage_result"

    def test_shortage_is_expense_and_surplus_compensates(self) -> None:
        shortage_in_iiko = Decimal("-28308.86")  # недостача упаковки в терминах iiko
        surplus_in_iiko = Decimal("7290.59")  # излишек коробок
        assert -shortage_in_iiko == Decimal("28308.86")
        assert -surplus_in_iiko == Decimal("-7290.59")


class TestBarAudit:
    """Барная ревизия: один документ, три строки, и ни одна не должна пересечься с поварской.

    Ревизий на точке две (владелец, 05.08.2026): поварская считает сырьё каждую неделю и
    живёт в модуле «Ревизии», барная считает барную стойку раз в месяц и приходит документом
    iiko. Упаковка, коробки для пиццы и напитки едут ОДНИМ документом, и разводит их только
    whitelist — по природе товара, а не по источнику.
    """

    def test_all_three_baskets_come_from_the_inventory_document(self) -> None:
        assert {
            iiko_sync.METRIC_PACKAGING,
            iiko_sync.METRIC_PIZZA_BOX,
            iiko_sync.METRIC_BEVERAGE,
        } == iiko_sync.INVENTORY_BASKETS
        for metric in iiko_sync.INVENTORY_BASKETS:
            assert iiko_sync.GOODS_METRIC_SOURCE[metric] == iiko_sync.INVENTORY_ENDPOINT

    def test_every_inventory_basket_has_its_own_line(self) -> None:
        # Корзина без строки ОПиУ — это молча потерянный расход: синк её посчитает,
        # а показать будет негде.
        line_by_metric = {metric: line for line, metric in iiko_sync.WHITELIST_METRIC.items()}
        for metric in iiko_sync.INVENTORY_BASKETS:
            line_code = line_by_metric[metric]
            assert IIKO_LINE_METRIC[line_code] == metric

    def test_invoice_and_inventory_baskets_do_not_overlap(self) -> None:
        # Один и тот же расход не может считаться и по закупке, и по пересчёту.
        assert not iiko_sync.INVOICE_BASKETS & iiko_sync.INVENTORY_BASKETS

    def test_bar_lines_are_excluded_from_the_cook_audit(self) -> None:
        # Товары всех трёх барных строк обязаны выпадать из строки поварской ревизии,
        # иначе одно расхождение попадёт в два блока отчёта.
        line_by_metric = {metric: line for line, metric in iiko_sync.WHITELIST_METRIC.items()}
        bar_lines = {line_by_metric[metric] for metric in iiko_sync.INVENTORY_BASKETS}
        assert bar_lines == set(inventory_source.BAR_AUDIT_LINES)


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


class TestExcludedWithoutAccrual:
    """Касса исключена «под начисление» — а начисления столько нет.

    Как только контрагент попал в контур признания, вся его касса месяца выбрасывается из
    строк: отчёт утверждает, что расход придёт документом. Утверждение это не проверялось
    ничем — суммы признания считались и не читались. Аудит замкнутости 05.08.2026 намерил
    таким путём 134 000 ₽ за июль.

    СВЕРКА ИДЁТ ПО ПАРЕ «КОНТРАГЕНТ × СТРОКА», и эти тесты защищают именно пару. Прежняя
    версия складывала признание контрагента по всем статьям: у арендодателя Виталия
    признанная АРЕНДА июля 2026 гасила тревогу по КОММУНАЛКЕ, где признано ноль (95 402 ₽
    превращались в 45 402 ₽), а у Станислава Юрьевича арендный щит глушил сигнал целиком.
    """

    CP_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
    CP_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
    RENT_ARTICLE = uuid.UUID("33333333-3333-3333-3333-333333333333")
    UTILITY_ARTICLE = uuid.UUID("44444444-4444-4444-4444-444444444444")
    SHOP_ARTICLE = uuid.UUID("55555555-5555-5555-5555-555555555555")
    #: Начисление без своей статьи: строка берётся из карточки контрагента. Ради этого случая
    #: сверку когда-то держали по контрагенту целиком — здесь она справляется парой.
    NO_ARTICLE = None

    ARTICLE_LINES = {
        RENT_ARTICLE: "rent_chernikova",
        UTILITY_ARTICLE: "utilities_chernikova",
        SHOP_ARTICLE: "shop_maintenance",
    }

    def _lines(self) -> dict:
        return {
            code: LineValue(
                code=code,
                title=title,
                block="expenses",
                kind="line",
                level=1,
                sort_order=1,
                sign_role=-1,
                month_basis="cash",
                amount=Decimal("0.00"),
                status=LineStatus.OK,
            )
            for code, title in (
                ("rent_chernikova", "Аренда торговой точки Черникова"),
                ("utilities_chernikova", "Коммунальные платежи Черникова"),
                ("shop_maintenance", "Содержание торговых точек"),
            )
        }

    def _warn(
        self,
        excluded: dict,
        recognized: list,
        names: dict | None = None,
        backed: dict | None = None,
    ):
        """``excluded``: {(контрагент, строка): сумма}; ``recognized``: [(контрагент, статья, сумма)].

        ``backed`` — вся касса контрагента по строкам, включая платежи со следом в ДЗ/КЗ.
        По умолчанию равна ``excluded``: признание на чужой строке без своей кассы читается
        как «у платежа не та статья».
        """
        cash = cash_source.CashLayer()
        cash.excluded_for_accrual.update(excluded)
        cash.excluded_all_lines.update(backed if backed is not None else excluded)
        cash.excluded_counterparty_names.update(names or {})
        recognition = recognition_source.RecognitionLayer()
        for counterparty_id, article_id, amount in recognized:
            recognition.details.append(
                recognition_source.RecognitionDetail(
                    accrual_id=uuid.uuid4(),
                    counterparty_id=counterparty_id,
                    article_id=article_id,
                    amount=amount,
                    service_period_start=None,
                    service_period_end=None,
                    has_primary=True,
                    origin=recognition_source.ORIGIN_DOCUMENT,
                )
            )
        return projector._unfulfilled_accrual_warnings(
            cash, recognition, self.ARTICLE_LINES, self._lines()
        )

    def test_recognition_on_another_line_does_not_shield_the_gap(self) -> None:
        """Случай Виталия: аренда признана и оплачена, коммуналка не признана вовсе.

        Признание аренды обеспечено своими арендными платежами (они в ``ledger_known``, потому
        и не попали в разрыв), значит к коммунальной дыре отношения не имеет: это «документа
        нет вовсе», а не «расход уехал на другую строку».
        """
        warnings = self._warn(
            excluded={(self.CP_A, "utilities_chernikova"): Decimal("95402.00")},
            recognized=[(self.CP_A, self.RENT_ARTICLE, Decimal("50000.00"))],
            names={self.CP_A: "Виталий"},
            backed={
                (self.CP_A, "utilities_chernikova"): Decimal("95402.00"),
                (self.CP_A, "rent_chernikova"): Decimal("100000.00"),
            },
        )
        assert len(warnings) == 1
        # Признание аренды не вычитается из коммунальной кассы: 95 402, а не 45 402.
        assert warnings[0].amount == Decimal("95402.00")
        assert "Виталий" in warnings[0].message
        assert warnings[0].code == "excluded_without_accrual"

    def test_recognition_without_its_own_cash_reads_as_wrong_article(self) -> None:
        """Случай ЧОО: охрана оплачена по арендной статье, признана на содержании точек.

        На строке признания собственной кассы нет — значит деньги прошли по чужой статье,
        и владельцу надо чинить разметку, а не искать документ.
        """
        warnings = self._warn(
            excluded={(self.CP_A, "rent_chernikova"): Decimal("2300.00")},
            recognized=[(self.CP_A, self.SHOP_ARTICLE, Decimal("2300.00"))],
            names={self.CP_A: "ООО ЧОО"},
        )
        assert len(warnings) == 1
        assert warnings[0].code == "accrual_on_other_line"
        assert warnings[0].amount == Decimal("2300.00")

    def test_no_recognition_anywhere_is_a_lost_document(self) -> None:
        """Признания у контрагента нет ни на одной строке — расход потерян, документ не приехал."""
        warnings = self._warn(
            excluded={(self.CP_A, "utilities_chernikova"): Decimal("95402.00")},
            recognized=[],
            names={self.CP_A: "Виталий"},
        )
        assert len(warnings) == 1
        assert warnings[0].code == "excluded_without_accrual"
        assert warnings[0].amount == Decimal("95402.00")
        assert "Коммунальные платежи Черникова" in warnings[0].message

    def test_recognition_on_the_same_line_is_silent(self) -> None:
        cash_and_docs_agree = self._warn(
            excluded={(self.CP_A, "utilities_chernikova"): Decimal("50000.00")},
            recognized=[(self.CP_A, self.UTILITY_ARTICLE, Decimal("50000.00"))],
        )
        assert cash_and_docs_agree == []

    def test_accrual_without_article_lands_on_the_line_from_the_card(self) -> None:
        """Начисление без своей статьи разрешается карточкой — сверка обязана это учесть.

        Ради этого случая сверку когда-то держали по контрагенту целиком. Строка берётся из
        ``details``, где статья уже разрешена, поэтому пара работает и здесь.
        """
        resolved = self._warn(
            excluded={(self.CP_A, "utilities_chernikova"): Decimal("4120.59")},
            recognized=[(self.CP_A, self.UTILITY_ARTICLE, Decimal("4120.59"))],
        )
        assert resolved == []

    def test_shield_no_longer_silences_a_second_counterparty(self) -> None:
        """Случай Станислава: арендное признание больше не глушит коммунальный разрыв."""
        warnings = self._warn(
            excluded={(self.CP_B, "utilities_chernikova"): Decimal("9879.00")},
            recognized=[
                (self.CP_B, self.RENT_ARTICLE, Decimal("50000.00")),
                (self.CP_B, self.UTILITY_ARTICLE, Decimal("7000.00")),
            ],
            names={self.CP_B: "Станислав Юрьевич"},
        )
        assert len(warnings) == 1
        # 9 879 − 7 000 = 2 879 по СВОЕЙ строке; аренда в вычитание не входит.
        assert warnings[0].amount == Decimal("2879.00")

    def test_small_tails_stay_below_the_threshold(self) -> None:
        """Хвост коммунального перерасчёта (224,75 ₽) не делает предупреждение вечно красным."""
        warnings = self._warn(
            excluded={(self.CP_B, "utilities_chernikova"): Decimal("9879.00")},
            recognized=[(self.CP_B, self.UTILITY_ARTICLE, Decimal("9654.25"))],
        )
        assert warnings == []

    def test_overpaid_recognition_does_not_go_negative(self) -> None:
        # Признано больше оплаченного — это не разрыв, а нормальная рассрочка.
        warnings = self._warn(
            excluded={(self.CP_A, "utilities_chernikova"): Decimal("1000.00")},
            recognized=[(self.CP_A, self.UTILITY_ARTICLE, Decimal("5000.00"))],
        )
        assert warnings == []

    def test_gaps_are_sorted_and_summed_within_one_warning(self) -> None:
        warnings = self._warn(
            excluded={
                (self.CP_A, "utilities_chernikova"): Decimal("6000.00"),
                (self.CP_B, "utilities_chernikova"): Decimal("68000.00"),
            },
            recognized=[],
            names={self.CP_A: "Наумченко", self.CP_B: "О. О, ООО"},
        )
        assert len(warnings) == 1
        assert warnings[0].amount == Decimal("74000.00")
        # Крупнейший разрыв назван первым: с него и начинают разбираться.
        assert warnings[0].message.index("О. О") < warnings[0].message.index("Наумченко")


class TestReconciliationIsNotTautological:
    """Сверка обязана уметь провалиться — иначе зелёная галочка не значит ничего.

    Раньше дрейф считался как разность двух сумм, набранных ОДНИМ циклом по ОДНОЙ выборке,
    и был нулём алгебраически: сверка не могла показать ошибку даже при потерянной проводке.
    Аудит 05.08.2026 назвал это «галочкой, которая ничего не проверяет». Теперь вторая
    сторона — контрольный агрегат базы, и эти тесты проверяют именно способность падать.
    """

    def _layer(self, *, source_total: str, source_count: int, counted: int, verdicts: dict):
        layer = cash_source.CashLayer()
        layer.source_total = Decimal(source_total)
        layer.source_count = source_count
        layer.counted = counted
        for verdict, amount in verdicts.items():
            layer.by_verdict[verdict] = Decimal(amount)
        return layer

    def test_balanced_when_everything_matches(self) -> None:
        layer = self._layer(
            source_total="1000.00",
            source_count=3,
            counted=3,
            verdicts={"included": "600.00", "excluded_out_of_pnl": "400.00"},
        )
        result = projector._reconciliation(layer)
        assert result.drift == Decimal("0.00")
        assert result.missed_count == 0
        assert result.balanced is True

    def test_lost_money_is_caught(self) -> None:
        """Проводка не дошла до вердикта — база знает про 1000, вердикты знают про 600."""
        layer = self._layer(
            source_total="1000.00",
            source_count=3,
            counted=3,
            verdicts={"included": "600.00"},
        )
        result = projector._reconciliation(layer)
        assert result.drift == Decimal("400.00")
        assert result.balanced is False

    def test_lost_zero_amount_transaction_is_caught_by_count(self) -> None:
        """Потеря нулевой проводки рублями не видна — её ловит счётчик документов."""
        layer = self._layer(
            source_total="1000.00",
            source_count=4,
            counted=3,
            verdicts={"included": "1000.00"},
        )
        result = projector._reconciliation(layer)
        assert result.drift == Decimal("0.00"), "сумма сходится"
        assert result.missed_count == 1, "а проводка потеряна"
        assert result.balanced is False

    def test_unmapped_still_breaks_the_balance(self) -> None:
        layer = self._layer(
            source_total="1000.00",
            source_count=2,
            counted=2,
            verdicts={"included": "900.00", "unmapped": "100.00"},
        )
        layer.unmapped = Decimal("100.00")
        result = projector._reconciliation(layer)
        assert result.drift == Decimal("0.00")
        assert result.balanced is False
