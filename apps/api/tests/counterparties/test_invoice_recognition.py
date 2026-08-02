"""Детерминированное распознавание реальных макетов счетов («Страница на оплату»).

Тексты — сокращённые, но структурно точные выдержки из настоящих PDF (iiko, Стартер, ЛЕММА),
полученных на личную почту владельца. Проверяем: тип документа (счёт vs акт/УПД), сумму,
реквизиты получателя (с разводкой нашего и чужого к/с по БИК) и разведение продуктов iiko.
Чистые юнит-тесты — БД не нужна.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.services.invoice_recognition import (
    RecognizedInvoice,
    _extract_service_periods,
    _confidence,
    _pick_number,
    deterministic_recognize,
)

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


def test_invoice_date_read_from_document_line():
    """Дата счёта — из строки «Счет на оплату № … от 04 апреля 2026 г.»."""
    rec = deterministic_recognize(IIKO_COURIERICA)
    assert rec.invoice_date == date(2026, 4, 4)


# Шапка унифицированного бланка: «от 05.01.2004» здесь — дата постановления Госкомстата,
# а не дата документа. 27.07.2026 такая ведомость встала в очередь оплат с датой 2004 года.
UNIFIED_FORM_HEADER = """\
Унифицированная форма № Т-53
Утверждена Постановлением Госкомстата
России от 05.01.2004 № 1
ПЛАТЕЖНАЯ ВЕДОМОСТЬ
Номер документа Дата составления Расчетный период
20-14 05.08.2026 01.07.2026 31.07.2026
Итого: 22696.00
"""


def test_form_approval_date_is_not_invoice_date():
    """Дата утверждения бланка датой документа не становится — лучше пусто, чем 2004 год."""
    rec = deterministic_recognize(UNIFIED_FORM_HEADER)
    assert rec.invoice_date is None


def test_invoice_date_survives_boilerplate_above_it():
    """Если ниже шапки есть настоящая «от <дата>» — берётся она, а не дата постановления."""
    rec = deterministic_recognize(
        UNIFIED_FORM_HEADER + "Счет на оплату № 12 от 05.08.2026 г.\n"
    )
    assert rec.invoice_date == date(2026, 8, 5)


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

    monkeypatch.setattr(ir, "extract_pdf_pages", lambda pdf: ["счёт с текстом"])
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

    monkeypatch.setattr(ir, "extract_pdf_pages", lambda pdf: ["счёт с текстом"])
    monkeypatch.setattr(ir, "deterministic_recognize", lambda text, context_text=None: det)

    async def _fake_llm(pdf, *, settings):
        return llm

    monkeypatch.setattr(ir, "llm_recognize", _fake_llm)

    result = await ir.recognize(b"pdf", settings=_fake_settings())
    assert result.service_period_ambiguous is True


# --- СДЭК: ПАКЕТ документов одним PDF (счёт + УПД + приложение к УПД) ------------------------
# Реальная структура вложения «Пакет СКБ-0437096 от 19_07_2026.pdf» (прод, 19.07.2026).

CDEK_PAGE_BILL = """\
Образец заполнения платежного поручения
КРАСНОДАРСКОЕ ОТДЕЛЕНИЕ N8619 ПАО СБЕРБАНК БИК 040349602
Сч. № 30101810100000000602
Банк получателя
ИНН 2370006152 КПП 237001001 Сч. № 40702810430000013829
ООО "СДЭК-СЛАВЯНСК"
Получатель
Оплата по счету № СКБ-0437096 от 19 июля 2026 г., за услуги доставки, в т.ч. НДС 1439,90.
Счет № Счет на оплату № СКБ-0437096 от 19 июля 2026 г.
Расшифровка счета: Документ Сумма, руб УПД № СКБ-0008640 от 19.07.2026 7 984,90
ИТОГО к оплате: № Наименование услуг НДС Сумма, руб 1 Услуги доставки 1 439,90 7 984,90
Итого с НДС 22%: 7 984,90
Всего наименований 1, на сумму 7 984,90 RUB.
"""

CDEK_PAGE_UPD = """\
Универсальный
передаточный
документ
Счет-фактура № СКБ-0008640 от 19 июля 2026 г. (1)
Продавец: ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ СДЭК-СЛАВЯНСК (2)
Покупатель: Индивидуальный предприниматель ШОКИНА КРИСТИНА ЮРЬЕВНА (6)
услуги доставки за период с 13.07.2026 по 19.07.2026 -- 6 545,00 без акциза 22% 1 439,90 7 984,90
Всего к оплате (9) 6 545,00 Х 1 439,90 7 984,90
"""

CDEK_PAGE_UPD_ANNEX = """\
Приложение 1
к УПД № СКБ-0008640 от 19.07.2026 г.
Услуги экспресс доставки по накладным
1 10294188371 14.07.2026 18.07.2026 ШОКИНА КРИСТИНА ЮРЬЕВНА 7893,40
Дополнительный сбор за объявленную стоимость 91,50 0,00 0,00 7984,90
Итого: 7893,40 91,50 0,00 0,00 7984,90 В том числе НДС 22% 1439,90
"""

CDEK_PACKAGE_PAGES = [CDEK_PAGE_BILL, CDEK_PAGE_UPD, CDEK_PAGE_UPD_ANNEX]


def test_cdek_package_splits_into_bill_and_closing_sections():
    """Приложение без своего заголовка прилипает к УПД, а не открывает третий документ."""
    from app.services.invoice_recognition import split_document_sections

    sections = split_document_sections(CDEK_PACKAGE_PAGES)
    assert [kind for kind, _ in sections] == ["invoice", "upd"]
    assert "Приложение 1" in sections[1][1]


def test_cdek_package_main_document_is_the_bill():
    """Главный документ пакета — счёт (его платят), УПД идёт спутником.

    Регресс на прод-баг: по склеенному тексту весь пакет опознавался как УПД (маркер
    «универсальный передаточный документ» со стр. 2 проверяется раньше маркеров счёта),
    счёт исчезал из очереди оплат, а кредиторка вставала на сумму строки реестра."""
    from app.services.invoice_recognition import deterministic_recognize_pages

    rec = deterministic_recognize_pages(CDEK_PACKAGE_PAGES)
    assert rec.document_kind == "invoice"
    assert rec.amount == Decimal("7984.90")  # не 7893,40 из приложения к УПД
    assert rec.invoice_number == "СКБ-0437096"
    assert rec.inn == "2370006152"
    assert rec.bank_acnt == "40702810430000013829"

    companion = rec.companion
    assert companion is not None
    assert companion.document_kind == "upd"
    assert companion.amount == Decimal("7984.90")  # итог с НДС, не 6 545,00 из колонки «без НДС»
    assert companion.invoice_number == "СКБ-0008640"
    assert companion.inn == "2370006152"  # унаследован от счёта: на листе УПД «ИНН/КПП продавца»


def test_single_document_pdf_keeps_one_recognition():
    """Обычный однодокументный файл спутника не получает — поведение не изменилось."""
    from app.services.invoice_recognition import deterministic_recognize_pages

    rec = deterministic_recognize_pages([IIKO_COURIERICA])
    assert rec.document_kind == "invoice"
    assert rec.companion is None
    assert rec.amount == Decimal("4260.00")


def test_upd_only_pdf_is_still_closing():
    """Пакет без счёта (только УПД) остаётся закрывающим документом, спутника нет."""
    from app.services.invoice_recognition import deterministic_recognize_pages

    rec = deterministic_recognize_pages([CDEK_PAGE_UPD, CDEK_PAGE_UPD_ANNEX])
    assert rec.document_kind == "upd"
    assert rec.companion is None


# --- Регрессы предделойного аудита (wf_8244f364) --------------------------------------------


def test_amount_does_not_glue_date_or_account_number():
    """Жадный шаблон денег склеивал соседние числа через пробел.

    «от 19.07.2026 7 984,90» читалось как одно число 20 267 984,90, а «Итого 4070…829 1 000,00»
    утаскивало в сумму 20-значный номер счёта. С максимумом по строке это давало бы платёж в
    десятки миллионов вместо тысяч."""
    from app.services.invoice_recognition import _pick_amount

    assert _pick_amount("ИТОГО к оплате УПД № СКБ-1 от 19.07.2026 7 984,90") == Decimal("7984.90")
    assert _pick_amount("Итого 40702810430000013829 1 000,00") == Decimal("1000.00")
    assert _pick_amount("Всего к оплате по счету от 01.08.2026 15 580,00") == Decimal("15580.00")
    # Обычные форматы не пострадали.
    assert _pick_amount("Всего к оплате: 98092,00") == Decimal("98092.00")
    assert _pick_amount("Итого 1 234 567,89") == Decimal("1234567.89")


def test_annex_referencing_invoice_stays_part_of_closing():
    """Приложение к УПД со ссылкой «Основание: Счет на оплату №…» — НЕ новый счёт.

    Иначе закрывающий документ разваливался на «счёт + спутник», приложение вставало в очередь
    оплат, и поставку можно было оплатить второй раз."""
    from app.services.invoice_recognition import (
        deterministic_recognize_pages,
        split_document_sections,
    )

    upd = (
        "Универсальный передаточный документ\n"
        "Счет-фактура № 123 от 19 июля 2026 г.\nПродавец: ООО Ромашка\nВсего к оплате 1 000,00"
    )
    annex = (
        "Приложение к УПД № 123\nОснование: Счет на оплату № 55 от 01.07.2026\nИтого 1 000,00"
    )
    assert [kind for kind, _ in split_document_sections([upd, annex])] == ["upd"]

    rec = deterministic_recognize_pages([upd, annex])
    assert rec.document_kind == "upd"
    assert rec.companion is None


def test_companion_keeps_its_own_amount_and_number():
    """Спутник не наследует сумму, номер и дату счёта.

    Наследование заводило кредиторку из суммы счёта (даже если в УПД своего итога нет) и ломало
    дедуп: тот же УПД, пришедший отдельным письмом, не схлопывался и удваивал долг."""
    from app.services.invoice_recognition import deterministic_recognize_pages

    bill = (
        "Образец заполнения платежного поручения\nИНН 2370006152 КПП 237001001\n"
        "ООО \"СДЭК-СЛАВЯНСК\"\nСчет на оплату № СКБ-0437096 от 19 июля 2026 г.\n"
        "Итого с НДС 22%: 7 984,90\n"
    )
    upd_without_total = (
        "Универсальный передаточный документ\n"
        "Счет-фактура № СКБ-0008640 от 19 июля 2026 г.\nПродавец: ООО СДЭК-СЛАВЯНСК\n"
    )
    rec = deterministic_recognize_pages([bill, upd_without_total])
    assert rec.amount == Decimal("7984.90")
    companion = rec.companion
    assert companion is not None
    assert companion.amount is None  # своего итога в УПД нет — чужой не подставляем
    assert companion.invoice_number == "СКБ-0008640"
    assert companion.inn == "2370006152"  # личность контрагента унаследована — это законно


# --- Бланк УПД: ИНН и продавец записаны иначе, чем в счёте ----------------------------------

UPD_ECOCENTER = """\
Универсальный передаточный документ
Счет-фактура № ВД-46890 от 31 июля 2026 г. (1)
Продавец: Общество с ограниченной ответственностью "ЭкоЦентр" (2) Покупатель: Индивидуальный
предприниматель Шокина Кристина Юрьевна
ИНН/КПП продавца: 3444177534/614343001 (2б) ИНН/КПП покупателя: 614307902094
1 Услуга по обращению с твердыми коммунальными отходами за июль 2026 г. по адресу
Всего к оплате (9) 3 348,22
Документ об отгрузке: Универсальный передаточный документ, №ВД-46890 от 31 июля 2026 г. (5а)
Дата отгрузки, передачи (сдачи) « 31 » июля 2026 [11]
"""

UPD_STARTER = """\
Универсальный передаточный документ
Счет-фактура № 2653 от 30 июня 2026 г. (1)
Продавец: ООО "НАЗАД В БУДУЩЕЕ" (2) Покупатель: ШОКИНА КРИСТИНА ЮРЬЕВНА ИП
ИНН/КПП продавца: 7839494297/781101001 (2б) ИНН/КПП покупателя: 614307902094
00-00000144 1 Вознаграждение за использование Платформы за
Июнь 2026 г.
Всего к оплате (9) 80 455,00
Дата отгрузки, передачи (сдачи) «30» июня 2026 г. [11]
"""


def test_upd_blank_yields_supplier_inn_not_ours() -> None:
    """В бланке УПД ИНН стоит как «ИНН/КПП продавца» — и брать надо продавца, а не покупателя.

    Пока эта форма не читалась, УПД от «ЭкоЦентра» и «Назад в будущее» приходили без ИНН:
    контрагент не находился, уверенность падала ниже порога, и закрывающий документ уходил в
    ручной разбор. Оба ИНН лежат в одной строке, поэтому важно, что наш отфильтрован.
    """
    eco = deterministic_recognize(UPD_ECOCENTER)
    assert eco.inn == "3444177534"
    assert eco.document_kind == "upd"
    assert eco.amount == Decimal("3348.22")
    # Период услуги — из текста строки, а не из даты документа.
    assert eco.service_period_start == date(2026, 7, 1)
    assert eco.service_period_end == date(2026, 7, 31)

    starter = deterministic_recognize(UPD_STARTER)
    assert starter.inn == "7839494297"
    assert starter.service_period_start == date(2026, 6, 1)
    assert starter.service_period_end == date(2026, 6, 30)


def test_upd_blank_yields_supplier_name_written_in_full() -> None:
    """Продавец в УПД пишется прописью — «Общество с ограниченной ответственностью».

    Аббревиатуры в бланке нет вовсе, и без полной формы имя поставщика не находилось: у
    «ЭкоЦентра» это стоило 0,1 уверенности и уводило документ из авто-приёма в ручной разбор.
    """
    eco = deterministic_recognize(UPD_ECOCENTER)
    assert eco.recipient_name is not None
    assert "ЭкоЦентр" in eco.recipient_name
    # Покупатель — это мы, его имя брать нельзя.
    assert "Шокина" not in eco.recipient_name


def test_amount_inn_and_name_reach_the_auto_intake_threshold() -> None:
    """Самое частое сочетание полей даёт РОВНО порог автозаведения, а не на йоту меньше.

    Уверенность складывалась из float-долей: 0.35 + 0.30 + 0.10 в двоичной арифметике даёт
    0.7499999999999999, и гейт ``< 0.75`` отправлял в ручной разбор полностью распознанный
    документ. На проде так висел УПД ЛЕММА № 24234 — сумма, ИНН и имя найдены, а документ
    в needs_review из-за разницы в одну триллионную.
    """
    rec = RecognizedInvoice(engine="deterministic")
    rec.amount = Decimal("24234.00")
    rec.inn = "7839494297"
    rec.recipient_name = 'ООО "ЛЕММА"'
    assert _confidence(rec) >= 0.75


def test_closing_document_number_is_extracted() -> None:
    """У акта и УПД номер извлекается — иначе дедупликация не схлопывает дубли.

    Поиск номера знал только формы со словом «счёт», и у закрывающего документа номер
    оставался пустым. Дедуп сравнивает номера: «ВД-46890» против пустоты не совпадает
    никогда, и один акт ЭкоЦентра заводился двумя кредиторками — 3 348,22 ₽ услуги
    превращались в 6 696,44 ₽ долга и расхода.
    """
    assert _pick_number("Акт № ВД-46890 от 31.07.2026") == "ВД-46890"
    assert _pick_number("Универсальный передаточный документ № 24234") == "24234"
    assert _pick_number("Акт оказанных услуг № 000123 от 30.06.2026") == "000123"
    # В пакете «счёт + акт» побеждает счёт: его платят, его номер уходит в назначение платежа.
    assert _pick_number("Счет на оплату № 555 от 01.08.2026\nАкт № ВД-1") == "555"


def test_offer_invoice_number_is_extracted() -> None:
    """«Счет-оферта на оказание услуг № …» тоже отдаёт номер.

    Между словом «счёт» и «№» здесь стоят посторонние слова, и шаблон рвался. Номер уходит в
    назначение платежа — без него поставщик не понимает, за что деньги: так в банк ушёл
    платёж ДоксИнБокс по счёту 0006643130 с пустым номером.
    """
    assert _pick_number("Счет-оферта на оказание услуг № 0006643130 от 01.08.2026") == "0006643130"
    assert _pick_number("СЧЕТ-ОФЕРТА №0006643130 от 1 августа 2026 г.") == "0006643130"
    # Обычные формы не сломались.
    assert _pick_number("Счет на оплату № 010826-3064-лк от 01.08.2026") == "010826-3064-лк"
    assert _pick_number("Счет 0000-001175 от 31 марта 2026 г.") == "0000-001175"


def test_abbreviated_month_is_a_service_period() -> None:
    """«Авг. 2026» в наименовании услуги — это период, и он должен распознаваться.

    Так выставляют счета студия Синапсис и Яндекс.Директ: месяц просто дописан в конец
    строки услуги, без слов «за» и «период». На проде их счета оставались без периода, платёж
    вставал в очередь «Ждём документ» и ждал документа, которого эти контрагенты не шлют
    вовсе — расход по ним не признавался никогда.
    """
    synapse = _extract_service_periods(
        "Seo-продвижение сайта teplofood.ru. Тариф Минимальный - абонентское обслуживание Авг. 2026"
    )
    assert [(start, end) for start, end, _, _ in synapse] == [
        (date(2026, 8, 1), date(2026, 8, 31))
    ]
    direct = _extract_service_periods(
        "Размещение рекламных материалов на проекте Яндекс.Директ - Авг. 2026"
    )
    assert [(start, end) for start, end, _, _ in direct] == [
        (date(2026, 8, 1), date(2026, 8, 31))
    ]


def test_contract_date_is_not_mistaken_for_a_period() -> None:
    """Дата договора с сокращённым месяцем периодом НЕ становится.

    В тех же счетах строкой выше стоит «к договору SUP-484108 от 12 Сен. 2025г.». Отличает
    период от даты ровно одно: у периода нет дня. Без этой защиты счёт Синапсиса за август
    получил бы период «сентябрь 2025» — расход уехал бы на год назад, и заметили бы это
    только на сверке.
    """
    assert _extract_service_periods("к договору SUP-484108 от 12 Сен. 2025г.") == []
    assert _extract_service_periods("CNTX-212566 от 25 Мар. 2021г.") == []
    # Год вне разумного диапазона тоже не период: в номерах договоров встречаются любые числа.
    assert _extract_service_periods("Договор Авг. 1999 расширенный") == []


def test_explicit_period_wins_over_abbreviation() -> None:
    """Явное «за период: …» сильнее сокращения — у него выше уверенность.

    Если в счёте есть обе формы, побеждать должна та, которую контрагент написал словами:
    сокращение в конце наименования слабее по замыслу.
    """
    both = _extract_service_periods(
        "Услуги за период: июль 2026 г. по договору обслуживание Июл. 2026"
    )
    assert [(start, end) for start, end, _, _ in both] == [(date(2026, 7, 1), date(2026, 7, 31))]
    assert both[0][3] >= 0.94
