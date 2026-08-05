"""Имя мерчанта из текста карт-операции — единственная опора для её опознания.

Карт-списание T-Банка не несёт реквизитов получателя: в выписке стоит сам банк-эквайер
(``АО "ТБанк"``, ИНН 7710140679, транзитный счёт 30232…), одинаковый у покупки в Ozon,
в «Магните» и у оплаты хостинга. Личность продавца живёт ТОЛЬКО в назначении:

    Оплата в OZON Moskva RUS
    Оплата в YM*ihc.ru MOSKVA RUS
    Оплата в PYATEROCHKA 19180 Volgodonsk RUS

Поэтому «запомнить контрагента» для таких операций = запомнить подстроку с именем мерчанта.
Полный текст для этого не годится: город и номер точки в нём меняются от платежа к платежу
(``PYATEROCHKA 5973`` / ``PYATEROCHKA 19180``), и правило по полному тексту срабатывает
ровно один раз. Здесь текст очищается до стабильного ядра:

    «Оплата в PYATEROCHKA 19180 Volgodonsk RUS»  → «PYATEROCHKA»
    «Оплата в YM*ihc.ru MOSKVA RUS»              → «ihc.ru»
    «Оплата в MAGAZIN MAGISTR Volgodonsk RUS»    → «MAGAZIN MAGISTR»

Именно ядро, а не первое слово: ``MAGAZIN MAGISTR`` и ``MAGAZIN PAPRIKA`` — разные лавки,
и обрезка до ``MAGAZIN`` слила бы их в одного контрагента.

Форматы префиксов и хвостов сверены с боевой выпиской (437 карт-операций T-Банка, 94
различных назначения) 05.08.2026.
"""

from __future__ import annotations

import re

# Начало текста карт-операции. Всё, что до имени мерчанта, — служебное.
CARD_PURPOSE_PREFIXES = (
    "оплата в ",
    "покупка в ",
    "отмена операции оплаты ",
    "возврат средств по операции оплаты ",
    "возврат средств по оплате ",
    "возврат по операции оплаты ",
)

# Хвост «<Город> <СТРАНА>»: страна — трёхбуквенный код (RUS, ARM…), город — токен перед ним.
_COUNTRY_CODE = re.compile(r"^[A-Z]{3}$")

# Служебные слова города, остающиеся после срезки самого города: «YM*avito Gorod Moskva RUS».
_CITY_NOISE = frozenset({"gorod", "g", "g.", "city"})

# Префикс платёжного агрегатора: YM* (ЮMoney), RK* и т.п. — к личности продавца не относится
# и у одного мерчанта то есть, то нет.
_AGGREGATOR_PREFIX = re.compile(r"^[A-Za-z0-9]{1,6}\*")

# Минимальная длина: подстрока короче трёх символов поймала бы пол-выписки.
MIN_PATTERN_LENGTH = 3


def merchant_token(payment_purpose: str | None) -> str | None:
    """Стабильное имя мерчанта из назначения карт-операции.

    Возвращает ``None``, если текст не похож на карт-операцию (обычная платёжка, комиссия,
    зачисление эквайринга) — там личность отправителя берут из реквизитов, а не из текста.
    """
    text = " ".join((payment_purpose or "").split())
    if not text:
        return None

    lowered = text.casefold()
    for prefix in CARD_PURPOSE_PREFIXES:
        if lowered.startswith(prefix):
            text = text[len(prefix) :]
            break
    else:
        return None

    tokens = text.split()
    if not tokens:
        return None

    # 1. Хвост «<Город> <СТРАНА>» — срезаем страну и следом сам город.
    if _COUNTRY_CODE.match(tokens[-1]):
        tokens = tokens[:-1]
        if len(tokens) > 1:  # у одинокого токена город не отрезаем — это и есть мерчант
            tokens = tokens[:-1]

    # 2. Остатки города и номер точки: «… Gorod», «PYATEROCHKA 19180», «MAGNIT GM VOLGODONSK 1».
    while len(tokens) > 1 and (tokens[-1].isdigit() or tokens[-1].casefold() in _CITY_NOISE):
        tokens = tokens[:-1]

    token = " ".join(tokens)

    # 3. Префикс агрегатора и внешние кавычки: «YM*ihc.ru» → «ihc.ru», «"IP PRAVDINA N.V."».
    token = _AGGREGATOR_PREFIX.sub("", token, count=1)
    token = _unwrap_quotes(token)

    if len(token) < MIN_PATTERN_LENGTH:
        return None
    return token


def normalized_name(value: str | None) -> str:
    """Название контрагента в сравнимом виде: без уточнения в скобках, регистра и кавычек.

    «IHC.ru (поставщик серверов)» → «ihc.ru» — так карточка реестра сходится с тем именем,
    которым продавец подписывается в выписке.
    """
    text = " ".join((value or "").split())
    text = re.sub(r"\s*\([^()]*\)\s*$", "", text)
    return _unwrap_quotes(text).casefold()


# Кавычки снимаем только ПАРНЫЕ, обнимающие всё имя («"IP PRAVDINA N.V."»). Односторонний
# strip покалечил бы «ООО "Ромашка"» до «ООО "Ромашка», и карточка перестала бы совпадать
# сама с собой при сравнении из другого источника.
_QUOTE_PAIRS = (('"', '"'), ("'", "'"), ("«", "»"))


def _unwrap_quotes(value: str) -> str:
    text = value.strip()
    for opening, closing in _QUOTE_PAIRS:
        if len(text) > 1 and text.startswith(opening) and text.endswith(closing):
            return text[1:-1].strip()
    return text
