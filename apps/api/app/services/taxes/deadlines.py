"""Срок уплаты взноса на травматизм по п. 4 ст. 22 Закона № 125-ФЗ.

СФР: https://sfr.gov.ru/employers/insurance_premiums_tariffs/~10310
Пятнадцатое число следующего месяца переносится с выходного вперёд.
"""

from datetime import date, timedelta


def next_weekday(value: date) -> date:
    """Перенести субботу/воскресенье на ближайший понедельник."""
    if value.weekday() >= 5:
        return value + timedelta(days=7 - value.weekday())
    return value


def injury_due(year: int, month: int) -> date:
    """15-е число следующего месяца с переносом выходного на рабочий день."""
    due = date(year + 1, 1, 15) if month == 12 else date(year, month + 1, 15)
    return next_weekday(due)
