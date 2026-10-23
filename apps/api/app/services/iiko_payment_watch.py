"""Сторож оплат в iiko: оплата, поставленная Тепло, должна оставаться в учёте iiko.

Зачем. Зеркала (:mod:`counterparty_iiko_payment`) ставят оплату накладной в iiko через
``add_payment``, а сверка (:mod:`iiko_payment_verify`) один раз подтверждает её проводкой
``INVOICE_PAYMENT`` и больше к ней не возвращается: после ``verified_at`` строка выпадает из её
выборки, а старше 30 дней сверка не смотрит вовсе. Оплату же в бэк-офисе iiko могут снять руками
когда угодно. Так случилось 13.10.2026 у «Местного закупа»: из учёта iiko пропали оплаты чеков
Ч-116 и Ч-123, подтверждённые 30.08 и 04.09, и Тепло этого не заметило. Закуп предоплатный —
деньги ушли картой в магазине, чеки на деле оплачены, а в iiko поставщик стал «должен».

Два правила, каждое — видимый случай owner-review (``kind = iiko_payment_drift``):

1. **Оплату сняли** (``payment_removed_in_iiko``). Успешная отправка Тепло (ok-строка журнала с
   суммой, не ручное подтверждение) по оплаченной у нас накладной должна покрываться проводками
   оплаты того же документа iiko — независимо от возраста накладной. Покрытия нет → случай:
   документ, сумма, когда оплату видели в последний раз и кто последним правил документ в iiko.
2. **Исправление не по схеме** (``correction_pair_mismatch``). Исправление оплаченного чека
   (``warehouse_invoice_push.book_correction_in_iiko``) задумано тремя документами: исходная X
   оплачена, проведён возврат X со ссылкой на неё, новая «X (испр.)» остаётся БЕЗ оплаты.
   Отклонения: оплату X сняли; «(испр.)» оплатили; возврата нет (не заведён, удалён, распроведён
   или ссылается не на X); «(испр.)» удалена.

Чего сторож НЕ делает: ничего не пишет в iiko и не ставит оплату заново — снова ставить её
автоматически или только показывать, решает владелец. Случай закрывается сам, когда iiko снова
совпадает со схемой. «Разобрано» от человека гасит именно эту картину (отпечаток отклонений):
та же картина больше не всплывает, новая — откроет новый случай.

Факты берём только чтением: OLAP ``TRANSACTIONS`` (проводки ``INVOICE_PAYMENT`` по номеру
документа и контрагенту — GUID документа отчёт не отдаёт) и Cloud ``incoming_invoice/get`` /
``returned_invoice/get`` (номер, статус, ``dateModified``/``userModified``; имя пользователя —
iikoServer ``employees/byId``). Журнал событий iiko правки оплат не пишет (проверено 23.10.2026:
ни правки Ч-116 от 13.10, ни снятых оплат в нём нет), поэтому «кто снял» назвать нельзя — только
кто и когда последним правил документ. Окно проводок широкое: оплату в iiko можно передвинуть на
любую дату — оплату «Ч-116 (испр.)» 23.10 нашли датированной 14.08 при накладной от 30.08.
"""

from __future__ import annotations

import http.client as hc
import json
import logging
import re
import ssl
import time
import urllib.parse
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Counterparty,
    CounterpartyAlias,
    IikoInvoicePaymentPush,
    ReconciliationCase,
    SupplierInvoice,
)
from app.services.iiko_payment_verify import AMOUNT_TOLERANCE, MAX_RESENDS, VERIFY_MAX_AGE

logger = logging.getLogger(__name__)
_MSK = ZoneInfo("Europe/Moscow")

CASE_KIND = "iiko_payment_drift"
REASON_PAYMENT_REMOVED = "payment_removed_in_iiko"
REASON_CORRECTION_PAIR = "correction_pair_mismatch"

# Коды отклонений — строки случая. По ним собирается отпечаток картины («Разобрано» гасит его).
DEV_PAYMENT_REMOVED = "payment_removed"  # правило 1: поставленной Тепло оплаты в iiko нет
DEV_ORIGINAL_PAYMENT_REMOVED = "original_payment_removed"  # пара: оплату исходной X сняли
DEV_CORRECTION_PAID = "correction_paid"  # пара: «X (испр.)» оплатили
DEV_RETURN_MISSING = "return_missing"  # пара: возврата X нет / не проведён / не на X
DEV_CORRECTION_DELETED = "correction_deleted"  # пара: «X (испр.)» удалена в iiko

# Оплату можно передвинуть в бэк-офисе на любую дату, поэтому проводки читаем с большим запасом
# в обе стороны от дат наших накладных: узкое окно приняло бы передвинутую оплату за снятую.
LEDGER_LOOKBACK = timedelta(days=366)
LEDGER_LOOKAHEAD = timedelta(days=366)

# Ручное подтверждение («оплата проведена вручную») пишет в журнал ok-строку со счётом «manual»:
# Тепло её в iiko не ставило и сумму не знает точно — сторож такие строки не судит.
_MANUAL_ACCOUNT = "manual"
_DELETED = "DELETED"
_PROCESSED = "PROCESSED"
_OLAP_PATH = "/resto/api/v2/reports/olap"
_INCOMING_GET = "/api/inventory/v1/incoming_invoice/get"
_RETURNED_GET = "/api/inventory/v1/returned_invoice/get"
# Cloud режет частые запросы: 23.10.2026 холостой прогон сторожа на втором десятке get подряд
# получил 429 «too many requests». Чтение идемпотентно, повторить его безопасно (в отличие от
# add_payment); между запросами — пауза, чтобы сторож не отнимал лимит у зеркал оплат.
CLOUD_READ_INTERVAL_SECONDS = 0.7
CLOUD_READ_RETRY_DELAYS = (2.0, 5.0, 10.0)


@dataclass(frozen=True)
class LedgerPayment:
    """Проводка оплаты накладной в учёте iiko — сторона денежного счёта.

    Не GUID документа: OLAP его не отдаёт, документ узнаём по номеру и контрагенту."""

    number: str  # номер документа в iiko — так его показывает отчёт по проводкам
    counteragent_id: str | None  # GUID контрагента iiko: номера у разных поставщиков совпадают
    account: str  # денежный счёт-источник («Денежные средства, эквайринг», «Главная касса»)
    amount: Decimal
    paid_on: date | None  # учётная дата проводки — её можно передвинуть в бэк-офисе


@dataclass(frozen=True)
class IikoDocument:
    """Документ iiko по Cloud ``get``: то, что нужно сторожу, а не весь документ."""

    document_id: str
    found: bool  # False — iiko ответила «document not found»
    number: str | None = None
    status: str | None = None  # PROCESSED — проведён, NEW — распроведён, DELETED — удалён
    counteragent_id: str | None = None
    modified_at: datetime | None = None  # dateModified: последняя правка документа
    modified_by_id: str | None = None  # userModified: кто правил последним
    incoming_invoice_id: str | None = None  # только у возвратной: на какую приходную ссылается
    total: Decimal | None = None  # сумма строк документа — оплачен ли он в iiko целиком


class IikoWatchUnavailable(RuntimeError):
    """iiko не ответила по существу (сеть, 5xx, лимит, нет креды) — судить о пропаже нельзя.

    Сторож такой документ пропускает до следующего прохода: принять молчание iiko за «оплаты
    нет» значило бы завести ложный случай."""


class IikoWatchSource(Protocol):
    """Откуда сторож берёт факты iiko. Боевой источник — :class:`LiveIikoWatchSource`; тесты
    подставляют свой. Методы синхронные (сеть) — сторож зовёт их в треде."""

    def ledger(self, date_from: date, date_to: date) -> list[LedgerPayment]:
        """Проводки оплаты накладных за учётные даты ``[date_from, date_to]``."""
        ...

    def incoming(self, document_id: str) -> IikoDocument:
        """Приходная накладная по GUID."""
        ...

    def returned(self, document_id: str) -> IikoDocument:
        """Возвратная накладная по GUID."""
        ...

    def user_name(self, user_id: str) -> str | None:
        """Имя пользователя iiko по GUID; ``None`` — iiko не отдала."""
        ...


def _parse_iiko_datetime(value: Any) -> datetime | None:
    """``2026-10-13T15:01:23.829+03:00`` → aware datetime; мусор → ``None``."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=_MSK)


def _parse_iiko_date(value: Any) -> date | None:
    """Учётная дата OLAP (``2026-08-14`` или ``2026-08-14T00:00:00``) → ``date``."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


class LiveIikoWatchSource:
    """Чтение живой iiko для сторожа. ТОЛЬКО чтение: OLAP-отчёт, Cloud ``get``, ``employees``.

    Перед использованием креды iiko должны быть в окружении
    (``iiko_sync._load_source_credential_env``) — сторож делает это сам. Токены берутся один раз
    на проход и переиспользуются; имена пользователей кэшируются."""

    def __init__(self) -> None:
        self._server_token: str | None = None
        self._cloud_token: str | None = None
        self._cloud_opener: Any = None
        self._names: dict[str, str | None] = {}
        self._last_cloud_call: float | None = None

    def _server(self) -> tuple[str, int, str]:
        from app.services.couriers.iiko_olap_sync import _auth_token, _iiko_host_and_port

        if self._server_token is None:
            try:
                self._server_token = _auth_token()
            except Exception as exc:  # noqa: BLE001 — нет креды/сеть: проход пропускаем целиком
                raise IikoWatchUnavailable(f"iikoServer: вход не удался ({exc})") from exc
        host, port = _iiko_host_and_port()
        return host, port, self._server_token

    def _server_request(self, method: str, path: str, body: bytes | None = None) -> bytes:
        host, port, token = self._server()
        sep = "&" if "?" in path else "?"
        conn = hc.HTTPSConnection(host, port, timeout=300, context=ssl._create_unverified_context())
        try:
            headers = {"Content-Type": "application/json"} if body is not None else {}
            conn.request(method, f"{path}{sep}key={token}", body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read()
        except OSError as exc:
            raise IikoWatchUnavailable(f"iikoServer {path}: {exc}") from exc
        finally:
            conn.close()
        if response.status != 200:
            raise IikoWatchUnavailable(f"iikoServer {path}: HTTP {response.status} {raw[:200]!r}")
        return raw

    def ledger(self, date_from: date, date_to: date) -> list[LedgerPayment]:
        """OLAP ``TRANSACTIONS``: только ``INVOICE_PAYMENT``, сторона денежного счёта.

        Группировка по номеру, контрагенту, счёту и учётной дате: без счёта доли одного документа
        (карта + наличные) слиплись бы, без контрагента — совпали бы номера разных поставщиков."""
        body = json.dumps(
            {
                "reportType": "TRANSACTIONS",
                "buildSummary": False,
                "groupByRowFields": [
                    "Document",
                    "Counteragent.Id",
                    "Account.Name",
                    "DateTime.DateTyped",
                ],
                "groupByColFields": [],
                "aggregateFields": ["Sum.Outgoing"],
                "filters": {
                    "DateTime.DateTyped": {
                        "filterType": "DateRange",
                        "periodType": "CUSTOM",
                        "from": f"{date_from.isoformat()}T00:00:00.000",
                        "to": f"{(date_to + timedelta(days=1)).isoformat()}T00:00:00.000",
                        "includeLow": True,
                        "includeHigh": False,
                    },
                    "TransactionType": {
                        "filterType": "IncludeValues",
                        "values": ["INVOICE_PAYMENT"],
                    },
                },
            }
        ).encode("utf-8")
        rows = json.loads(self._server_request("POST", _OLAP_PATH, body)).get("data", [])
        payments: list[LedgerPayment] = []
        for row in rows:
            amount = Decimal(str(row.get("Sum.Outgoing") or 0))
            if amount <= 0:  # зеркальная сторона проводки (долг поставщику) — берём только расход
                continue
            payments.append(
                LedgerPayment(
                    number=str(row.get("Document") or ""),
                    counteragent_id=row.get("Counteragent.Id") or None,
                    account=str(row.get("Account.Name") or ""),
                    amount=amount,
                    paid_on=_parse_iiko_date(row.get("DateTime.DateTyped")),
                )
            )
        return payments

    def _cloud_get(self, path: str, document_id: str) -> dict | None:
        from app.services.iiko_cloud_client import iiko_auth_token, iiko_cloud_call, iiko_opener
        from app.services.iiko_location import get_organization_id

        if self._cloud_token is None:
            self._cloud_opener = iiko_opener()
            try:
                self._cloud_token = iiko_auth_token(self._cloud_opener)
            except Exception as exc:  # noqa: BLE001 — нет креды/сеть
                raise IikoWatchUnavailable(f"iiko Cloud: вход не удался ({exc})") from exc
        for delay in (*CLOUD_READ_RETRY_DELAYS, None):
            if self._last_cloud_call is not None:
                wait = CLOUD_READ_INTERVAL_SECONDS - (time.monotonic() - self._last_cloud_call)
                if wait > 0:
                    time.sleep(wait)
            status, response = iiko_cloud_call(
                path,
                {"organizationId": get_organization_id(), "documentId": document_id},
                token=self._cloud_token,
                opener=self._cloud_opener,
            )
            self._last_cloud_call = time.monotonic()
            if status != 429 or delay is None:
                break
            time.sleep(delay)
        if status == 200 and isinstance(response, dict):
            return response
        if status == 404 and "not found" in json.dumps(response, ensure_ascii=False).lower():
            return None
        raise IikoWatchUnavailable(f"iiko Cloud {path}: HTTP {status} {str(response)[:200]}")

    @staticmethod
    def _document(document_id: str, raw: dict | None) -> IikoDocument:
        if raw is None:
            return IikoDocument(document_id=document_id, found=False)
        return IikoDocument(
            document_id=document_id,
            found=True,
            number=raw.get("number"),
            status=raw.get("status"),
            counteragent_id=raw.get("counteragent"),
            modified_at=_parse_iiko_datetime(raw.get("dateModified")),
            modified_by_id=raw.get("userModified"),
            incoming_invoice_id=raw.get("incomingInvoiceId"),
            total=sum(
                (Decimal(str(item.get("sum") or 0)) for item in raw.get("items") or []),
                Decimal("0"),
            ),
        )

    def incoming(self, document_id: str) -> IikoDocument:
        """Cloud ``incoming_invoice/get``."""
        return self._document(document_id, self._cloud_get(_INCOMING_GET, document_id))

    def returned(self, document_id: str) -> IikoDocument:
        """Cloud ``returned_invoice/get``."""
        return self._document(document_id, self._cloud_get(_RETURNED_GET, document_id))

    def user_name(self, user_id: str) -> str | None:
        """iikoServer ``employees/byId`` → ``<name>``. Ошибка — ``None``: имя лишь подсказка."""
        if user_id not in self._names:
            try:
                raw = self._server_request(
                    "GET", f"/resto/api/employees/byId/{urllib.parse.quote(user_id)}"
                ).decode("utf-8", "replace")
                found = re.search(r"<name>(.*?)</name>", raw, flags=re.S)
                self._names[user_id] = found.group(1).strip() if found else None
            except IikoWatchUnavailable:
                self._names[user_id] = None
        return self._names[user_id]


class _Ledger:
    """Проводки оплат, разложенные по номеру документа, — ответ на «чем оплачен документ»."""

    def __init__(self, payments: list[LedgerPayment]) -> None:
        self._by_number: dict[str, list[LedgerPayment]] = defaultdict(list)
        for payment in payments:
            self._by_number[payment.number].append(payment)

    def payments(self, number: str | None, counteragents: set[str]) -> list[LedgerPayment]:
        """Оплаты документа с этим номером у этих контрагентов.

        Контрагент неизвестен (нет псевдонима iiko) — берём все документы с номером: чужая
        проводка может замаскировать пропажу, но не родит ложный случай. Строку отчёта без
        контрагента не отбрасываем по той же причине."""
        if not number:
            return []
        rows = self._by_number.get(number, [])
        if not counteragents:
            return list(rows)
        return [
            row
            for row in rows
            if row.counteragent_id is None or row.counteragent_id in counteragents
        ]


def _total(payments: list[LedgerPayment]) -> Decimal:
    return sum((payment.amount for payment in payments), Decimal("0"))


def _covers(found: Decimal, expected: Decimal) -> bool:
    """Оплата на месте: проводок по документу не меньше нашей суммы (копейки округления терпим).

    Больше — тоже на месте: человек мог доплатить в бэк-офисе; судим только недостачу."""
    return found > 0 and found >= expected - AMOUNT_TOLERANCE


def _paid_in_full(found: Decimal, expected: Decimal, document: IikoDocument | None) -> bool:
    """Документ в iiko не пострадал: стоит оплата Тепло — или документ оплачен на всю свою сумму.

    Второе — когда накладную после оплаты уменьшили и в бэк-офисе переставили оплату ровно на
    новую сумму: Тепло ставило 18 477,13 ₽, накладная стала 18 476,08 ₽, и в iiko оплачено
    18 476,08 ₽ (ИП Карпов, накладная 18, 06.08.2026). Долга в iiko нет — пропажи тоже."""
    if _covers(found, expected):
        return True
    return (
        document is not None
        and document.found
        and document.status != _DELETED
        and bool(document.total)
        and found >= (document.total or Decimal("0")) - AMOUNT_TOLERANCE
    )


def _rub(amount: Decimal) -> str:
    """``2583.56`` → ``2 583,56 ₽`` — как суммы пишут люди."""
    text = f"{Decimal(amount).quantize(Decimal('0.01')):,.2f}"
    return text.replace(",", " ").replace(".", ",") + " ₽"


def _when(moment: datetime | None) -> str:
    """Момент по Москве: ``13.10.2026 15:01``."""
    if moment is None:
        return "—"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(_MSK).strftime("%d.%m.%Y %H:%M")


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


@dataclass
class _Edit:
    """Последняя правка документа в iiko — подсказка человеку, а не обвинение."""

    at: datetime | None
    by_id: str | None
    by_name: str | None


@dataclass
class InvoiceDrift:
    """Найденная картина по одной накладной: одно правило, список отклонений."""

    invoice_id: uuid.UUID
    reason_code: str
    amount: Decimal  # сумма, которую случай показывает крупно
    deviations: list[dict[str, Any]]


@dataclass
class DriftReport:
    """Итог чтения iiko: что разошлось, что сверено чисто, что не удалось проверить."""

    findings: list[InvoiceDrift] = field(default_factory=list)
    # (накладная, правило), сверенные полностью и без отклонений: их висящий случай закрывается.
    clean: set[tuple[uuid.UUID, str]] = field(default_factory=set)
    # строки журнала, чья оплата сейчас найдена в iiko, — им ставится ``iiko_seen_at``.
    seen_push_ids: set[uuid.UUID] = field(default_factory=set)
    # накладные, которые сторож сейчас сторожит, и правило, по которому каждую рассудил целиком:
    # по ним закрываются случаи, ставшие ненужными (оплату у нас откатили, чек исправили).
    watched: set[uuid.UUID] = field(default_factory=set)
    judged: dict[uuid.UUID, str] = field(default_factory=dict)
    checked_payments: int = 0
    checked_pairs: int = 0
    unavailable: int = 0


def _rule1_eligible(push: IikoInvoicePaymentPush, now: datetime) -> bool:
    """Строку судит сторож, а не сверка: сверка либо уже подтвердила её, либо до неё не дотянется.

    Неподтверждённую свежую строку (окно ``VERIFY_MAX_AGE``) ведёт сверка — она сама
    переотправит платёж; вмешаться здесь значило бы завести случай на то, что ещё лечится само.
    Исчерпавшую переотправки сверка уже отдала в ручной случай — второй не нужен."""
    if push.verified_at is not None:
        return True
    created = push.created_at if push.created_at.tzinfo else push.created_at.replace(tzinfo=UTC)
    return created < now - VERIFY_MAX_AGE and push.resend_count < MAX_RESENDS


async def _edit_of(document: IikoDocument | None, source: IikoWatchSource) -> _Edit:
    if document is None or not document.found:
        return _Edit(None, None, None)
    name = None
    if document.modified_by_id:
        by_id = document.modified_by_id
        name = await anyio.to_thread.run_sync(lambda: source.user_name(by_id))
    return _Edit(document.modified_at, document.modified_by_id, name)


def _edit_clause(edit: _Edit, last_seen: datetime | None) -> tuple[str, bool | None]:
    """Фраза о последней правке документа и признак «правили уже после того, как оплату видели».

    Правка позже последнего «видели» — правдоподобный момент, когда оплату сняли. Правка раньше —
    сам документ не трогали, оплату убрали отдельно от него (так было у Ч-123 13.10.2026)."""
    if edit.at is None:
        return "", None
    who = f"«{edit.by_name}»" if edit.by_name else f"пользователь {edit.by_id or '—'}"
    clause = f" Последняя правка документа в iiko — {_when(edit.at)}, {who}."
    if last_seen is None:
        return clause, None
    after = edit.at > last_seen
    if not after:
        clause += (
            " Правка раньше, чем оплату последний раз видели в iiko, — оплату сняли отдельно"
            " от правки документа."
        )
    return clause, after


def _seen_bound(pushes: list[IikoInvoicePaymentPush]) -> datetime | None:
    """Когда оплату последний раз видели в iiko: отметка сторожа, иначе подтверждение сверки."""
    moments = [m for p in pushes for m in (p.iiko_seen_at, p.verified_at) if m is not None]
    return max(moments) if moments else None


def _seen_phrase(pushes: list[IikoInvoicePaymentPush], last_seen: datetime | None) -> str:
    """Чем подтверждено, что оплата была. Сторож сверяет сумму документа целиком; сверка же
    подтверждала каждую долю чека отдельно — доля могла найтись, а вся сумма так и не сойтись,
    поэтому «видели целиком» говорим только по отметке сторожа."""
    if any(p.iiko_seen_at is not None for p in pushes):
        return f" Последний раз оплату целиком видели в iiko {_when(last_seen)}."
    return f" Проводку оплаты сверка подтверждала {_when(last_seen)}."


async def _removed_payment_deviation(
    *,
    code: str,
    title: str,
    pushes: list[IikoInvoicePaymentPush],
    document: IikoDocument | None,
    number: str,
    expected: Decimal,
    found: Decimal,
    source: IikoWatchSource,
) -> dict[str, Any]:
    """Строка случая «оплаты, поставленной Тепло, в iiko нет» — общая для обоих правил."""
    last_seen = _seen_bound(pushes)
    confirmed = any(p.verified_at is not None or p.iiko_seen_at is not None for p in pushes)
    pushed_at = min(p.created_at for p in pushes)
    edit = await _edit_of(document, source)
    missing = (expected - found).quantize(Decimal("0.01"))
    text = (
        f"{title} {number}: оплата {_rub(expected)}, которую Тепло поставило в iiko"
        f" {_when(pushed_at)},"
    )
    if found > 0:
        text += f" в учёте iiko покрыта лишь на {_rub(found)} — не хватает {_rub(missing)}."
    else:
        text += " в учёте iiko не найдена."
    deleted = document is not None and (not document.found or document.status == _DELETED)
    if document is not None and not document.found:
        text += " Самого документа в iiko больше нет."
    elif deleted:
        text += " Документ в iiko удалён."
    if confirmed:
        text += _seen_phrase(pushes, last_seen)
    else:
        text += " Подтверждения проводки не было ни разу."
    clause, after = _edit_clause(edit, last_seen if confirmed else None)
    return {
        "code": code,
        # Документ, на который Тепло ставило оплату: его GUID помнит строка журнала.
        "document_id": document.document_id if document is not None else pushes[0].external_id,
        "document_number": number,
        "amount": str(missing),
        "expected": str(expected.quantize(Decimal("0.01"))),
        "found": str(found.quantize(Decimal("0.01"))),
        "pushed_at": _iso(pushed_at),
        "last_seen_at": _iso(last_seen) if confirmed else None,
        "confirmed": confirmed,
        "document_deleted": deleted,
        "modified_at": _iso(edit.at),
        "modified_by_id": edit.by_id,
        "modified_by": edit.by_name,
        "modified_after_seen": after,
        "push_keys": sorted(p.idempotency_key for p in pushes),
        "text": text + clause,
    }


async def _check_payments(
    *,
    invoice: SupplierInvoice,
    pushes: list[IikoInvoicePaymentPush],
    ledger: _Ledger,
    counteragents: set[str],
    source: IikoWatchSource,
    report: DriftReport,
) -> None:
    """Правило 1 для неисправленной накладной: каждая её оплата Тепло стоит в учёте iiko."""
    by_document: dict[str, list[IikoInvoicePaymentPush]] = defaultdict(list)
    for push in pushes:
        by_document[push.external_id].append(push)
    deviations: list[dict[str, Any]] = []
    for document_id, group in by_document.items():
        report.checked_payments += len(group)
        expected = sum((p.amount for p in group), Decimal("0"))
        number = invoice.number or ""
        found = _total(ledger.payments(number, counteragents))
        if _covers(found, expected):
            report.seen_push_ids.update(p.id for p in group)
            continue
        # По номеру из нашей базы не нашли — спросим сам документ: номер в iiko могли поменять,
        # а контрагента у документа — переназначить. Только после этого считаем оплату снятой.
        try:
            document = await anyio.to_thread.run_sync(
                lambda doc_id=document_id: source.incoming(doc_id)
            )
        except IikoWatchUnavailable as exc:
            logger.warning("сторож оплат: документ %s не прочитан — %s", document_id, exc)
            report.unavailable += 1
            return  # судить накладную по половине фактов нельзя — вернёмся следующим проходом
        if document.found and document.status != _DELETED:
            number = document.number or number
            doc_counteragents = (
                {document.counteragent_id} if document.counteragent_id else counteragents
            )
            found = _total(ledger.payments(number, doc_counteragents))
            if _paid_in_full(found, expected, document):
                report.seen_push_ids.update(p.id for p in group)
                continue
        deviations.append(
            await _removed_payment_deviation(
                code=DEV_PAYMENT_REMOVED,
                title="Накладная",
                pushes=group,
                document=document,
                number=number,
                expected=expected,
                found=found,
                source=source,
            )
        )
    if deviations:
        report.findings.append(
            InvoiceDrift(
                invoice_id=invoice.id,
                reason_code=REASON_PAYMENT_REMOVED,
                amount=sum((Decimal(d["amount"]) for d in deviations), Decimal("0")),
                deviations=deviations,
            )
        )
    else:
        report.clean.add((invoice.id, REASON_PAYMENT_REMOVED))
    report.judged[invoice.id] = REASON_PAYMENT_REMOVED


async def _check_correction_pair(
    *,
    invoice: SupplierInvoice,
    pushes: list[IikoInvoicePaymentPush],
    ledger: _Ledger,
    counteragents: set[str],
    source: IikoWatchSource,
    report: DriftReport,
) -> None:
    """Правило 2: исправленный чек — исходная X оплачена, возврат X проведён, «(испр.)» без
    оплаты. Любое отклонение от этих трёх условий — строка случая."""
    from app.services.warehouse_invoice_push import _correction_number

    report.checked_pairs += 1
    correction_id = invoice.iiko_correction_new_external_id or ""
    return_id = invoice.iiko_return_external_id
    # После исправления external_id накладной указывает на «(испр.)», а исходную X помнят строки
    # журнала — Тепло ставило оплату именно на неё — и ссылка возврата.
    original_pushes = [p for p in pushes if p.external_id != correction_id]
    try:
        returned = (
            await anyio.to_thread.run_sync(lambda: source.returned(return_id))
            if return_id
            else None
        )
        original_ids = {p.external_id for p in original_pushes}
        original_id = next(iter(original_ids)) if len(original_ids) == 1 else None
        if original_id is None and returned is not None and returned.found:
            original_id = returned.incoming_invoice_id
        correction = await anyio.to_thread.run_sync(lambda: source.incoming(correction_id))
        original = (
            await anyio.to_thread.run_sync(lambda: source.incoming(original_id))
            if original_id
            else None
        )
    except IikoWatchUnavailable as exc:
        logger.warning("сторож оплат: пара исправления %s не прочитана — %s", invoice.id, exc)
        report.unavailable += 1
        return

    base_number = invoice.number or ""
    original_number = (
        original.number if original is not None and original.found else None
    ) or base_number
    correction_number = (
        (correction.number if correction.found else None) or _correction_number(base_number) or ""
    )
    original_counteragents = (
        {original.counteragent_id}
        if original is not None and original.found and original.counteragent_id
        else counteragents
    )
    correction_counteragents = (
        {correction.counteragent_id}
        if correction.found and correction.counteragent_id
        else counteragents
    )
    deviations: list[dict[str, Any]] = []

    # 1. Исходная X оплачена — судим только оплату, которую ставило Тепло: если Тепло её не
    #    ставило вовсе, это «оплата оригинала так и не дошла» (дозор sweep_unsettled), не пропажа.
    original_found = _total(ledger.payments(original_number, original_counteragents))
    if original_pushes:
        expected = sum((p.amount for p in original_pushes), Decimal("0"))
        found = original_found
        if _paid_in_full(found, expected, original):
            report.seen_push_ids.update(p.id for p in original_pushes)
        else:
            deviations.append(
                await _removed_payment_deviation(
                    code=DEV_ORIGINAL_PAYMENT_REMOVED,
                    title="Исходная накладная",
                    pushes=original_pushes,
                    document=original,
                    number=original_number,
                    expected=expected,
                    found=found,
                    source=source,
                )
            )

    # 2. «(испр.)» без оплаты: возврат уже погасил исходную, оплата новой задваивает расчёт.
    #    Человек видит неоплаченную «(испр.)» долгом и оплачивает её — так у «Местного закупа»
    #    к 06.10.2026 у всех трёх пар стояли оплаты и X, и «(испр.)» при проведённом возврате;
    #    13.10 задвоение сняли руками, у Ч-116 и Ч-123 — оплатой исходной. Эти два состояния
    #    называем разными словами: человеку важно, задвоилась оплата или переехала.
    correction_paid = ledger.payments(correction_number, correction_counteragents)
    if correction_paid:
        paid_total = _total(correction_paid)
        edit = await _edit_of(correction, source)
        dates = ", ".join(
            sorted({p.paid_on.strftime("%d.%m.%Y") for p in correction_paid if p.paid_on})
        )
        paid_words = f"{_rub(paid_total)}{f', дата оплаты {dates}' if dates else ''}"
        original_paid = original_found > 0
        if original_paid:
            text = (
                f"Оплата задвоилась: в iiko оплачены и исходная накладная {original_number}"
                f" ({_rub(original_found)}), и новая «{correction_number}» ({paid_words}),"
                " а возврат уже погасил приход исходной. По схеме исправления «(испр.)»"
                " остаётся без оплаты."
            )
        elif original_pushes:
            text = (
                f"Оплата переехала: у исходной накладной {original_number} её нет, а новая"
                f" «{correction_number}» оплачена ({paid_words}). По схеме исправления оплата"
                " стоит на исходной, а «(испр.)» остаётся без оплаты."
            )
        else:
            # Тепло оплату исходной не ставило (так у «АЛЬЯНС ЮГ»: отправка не прошла) —
            # «переехала» было бы домыслом, говорим только то, что видно в iiko.
            text = (
                f"У исходной накладной {original_number} оплаты нет, а новая"
                f" «{correction_number}» оплачена ({paid_words}). По схеме исправления оплата"
                " стоит на исходной, а «(испр.)» остаётся без оплаты."
            )
        clause, _after = _edit_clause(edit, None)
        deviations.append(
            {
                "code": DEV_CORRECTION_PAID,
                "document_id": correction_id,
                "document_number": correction_number,
                "amount": str(paid_total.quantize(Decimal("0.01"))),
                # True — задвоение (оплачены обе), False — оплата переехала на «(испр.)».
                "original_paid": original_paid,
                "payments": [
                    {
                        "amount": str(p.amount),
                        "paid_on": p.paid_on.isoformat() if p.paid_on else None,
                        "account": p.account,
                    }
                    for p in correction_paid
                ],
                "modified_at": _iso(edit.at),
                "modified_by_id": edit.by_id,
                "modified_by": edit.by_name,
                "text": text + clause,
            }
        )

    # 3. Возврат X проведён и ссылается на X.
    return_problem: str | None = None
    return_number = returned.number if returned is not None and returned.found else None
    if returned is None:
        return_problem = (
            "Возврат исходной накладной не заведён: «(испр.)» стоит приходом рядом с исходной."
        )
    elif not returned.found:
        return_problem = f"Возврат исходной накладной (документ {return_id}) в iiko не найден."
    elif returned.status == _DELETED:
        return_problem = f"Возврат {return_number or '—'} исходной накладной удалён в iiko."
    elif returned.status != _PROCESSED:
        return_problem = (
            f"Возврат {return_number or '—'} исходной накладной не проведён в iiko"
            f" (статус {returned.status or '—'})."
        )
    elif original_id and returned.incoming_invoice_id != original_id:
        return_problem = (
            f"Возврат {return_number or '—'} ссылается не на исходную накладную {original_number}."
        )
    if return_problem is not None:
        edit = await _edit_of(returned, source)
        clause, _after = _edit_clause(edit, None)
        deviations.append(
            {
                "code": DEV_RETURN_MISSING,
                "document_id": return_id,
                "document_number": return_number,
                "status": returned.status if returned is not None else None,
                "amount": "0.00",
                "modified_at": _iso(edit.at),
                "modified_by_id": edit.by_id,
                "modified_by": edit.by_name,
                "text": return_problem + clause,
            }
        )

    # 4. Сама «(испр.)» на месте: без неё товар правильной поставки в iiko не приходован.
    if not correction.found or correction.status == _DELETED:
        edit = await _edit_of(correction, source)
        clause, _after = _edit_clause(edit, None)
        deviations.append(
            {
                "code": DEV_CORRECTION_DELETED,
                "document_id": correction_id,
                "document_number": correction_number,
                "amount": "0.00",
                "modified_at": _iso(edit.at),
                "modified_by_id": edit.by_id,
                "modified_by": edit.by_name,
                "text": (
                    f"Новая накладная «{correction_number}» "
                    f"{'удалена в iiko' if correction.found else 'в iiko не найдена'}."
                )
                + clause,
            }
        )

    if deviations:
        # Крупно — оплата исходной, которую ставило Тепло; без неё — лишняя оплата «(испр.)».
        original_total = sum((p.amount for p in original_pushes), Decimal("0"))
        report.findings.append(
            InvoiceDrift(
                invoice_id=invoice.id,
                reason_code=REASON_CORRECTION_PAIR,
                amount=original_total or _total(correction_paid),
                deviations=deviations,
            )
        )
    else:
        report.clean.add((invoice.id, REASON_CORRECTION_PAIR))
    report.judged[invoice.id] = REASON_CORRECTION_PAIR


async def collect_payment_drift(
    session: AsyncSession, source: IikoWatchSource, *, now: datetime | None = None
) -> DriftReport:
    """Прочитать iiko и сравнить с журналом отправок. Ничего не пишет ни в базу, ни в iiko.

    Отдельно от записи случаев, чтобы ту же сверку можно было прогнать на бою вхолостую
    (``app.scripts.iiko_payment_watch_report``)."""
    now = now or datetime.now(UTC)
    report = DriftReport()
    rows = (
        await session.execute(
            select(IikoInvoicePaymentPush, SupplierInvoice)
            .join(SupplierInvoice, SupplierInvoice.id == IikoInvoicePaymentPush.invoice_id)
            .where(
                IikoInvoicePaymentPush.status == "ok",
                IikoInvoicePaymentPush.amount > 0,
                IikoInvoicePaymentPush.account_to != _MANUAL_ACCOUNT,
                # Маркер «Касса: зеркалирование завершено» — служебная строка, не оплата.
                IikoInvoicePaymentPush.idempotency_key.not_like(
                    "kassa\\_goods\\_done:%", escape="\\"
                ),
                # Оплату у нас откатили — её отсутствие в iiko теперь норма, а не пропажа.
                SupplierInvoice.payment_status.in_(("paid", "partially_paid")),
            )
            .order_by(IikoInvoicePaymentPush.created_at)
        )
    ).all()
    corrected = (
        await session.scalars(
            select(SupplierInvoice).where(
                SupplierInvoice.iiko_correction_new_external_id.is_not(None),
                SupplierInvoice.payment_status.in_(("paid", "partially_paid")),
            )
        )
    ).all()

    pushes_by_invoice: dict[uuid.UUID, list[IikoInvoicePaymentPush]] = defaultdict(list)
    invoices: dict[uuid.UUID, SupplierInvoice] = {inv.id: inv for inv in corrected}
    for push, invoice in rows:
        invoices[invoice.id] = invoice
        if invoice.iiko_correction_new_external_id is not None or _rule1_eligible(push, now):
            pushes_by_invoice[invoice.id].append(push)
    to_check = [
        inv
        for inv in invoices.values()
        if inv.iiko_correction_new_external_id is not None or pushes_by_invoice.get(inv.id)
    ]
    report.watched = {inv.id for inv in to_check}
    if not to_check:
        return report

    aliases: dict[uuid.UUID, set[str]] = defaultdict(set)
    for counterparty_id, alias in (
        await session.execute(
            select(CounterpartyAlias.counterparty_id, CounterpartyAlias.alias).where(
                CounterpartyAlias.source == "iiko",
                CounterpartyAlias.counterparty_id.in_({inv.counterparty_id for inv in to_check}),
            )
        )
    ).all():
        aliases[counterparty_id].add(alias)

    anchors = [
        d
        for inv in to_check
        for d in (
            inv.invoice_date,
            inv.issued_at.date() if inv.issued_at else None,
            *(p.created_at.date() for p in pushes_by_invoice.get(inv.id, [])),
        )
        if d is not None
    ]
    date_from = (min(anchors) if anchors else now.date()) - LEDGER_LOOKBACK
    date_to = now.date() + LEDGER_LOOKAHEAD
    # Без проводок судить нечего: ошибка поднимается наверх, джоб повторит следующим проходом.
    ledger = _Ledger(await anyio.to_thread.run_sync(lambda: source.ledger(date_from, date_to)))

    for invoice in to_check:
        pushes = pushes_by_invoice.get(invoice.id, [])
        counteragents = aliases.get(invoice.counterparty_id, set())
        if invoice.iiko_correction_new_external_id is not None:
            await _check_correction_pair(
                invoice=invoice,
                pushes=pushes,
                ledger=ledger,
                counteragents=counteragents,
                source=source,
                report=report,
            )
        else:
            await _check_payments(
                invoice=invoice,
                pushes=pushes,
                ledger=ledger,
                counteragents=counteragents,
                source=source,
                report=report,
            )
    return report


def _fingerprint(deviations: list[dict[str, Any]]) -> str:
    """Отпечаток картины: какие отклонения и на каких документах. Суммы и даты не входят —
    «Разобрано» гасит саму картину, а не её копейки."""
    return "|".join(sorted(f"{d['code']}:{d.get('document_id') or '-'}" for d in deviations))


async def _pending_case(
    session: AsyncSession, invoice_id: uuid.UUID, reason_code: str
) -> ReconciliationCase | None:
    return await session.scalar(
        select(ReconciliationCase)
        .where(
            ReconciliationCase.kind == CASE_KIND,
            ReconciliationCase.status == "pending",
            ReconciliationCase.payload["invoice_id"].astext == str(invoice_id),
            ReconciliationCase.payload["reason_code"].astext == reason_code,
        )
        .limit(1)
    )


async def _upsert_case(session: AsyncSession, drift: InvoiceDrift, now: datetime) -> str:
    """Завести или обновить случай по накладной. Возвращает ``opened``/``updated``/``silenced``.

    Один висящий случай на (накладную, правило): повторный проход обновляет строки на месте и
    сохраняет, когда каждое отклонение заметили впервые, — это и есть «когда пропала» сверху."""
    fingerprint = _fingerprint(drift.deviations)
    existing = await _pending_case(session, drift.invoice_id, drift.reason_code)
    first_seen: dict[str, str] = {}
    if existing is not None:
        for old in (existing.payload or {}).get("deviations") or []:
            key = f"{old.get('code')}:{old.get('document_id') or '-'}"
            if old.get("detected_at"):
                first_seen[key] = old["detected_at"]
    else:
        silenced = await session.scalar(
            select(ReconciliationCase.id)
            .where(
                ReconciliationCase.kind == CASE_KIND,
                ReconciliationCase.status == "dismissed",
                ReconciliationCase.payload["invoice_id"].astext == str(drift.invoice_id),
                ReconciliationCase.payload["fingerprint"].astext == fingerprint,
            )
            .limit(1)
        )
        if silenced is not None:
            return "silenced"
    deviations = []
    for deviation in drift.deviations:
        key = f"{deviation['code']}:{deviation.get('document_id') or '-'}"
        deviations.append({**deviation, "detected_at": first_seen.get(key, now.isoformat())})

    invoice = await session.get(SupplierInvoice, drift.invoice_id)
    supplier = (
        await session.get(Counterparty, invoice.counterparty_id) if invoice is not None else None
    )
    issued = None
    if invoice is not None:
        issued = (
            invoice.issued_at.isoformat()
            if invoice.issued_at
            else (invoice.invoice_date.isoformat() if invoice.invoice_date else None)
        )
    payload = {
        "invoice_id": str(drift.invoice_id),
        "invoice_number": invoice.number if invoice is not None else None,
        "supplier_name": supplier.name if supplier is not None else None,
        "issued_at": issued,
        "amount": str(drift.amount.quantize(Decimal("0.01"))),
        "reason_code": drift.reason_code,
        "reason": " ".join(d["text"] for d in deviations),
        "deviations": deviations,
        "fingerprint": fingerprint,
        # Ставить оплату заново автоматически или только показывать — решение владельца; пока
        # сторож только показывает, кнопки повтора у случая нет.
        "retriable": False,
        "checked_at": now.isoformat(),
    }
    if existing is not None:
        existing.payload = payload
        return "updated"
    session.add(
        ReconciliationCase(kind=CASE_KIND, status="pending", provider="iiko", payload=payload)
    )
    logger.warning(
        "сторож оплат: накладная %s разошлась с iiko (%s): %s",
        drift.invoice_id,
        drift.reason_code,
        payload["reason"],
    )
    return "opened"


async def apply_payment_drift(
    session: AsyncSession, report: DriftReport, *, now: datetime | None = None
) -> dict[str, int]:
    """Записать итог: случаи открыть/обновить, сверенные чисто — закрыть, увиденным — отметка."""
    now = now or datetime.now(UTC)
    result = {
        "checked_payments": report.checked_payments,
        "checked_pairs": report.checked_pairs,
        "unavailable": report.unavailable,
        "opened": 0,
        "updated": 0,
        "silenced": 0,
        "resolved": 0,
    }
    # Сначала — висящие случаи, которым больше нечего показывать. Накладную, которую в этот
    # проход прочитать не удалось, не трогаем: её судьба выяснится следующим проходом.
    found_keys = {(drift.invoice_id, drift.reason_code) for drift in report.findings}
    pending = (
        await session.scalars(
            select(ReconciliationCase).where(
                ReconciliationCase.kind == CASE_KIND, ReconciliationCase.status == "pending"
            )
        )
    ).all()
    for case in pending:
        try:
            invoice_id = uuid.UUID(str((case.payload or {}).get("invoice_id")))
        except ValueError:
            continue
        key = (invoice_id, (case.payload or {}).get("reason_code"))
        if key in found_keys:
            continue
        if key in report.clean:
            why = "iiko_matches_again"  # iiko снова совпадает со схемой
        elif invoice_id not in report.watched:
            why = "no_longer_watched"  # оплату у нас откатили — сторожить нечего
        elif invoice_id in report.judged and report.judged[invoice_id] != key[1]:
            why = "rule_changed"  # чек исправили: теперь его судит правило пары
        else:
            continue
        case.status = "resolved"
        case.resolved_at = now
        case.resolution_payload = {"reason": why}
        result["resolved"] += 1
    for drift in report.findings:
        result[await _upsert_case(session, drift, now)] += 1
    if report.seen_push_ids:
        pushes = (
            await session.scalars(
                select(IikoInvoicePaymentPush).where(
                    IikoInvoicePaymentPush.id.in_(report.seen_push_ids)
                )
            )
        ).all()
        for push in pushes:
            push.iiko_seen_at = now
    await session.commit()
    return result


async def watch_iiko_payments(
    session: AsyncSession, *, source: IikoWatchSource | None = None
) -> dict[str, int]:
    """Проход сторожа: прочитать iiko, сравнить с журналом, завести/закрыть случаи.

    Без ``source`` читает живую iiko (только чтение) с кредами из vault. Ошибка отчёта по
    проводкам поднимается наверх — состояние в базе не тронуто, джоб повторит проход."""
    if source is None:
        from app.services.iiko_sync import _load_source_credential_env

        await _load_source_credential_env(session)
        source = LiveIikoWatchSource()
    now = datetime.now(UTC)
    report = await collect_payment_drift(session, source, now=now)
    return await apply_payment_drift(session, report, now=now)
