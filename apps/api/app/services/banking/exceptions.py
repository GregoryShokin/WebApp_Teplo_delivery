from __future__ import annotations


class BankFetchError(RuntimeError):
    """Банк вернул ошибку или недоступен.

    ``status_code`` — HTTP-код ответа банка (если ответ был), ``detail`` — разобранная
    из тела причина, пригодная для показа пользователю (напр. «неверный контрольный
    разряд счёта»). Оба опциональны: сетевые сбои и внутренние проверки бросают ошибку
    без них. По ``status_code`` роут отличает отказ по данным (4xx → 422 с причиной) от
    недоступности банка (→ 502), чтобы не отдавать наружу голую 500.
    """

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        status_code: int | None = None,
        detail: str | None = None,
    ) -> None:
        self.provider = provider
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{provider}: {message}")


class BankCredentialsError(BankFetchError):
    pass
