"""Имя мерчанта из текста карт-операции — на реальных назначениях боевой выписки T-Банка."""

from __future__ import annotations

import pytest

from app.services.banking.merchant_text import merchant_token, normalized_name


@pytest.mark.parametrize(
    ("purpose", "expected"),
    [
        # Хвост «<Город> <СТРАНА>» срезается, ядро имени остаётся целиком.
        ("Оплата в OZON Moskva RUS", "OZON"),
        ("Оплата в EXSPRESS VOLGODONSK RUS", "EXSPRESS"),
        ("Оплата в MANGO-OFFICE.RU MOSKVA RUS", "MANGO-OFFICE.RU"),
        ("Оплата в TBank-Avito Moskva RUS", "TBank-Avito"),
        ("Оплата в KRASNOE&BELOE Volgodonsk RUS", "KRASNOE&BELOE"),
        ("Оплата в 100BUMAG Volgodonsk RUS", "100BUMAG"),
        # Номер точки уходит: иначе у каждой «Пятёрочки» было бы своё правило.
        ("Оплата в PYATEROCHKA 19180 Volgodonsk RUS", "PYATEROCHKA"),
        ("Оплата в PYATEROCHKA 5973 Volgodonsk RUS", "PYATEROCHKA"),
        ("Оплата в APTEKA APREL 51045 Volgodonsk RUS", "APTEKA APREL"),
        ("Оплата в SDEK ENTUZIASTOV 31 Volgodonsk RUS", "SDEK ENTUZIASTOV"),
        ("Оплата в MAGNIT GM VOLGODONSK 1 Volgodonsk RUS", "MAGNIT GM VOLGODONSK"),
        # Префикс агрегатора к личности продавца не относится.
        ("Оплата в YM*ihc.ru MOSKVA RUS", "ihc.ru"),
        ("Оплата в YM*tuna DILIZHAN ARM", "tuna"),
        ("Оплата в RK*SpycatWidget G.Moskva RUS", "SpycatWidget"),
        ("Оплата в YM*avito Gorod Moskva RUS", "avito"),
        # Кавычки в имени мерчанта — оформление банка, а не часть названия.
        ('Оплата в "IP PRAVDINA N.V." VOLGODONSK RUS', "IP PRAVDINA N.V."),
        # Возвраты и отмены — тот же продавец, тот же паттерн.
        ("Возврат средств по оплате OZON Moskva RUS", "OZON"),
        ("Отмена операции оплаты MAGAZIN PAPRIKA Volgodonsk RUS", "MAGAZIN PAPRIKA"),
        (
            "Возврат средств по операции оплаты MAGNIT GM VOLGODONSK 1 Volgodonsk RUS",
            "MAGNIT GM VOLGODONSK",
        ),
    ],
)
def test_merchant_token_extracts_seller_name(purpose: str, expected: str) -> None:
    assert merchant_token(purpose) == expected


def test_merchant_token_keeps_shops_apart() -> None:
    """Обрезка до первого слова слила бы разные лавки в одного контрагента."""
    assert merchant_token("Оплата в MAGAZIN MAGISTR Volgodonsk RUS") == "MAGAZIN MAGISTR"
    assert merchant_token("Оплата в MAGAZIN PAPRIKA Volgodonsk RUS") == "MAGAZIN PAPRIKA"


@pytest.mark.parametrize(
    "purpose",
    [
        None,
        "",
        # Не карт-операции: личность отправителя в них берут из реквизитов, а не из текста.
        "Зачисление средств по терминалам эквайринга от 01.08.2026.",
        "Комиссия за перевод средств",
        "Плата за обслуживание счета",
        "Перевод средств по договору №СТ22989030758920125 от 07 июля 2025 г.",
    ],
)
def test_merchant_token_ignores_non_card_operations(purpose: str | None) -> None:
    assert merchant_token(purpose) is None


def test_merchant_token_rejects_too_short_pattern() -> None:
    """Подстрока короче трёх символов поймала бы пол-выписки."""
    assert merchant_token("Оплата в AB Moskva RUS") is None


def test_normalized_name_drops_parenthetical_qualifier() -> None:
    assert normalized_name("IHC.ru (поставщик серверов)") == "ihc.ru"
    assert normalized_name('ООО "Ромашка"') == 'ооо "ромашка"'
