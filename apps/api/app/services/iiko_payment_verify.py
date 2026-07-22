"""Пост-проверка зеркала оплат: подтверждать проводку в iiko, а не верить ответу ``add_payment``.

Зачем: ``add_payment`` отвечает 201 с ``accountingTransactionId``, но проводка ``INVOICE_PAYMENT``
в учёте появляется НЕ всегда. Сплошная сверка 22.07.2026: из 55 успешных отправок 51 подтверждена
проводкой, 4 — нет (накладные 4-2, Ч-38, 31, 36), причём внутри одной серии часть запросов прошла,
часть — нет. Со стороны Cloud API отличить эти случаи нечем: ответ одинаково успешный.

Как проверяем: OLAP-отчёт ``TRANSACTIONS`` через iikoServer API — единственный источник, который
показывает фактические проводки. Ключ сопоставления — (номер документа, сумма): GUID документа в
TRANSACTIONS не отдаётся, а номера накладных повторяются, поэтому сумма обязательна.

Что делаем с неподтверждённым: даём iiko ``VERIFY_GRACE`` на проведение, затем несколько раз
перепроверяем и только после этого переотправляем платёж (снимаем ``ok`` и done-маркер, чтобы
зеркалящий джоб взял накладную снова). Переотправок не больше ``MAX_RESENDS`` — дальше кейс в
owner-review, иначе при системном сбое iiko мы бы долбили его вечно и рисковали задвоить оплату.
"""

from __future__ import annotations

import http.client as hc
import json
import logging
import ssl
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import anyio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import IikoInvoicePaymentPush, SupplierInvoice

logger = logging.getLogger(__name__)

# Сколько ждать после отправки, прежде чем судить об отсутствии проводки. Проводка появляется
# практически сразу, но Server↔Cloud синхронизация иногда отстаёт — берём с запасом.
VERIFY_GRACE = timedelta(minutes=30)
# Старые пуши не проверяем: OLAP-окно ограничено, а разбор древних расхождений — ручной.
VERIFY_MAX_AGE = timedelta(days=30)
# Столько раз подряд не нашли проводку → переотправляем платёж.
VERIFY_ATTEMPTS_BEFORE_RESEND = 3
# Столько переотправок допускаем, дальше — ручной разбор (страховка от задвоения).
MAX_RESENDS = 2
# Запас по датам для OLAP: проводка датируется датой накладной, а не датой отправки.
OLAP_LOOKBACK = timedelta(days=14)
# Разные копейки округления между нашей суммой и суммой проводки терпим.
AMOUNT_TOLERANCE = Decimal("0.05")

_TRANSACTIONS_PATH = "/resto/api/v2/reports/olap"


def fetch_invoice_payment_transactions(
    date_from: date, date_to: date
) -> list[tuple[str, Decimal]]:
    """Проводки ``INVOICE_PAYMENT`` за период: список (номер документа, сумма).

    Синхронно (исполнять в треде) — iikoServer API, тот же транспорт, что у OLAP-выгрузок курьеров.
    """
    from app.services.couriers.iiko_olap_sync import _auth_token, _iiko_host_and_port

    token = _auth_token()
    host, port = _iiko_host_and_port()
    body = json.dumps(
        {
            "reportType": "TRANSACTIONS",
            "buildSummary": False,
            # Счёт в группировке обязателен: без него доли одного документа (карта + наличные)
            # слиплись бы в одну строку и ни одна наша запись не совпала бы с ней.
            "groupByRowFields": ["Document", "TransactionType", "Account.Name"],
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
                }
            },
        }
    ).encode("utf-8")
    ctx = ssl._create_unverified_context()
    conn = hc.HTTPSConnection(host, port, timeout=180, context=ctx)
    try:
        conn.request(
            "POST",
            f"{_TRANSACTIONS_PATH}?key={token}",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        raw = response.read()
        if response.status != 200:
            raise RuntimeError(f"iiko OLAP TRANSACTIONS failed: {response.status} {raw[:300]!r}")
        rows = json.loads(raw).get("data", [])
    finally:
        conn.close()

    payments: list[tuple[str, Decimal]] = []
    for row in rows:
        if row.get("TransactionType") != "INVOICE_PAYMENT":
            continue
        amount = Decimal(str(row.get("Sum.Outgoing") or 0))
        if amount <= 0:  # вторая (зеркальная) сторона проводки — берём только расход
            continue
        payments.append((str(row.get("Document") or ""), amount))
    return payments


def _covered(payments: list[tuple[str, Decimal]], number: str, amount: Decimal) -> bool:
    """Платёж подтверждён, если проводок по документу набирается НЕ МЕНЬШЕ нашей суммы.

    Сравнивать построчно нельзя: OLAP не отдаёт GUID документа, и одна наша отправка может лежать
    там несколькими строками — смешанный чек идёт двумя долями (карта + наличные) на разные счета,
    а непредставимая сумма дробится на части. Точное равенство отдельной строке в таких случаях не
    достигается, и платёж выглядел бы потерянным (проверено на проде: накладная №4, доли 1515 и
    300, наша запись на 1515). Перебор в другую сторону — подтвердить чужой проводкой при
    совпадении номера — безопаснее: цена ложной потери всего лишь пропущенная сверка, а цена
    ложной переотправки — задвоенная оплата в учёте iiko."""
    total = sum((value for doc, value in payments if doc == number), Decimal("0"))
    return total >= amount - AMOUNT_TOLERANCE and total > 0


async def _clear_kassa_done_marker(session: AsyncSession, invoice_id) -> None:
    """Снять маркер ``kassa_goods_done:<id>`` — иначе зеркалящий джоб не возьмёт накладную снова."""
    await session.execute(
        delete(IikoInvoicePaymentPush).where(
            IikoInvoicePaymentPush.idempotency_key == f"kassa_goods_done:{invoice_id}"
        )
    )


async def verify_mirrored_payments(session: AsyncSession, *, limit: int = 200) -> dict[str, int]:
    """Подтвердить проводки по отправленным платежам; неподтверждённые — переотправить.

    Не бросает на пустой выборке; ошибка OLAP поднимается наверх (джоб залогирует и повторит
    следующим проходом — состояние в БД не тронуто)."""
    now = datetime.now(UTC)
    rows = (
        await session.scalars(
            select(IikoInvoicePaymentPush)
            .join(SupplierInvoice, SupplierInvoice.id == IikoInvoicePaymentPush.invoice_id)
            .where(
                IikoInvoicePaymentPush.status == "ok",
                IikoInvoicePaymentPush.amount > 0,
                IikoInvoicePaymentPush.invoice_id.is_not(None),
                IikoInvoicePaymentPush.verified_at.is_(None),
                IikoInvoicePaymentPush.created_at <= now - VERIFY_GRACE,
                IikoInvoicePaymentPush.created_at >= now - VERIFY_MAX_AGE,
                IikoInvoicePaymentPush.resend_count < MAX_RESENDS,
                # Накладная перестала быть оплаченной (оплату откатили) — отсутствие проводки в
                # iiko теперь НОРМА, а не потеря: переотправлять платёж нельзя, он вернул бы
                # ошибочную оплату. Такие пуши просто оставляем как есть.
                SupplierInvoice.payment_status == "paid",
            )
            .order_by(IikoInvoicePaymentPush.created_at)
            .limit(limit)
        )
    ).all()

    result = {"checked": len(rows), "verified": 0, "pending": 0, "resent": 0, "manual": 0}
    if not rows:
        return result

    date_from = min(row.created_at for row in rows).date() - OLAP_LOOKBACK
    payments = await anyio.to_thread.run_sync(
        lambda: fetch_invoice_payment_transactions(date_from, now.date())
    )

    for row in rows:
        invoice = await session.get(SupplierInvoice, row.invoice_id)
        number = (invoice.number or "") if invoice is not None else ""
        if number and _covered(payments, number, row.amount):
            row.verified_at = now
            result["verified"] += 1
            continue

        row.verify_attempts += 1
        if row.verify_attempts < VERIFY_ATTEMPTS_BEFORE_RESEND:
            result["pending"] += 1
            continue

        # Проводки нет и после нескольких проверок — платёж до учёта iiko не дошёл. Снимаем ok,
        # чтобы зеркалящий джоб отправил его заново.
        row.status = "error"
        row.error = "проводка в iiko не подтверждена — переотправка"
        row.attempts = 0
        row.verify_attempts = 0
        row.resend_count += 1
        if row.idempotency_key.startswith("kassa_goods:"):
            await _clear_kassa_done_marker(session, row.invoice_id)
        result["resent"] += 1
        logger.warning(
            "iiko payment verify: проводка по %s не найдена — переотправка %s из %s",
            row.idempotency_key,
            row.resend_count,
            MAX_RESENDS,
        )
        if row.resend_count >= MAX_RESENDS:
            from app.services.counterparty_iiko_payment import _open_iiko_payment_case

            await _open_iiko_payment_case(
                session,
                invoice_id=row.invoice_id,
                external_id=row.external_id,
                amount=row.amount,
                reason=(
                    "iiko принимает add_payment, но проводка в учёте не появляется — "
                    f"исчерпаны {MAX_RESENDS} автоматические переотправки, нужен ручной разбор"
                ),
                reason_code="payment_not_in_iiko_ledger",
            )
            result["manual"] += 1

    await session.commit()
    return result
