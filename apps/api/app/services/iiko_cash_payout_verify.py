"""Пост-сверка денежных проводок в iiko: подтверждать выдачу проводкой в учёте, а не ответом API.

Зачем. ``addPayOut`` отвечает ``SUCCESS``, но это не доказывает, что проводка появилась в учёте —
ровно та же история, что с ``add_payment`` по накладным (сверка 22.07: 4 из 55 отправок не дошли).
А если ответа не было вовсе (обрыв), исход и подавно неизвестен: журнал помечает такую строку
``unknown`` и запрещает повтор, но подтвердить или опровергнуть её может только учёт iiko.

Разведка 29.07 на живом API показала, что наши проводки в OLAP-отчёте ``TRANSACTIONS`` видны с
типом ``CUSTOM`` и НАШИМ комментарием (``Приложение/…: "Возврат депозита курьеру (операция #41)"``),
поэтому сопоставляем точно по id операции, а не по паре «дата+сумма». Тогда же выяснилось, что
за 20.06–29.07 в iiko нет НИ ОДНОЙ проводки по авансам и производственным депозитам, хотя
наличные выдачи с ТК Черникова были — эта сверка и предназначена ловить такие пропажи.

Что делаем с неподтверждённым: даём iiko ``VERIFY_GRACE`` на проведение, затем перепроверяем
несколько раз и только после этого заводим кейс owner-review. Автоматически НИЧЕГО не
переотправляем: ``addPayOut`` необратим, а лишняя проводка — это выданные дважды деньги в учёте.
"""

from __future__ import annotations

import json
import logging
import ssl
from datetime import UTC, date, datetime, timedelta

import anyio
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import IikoCashPayout
from app.services.iiko_cash_payout_log import KIND_TITLES, open_manual_case

logger = logging.getLogger(__name__)

# Сколько ждать после отправки, прежде чем судить об отсутствии проводки.
VERIFY_GRACE = timedelta(minutes=30)
# Старые проводки не проверяем: OLAP-окно ограничено, а разбор древних расхождений — ручной.
VERIFY_MAX_AGE = timedelta(days=30)
# Столько раз подряд не нашли проводку → заводим кейс (по 30 минут между проходами).
VERIFY_ATTEMPTS_BEFORE_CASE = 3
# Запас по датам: проводка датируется днём выдачи, а не моментом отправки.
OLAP_LOOKBACK = timedelta(days=3)

_TRANSACTIONS_PATH = "/resto/api/v2/reports/olap"


def fetch_cash_payout_comments(date_from: date, date_to: date) -> list[str]:
    """Комментарии проводок за период (синхронно, исполнять в треде).

    Тот же транспорт, что у сверки оплат накладных. Нас интересует только текст комментария:
    id операции в нём — точный ключ сопоставления."""
    import http.client as hc

    from app.services.couriers.iiko_olap_sync import _auth_token, _iiko_host_and_port

    token = _auth_token()
    host, port = _iiko_host_and_port()
    body = json.dumps(
        {
            "reportType": "TRANSACTIONS",
            "buildSummary": False,
            "groupByRowFields": ["TransactionType", "Comment"],
            "groupByColFields": [],
            "aggregateFields": ["Sum.Outgoing", "Sum.Incoming"],
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
    return [str(row.get("Comment") or "") for row in rows]


def _is_posted(comments: list[str], source_id: str) -> bool:
    """Проводка нашлась, если чей-то комментарий несёт id этой операции.

    Ищем обе формы: у выдач id — UUID (``(операция 987ca997-…)``), у транзакции депозита
    курьера — целое с решёткой (``(операция #41)``)."""
    needles = (f"(операция {source_id})", f"(операция #{source_id})")
    return any(needle in comment for comment in comments for needle in needles)


async def verify_cash_payouts(session: AsyncSession, *, limit: int = 200) -> dict[str, int]:
    """Подтвердить проводки журнала по учёту iiko; неподтверждённые — в owner-review.

    Ошибка OLAP поднимается наверх (джоб залогирует и повторит следующим проходом — состояние
    в БД не тронуто)."""
    now = datetime.now(UTC)
    rows = (
        await session.scalars(
            select(IikoCashPayout)
            .where(
                # posted — проверяем, что ответ iiko не обманул; unknown — выясняем, чем
                # закончилась оборвавшаяся отправка. Остальные (тип не найден, явный отказ)
                # проверять нечего: проводки заведомо нет.
                or_(
                    IikoCashPayout.status == "posted",
                    IikoCashPayout.reason_code == "unknown",
                ),
                IikoCashPayout.verified_at.is_(None),
                IikoCashPayout.created_at <= now - VERIFY_GRACE,
                IikoCashPayout.created_at >= now - VERIFY_MAX_AGE,
                IikoCashPayout.verify_attempts < VERIFY_ATTEMPTS_BEFORE_CASE,
            )
            .order_by(IikoCashPayout.created_at)
            .limit(limit)
        )
    ).all()

    result = {"checked": len(rows), "verified": 0, "pending": 0, "manual": 0}
    if not rows:
        return result

    date_from = min(row.payout_date for row in rows) - OLAP_LOOKBACK
    comments = await anyio.to_thread.run_sync(
        lambda: fetch_cash_payout_comments(date_from, now.date())
    )

    for row in rows:
        if _is_posted(comments, row.source_id):
            row.verified_at = now
            if row.status != "posted":
                # Отправка оборвалась, но проводка всё-таки прошла — теперь это видно.
                row.status = "posted"
                row.reason_code = None
                row.error = None
            result["verified"] += 1
            continue

        row.verify_attempts += 1
        if row.verify_attempts < VERIFY_ATTEMPTS_BEFORE_CASE:
            result["pending"] += 1
            continue

        title = KIND_TITLES.get(row.kind, row.kind)
        await open_manual_case(
            session,
            source_id=row.source_id,
            payout_kind=row.kind,
            amount=row.amount,
            payout_date=row.payout_date,
            reason_code="not_in_iiko_ledger",
            reason=(
                f"{title} на {row.amount} от {row.payout_date.isoformat()} не найдена в учёте "
                "iiko — проведите изъятие вручную в бэк-офисе, иначе остаток «Главной кассы» "
                "останется завышенным"
            ),
            retriable=False,
        )
        result["manual"] += 1
        logger.warning(
            "iiko cash payout %s/%s: проводки в учёте нет — заведён кейс", row.kind, row.source_id
        )

    await session.commit()
    return result
