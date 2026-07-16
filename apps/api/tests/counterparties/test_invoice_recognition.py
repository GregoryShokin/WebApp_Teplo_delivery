"""Детерминированное распознавание реальных макетов счетов («Страница на оплату»).

Тексты — сокращённые, но структурно точные выдержки из настоящих PDF (iiko, Стартер, ЛЕММА),
полученных на личную почту владельца. Проверяем: тип документа (счёт vs акт/УПД), сумму,
реквизиты получателя (с разводкой нашего и чужого к/с по БИК) и разведение продуктов iiko.
Чистые юнит-тесты — БД не нужна.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.services.invoice_recognition import deterministic_recognize

# --- iiko: «Курьерика» (-лсп) и лицензия iikoCloud (-лк); один поставщик, один счёт ---------

IIKO_COURIERICA = """\
Внимание! Оплата данного счета означает согласие с условиями поставки товара.
Филиал "Корпоративный" ПАО "Совкомбанк" г. Москва БИК 044525360
Сч. № 30101810445250000360
Банк получателя
ИНН 1655166016 КПП  772601001 Сч. № 40702810312010245858
АО "АЙКО"
Получатель
Счет на оплату № 040426-39627-лсп  от 04 апреля 2026 г.
Покупатель: ИП Шокина Кристина Юрьевна, ИНН 890307589201
Комментарий: Лицензии iiko is-175038/2024 (Foodmarket Тепло, ЦО)
1 Лицензия на ПО Курьерика Курьер 1 мес 3 540,00 3 540,00
2 Лицензия на ПО Курьерика Логист 1 мес 720,00 720,00
Итого: 4 260,00
Всего к оплате: 4 260,00
"""

IIKO_LICENSE = """\
Филиал "Корпоративный" ПАО "Совкомбанк" г. Москва БИК 044525360
Сч. № 30101810445250000360
Банк получателя
ИНН 1655166016 КПП  772601001 Сч. № 40702810312010245858
АО "АЙКО"
Получатель
Счет на оплату № 040426-2244-лк    от 04 апреля 2026 г.
Покупатель: ИП Шокина Кристина Юрьевна, ИНН 890307589201
Комментарий: Лицензии iikoCloud ic-109122/2023 (Foodmarket Тепло, ЦО)
1 Лицензия на ПО iikoCloud Enterprise (1мес) 1 шт 16 430,00 16 430,00
Всего к оплате: 16 430,00
"""

# Передаточный акт iiko («Документы для ИП…»): НЕ счёт на оплату, символическая сумма.
IIKO_ACT = """\
Лицензиар
АО "АЙКО", ИНН 1655166016, р/с 40702810312010245858, в банке Филиал "Корпоративный"
ПАО "Совкомбанк", БИК 044525360, к/с 30101810445250000360
организация, адрес, телефон, факс, банковские реквизиты
Лицензиат ИП Шокина Кристина Юрьевна, ИНН 890307589201, р/с 40802810100002438573,
в банке АО "ТБанк", БИК 044525974, к/с 30101810145250000974
организация, адрес, телефон, факс, банковские реквизиты
Сумма 1,00
"""

# --- Стартер = ООО «Назад в будущее»: счёт (без «№»!) + УПД (закрывающий) -------------------

STARTER_INVOICE = """\
ООО «Назад в будущее»
Образец заполнения платежного поручения
ФИЛИАЛ "САНКТ-ПЕТЕРБУРГСКИЙ"АО "АЛЬФА-БАНК"
БИК 044030786
Банк получателя Сч.№ 30101810600000000786
ИНН 7839494297 КПП 781101001 Сч.№ 40702810232060001962
ООО «Назад в будущее»
Получатель
Счет 0000-001175 от 31 марта 2026 г.
Получатель: ИП ШОКИНА КРИСТИНА ЮРЬЕВНА, ИНН: 890307589201
1 Вознаграждение за использование Платформы за Март 2026 г. 98 092,00 - 98 092,00
Всего к оплате: 98 092,00
"""

STARTER_UPD = """\
Универсальный
передаточный
документ
Счет-фактура № 1181 от 31 марта 2026 г. (1)
Продавец: ООО "НАЗАД В БУДУЩЕЕ" (2) Покупатель: ШОКИНА КРИСТИНА ЮРЬЕВНА ИП (6)
ИНН/КПП продавца: 7839494297/781101001 (2б) ИНН/КПП покупателя: 890307589201/ (6б)
"""

# --- ЛЕММА: счёт-договор с «Лицензиар/Лицензиат» — валидный счёт; к/с поставщика и наш ------

LEMMA_INVOICE = """\
СЧЕТ-ДОГОВОР № 52482/1/У от 10 марта 2026
Лицензиар Общество с ограниченной ответственностью "ЛЕММА"
ИНН 6168118525, КПП 616801001
Реквизиты: ООО "Банк Точка" р/с 40702810401500157556  БИК 044525104
к/с 30101810745374525104
Лицензиат ИП Шокина Кристина Юрьевна
ИНН 890307589201, КПП
Реквизиты: АО "Тинькофф Банк" р/с 40802810100002438573  БИК 044525974
к/с 30101810145250000974
1 Программа для ЭВМ "Лемма.Поддержка" за период: апрель 2026 г. 1 3 700,00 3 700,00
Всего к оплате: 3 700,00
"""


def test_iiko_courierica_invoice():
    rec = deterministic_recognize(IIKO_COURIERICA)
    assert rec.document_kind == "invoice"
    assert rec.is_payment_invoice is True
    assert rec.amount == Decimal("4260.00")
    assert rec.inn == "1655166016"
    assert rec.bank_acnt == "40702810312010245858"
    assert rec.corr_account == "30101810445250000360"
    assert rec.bank_bik == "044525360"
    assert rec.invoice_number == "040426-39627-лсп"
    assert rec.product_hint == "courierica"


def test_iiko_license_invoice():
    rec = deterministic_recognize(IIKO_LICENSE)
    assert rec.document_kind == "invoice"
    assert rec.amount == Decimal("16430.00")
    assert rec.product_hint == "iiko_license"
    assert rec.bank_acnt == "40702810312010245858"


def test_iiko_act_is_not_invoice():
    rec = deterministic_recognize(IIKO_ACT)
    # Передаточный акт не должен материализоваться в счёт к оплате, даже имея сумму и реквизиты.
    assert rec.document_kind == "act"
    assert rec.is_payment_invoice is False


def test_starter_invoice_number_without_hash():
    rec = deterministic_recognize(STARTER_INVOICE)
    assert rec.document_kind == "invoice"
    assert rec.amount == Decimal("98092.00")
    assert rec.inn == "7839494297"
    assert rec.invoice_number == "0000-001175"  # формат «Счет <номер> от», без «№»
    assert rec.bank_acnt == "40702810232060001962"
    assert rec.corr_account == "30101810600000000786"
    assert rec.service_period_start == date(2026, 3, 1)
    assert rec.service_period_end == date(2026, 3, 31)
    assert rec.service_period_ambiguous is False


def test_starter_upd_is_not_invoice():
    rec = deterministic_recognize(STARTER_UPD)
    assert rec.document_kind == "upd"
    assert rec.is_payment_invoice is False


def test_lemma_invoice_with_licensor_wording():
    rec = deterministic_recognize(LEMMA_INVOICE)
    # «Лицензиар/Лицензиат» сами по себе НЕ делают документ актом — это валидный счёт-договор.
    assert rec.document_kind == "invoice"
    assert rec.inn == "6168118525"
    assert rec.amount == Decimal("3700.00")
    # р/с и к/с — поставщика, НЕ наши (наш р/с 40802…, к/с …0974 рядом с нашим ИНН).
    assert rec.bank_acnt == "40702810401500157556"
    assert rec.corr_account == "30101810745374525104"  # к/с по БИК 044525104, а не наш …0974
    assert rec.service_period_start == date(2026, 4, 1)
    assert rec.service_period_end == date(2026, 4, 30)


def test_explicit_service_period_range():
    rec = deterministic_recognize(
        IIKO_LICENSE + "\nПериод лицензии: с 01.08.2026 по 31.08.2026"
    )
    assert rec.service_period_start == date(2026, 8, 1)
    assert rec.service_period_end == date(2026, 8, 31)
    assert rec.service_period_source == "document_range"


def test_service_period_can_come_from_email_subject():
    rec = deterministic_recognize(
        IIKO_LICENSE,
        context_text="Лицензия за август 2026 — счёт 040426-2244-лк.pdf",
    )
    assert rec.service_period_start == date(2026, 8, 1)
    assert rec.service_period_end == date(2026, 8, 31)
    assert rec.service_period_source == "subject_month"


def test_multiple_service_periods_require_manual_review():
    rec = deterministic_recognize(
        LEMMA_INVOICE + "\nДополнительная услуга за май 2026 г."
    )
    assert rec.service_period_ambiguous is True
    assert rec.service_period_start is None
    assert rec.service_period_end is None
    assert rec.service_period_candidates == [
        (date(2026, 4, 1), date(2026, 4, 30)),
        (date(2026, 5, 1), date(2026, 5, 31)),
    ]


def test_empty_text_is_unknown():
    rec = deterministic_recognize("")
    assert rec.document_kind == "unknown"
    assert rec.is_payment_invoice is False


# --- Корпоративная почта: Охрана Юг, Спецавто Юг, DocsInbox -----------------------------------

OHRANA_INVOICE = """\
ЮГО-ЗАПАДНЫЙ БАНК ПАО СБЕРБАНК БИК 046015602
Сч. № 30101810600000000602
Банк получателя
ИНН 6167107489 КПП 615501001 Сч. № 40702810152090084641
ООО "ЧОО "Охрана Юг"
Получатель
Счет на оплату № УТ-5442 от 4 мая 2026 г.
Поставщик: ООО "ЧОО "Охрана Юг", ИНН 6167107489, КПП 615501001
Покупатель: ИП Шокина Кристина Юрьевна, ИНН 890307589201
Всего к оплате: 2300,00
"""

SPECAVTO_INVOICE = """\
ФИЛИАЛ "РОСТОВСКИЙ" АО "АЛЬФА-БАНК" БИК 046015207
Сч. № 30101810500000000207
Банк получателя
ИНН 6167146456 КПП 616601001 Сч. № 40702810526000010881
ООО "СПЕЦАВТО ЮГ"
Получатель
Счет на оплату № УТ-3526 от 1 июня 2026 г.
Поставщик: ООО "СПЕЦАВТО ЮГ", ИНН 6167146456, КПП 616601001
Покупатель: ИП Шокина Кристина Юрьевна, ИНН 890307589201
Всего к оплате: 575,00
"""

# Акт выполненных работ Спецавто: НЕ счёт (нет «счёт на оплату», есть «о приёмке…»).
SPECAVTO_ACT = """\
Акт № УТ-3185 от 31 мая 2026 г.
о приемке выполненных работ
(оказанных услуг)
1 Услуги по техническому обслуживанию средств ОПТС 1 мес 500,00 500,00
Итого: 500,00
Всего оказано услуг 1, на сумму 500,00 руб.
"""

DOCSINBOX_INVOICE = """\
ООО "Банк Точка" БИК 044525104
Сч. № 30101810745374525104
Банк получателя
ИНН 7802193688 КПП 781101001 Сч. № 40702810303270004079
Общество с ограниченной ответственностью "ДОКСИНБОКС"
Получатель
Счет-оферта на право использования сервиса DocsInBox № 0006625289 от 20 мая 2026 г
Всего к оплате: 15580,00
Условия: стороны ежеквартально подписывают акт сверки взаимных расчётов.
"""

# Закрывающий акт DocsInbox: начинается со слова «Акт», но в теле ссылается на «счёт-оферту» —
# не должен из-за этого стать счётом (регрессия порядка проверок в классификаторе).
DOCSINBOX_ACT = """\
Акт приема-предоставления прав на использование программ
№407112 от 31 марта 2026 г.
Лицензиар: ООО "ДОКСИНБОКС", ИНН 7802193688, КПП 781101001
Лицензиат: ИП Шокина Кристина Юрьевна, ИНН 890307589201
Основание: Счет-оферта № 0006616604
Всего к оплате: 15580,00
"""


def test_ohrana_invoice():
    rec = deterministic_recognize(OHRANA_INVOICE)
    assert rec.document_kind == "invoice"
    assert rec.amount == Decimal("2300.00")
    assert rec.inn == "6167107489"
    assert rec.bank_acnt == "40702810152090084641"
    assert rec.corr_account == "30101810600000000602"  # к/с по БИК 046015602 (Сбербанк)
    assert rec.bank_bik == "046015602"


def test_specavto_invoice():
    rec = deterministic_recognize(SPECAVTO_INVOICE)
    assert rec.document_kind == "invoice"
    assert rec.amount == Decimal("575.00")
    assert rec.inn == "6167146456"
    assert rec.bank_acnt == "40702810526000010881"
    assert rec.corr_account == "30101810500000000207"  # к/с по БИК 046015207 (Альфа-Банк)
    assert rec.recipient_name == 'ООО "СПЕЦАВТО ЮГ"'


def test_specavto_act_is_not_invoice():
    rec = deterministic_recognize(SPECAVTO_ACT)
    assert rec.document_kind == "act"
    assert rec.is_payment_invoice is False


def test_docsinbox_subscription_invoice():
    rec = deterministic_recognize(DOCSINBOX_INVOICE)
    assert rec.document_kind == "invoice"
    assert rec.amount == Decimal("15580.00")
    assert rec.inn == "7802193688"
    assert rec.bank_acnt == "40702810303270004079"
    assert rec.corr_account == "30101810745374525104"  # к/с по БИК 044525104 (Банк Точка)


def test_docsinbox_act_referencing_invoice_stays_act():
    rec = deterministic_recognize(DOCSINBOX_ACT)
    # Заголовок «Акт …» определяет тип РАНЬШЕ маркера «счёт-оферта» в теле основания.
    assert rec.document_kind == "act"
    assert rec.is_payment_invoice is False


# --- Слияние det+LLM: спорность периода снимается, если LLM дал один период ------------------


def _fake_settings():
    from types import SimpleNamespace

    return SimpleNamespace(anthropic_api_key="k", invoice_recognition_min_confidence=0.7)


async def test_ambiguous_reset_when_llm_resolves_single_period(monkeypatch):
    """Det нашёл >1 период (спорно, дат нет); LLM дал один — спорность снимаем."""
    from app.services import invoice_recognition as ir

    det = ir.RecognizedInvoice()
    det.service_period_ambiguous = True
    det.notes = ["обнаружено несколько периодов оказания услуг — требуется ручной разбор"]

    llm = ir.RecognizedInvoice()
    llm.service_period_start = date(2026, 6, 1)
    llm.service_period_end = date(2026, 6, 30)
    llm.service_period_candidates = [(date(2026, 6, 1), date(2026, 6, 30))]
    llm.confidence = 0.9

    monkeypatch.setattr(ir, "extract_pdf_text", lambda pdf: "счёт с текстом")
    monkeypatch.setattr(ir, "deterministic_recognize", lambda text, context_text=None: det)

    async def _fake_llm(pdf, *, settings):
        return llm

    monkeypatch.setattr(ir, "llm_recognize", _fake_llm)

    result = await ir.recognize(b"pdf", settings=_fake_settings())
    assert result.service_period_ambiguous is False
    assert result.service_period_start == date(2026, 6, 1)
    assert not any("несколько периодов" in n for n in result.notes)


async def test_ambiguous_kept_when_llm_also_multiple(monkeypatch):
    """Если и LLM видит несколько кандидатов — спорность сохраняем, счёт идёт оператору."""
    from app.services import invoice_recognition as ir

    det = ir.RecognizedInvoice()
    det.service_period_ambiguous = True

    llm = ir.RecognizedInvoice()
    llm.service_period_start = date(2026, 6, 1)
    llm.service_period_end = date(2026, 6, 30)
    llm.service_period_candidates = [
        (date(2026, 6, 1), date(2026, 6, 30)),
        (date(2026, 5, 1), date(2026, 5, 31)),
    ]

    monkeypatch.setattr(ir, "extract_pdf_text", lambda pdf: "счёт с текстом")
    monkeypatch.setattr(ir, "deterministic_recognize", lambda text, context_text=None: det)

    async def _fake_llm(pdf, *, settings):
        return llm

    monkeypatch.setattr(ir, "llm_recognize", _fake_llm)

    result = await ir.recognize(b"pdf", settings=_fake_settings())
    assert result.service_period_ambiguous is True
