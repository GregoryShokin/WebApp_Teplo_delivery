"""Поиск платёжных реквизитов контрагента в собственной истории.

Реквизиты для платёжного поручения (расчётный счёт, БИК, корр-счёт, ИНН, КПП) у нас уже
есть дважды: в проведённых банковских операциях и в распознанных счетах из почты. Модуль
достаёт их оттуда — по ИНН, названию или счёту — чтобы карточку не набивали руками
с бумажки. Ничего не применяется молча: наружу уходят КАНДИДАТЫ с указанием источника
и даты, выбирает и подтверждает человек. Имя и ИНН в платёжке пишет банк со слов
плательщика, поэтому «нашли в истории» — не то же самое, что «проверено».

Раскладка ключей задана ЯВНО по провайдеру и это не педантизм. У Т-Банка реквизиты
получателя лежат в блоке ``receiver`` (``bicRu``, ``corAcct``, ``acct``), а на верхнем
уровне payload есть ключ ``bic`` — но это БИК САМОГО Т-Банка, одинаковый у всех 1147
операций. Прежняя эвристика «поищем ключ bic где-нибудь в payload» подставляла в карточку
именно его: банк получателя выходил заведомо чужой, а корр-счёт пустой (ключа
``corrAccount`` в данных Т-Банка нет вовсе). Сбер же кладёт те же поля плоско
в ``rurTransfer`` с префиксом ``payee``/``payer`` — блока ``receiver`` у него нет.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BankOperation, Counterparty, EmailInvoiceIntake, OwnAccountsRegistry
from app.services.banking.base import clean_digits
from app.services.banking.requisites import OFFICIAL_SUPPLIER_REQUIRED_REQUISITES

# ИНН банков-эквайеров: «получатель» таких операций — сам банк (карт-оплата, комиссия),
# а не контрагент. Список общий с матчингом накладных на банковские операции: там он
# отсекает те же строки из кандидатов оплаты счёта.
BANK_NOISE_INNS = frozenset({"7710140679", "7707083893"})

# Ключи карточки контрагента (``CounterpartyPayableProfile.requisites``) — те же, что
# показывает форма и ждёт платёжка банка.
REQUISITE_KEYS = (
    "recipientName",
    "inn",
    "kpp",
    "bankAcnt",
    "bankBik",
    "recipientCorrAccountNumber",
)

# Т-Банк: контрагент операции — блок ``receiver`` (у исходящих) / ``payer`` (у входящих).
# Ключ ``bic`` читаем ТОЛЬКО внутри блока: на верхнем уровне это БИК нашего банка.
_TBANK_KEYS = {
    "recipientName": ("name",),
    "inn": ("inn",),
    "kpp": ("kpp",),
    "bankAcnt": ("acct",),
    "bankBik": ("bicRu", "bic"),
    "recipientCorrAccountNumber": ("corAcct",),
}

# Сбер: тот же контрагент — плоско в ``rurTransfer`` с префиксом стороны.
_SBER_OUT_KEYS = {
    "recipientName": ("payeeName",),
    "inn": ("payeeInn",),
    "kpp": ("payeeKpp",),
    "bankAcnt": ("payeeAccount",),
    "bankBik": ("payeeBankBic",),
    "recipientCorrAccountNumber": ("payeeBankCorrAccount",),
}
_SBER_IN_KEYS = {
    "recipientName": ("payerName",),
    "inn": ("payerInn",),
    "kpp": ("payerKpp",),
    "bankAcnt": ("payerAccount",),
    "bankBik": ("payerBankBic",),
    "recipientCorrAccountNumber": ("payerBankCorrAccount",),
}

# Поля, которые в платёжке всегда цифровые: банк отдаёт их с пробелами/дефисами вразнобой.
_DIGIT_KEYS = frozenset({"inn", "kpp", "bankAcnt", "bankBik", "recipientCorrAccountNumber"})

# Сколько операций поднимаем из БД на один поиск. Группировка схлопывает их в единицы
# кандидатов (579 карт-операций Т-Банка — это одна строка), поэтому запас берём с горкой,
# но не безлимитный: ``raw_payload`` — тяжёлый JSONB.
_SCAN_LIMIT = 400
_DEFAULT_LIMIT = 6
# Короткий запрос («ООО») отдаёт полреестра и ничего не сообщает — ищем от трёх символов.
_MIN_QUERY_LENGTH = 3


@dataclass(slots=True)
class RequisitesCandidate:
    """Найденный в истории набор реквизитов с происхождением (для показа человеку)."""

    key: str
    source: str
    source_label: str
    bank_name: str | None
    last_seen_on: date | None
    last_amount: Decimal | None
    hits: int
    requisites: dict[str, str]
    missing: list[str] = field(default_factory=list)
    existing_counterparty_id: uuid.UUID | None = None
    existing_counterparty_name: str | None = None
    own_account: bool = False


def _clean_value(key: str, value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    text = str(value).strip()
    if not text:
        return None
    if key in _DIGIT_KEYS:
        text = clean_digits(text)
        # Сбер пишет отсутствующий КПП как «0» — это не реквизит, а заглушка схемы.
        if not text or not text.strip("0"):
            return None
    return text


def _collect(
    target: dict[str, str], block: dict[str, Any], mapping: dict[str, tuple[str, ...]]
) -> None:
    """Добрать недостающие поля из блока. Уже найденное не перетираем: источники идут
    от точного (раскладка провайдера) к приблизительному (плоские колонки)."""
    for target_key, source_keys in mapping.items():
        if target.get(target_key):
            continue
        for source_key in source_keys:
            value = _clean_value(target_key, block.get(source_key))
            if value:
                target[target_key] = value
                break


def requisites_from_operation(operation: BankOperation) -> dict[str, str]:
    """Реквизиты контрагента операции: у исходящей — получателя, у входящей — плательщика."""
    payload = operation.raw_payload if isinstance(operation.raw_payload, dict) else {}
    outgoing = operation.direction == "out"
    values: dict[str, str] = {}

    block = payload.get("receiver" if outgoing else "payer")
    if isinstance(block, dict):
        _collect(values, block, _TBANK_KEYS)

    transfer = payload.get("rurTransfer")
    if isinstance(transfer, dict):
        _collect(values, transfer, _SBER_OUT_KEYS if outgoing else _SBER_IN_KEYS)

    _collect(
        values,
        {
            "name": operation.counterparty_name_raw,
            "inn": operation.counterparty_inn_raw,
            "acct": operation.counterparty_account_raw,
        },
        {"recipientName": ("name",), "inn": ("inn",), "bankAcnt": ("acct",)},
    )
    return values


def bank_name_from_operation(operation: BankOperation) -> str | None:
    """Название банка контрагента — для подписи кандидата, в карточку не уходит."""
    payload = operation.raw_payload if isinstance(operation.raw_payload, dict) else {}
    block = payload.get("receiver" if operation.direction == "out" else "payer")
    if isinstance(block, dict) and block.get("bankName"):
        return str(block["bankName"]).strip() or None
    transfer = payload.get("rurTransfer")
    if isinstance(transfer, dict):
        key = "payeeBankName" if operation.direction == "out" else "payerBankName"
        if transfer.get(key):
            return str(transfer[key]).strip() or None
    return None


def _is_noise(operation: BankOperation, requisites: dict[str, str], exact_inn: str | None) -> bool:
    """Карт-операции и платежи в адрес самого банка — не реквизиты контрагента.

    Исключение: если человек ищет ровно по этому ИНН, значит банк ему и нужен —
    прятать найденное было бы враньём.
    """
    inn = requisites.get("inn") or ""
    if exact_inn and inn == exact_inn:
        return False
    if inn in BANK_NOISE_INNS:
        return True
    payload = operation.raw_payload if isinstance(operation.raw_payload, dict) else {}
    return str(payload.get("category") or "").strip() == "cardOperation"


def _like_pattern(text: str) -> str:
    """Экранируем спецсимволы LIKE: «100%» в названии не должен стать маской."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _missing_required(requisites: dict[str, str]) -> list[str]:
    return [
        label
        for key, label in OFFICIAL_SUPPLIER_REQUIRED_REQUISITES.items()
        if not requisites.get(key)
    ]


def _amount(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


@dataclass(slots=True)
class _Bucket:
    """Накопитель по одному контрагенту: свежая операция задаёт дату и сумму, а поля
    добираются со всех — КПП банк присылает не в каждой платёжке."""

    key: str
    source: str
    source_label: str
    bank_name: str | None = None
    last_seen_on: date | None = None
    last_amount: Decimal | None = None
    hits: int = 0
    requisites: dict[str, str] = field(default_factory=dict)

    def absorb(
        self,
        requisites: dict[str, str],
        *,
        seen_on: date | None,
        amount: Decimal | None,
        bank_name: str | None = None,
    ) -> None:
        self.hits += 1
        newer = self.last_seen_on is None or (seen_on is not None and seen_on > self.last_seen_on)
        for field_key, value in requisites.items():
            if value and (newer or not self.requisites.get(field_key)):
                self.requisites[field_key] = value
        if newer:
            self.last_seen_on = seen_on
            self.last_amount = amount
            self.bank_name = bank_name or self.bank_name
        elif bank_name and not self.bank_name:
            self.bank_name = bank_name


def _bucket_key(source: str, requisites: dict[str, str]) -> str | None:
    """Один контрагент = один ИНН+счёт: смена счёта поставщиком — это отдельный кандидат,
    и человек должен увидеть оба, а не молча получить старый."""
    inn = requisites.get("inn") or ""
    account = requisites.get("bankAcnt") or ""
    if not inn and not account:
        return None
    return f"{source}:{inn}:{account}"


async def _bank_candidates(
    session: AsyncSession, text: str, digits: str
) -> dict[str, _Bucket]:
    conditions = [BankOperation.counterparty_name_raw.ilike(_like_pattern(text), escape="\\")]
    exact_inn = digits if len(digits) in (10, 12) else None
    if exact_inn:
        conditions.append(BankOperation.counterparty_inn_raw == exact_inn)
    if len(digits) == 20:
        conditions.append(BankOperation.counterparty_account_raw == digits)

    operations = (
        await session.scalars(
            select(BankOperation)
            .where(or_(*conditions))
            .order_by(BankOperation.operation_date.desc())
            .limit(_SCAN_LIMIT)
        )
    ).all()

    buckets: dict[str, _Bucket] = {}
    for operation in operations:
        requisites = requisites_from_operation(operation)
        if _is_noise(operation, requisites, exact_inn):
            continue
        key = _bucket_key("bank", requisites)
        if key is None:
            continue
        label = "Платёж через Т-Банк" if operation.provider == "tbank" else "Платёж через Сбер"
        if operation.direction == "in":
            label = f"Поступление ({'Т-Банк' if operation.provider == 'tbank' else 'Сбер'})"
        bucket = buckets.setdefault(key, _Bucket(key=key, source="bank", source_label=label))
        bucket.absorb(
            requisites,
            seen_on=operation.operation_date,
            amount=operation.amount,
            bank_name=bank_name_from_operation(operation),
        )
    return buckets


def _intake_requisite(key: str) -> Any:
    """Поле реквизитов счёта из почты: правка оператора важнее сырого распознавания.

    ``requisites`` — что вытащил парсер из PDF, ``requisites_reviewed`` — что оператор
    исправил в окне разбора. Исправлял он ровно потому, что распознанное врало, поэтому
    кандидату в подсказку идёт проверенное значение, а сырое остаётся фолбэком.
    """
    return func.coalesce(
        EmailInvoiceIntake.recognition["requisites_reviewed"][key].astext,
        EmailInvoiceIntake.recognition["requisites"][key].astext,
    )


async def _email_candidates(session: AsyncSession, text: str, digits: str) -> dict[str, _Bucket]:
    conditions = [_intake_requisite("recipientName").ilike(_like_pattern(text), escape="\\")]
    if len(digits) in (10, 12):
        conditions.append(_intake_requisite("inn") == digits)
    if len(digits) == 20:
        conditions.append(_intake_requisite("bankAcnt") == digits)

    rows = (
        await session.scalars(
            select(EmailInvoiceIntake)
            .where(EmailInvoiceIntake.status != "failed")
            .where(or_(*conditions))
            .order_by(EmailInvoiceIntake.received_at.desc().nullslast())
            .limit(_SCAN_LIMIT)
        )
    ).all()

    buckets: dict[str, _Bucket] = {}
    for row in rows:
        recognition = row.recognition if isinstance(row.recognition, dict) else {}
        blocks = [
            block
            for block in (
                recognition.get("requisites_reviewed"),
                recognition.get("requisites"),
            )
            if isinstance(block, dict)
        ]
        if not blocks:
            continue
        requisites: dict[str, str] = {}
        # Порядок = приоритет: ``_collect`` не перетирает уже найденное, поэтому правки
        # оператора выигрывают у сырого распознавания — как и в фильтре выше.
        for block in blocks:
            _collect(requisites, block, {key: (key,) for key in REQUISITE_KEYS})
        key = _bucket_key("email", requisites)
        if key is None:
            continue
        bucket = buckets.setdefault(
            key, _Bucket(key=key, source="email", source_label="Счёт из почты")
        )
        bucket.absorb(
            requisites,
            seen_on=row.received_at.date() if row.received_at else None,
            amount=_amount(recognition.get("amount")),
        )
    return buckets


async def search_history_requisites(
    session: AsyncSession, query: str, *, limit: int = _DEFAULT_LIMIT
) -> list[RequisitesCandidate]:
    """Кандидаты реквизитов по ИНН / названию / расчётному счёту.

    Полные наборы идут первыми: неполный кандидат карточку всё равно не закроет
    (подтвердить реквизиты без БИК и корр-счёта нельзя), но показать его стоит —
    он экономит ввод ИНН и названия.
    """
    text = (query or "").strip()
    if len(text) < _MIN_QUERY_LENGTH:
        return []
    digits = clean_digits(text)

    buckets = await _bank_candidates(session, text, digits)
    for key, bucket in (await _email_candidates(session, text, digits)).items():
        buckets.setdefault(key, bucket)

    candidates = [
        RequisitesCandidate(
            key=bucket.key,
            source=bucket.source,
            source_label=bucket.source_label,
            bank_name=bucket.bank_name,
            last_seen_on=bucket.last_seen_on,
            last_amount=bucket.last_amount,
            hits=bucket.hits,
            requisites=dict(bucket.requisites),
            missing=_missing_required(bucket.requisites),
        )
        for bucket in buckets.values()
    ]
    candidates.sort(
        key=lambda item: (
            bool(item.missing),
            -(item.last_seen_on.toordinal() if item.last_seen_on else 0),
            -item.hits,
        )
    )
    candidates = candidates[:limit]
    await _annotate(session, candidates)
    return candidates


async def _annotate(session: AsyncSession, candidates: list[RequisitesCandidate]) -> None:
    """Пометить кандидатов, у которых карточка уже есть (дедуп до 409 от уникального
    индекса по ИНН) и чей счёт — наш собственный (перевод между своими счетами)."""
    if not candidates:
        return
    inns = {item.requisites.get("inn") for item in candidates if item.requisites.get("inn")}
    accounts = {
        item.requisites.get("bankAcnt") for item in candidates if item.requisites.get("bankAcnt")
    }
    existing: dict[str, tuple[uuid.UUID, str]] = {}
    if inns:
        rows = await session.execute(
            select(Counterparty.inn, Counterparty.id, Counterparty.name).where(
                Counterparty.inn.in_(inns)
            )
        )
        existing = {inn: (cp_id, name) for inn, cp_id, name in rows.all() if inn}
    own: set[str] = set()
    if accounts:
        own = {
            number
            for number in (
                await session.scalars(
                    select(OwnAccountsRegistry.account_number).where(
                        OwnAccountsRegistry.account_number.in_(accounts)
                    )
                )
            ).all()
            if number
        }
    for item in candidates:
        found = existing.get(item.requisites.get("inn") or "")
        if found:
            item.existing_counterparty_id, item.existing_counterparty_name = found
        item.own_account = (item.requisites.get("bankAcnt") or "") in own
