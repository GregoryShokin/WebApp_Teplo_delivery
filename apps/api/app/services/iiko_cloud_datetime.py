"""Время документа для iiko Cloud: что послать, чтобы в бэк-офисе стояло время Тепла.

Спека Cloud обещает ISO 8601 с offset, но разные семейства API читают присланное время
по-разному, и это поведение меняется без предупреждения (сверено чтением ``get``):

- Inventory API (приходные, расходные, возвратные накладные) до 21.10.2026 вычитал offset из
  цифр: ``11:19+03:00`` ложилось на 08:19. Тепло с 24.08 компенсировало это, присылая 14:19.
- С 22.10.2026 (между Ч-193 21.10 11:49 и 515382 22.10 11:39) Inventory API читает время честно:
  ``16:08+03:00`` ложится на 16:08. Прежняя компенсация стала лишней, и все накладные Тепла
  ложились на 3 часа позже, 515385 от 23.10 22:04 — на 24.10 01:04.
- Finance API (акты услуг ``incoming_service``) на 25.10.2026 по-прежнему вычитает: проба
  PROBE-A прислала 15:50+03:00, в iiko 12:50.

Поэтому поправка — не константа формата, а наблюдаемый СДВИГ: на сколько iiko сдвигает
присланные цифры, своё значение у каждого семейства API. Значение по умолчанию — последнее
подтверждённое чтением. Отправка накладной читает документ обратно до проведения
(:func:`app.services.warehouse_invoice_push._settle_document_date`); если iiko положила его не
туда, сдвиг выучивается из факта (:func:`learn_iiko_shift`), дата переписывается, а следующие
документы процесса идут уже с новым сдвигом. Так очередная смена поведения iiko стоит одной
лишней правки документа, а не недели документов не в тех сутках.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_MSK = ZoneInfo("Europe/Moscow")

#: Приходные, расходные и возвратные накладные, акты списания (``/api/inventory/v1/...``).
INVENTORY = "inventory"
#: Акты услуг и прочие финансовые документы (``/api/finance/v1/...``).
FINANCE = "finance"

#: Сдвиг, который iiko делает с присланными цифрами: в бэк-офисе окажется «прислано + сдвиг».
#: Inventory — 0 с 22.10.2026 (Ч-195: прислано 16:08+03:00, в iiko 16:08); Finance — −3 часа
#: (PROBE-A 25.10.2026: прислано 15:50+03:00, в iiko 12:50).
DEFAULT_SHIFTS: dict[str, timedelta] = {
    INVENTORY: timedelta(0),
    FINANCE: timedelta(hours=-3),
}

# Выученное в этом процессе поверх DEFAULT_SHIFTS. Пишется только из learn_iiko_shift.
_learned: dict[str, timedelta] = {}

# Правдоподобный сдвиг: в пределах суток часовых поясов и кратен 15 минутам, как любой offset.
# Иное — не смена поведения iiko, а чужая правка документа; учиться на нём нельзя.
_MAX_SHIFT = timedelta(hours=14)
_SHIFT_STEP = timedelta(minutes=15)


def iiko_shift(family: str) -> timedelta:
    """Сдвиг, с которым сейчас кодируется время для семейства API ``family``."""
    return _learned.get(family, DEFAULT_SHIFTS[family])


def moscow_wall_time(moment: datetime) -> datetime:
    """Московское стенное время без зоны: наивное считаем уже московским, aware — переводим."""
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(_MSK).replace(tzinfo=None)


def encode_iiko_wall_time(moment: datetime, family: str = INVENTORY) -> str:
    """Значение поля даты, после которого iiko покажет московское время ``moment``.

    Шлём московские цифры за вычетом сдвига семейства, offset всегда ``+03:00``; формат —
    с ТОЧКОЙ перед миллисекундами (``2026-10-23T13:08:00.000+03:00``), который принимают
    ``create``/``update`` Cloud. Наивное время считаем московским.
    """
    sent = moscow_wall_time(moment) - iiko_shift(family)
    return f"{sent.strftime('%Y-%m-%dT%H:%M:%S')}.{sent.microsecond // 1000:03d}+03:00"


def stored_wall_time(value: str | None) -> datetime | None:
    """Московское стенное время из даты, которую вернул iiko (или которую мы послали)."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace(",", "."))
    except ValueError:
        return None
    return moscow_wall_time(parsed)


def learn_iiko_shift(family: str, *, sent: str, stored: str | None) -> bool:
    """Выучить сдвиг семейства из пары «послали — iiko сохранила». ``True`` — сдвиг изменился.

    Неправдоподобную разницу (не кратна 15 минутам или больше 14 часов) не учим: это не смена
    поведения iiko, а документ, который успели поправить руками, — вызывающий сообщит о нём.
    """
    sent_wall, stored_wall = stored_wall_time(sent), stored_wall_time(stored)
    if sent_wall is None or stored_wall is None:
        return False
    # Доли секунды не в счёт: iiko хранит миллисекунды, но сравнивать сдвиг надо по цифрам времени.
    shift = stored_wall.replace(microsecond=0) - sent_wall.replace(microsecond=0)
    if abs(shift) > _MAX_SHIFT or shift % _SHIFT_STEP:
        return False
    previous = iiko_shift(family)
    if shift == previous:
        return False
    _learned[family] = shift
    logger.warning(
        "iiko Cloud (%s) сменила разбор времени: сдвиг был %s, стал %s (послали %s, сохранено %s)",
        family, previous, shift, sent, stored,
    )
    return True


def missed_wall_time(
    family: str, *, sent: str | None, stored: str | None, used: timedelta
) -> datetime | None:
    """Куда целили, если iiko положила документ не туда; None — лёг верно или сверить нечем.

    Целевое время — присланные цифры плюс сдвиг ``used``, с которым собрано тело. Промах заодно
    учит сдвиг семейства (:func:`learn_iiko_shift`), если он правдоподобен; изменился ли сдвиг,
    вызывающий видит по :func:`iiko_shift`. Доли секунды не в счёт: сравниваем цифры, которые
    человек видит в бэк-офисе.
    """
    sent_wall, stored_wall = stored_wall_time(sent), stored_wall_time(stored)
    if sent is None or sent_wall is None or stored_wall is None:
        return None
    wanted = (sent_wall + used).replace(microsecond=0)
    if stored_wall.replace(microsecond=0) == wanted:
        return None
    learn_iiko_shift(family, sent=sent, stored=stored)
    return wanted


def reset_learned_shifts() -> None:
    """Забыть выученное (тесты; новый процесс начинает с DEFAULT_SHIFTS)."""
    _learned.clear()
