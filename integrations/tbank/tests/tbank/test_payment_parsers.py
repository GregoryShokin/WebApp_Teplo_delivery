from __future__ import annotations

import sys
import tempfile
import unittest
from importlib import import_module
from pathlib import Path
from typing import Any


SCRIPT_ROOT = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPT_ROOT))
payment_parsers = import_module("payment_parsers")
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "tbank"


class PaymentParserTests(unittest.TestCase):
    def test_invoice_parser_extracts_core_requisites(self) -> None:
        text = (FIXTURE_ROOT / "synthetic_invoice.txt").read_text(encoding="utf-8")

        result = payment_parsers.parse_text(text, metadata={"source_file": "synthetic_invoice.txt"})
        basis = result["payment_basis"]

        self.assertEqual(basis["source_type"], "invoice")
        self.assertEqual(basis["inn"], "7701234567")
        self.assertEqual(basis["kpp"], "770101001")
        self.assertEqual(basis["bank_bik"], "044525225")
        self.assertEqual(basis["bank_account"], "40702810900000012345")
        self.assertEqual(basis["corr_account"], "30101810400000000225")
        self.assertEqual(basis["amount"], "12345.67")
        self.assertEqual(basis["document_number"], "INV-42")
        self.assertEqual(basis["document_date"], "15.05.2026")
        self.assertFalse(result["requires_owner_review"])

    def test_water_utility_invoice_pays_only_rows_4_to_7(self) -> None:
        text = (FIXTURE_ROOT / "synthetic_water_utility_invoice.txt").read_text(encoding="utf-8")

        result = payment_parsers.parse_text(
            text,
            metadata={"source_file": "synthetic_water_utility_invoice.txt"},
        )
        basis = result["payment_basis"]

        self.assertEqual(basis["source_type"], "invoice")
        self.assertEqual(basis["parser_name"], "water_utility_invoice_v1")
        self.assertEqual(basis["counterparty_alias"], "Водоканал")
        self.assertEqual(
            basis["counterparty_name"],
            'Муниципальное унитарное предприятие муниципального образования "Город Тестск" "Водоканал"',
        )
        self.assertEqual(basis["inn"], "7800000000")
        self.assertEqual(basis["kpp"], "780001001")
        self.assertEqual(basis["bank_bik"], "044525999")
        self.assertEqual(basis["bank_account"], "40702810000000000999")
        self.assertEqual(basis["corr_account"], "30101810900000000999")
        self.assertEqual(basis["bank_name"], 'Филиал "Тестовый" Банка (ПАО) г. Тестоград')
        self.assertEqual(basis["amount"], "624.00")
        self.assertEqual(basis["vat_mode"], "included")
        self.assertEqual(basis["vat_amount"], "104.00")
        self.assertEqual(basis["document_type"], "Счет")
        self.assertEqual(basis["document_number"], "7788")
        self.assertEqual(basis["document_date"], "31.03.2026")
        self.assertEqual(basis["service_period_start"], "2026-03-01")
        self.assertEqual(basis["service_period_end"], "2026-03-31")
        self.assertEqual(basis["dds_article_candidate"], "Аренда торговых точек")
        self.assertEqual(basis["pnl_article_candidate"], "Коммунальные платежи")
        self.assertIn("строкам 4-7", basis["payment_purpose_suggestion"])
        self.assertFalse(result["requires_owner_review"])
        self.assertEqual(result["status"], "parsed_ready")

    def test_water_utility_invoice_tolerates_photo_ocr_table_noise(self) -> None:
        text = """
Счет N 9901 от 30.04.2026г. за апрель месяц 2026 г.
Поставщик: Муниципальное унитарное предприятие муниципального образования "Город
Тестск" "Водоканал", ИНН 7800000000, КПП 780001001
Филиал "Тестовый" Банка (ПАО) г. Тестоград БИК 044525999
Сч N 30101810900000000999
Сч N 40702810000000000999
Товары (работы, услуги) Сумма Сумма НДС Сумма с НДС
т Водоснабжение 4083 3035 102880 205,76 123456
2_ [Водоотведение ХВ 38 [us 39.47 1250.00 250,00 1500.00]
'3 [Плата за негативное воздействие. 38 мз. 18,42] 583,33 116,67, 700,00
4 [Водоснабжение 30283 3035] 1028.80) 205,76 7234.56)
5 [Водоотведение ХВ 38]мЗ) 3947 1250.00 250.00 7500.00
@_ [Плата за негативное воздействие 38] 18,42 583,33 116,67 700,00
7 [Сброс загрязняющих веществ сверх 38)u3 52.63 166667 333,33 200000
установленных нормативов состава сточных вод
Итого: 6 945,00
Сумма НДС: 1 390,00
Всего с НДС: 8 335,00
Всего к оплате: 8 335,00
"""

        result = payment_parsers.parse_text(text, metadata={"source_file": "water_photo_ocr_synthetic.txt"})
        basis = result["payment_basis"]

        self.assertEqual(basis["parser_name"], "water_utility_invoice_v1")
        self.assertEqual(basis["amount"], "5434.56")
        self.assertEqual(basis["vat_amount"], "905.76")
        self.assertFalse(result["requires_owner_review"])
        self.assertEqual(result["status"], "parsed_ready")

    def test_water_utility_invoice_never_uses_grand_total_when_rows_are_noisy(self) -> None:
        text = """
Филиал "Тестовый" Банка (ПАО) г. Тестоград БИК 044525999
Сч N 30101810900000000999
ИНН 7800000000 КПП 780001001 Сч N 40702810000000000999
Муниципальное унитарное предприятие муниципального образования "Город Тестск" "Водоканал"

Счет № 5484 от 30.04.2026г. за апрель месяц 2026 г.

Поставщик Муниципальное унитарное предприятие муниципального образования "Город
Тестск" "Водоканал", ИНН 7800000000, КПП 780001001

Товары (работы, услуги) Сумма Сумма НДС Сумма с НДС
т Водоснабжение ЕС 3635 464.18 32212 178630
2 [Водоотведение ХВ 38 [M3 91.57 1579.66 347.53) 92719]
'3 [Плата за негативное воздействие ECE 20,79) 790,02] 173,80 963,82
4 [Водоснабжение ЕС 3635 464.18 32212 178630
5 [Водоотведение ХВ 38 |мЗ) 4157 1579.56] 347.53 7927.18
5 [Плата sa негативное воздействие 36 [M3 20,79 790.02] 173,80 963,82
7 [Сброс загрязняющих веществ сверх 36 |u3 8314 315932] 85.05 385437
установленных нормативов состава сточных вод

Итого: 10 827,04
Сумма НДС: 2381,95
Всего с НДС: 13 208,99
Всего к оплате: 13 208,99
"""

        result = payment_parsers.parse_text(text, metadata={"source_file": "water_realistic_ocr.txt"})
        basis = result["payment_basis"]

        self.assertEqual(basis["parser_name"], "water_utility_invoice_v1")
        self.assertEqual(basis["amount"], "8531.68")
        self.assertEqual(basis["vat_amount"], "1538.50")
        self.assertNotEqual(basis["amount"], "13208.99")

    def test_water_utility_invoice_reconciles_selected_rows_with_table_total(self) -> None:
        text = """
Счет № 3784 от 31.03.2026г. за март месяц 2026 г.
Поставщик: Муниципальное унитарное предприятие муниципального образования "Город
Тестск" "Водоканал", ИНН 7800000000, КПП 780001001
Филиал "Тестовый" Банка (ПАО) г. Тестоград. БИК 044525999
сч. № 30101810900000000999
Сч. № 40702810102300005667
Товары (работы, услуги) Сумма Сумма НДС Сумма с НДС
1 [Водоснабжение ЭМЗ 3635 348.59] 296.69 1645.28
2 [Водоотведение XB 35 м3. 41,57) 1 454.95] 320,09) 1 775,04
3 [Плата за негативное воздействие 35 м3. 20,79) 727,65) 160,08 | 887,73
4 [Водоснабжение 39.223 3635 1425.65) 313.64 173929
5_ [Водоотведение XB. 37 м3 41.87 1538.09] 33838 1876.47
6 [Плата за негативное воздействие 37]u3 20,79 768,23) 169.23 938.45
7 [Сброс загрязняющих веществ сверх 37|мз 83,14 3076.18 676,76 3752.4
установленных нормативов состава сточных вод
Итого: 10 340,34
Сумма НДС: 2274,87
Всего с НДС: 12 615,21
Всего к оплате: 12 615,21
"""

        result = payment_parsers.parse_text(text, metadata={"source_file": "water_march_realistic_ocr.txt"})
        basis = result["payment_basis"]

        self.assertEqual(basis["parser_name"], "water_utility_invoice_v1")
        self.assertEqual(basis["amount"], "8307.16")
        self.assertEqual(basis["vat_amount"], "1498.01")
        self.assertNotEqual(basis["amount"], "12615.21")

    def test_water_utility_invoice_tolerates_ocr_document_date_separator(self) -> None:
        text = (FIXTURE_ROOT / "synthetic_water_utility_invoice.txt").read_text(encoding="utf-8")
        noisy_text = text.replace("31.03.2026г.", "31.03,2026r.")

        result = payment_parsers.parse_text(
            noisy_text,
            metadata={"source_file": "synthetic_water_utility_invoice_ocr_date.txt"},
        )
        basis = result["payment_basis"]

        self.assertEqual(basis["parser_name"], "water_utility_invoice_v1")
        self.assertEqual(basis["document_date"], "31.03.2026")
        self.assertEqual(basis["service_period_start"], "2026-03-01")
        self.assertEqual(basis["service_period_end"], "2026-03-31")
        self.assertEqual(result["status"], "parsed_ready")

    def test_water_utility_invoice_without_service_period_requires_owner_review(self) -> None:
        text = (FIXTURE_ROOT / "synthetic_water_utility_invoice.txt").read_text(encoding="utf-8")
        no_period_text = text.replace(" за март месяц 2026 г.", "")

        result = payment_parsers.parse_text(
            no_period_text,
            metadata={"source_file": "synthetic_water_utility_invoice_no_period.txt"},
        )
        basis = result["payment_basis"]

        self.assertEqual(basis["parser_name"], "water_utility_invoice_v1")
        self.assertEqual(basis["document_date"], "31.03.2026")
        self.assertEqual(basis["service_period_start"], "")
        self.assertEqual(basis["service_period_end"], "")
        self.assertTrue(result["requires_owner_review"])
        self.assertEqual(result["status"], "owner_review")
        self.assertIn("service_period_not_found", basis["owner_review_reasons"])

    def test_electricity_actual_and_advance_are_linked_for_bank_amount(self) -> None:
        actual_text = (FIXTURE_ROOT / "synthetic_electricity_actual_act.txt").read_text(encoding="utf-8")
        advance_text = (FIXTURE_ROOT / "synthetic_electricity_advance_act.txt").read_text(encoding="utf-8")

        actual_result = payment_parsers.parse_text(
            actual_text,
            metadata={"source_file": "synthetic_electricity_actual_act.txt"},
        )
        advance_result = payment_parsers.parse_text(
            advance_text,
            metadata={"source_file": "synthetic_electricity_advance_act.txt"},
        )

        actual_basis = actual_result["payment_basis"]
        advance_basis = advance_result["payment_basis"]
        self.assertEqual(actual_basis["parser_name"], "electricity_act_v1")
        self.assertEqual(actual_basis["electricity_act_kind"], "actual")
        self.assertEqual(actual_basis["service_period_start"], "2026-01-01")
        self.assertEqual(actual_basis["electricity_period_amount"], "73000.00")
        self.assertEqual(actual_basis["electricity_paid_advance_amount"], "51000.00")
        self.assertEqual(actual_basis["electricity_amount_due"], "22000.00")
        self.assertEqual(actual_basis["amount"], "22000.00")
        self.assertIn("electricity_pair_waiting_for_advance", actual_basis["owner_review_reasons"])

        self.assertEqual(advance_basis["electricity_act_kind"], "advance")
        self.assertEqual(advance_basis["service_period_start"], "2026-02-01")
        self.assertEqual(advance_basis["electricity_advance_amount"], "50000.00")
        self.assertIn("electricity_pair_waiting_for_actual", advance_basis["owner_review_reasons"])

        with tempfile.TemporaryDirectory() as tmpdir:
            store = payment_parsers.PaymentIntakeStore(Path(tmpdir) / "intake.sqlite3")
            actual_ids = store.save_parse_result(
                actual_result,
                text=actual_text,
                metadata={"source_file": "synthetic_electricity_actual_act.txt"},
            )
            accrual_before_pair = store.get_expense_accrual_by_parsed_id(actual_ids["parsed_id"])
            advance_ids = store.save_parse_result(
                advance_result,
                text=advance_text,
                metadata={"source_file": "synthetic_electricity_advance_act.txt"},
            )

            merged_candidate = store.get_candidate(actual_ids["candidate_id"])
            merged_basis = merged_candidate["payment_basis"]
            linked_advance = store.get_candidate(advance_ids["candidate_id"])
            accrual_after_pair = store.get_expense_accrual_by_parsed_id(actual_ids["parsed_id"])
            accruals = store.list_expense_accruals()

        self.assertIsNotNone(accrual_before_pair)
        self.assertEqual(accrual_before_pair["status"], "parsed_ready")
        self.assertEqual(accrual_before_pair["amount"], "73000.00")
        self.assertEqual(accrual_before_pair["service_period_start"], "2026-01-01")
        self.assertEqual(accrual_before_pair["payment_candidate_id"], actual_ids["candidate_id"])
        self.assertEqual(merged_basis["electricity_pair_status"], "merged")
        self.assertEqual(merged_basis["amount"], "72000.00")
        self.assertEqual(merged_basis["electricity_payment_amount"], "72000.00")
        self.assertEqual(merged_basis["electricity_period_amount"], "73000.00")
        self.assertIn("аванс за февраль 2026", merged_basis["payment_purpose_suggestion"])
        self.assertNotIn("electricity_pair_waiting_for_advance", merged_basis["owner_review_reasons"])
        self.assertTrue(merged_candidate["requires_owner_review"])
        self.assertIn(
            "missing_required_fields:bank_bik,bank_account",
            merged_basis["owner_review_reasons"],
        )
        self.assertEqual(linked_advance["payment_basis"]["electricity_pair_status"], "linked")
        self.assertIn(
            "electricity_advance_linked_to_actual_candidate",
            linked_advance["payment_basis"]["owner_review_reasons"],
        )
        self.assertIsNotNone(accrual_after_pair)
        self.assertEqual(accrual_after_pair["amount"], "73000.00")
        self.assertEqual(accrual_after_pair["status"], "parsed_ready")
        self.assertEqual(accrual_after_pair["accrual_basis"]["expense_kind"], "electricity_period_expense")
        self.assertEqual(accrual_after_pair["accrual_basis"]["payment_amount_candidate"], "72000.00")
        self.assertEqual(len(accruals), 1)

    def test_electricity_actual_kind_tolerates_noisy_losses_ocr(self) -> None:
        text = (FIXTURE_ROOT / "synthetic_electricity_actual_act.txt").read_text(encoding="utf-8")
        noisy_text = text.replace("Потери январь", "Потерп январь")

        result = payment_parsers.parse_text(
            noisy_text,
            metadata={"source_file": "synthetic_electricity_actual_act.txt"},
        )
        basis = result["payment_basis"]

        self.assertEqual(basis["parser_name"], "electricity_act_v1")
        self.assertEqual(basis["electricity_act_kind"], "actual")
        self.assertEqual(basis["electricity_period_amount"], "73000.00")
        self.assertEqual(basis["electricity_amount_due"], "22000.00")

    def test_electricity_actual_tolerates_ocr_document_date_separator(self) -> None:
        text = (FIXTURE_ROOT / "synthetic_electricity_actual_act.txt").read_text(encoding="utf-8")
        noisy_text = text.replace("16.02.2026г.", "16.02,2026r.")

        result = payment_parsers.parse_text(
            noisy_text,
            metadata={"source_file": "synthetic_electricity_actual_ocr_date.txt"},
        )
        basis = result["payment_basis"]

        self.assertEqual(basis["parser_name"], "electricity_act_v1")
        self.assertEqual(basis["document_date"], "16.02.2026")
        self.assertEqual(basis["service_period_start"], "2026-01-01")
        self.assertEqual(basis["service_period_end"], "2026-01-31")
        self.assertEqual(basis["electricity_amount_due"], "22000.00")

    def test_electricity_actual_without_service_period_requires_owner_review(self) -> None:
        text = (FIXTURE_ROOT / "synthetic_electricity_actual_act.txt").read_text(encoding="utf-8")
        no_period_text = text.replace("Электроэнергия за январь", "Электроэнергия").replace(
            "Потери январь",
            "Потери",
        )

        result = payment_parsers.parse_text(
            no_period_text,
            metadata={"source_file": "synthetic_electricity_actual_no_period.txt"},
        )
        basis = result["payment_basis"]

        self.assertEqual(basis["parser_name"], "electricity_act_v1")
        self.assertEqual(basis["document_date"], "16.02.2026")
        self.assertEqual(basis["service_period_start"], "")
        self.assertEqual(basis["service_period_end"], "")
        self.assertTrue(result["requires_owner_review"])
        self.assertEqual(result["status"], "owner_review")
        self.assertIn("service_period_not_found", basis["owner_review_reasons"])

    def test_electricity_actual_uses_total_not_unit_price_from_noisy_ocr(self) -> None:
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

        result = payment_parsers.parse_text(text, metadata={"source_file": "electricity_actual_noisy_ocr.txt"})
        basis = result["payment_basis"]

        self.assertEqual(basis["electricity_act_kind"], "actual")
        self.assertEqual(basis["service_period_start"], "2026-02-01")
        self.assertEqual(basis["service_period_end"], "2026-02-28")
        self.assertEqual(basis["electricity_period_amount"], "94373.00")
        self.assertEqual(basis["amount"], "")
        self.assertIn("electricity_amount_due_not_found", basis["owner_review_reasons"])

    def test_electricity_actual_separates_payment_due_from_period_expense(self) -> None:
        text = """
ИНН 614314309921
АКТ
приема-передачи электроэнергии
от 18.03.2026г. по договору N 13
Электроэнергия за февраль 7557 кВтч 11.84 89483,00
Потери февраль 413 кВтч 11.84 4890,00
ИТОГО 94373,00
Итого: 94373,00р.
Всего: 94373,00р.
Оплачено: 50000р -19.02.2026
К оплате 44373р.
"""

        result = payment_parsers.parse_text(text, metadata={"source_file": "electricity_actual_due.txt"})
        basis = result["payment_basis"]

        self.assertEqual(basis["electricity_period_amount"], "94373.00")
        self.assertEqual(basis["electricity_paid_advance_amount"], "50000.00")
        self.assertEqual(basis["electricity_amount_due"], "44373.00")
        self.assertEqual(basis["amount"], "44373.00")

    def test_electricity_actual_can_infer_due_from_total_and_dated_paid_amount(self) -> None:
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

        result = payment_parsers.parse_text(text, metadata={"source_file": "electricity_actual_inferred_due.txt"})
        basis = result["payment_basis"]

        self.assertEqual(basis["electricity_period_amount"], "94373.00")
        self.assertEqual(basis["electricity_paid_advance_amount"], "50000.00")
        self.assertEqual(basis["electricity_amount_due"], "44373.00")
        self.assertEqual(basis["amount"], "44373.00")

    def test_electricity_pair_does_not_merge_without_actual_amount_due(self) -> None:
        actual_text = """
ИНН 614314309921
АКТ
приема-передачи электроэнергии
от 16.02.2026г. по договору N 13
Электроэнергия за январь 7000 кВтч 10.00 70000,00
Потери январь 300 кВтч 10.00 3000,00
ИТОГО 73000,00
Итого: 73000,00р.
Всего: 73000,00р.
"""
        advance_text = (FIXTURE_ROOT / "synthetic_electricity_advance_act.txt").read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmpdir:
            store = payment_parsers.PaymentIntakeStore(Path(tmpdir) / "intake.sqlite3")
            actual_ids = store.save_parse_result(
                payment_parsers.parse_text(actual_text, metadata={"source_file": "electricity_actual_without_due.txt"}),
                text=actual_text,
                metadata={"source_file": "electricity_actual_without_due.txt"},
            )
            store.save_parse_result(
                payment_parsers.parse_text(advance_text, metadata={"source_file": "synthetic_electricity_advance_act.txt"}),
                text=advance_text,
                metadata={"source_file": "synthetic_electricity_advance_act.txt"},
            )
            actual_candidate = store.get_candidate(actual_ids["candidate_id"])

        self.assertEqual(actual_candidate["payment_basis"]["electricity_pair_status"], "unpaired")
        self.assertEqual(actual_candidate["payment_basis"]["amount"], "")
        self.assertTrue(
            any(
                reason.startswith("missing_required_fields:")
                and "amount" in reason
                for reason in actual_candidate["payment_basis"]["owner_review_reasons"]
            )
        )

    def test_electricity_pair_tolerates_ocr_date_and_amount_separators(self) -> None:
        actual_text = (FIXTURE_ROOT / "synthetic_electricity_actual_act.txt").read_text(encoding="utf-8")
        advance_text = (FIXTURE_ROOT / "synthetic_electricity_advance_act.txt").read_text(encoding="utf-8")
        noisy_advance_text = (
            advance_text.replace("от 16.02.2026г.", "от 16.02,2026r.")
            .replace("50000,00", "50000_00")
            .replace("Всего  50000_00р.", "Всего  50000:00р.")
        )

        advance_result = payment_parsers.parse_text(
            noisy_advance_text,
            metadata={"source_file": "synthetic_electricity_advance_ocr.txt"},
        )
        advance_basis = advance_result["payment_basis"]
        self.assertEqual(advance_basis["document_date"], "16.02.2026")
        self.assertEqual(advance_basis["electricity_act_kind"], "advance")
        self.assertEqual(advance_basis["electricity_advance_amount"], "50000.00")

        with tempfile.TemporaryDirectory() as tmpdir:
            store = payment_parsers.PaymentIntakeStore(Path(tmpdir) / "intake.sqlite3")
            actual_ids = store.save_parse_result(
                payment_parsers.parse_text(actual_text, metadata={"source_file": "synthetic_electricity_actual_act.txt"}),
                text=actual_text,
                metadata={"source_file": "synthetic_electricity_actual_act.txt"},
            )
            store.save_parse_result(
                advance_result,
                text=noisy_advance_text,
                metadata={"source_file": "synthetic_electricity_advance_ocr.txt"},
            )
            merged_candidate = store.get_candidate(actual_ids["candidate_id"])

        self.assertEqual(merged_candidate["payment_basis"]["electricity_pair_status"], "merged")
        self.assertEqual(merged_candidate["payment_basis"]["electricity_payment_amount"], "72000.00")

    def test_receipt_without_requisites_requires_owner_review(self) -> None:
        text = "Кассовый чек\nФН 1234567890123456 ФД 12345\nИТОГ 500,00"

        result = payment_parsers.parse_text(text, metadata={"source_file": "receipt.txt"})

        self.assertEqual(result["payment_basis"]["source_type"], "receipt")
        self.assertTrue(result["requires_owner_review"])
        self.assertIn("receipt_requires_payment_context", result["owner_review_reasons"])


class WaterUtilityRealOcrGoldenTests(unittest.TestCase):
    """Golden tests against real tesseract OCR captured from Telegram-uploaded
    Волгодонск Водоканал invoices.

    Each fixture is the raw `tesseract <jpg> -l rus+eng` output for a real
    upload preserved under `research/private/tbank/payment_orders/uploads/<id>/`.
    They guard against regressions in the OCR-noise recovery paths:

      * `|` table border read as a leading `1` on the bank account.
      * Row-6 marker mis-read as `©` (or `5` for an earlier invoice).
      * Row-1 marker mis-read as `т`.
    """

    def _load(self, fixture: str) -> dict[str, Any]:
        text = (FIXTURE_ROOT / fixture).read_text(encoding="utf-8")
        return payment_parsers.parse_text(text, metadata={"source_file": fixture})

    def _assert_water_invoice_common(self, basis: dict[str, Any]) -> None:
        self.assertEqual(basis["parser_name"], "water_utility_invoice_v1")
        self.assertEqual(basis["counterparty_alias"], "Водоканал")
        self.assertEqual(basis["bank_account"], "40702810102300005667")
        self.assertEqual(basis["corr_account"], "30101810145250000411")
        self.assertEqual(basis["bank_bik"], "044525411")
        self.assertEqual(basis["inn"], "6143049157")

    def test_february_2026_with_garbled_row_six(self) -> None:
        # OCR mis-reads the row-6 marker as `©` *and* prepends `1` to the
        # bank account from a vertical table border. Both must be recovered.
        result = self._load("water_real_20260523_194915_cfb2a7fa.txt")
        basis = result["payment_basis"]
        self.assertEqual(result["status"], "parsed_ready")
        self.assertFalse(result["requires_owner_review"])
        self._assert_water_invoice_common(basis)
        self.assertEqual(basis["amount"], "10327.82")
        self.assertEqual(basis["vat_amount"], "1862.39")
        self.assertEqual(basis["document_number"], "1758")
        self.assertEqual(basis["document_date"], "28.02.2026")
        self.assertEqual(basis["service_period_start"], "2026-02-01")
        self.assertEqual(basis["service_period_end"], "2026-02-28")
        self.assertEqual(basis["kpp"], "614301001")

    def test_march_2026_kpp_recovered_cleanly(self) -> None:
        result = self._load("water_real_20260523_192709_ded644bc.txt")
        basis = result["payment_basis"]
        self.assertEqual(result["status"], "parsed_ready")
        self._assert_water_invoice_common(basis)
        self.assertEqual(basis["amount"], "8307.16")
        self.assertEqual(basis["vat_amount"], "1498.01")
        self.assertEqual(basis["document_number"], "3784")
        self.assertEqual(basis["document_date"], "31.03.2026")
        self.assertEqual(basis["service_period_start"], "2026-03-01")
        self.assertEqual(basis["kpp"], "614301001")

    def test_april_2026_with_duplicate_row_marker(self) -> None:
        # OCR for this invoice reads row 6's marker as `5` (a duplicate of
        # row 5) — the parser must re-slot it via its `negative_impact` label.
        result = self._load("water_real_20260523_192636_b198d13d.txt")
        basis = result["payment_basis"]
        self.assertEqual(result["status"], "parsed_ready")
        self._assert_water_invoice_common(basis)
        self.assertEqual(basis["amount"], "8531.68")
        self.assertEqual(basis["vat_amount"], "1538.50")
        self.assertEqual(basis["document_number"], "5484")
        self.assertEqual(basis["document_date"], "30.04.2026")
        self.assertEqual(basis["service_period_start"], "2026-04-01")

    def test_january_2026_tesseract_fails_then_vision_column_major_recovers(self) -> None:
        # Telegram-compressed image where tesseract cannot extract any of
        # the table rows: most label rows have no money tokens at all, so
        # neither the numbered-row collector nor the cross-check fallback
        # can recover. The orchestrator must fall through to macOS Vision,
        # whose column-major output the parser handles via a dedicated path.
        tesseract_result = self._load("water_real_20260523_201747_f96cd3fc_tesseract.txt")
        self.assertEqual(tesseract_result["status"], "owner_review")
        self.assertIn("amount", tesseract_result["missing_required_fields"])

        vision_result = self._load("water_real_20260523_201747_f96cd3fc_vision.txt")
        basis = vision_result["payment_basis"]
        self.assertEqual(vision_result["status"], "parsed_ready")
        self._assert_water_invoice_common(basis)
        self.assertEqual(basis["amount"], "9878.79")
        self.assertEqual(basis["vat_amount"], "1781.43")
        self.assertEqual(basis["document_number"], "156")
        self.assertEqual(basis["document_date"], "31.01.2026")
        self.assertEqual(basis["service_period_start"], "2026-01-01")
        self.assertEqual(basis["service_period_end"], "2026-01-31")


if __name__ == "__main__":
    unittest.main()
