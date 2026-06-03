from __future__ import annotations


class BankFetchError(RuntimeError):
    def __init__(self, provider: str, message: str) -> None:
        self.provider = provider
        super().__init__(f"{provider}: {message}")


class BankCredentialsError(BankFetchError):
    pass
