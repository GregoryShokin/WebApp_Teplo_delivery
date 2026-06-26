"""Резолв PAYIN-типа внесения «Пополнение депозитов курьерам» по структуре счетов."""

from __future__ import annotations

from app.services.couriers.deposit_iiko_payout import (
    TOPUP_TYPE_KEY,
    _build_payin_type_index,
)

ACCOUNTS = {
    "id_kassa": "Главная касса",
    "id_topup": "Пополнение депозитов курьерам",
    "id_return": "Возвраты депозитов курьеров",
}

TYPES = [
    {
        "id": "T_TOPUP",
        "transactionType": "PAYIN",
        "chiefAccount": "id_kassa",
        "account": "id_topup",
        "cashFlowCategory": {"code": "40"},
    },
    # PAYOUT на той же кассе — PAYIN-индекс его игнорирует (иначе перепутали бы направление).
    {
        "id": "T_RETURN",
        "transactionType": "PAYOUT",
        "chiefAccount": "id_kassa",
        "account": "id_return",
        "cashFlowCategory": {"code": "37"},
    },
]


def test_payin_index_resolves_topup_and_ignores_payout() -> None:
    index = _build_payin_type_index(ACCOUNTS, TYPES)
    assert index.get(TOPUP_TYPE_KEY) == "T_TOPUP"
    assert "T_RETURN" not in index.values()
