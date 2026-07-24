"""Owner-approved реквизиты единого налогового платежа (ЕНП) в Казначейство/ФНС.

Получатель бюджета — фиксированная константа, НЕ контрагент: казначейские реквизиты не
подчиняются клиентскому контролю счёта (579-П) и не должны попадать в реестр контрагентов.
Значения сверены с платёжным поручением налогового агента (форма 0401060). Единый КБК ЕНП
покрывает УСН, НДФЛ, взносы — на едином налоговом счёте разносит их ФНС.

DO NOT CHANGE WITHOUT AN EXPLICIT OWNER REQUEST — как и другие owner-locked реквизиты, эти
поля нельзя менять при рефакторинге, миграциях или работе над провайдерами.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Final

# OWNER-APPROVED FINANCIAL CONSTANT — реквизиты ЕНП (единый налоговый платёж).
TREASURY_ENP_REQUISITES: Final[Mapping[str, Any]] = MappingProxyType(
    {
        "recipientName": "Казначейство России (ФНС России)",
        "inn": "7727406020",
        "kpp": "770701001",
        "bankAcnt": "03100643000000018500",
        "bankBik": "017003983",
        "bankName": "ОКЦ № 7 ГУ Банка России по ЦФО//УФК по Тульской области, г. Тула",
        "recipientCorrAccountNumber": "40102810445370000059",
        "executionOrder": 5,
        # Налоговый блок платёжки: ЕНП идёт единой суммой под КБК ЕНП; статус плательщика «01»,
        # остальные поля (ОКТМО, период, УИН, № и дата документа) для ЕНП — «0» (разносит ФНС).
        "kbk": "18201061201010000510",
        "taxPayerStatus": "01",
        "oktmo": "0",
        "paymentPurpose": "Единый налоговый платеж",
    }
)


def treasury_enp_requisites() -> dict[str, Any]:
    """Изменяемая копия реквизитов ЕНП для одного вызова банк-клиента."""
    return dict(TREASURY_ENP_REQUISITES)
