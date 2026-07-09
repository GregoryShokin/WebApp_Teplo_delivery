"""Проверка контрольного разряда банковских реквизитов (алгоритм Банка России).

Т-Банк отклоняет платёжное поручение с неверным контрольным разрядом счёта
(HTTP 422 ``INVALID_DATA`` — «Неверный контрольный разряд счета»), а необработанный
отказ раньше вылетал наружу как голая 500. Проверяем расчётный счёт получателя ещё на
своей стороне: чтобы не помечать битые реквизиты «подтверждёнными» и давать понятную
ошибку до похода в банк.
"""

from __future__ import annotations

from typing import Any

from app.services.banking.base import clean_digits

# Весовые коэффициенты контрольного ключа расчётного/лицевого счёта (Положение Банка
# России № 579-П): применяются к строке «3 цифры БИК + 20 цифр счёта» (23 позиции).
_WEIGHTS = (7, 1, 3) * 8  # 24 значения, используем первые 23


def account_control_key_valid(account: str, bik: str) -> bool:
    """True, если контрольный разряд счёта ``account`` сходится с БИК ``bik``.

    Работает для клиентских счетов банка (расчётный/лицевой, 20 цифр): префикс
    контрольной строки — последние 3 цифры БИК. Неверная длина счёта/БИК → False.
    """
    account = clean_digits(account)
    bik = clean_digits(bik)
    if len(account) != 20 or len(bik) != 9:
        return False
    key = bik[-3:] + account
    total = sum(int(digit) * weight for digit, weight in zip(key, _WEIGHTS, strict=False))
    return total % 10 == 0


def payee_account_error(requisites: dict[str, Any] | None) -> str | None:
    """Сообщение об ошибке, если счёт получателя не проходит контроль; иначе None.

    Проверяем только когда заданы оба поля (``bankAcnt`` + ``bankBik``): пустые/неполные
    реквизиты — забота отдельного гарда «реквизиты не подтверждены», не наша.
    """
    requisites = requisites or {}
    account = clean_digits(requisites.get("bankAcnt"))
    bik = clean_digits(requisites.get("bankBik"))
    if not account or not bik:
        return None
    if not account_control_key_valid(account, bik):
        return "Неверный контрольный разряд расчётного счёта — проверьте счёт получателя и БИК"
    return None
