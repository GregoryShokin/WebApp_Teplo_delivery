"""Золотые тесты распознавания коммунальных документов.

Фикстуры в ``tests/fixtures/utility`` — это НЕ выдуманные строки, а сырой OCR-выхлоп с
реальных фотографий счетов Волгодонского Водоканала (``water_real_*``), снятых с телефона и
прогнанных tesseract'ом и macOS Vision. Именно поэтому они ценны: синтетика не воспроизводит
ни склеенную с числом границу таблицы, ни номер строки, прочитанный как ``©``, а чинить
приходится ровно это. Ожидаемые суммы сверены с бумажными счетами.

Проверяем три вещи: сумма воды считается по строкам 4-7 и никогда не равна итогу документа;
акт электроэнергии различает факт и аванс и не путает расход периода с суммой к доплате;
чужой документ возвращает ``None``, а не «почти коммунальный» разбор.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from app.services.utility_recognition import UtilityRecognition, recognize_utility_document

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "utility"


def load(fixture: str) -> str:
    return (FIXTURE_ROOT / fixture).read_text(encoding="utf-8")


def recognize(fixture: str) -> UtilityRecognition:
    result = recognize_utility_document(load(fixture), {"source_file": fixture})
    assert result is not None, f"фикстура {fixture} не опознана как коммунальный документ"
    return result


def assert_volgodonsk_water(result: UtilityRecognition) -> None:
    """Реквизиты Водоканала одни и те же на всех четырёх счетах — их и сверяем.

    КПП здесь намеренно нет: на апрельском скане он распознан как ``614301007`` — цифра
    съедена шумом. Это не повод чинить парсер догадкой, поэтому КПП проверяется точечно
    там, где OCR его вытащил чисто.
    """
    assert result.kind == "water"
    assert result.raw["parser_name"] == "water_utility_invoice_v1"
    assert result.raw["counterparty_alias"] == "Водоканал"
    assert result.raw["inn"] == "6143049157"
    assert result.raw["bank_bik"] == "044525411"
    assert result.raw["bank_account"] == "40702810102300005667"
    assert result.raw["corr_account"] == "30101810145250000411"


# --- Вода: реальные фотографии счетов ---------------------------------------


def test_february_2026_row_six_marker_garbled() -> None:
    # OCR прочитал номер строки 6 как `©`, а к номеру счёта приклеил `1` от границы
    # таблицы. Обе поломки должны быть починены, иначе сумма уедет.
    result = recognize("water_real_20260523_194915_cfb2a7fa.txt")

    assert_volgodonsk_water(result)
    assert result.amount == Decimal("10327.82")
    assert result.raw["vat_amount"] == "1862.39"
    assert result.document_number == "1758"
    assert result.document_date == date(2026, 2, 28)
    assert result.period_start == date(2026, 2, 1)
    assert result.period_end == date(2026, 2, 28)
    assert result.raw["kpp"] == "614301001"


def test_march_2026_selected_rows_reconciled_with_invoice_total() -> None:
    result = recognize("water_real_20260523_192709_ded644bc.txt")

    assert_volgodonsk_water(result)
    assert result.amount == Decimal("8307.16")
    assert result.raw["vat_amount"] == "1498.01"
    assert result.document_number == "3784"
    assert result.document_date == date(2026, 3, 31)
    assert result.period_start == date(2026, 3, 1)
    assert result.raw["kpp"] == "614301001"


def test_april_2026_duplicate_row_marker() -> None:
    # Здесь OCR прочитал номер строки 6 как `5` — дубль предыдущей. Строка возвращается
    # на место по своему заголовку «Плата за негативное воздействие».
    result = recognize("water_real_20260523_192636_b198d13d.txt")

    assert_volgodonsk_water(result)
    assert result.amount == Decimal("8531.68")
    assert result.raw["vat_amount"] == "1538.50"
    assert result.document_number == "5484"
    assert result.document_date == date(2026, 4, 30)
    assert result.period_start == date(2026, 4, 1)


def test_january_2026_column_major_vision_output() -> None:
    # macOS Vision выдаёт ту же таблицу по столбцам, а не по строкам, — для неё в парсере
    # отдельный путь. Сумма обязана совпасть с бумажным счётом.
    result = recognize("water_real_20260523_201747_f96cd3fc_vision.txt")

    assert_volgodonsk_water(result)
    assert result.amount == Decimal("9878.79")
    assert result.raw["vat_amount"] == "1781.43"
    assert result.document_number == "156"
    assert result.document_date == date(2026, 1, 31)
    assert result.period_start == date(2026, 1, 1)
    assert result.period_end == date(2026, 1, 31)


def test_unreadable_table_gives_no_amount_instead_of_invoice_total() -> None:
    # Тот же счёт, но пожатый телеграмом: tesseract не вытащил ни одной строки таблицы.
    # Правильный ответ — пустая сумма и причина, а не «Всего к оплате» из подвала:
    # итог включает перерасчёт прошлого периода, который бизнес не платит.
    result = recognize("water_real_20260523_201747_f96cd3fc_tesseract.txt")

    assert result.kind == "water"
    assert result.amount is None
    assert "water_utility_rows_4_7_not_found" in result.reasons
    assert "amount_not_found" in result.reasons


def test_synthetic_water_invoice_sums_rows_four_to_seven() -> None:
    # Синтетический счёт с чистым текстом: строки 1-3 дают 312,00, строки 4-7 — 624,00.
    # Итог документа (936,00) не должен появиться нигде.
    result = recognize("synthetic_water_utility_invoice.txt")

    assert result.kind == "water"
    assert result.amount == Decimal("624.00")
    assert result.raw["vat_amount"] == "104.00"
    assert result.raw["vat_mode"] == "included"
    assert result.document_number == "7788"
    assert result.document_date == date(2026, 3, 31)
    assert result.period_start == date(2026, 3, 1)
    assert result.period_end == date(2026, 3, 31)
    assert "по строкам 4-7" in result.raw["payment_purpose_suggestion"]
    assert result.confidence >= 0.65


def test_water_invoice_without_service_period_asks_for_human() -> None:
    # Период — не украшение: расход коммуналки признаётся в месяце потребления. Без него
    # документ обязан уйти человеку, а не быть проведённым по дате счёта.
    text = load("synthetic_water_utility_invoice.txt").replace(" за март месяц 2026 г.", "")

    result = recognize_utility_document(text)

    assert result is not None
    assert result.period_start is None
    assert result.period_end is None
    assert result.document_date == date(2026, 3, 31)
    assert "service_period_not_found" in result.reasons


# --- Электричество: факт и аванс ---------------------------------------------


def test_electricity_actual_act_separates_period_expense_from_amount_due() -> None:
    # Расход января — 73 000 (энергия + потери) целиком, а в банк уходит 22 000:
    # 51 000 уже внесено авансом в январе. Смешать их значит либо задвоить расход,
    # либо заплатить повторно.
    result = recognize("synthetic_electricity_actual_act.txt")

    assert result.kind == "electricity"
    assert result.raw["electricity_act_kind"] == "actual"
    assert result.period_start == date(2026, 1, 1)
    assert result.period_end == date(2026, 1, 31)
    assert result.document_date == date(2026, 2, 16)
    assert result.raw["electricity_period_amount"] == "73000.00"
    assert result.raw["electricity_paid_advance_amount"] == "51000.00"
    assert result.raw["electricity_amount_due"] == "22000.00"
    assert result.amount == Decimal("22000.00")
    assert "electricity_pair_waiting_for_advance" in result.reasons


def test_electricity_advance_act_is_for_the_next_month() -> None:
    # Аванс выставляется актом от 16.02 за ФЕВРАЛЬ — год берётся из даты акта, а месяц
    # периода следует за ней. Обратный сдвиг (как у факта) здесь был бы ошибкой.
    result = recognize("synthetic_electricity_advance_act.txt")

    assert result.kind == "electricity"
    assert result.raw["electricity_act_kind"] == "advance"
    assert result.period_start == date(2026, 2, 1)
    assert result.period_end == date(2026, 2, 28)
    assert result.raw["electricity_advance_amount"] == "50000.00"
    assert result.amount == Decimal("50000.00")
    assert "electricity_pair_waiting_for_actual" in result.reasons


def test_electricity_actual_takes_row_total_not_unit_price() -> None:
    # Живой OCR акта: колонки съехали, а цена за кВт·ч (11.84) стоит перед суммой строки.
    # Порог отсечения по величине не даёт принять цену за сумму. Строки «К оплате» в акте
    # нет — сумма к платежу остаётся пустой с причиной, расход периода при этом известен.
    text = """
ИНН 614314309921
АКТ
приема-передачи электроэнергии
от 18.03.2026r. по договору N 13
Юрьевна и поставщиком ИП Гордеев BA
Наименование работ, услуг
Кол-во | Ex изм. | Цена Cro
`Электрознергия за февраль 7557 кВтч 11.84 89483.00
‚Потеря февраль 43 [тя 11.84 [489000
ИТОГО | 94373.00.
Итого: 94373_00p_
Всего: 94373.00р_
"""

    result = recognize_utility_document(text, {"source_file": "electricity_actual_noisy_ocr.txt"})

    assert result is not None
    assert result.kind == "electricity"
    assert result.raw["electricity_act_kind"] == "actual"
    assert result.period_start == date(2026, 2, 1)
    assert result.period_end == date(2026, 2, 28)
    assert result.raw["electricity_period_amount"] == "94373.00"
    assert result.amount is None
    assert "electricity_amount_due_not_found" in result.reasons


def test_electricity_actual_infers_amount_due_from_paid_advance() -> None:
    # Строки «К оплате» нет, но есть «50000p -19.02.2026» — внесённый аванс с датой.
    # Остаток выводится вычитанием и только когда он положительный.
    text = """
ИНН 614314309921
АКТ
приема-передачи электроэнергии
от 18.03.2026г. по договору N 13
Электрознергия за февраль 7557 кВтч 11.84 89483.00
Потери февраль 413 кВтч 11.84 4890.00
ИТОГО 94373.00
Итого: 94373_00p_
Всего: 94373.00р_
50000p -19.02.2026
"""

    result = recognize_utility_document(text)

    assert result is not None
    assert result.raw["electricity_period_amount"] == "94373.00"
    assert result.raw["electricity_paid_advance_amount"] == "50000.00"
    assert result.amount == Decimal("44373.00")


def test_electricity_year_rolls_back_for_december_act_signed_in_january() -> None:
    # Акт подписан 15.01.2027, а период — декабрь. Без поправки на стык лет декабрь уехал
    # бы в будущее и расход сел бы не в тот год.
    text = """
ИНН 614314309921
АКТ
приема-передачи электроэнергии
от 15.01.2027г. по договору N 13
Электроэнергия за декабрь 7000 кВтч 10.00 70000,00
Потери декабрь 300 кВтч 10.00 3000,00
ИТОГО 73000,00
Оплачено: 51000р -20.12.2026
К оплате 22000р.
"""

    result = recognize_utility_document(text)

    assert result is not None
    assert result.period_start == date(2026, 12, 1)
    assert result.period_end == date(2026, 12, 31)


# --- Чужие документы ---------------------------------------------------------


def test_supplier_invoice_is_not_a_utility_document() -> None:
    # Обычный счёт поставщика: реквизиты, ИНН, сумма — всё на месте, и по одним лишь
    # баллам он дотянулся бы до парсера воды. Обязательная примета домена не даёт этому
    # случиться: иначе документ получил бы сумму «по строкам 4-7» из чужой таблицы.
    assert recognize_utility_document(load("synthetic_invoice.txt")) is None


def test_cash_receipt_is_not_a_utility_document() -> None:
    assert recognize_utility_document("Кассовый чек\nФН 1234567890123456\nИТОГ 500,00") is None


def test_empty_text_is_not_a_utility_document() -> None:
    assert recognize_utility_document("") is None
    assert recognize_utility_document("   \n\n  ") is None


def test_types_are_normalized_for_the_caller() -> None:
    # Контракт модуля: наружу отдаются Decimal и date, а не строки бота.
    result = recognize("synthetic_water_utility_invoice.txt")

    assert isinstance(result.amount, Decimal)
    assert isinstance(result.period_start, date)
    assert isinstance(result.period_end, date)
    assert isinstance(result.document_date, date)
    assert isinstance(result.document_number, str)
    assert isinstance(result.confidence, float)
    assert result.raw["source_file"] == "synthetic_water_utility_invoice.txt"
