"""Проба цепочки исправления на копейках: подтвердить проводки зачёта и акта на живой iiko.

Зачем. Шаг 3 цепочки ставит оплату «(испр.)» со счёта «Авансы поставщиков». Проба 11.07.2026
показала по балансу поставщика «сдвиг 0», но её документы удалены, и проводки прочитать уже
нельзя. Пока не видно, на какой счёт и какого контрагента ложится каждая сторона зачёта (и акта
разницы), включать ``iiko_correction_offset_enabled`` нельзя.

С ``--execute`` — ЗАПИСЬ в живую iiko, только по слову владельца: на тестового поставщика
заводит исходную на 3 ₽ и оплачивает её с эквайринга (как Тепло оплачивает чек), сторнирует
её возвратом, заводит «(испр.)» на 2 ₽, оплачивает её зачётом из «Авансов поставщиков» и, если
задана услуга, проводит акт разницы на 1 ₽ на «Затраты на персонал». Потом читает проводки
каждого документа (``/api/finance/v1/document_transactions/list``) и печатает их «откуда →
куда» со счётом и контрагентом. Оплаченные документы через API не удаляются — после пробы их
убирают в бэк-офисе, как пробы 10–11.07.

Без ``--execute`` только печатает, что завела бы, и ничего не пишет.

Проба 25.10.2026 (КВАРЦ, документы PROBE-*-1025-125054): зачёт лёг «Авансы поставщиков»[поставщик]
→ «Задолженность»[поставщик], акт — «Задолженность»[поставщик] → «Затраты на персонал»; общий счёт
с поставщиком не сдвинулся. Оплата через 2 секунды после проведения получила «already paid» без
проводки — поэтому перед оплатами здесь выдержка ``SETTLE_SECONDS``.

    python -m app.scripts.iiko_correction_chain_probe --counteragent <GUID> --product <GUID> \\
        --store <GUID> [--service <GUID>] [--execute]
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import time
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.db.session import AsyncSessionLocal
from app.services.iiko_sync import _load_source_credential_env

_MSK = ZoneInfo("Europe/Moscow")
# Пауза между вызовами Cloud: он режет частые запросы (429).
_PAUSE = 1.0
# Выдержка между проведением накладной и оплатой: раньше iiko отвечает «already paid».
SETTLE_SECONDS = 150


def _parse() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Copeck probe of the correction chain in iiko.")
    parser.add_argument("--counteragent", required=True, help="GUID тестового поставщика iiko")
    parser.add_argument("--product", required=True, help="GUID товара для строк накладных")
    parser.add_argument("--store", required=True, help="GUID склада")
    parser.add_argument("--unit", required=True, help="GUID основной единицы товара")
    parser.add_argument("--service", default=None, help="GUID услуги для акта разницы")
    parser.add_argument("--execute", action="store_true", help="на самом деле писать в iiko")
    return parser.parse_args()


def _call(path: str, body: dict) -> tuple[int, object]:
    from app.services.iiko_cloud_client import iiko_cloud_call

    time.sleep(_PAUSE)
    return iiko_cloud_call(path, body)


def _legs(document_id: str, accounts: dict[str, str]) -> list[str]:
    """Проводки документа «откуда → куда» с именами счетов и контрагентом."""
    from app.services.iiko_location import get_organization_id

    status, rows = _call(
        "/api/finance/v1/document_transactions/list",
        {"organizationId": get_organization_id(), "documentId": document_id},
    )
    totals: dict[tuple, Decimal] = collections.defaultdict(Decimal)
    for row in rows if isinstance(rows, list) else []:
        source, target = row.get("from") or {}, row.get("to") or {}
        key = (
            row.get("type"),
            accounts.get(source.get("account"), source.get("account")),
            source.get("counteragent"),
            accounts.get(target.get("account"), target.get("account")),
            target.get("counteragent"),
        )
        totals[key] += Decimal(str(row.get("sum") or 0))
    return [
        f"{kind}: «{src}»[{src_cp or '—'}] → «{dst}»[{dst_cp or '—'}] {value}"
        for (kind, src, src_cp, dst, dst_cp), value in totals.items()
    ] or [f"(HTTP {status}, проводок нет)"]


def _run(args: argparse.Namespace) -> None:
    from app.services.counterparty_iiko_payment import (
        IIKO_ACQUIRING_ACCOUNT,
        _call_add_payment,
        build_add_payment_payload,
    )
    from app.services.iiko_invoice_cloud import (
        CloudInvoiceDoc,
        CloudInvoiceLine,
        build_invoice_body,
        extract_document_id,
        format_iiko_invoice_datetime,
    )
    from app.services.iiko_location import get_organization_id
    from app.services.iiko_returned_cloud import build_returned_invoice_body
    from app.services.kassa.iiko_cashshift_sync import _auth_token, _fetch_accounts_map

    accounts = _fetch_accounts_map(_auth_token())
    by_name = {name: guid for guid, name in accounts.items()}
    advances, staff = by_name.get("Авансы поставщиков"), by_name.get("Затраты на персонал")
    now = datetime.now(_MSK)
    stamp = now.strftime("%m%d-%H%M%S")
    print("Проба заведёт на поставщика", args.counteragent)
    print(f"  PROBE-X-{stamp}: 1 × 3 ₽, оплата 3 ₽ с эквайринга")
    print(f"  PROBE-R-{stamp}: сторно исходной на 3 ₽ со ссылкой на неё")
    print(
        f"  PROBE-Y-{stamp}: 1 × 2 ₽, оплата 2 ₽ зачётом со счёта «Авансы поставщиков» ({advances})"
    )
    print(f"  PROBE-A-{stamp}: акт 1 ₽ на «Затраты на персонал»" if args.service else "  акт: нет")
    if not args.execute:
        print("Без --execute в iiko ничего не пишется.")
        return
    org = get_organization_id()

    def invoice(number: str, price: float) -> dict:
        return build_invoice_body(
            CloudInvoiceDoc(
                direction="payable",
                counteragent=args.counteragent,
                date=now,
                number=number,
                default_store=args.store,
                lines=[
                    CloudInvoiceLine(
                        num=1,
                        product=args.product,
                        store=args.store,
                        amount=1.0,
                        price=price,
                        sum=price,
                        amount_unit=args.unit,
                    )
                ],
            )
        )

    def create_and_post(kind: str, body: dict) -> str:
        status, response = _call(f"/api/inventory/v1/{kind}/create", body)
        document_id = extract_document_id(response)
        if not document_id:
            raise SystemExit(f"{kind}/create: HTTP {status} {response}")
        # Сразу в вывод: при сбое на середине список для уборки в бэк-офисе уже полный.
        print(f"СОЗДАН {kind} {body.get('number')} {document_id}", flush=True)
        print(
            f"{kind}/post:",
            _call(
                f"/api/inventory/v1/{kind}/post", {"organizationId": org, "documentId": document_id}
            ),
        )
        return document_id

    x = create_and_post("incoming_invoice", invoice(f"PROBE-X-{stamp}", 3.0))
    time.sleep(SETTLE_SECONDS)
    print(
        "оплата X:",
        _call_add_payment(
            build_add_payment_payload(
                external_id=x,
                amount=Decimal("3"),
                account_id=IIKO_ACQUIRING_ACCOUNT,
                payment_dt=now,
            )
        ),
    )
    r = create_and_post(
        "returned_invoice",
        build_returned_invoice_body(
            counteragent=args.counteragent,
            date=now,
            incoming_invoice_id=x,
            default_store=args.store,
            number=f"PROBE-R-{stamp}",
            items=[
                {
                    "num": 1,
                    "product": args.product,
                    "store": args.store,
                    "amount": 1.0,
                    "price": 3.0,
                    "sum": 3.0,
                }
            ],
        ),
    )
    y = create_and_post("incoming_invoice", invoice(f"PROBE-Y-{stamp}", 2.0))
    time.sleep(SETTLE_SECONDS)
    print(
        "зачёт Y:",
        _call_add_payment(
            build_add_payment_payload(
                external_id=y, amount=Decimal("2"), account_id=advances or "", payment_dt=now
            )
        ),
    )
    act = None
    if args.service and staff:
        status, response = _call(
            "/api/finance/v1/incoming_service/create",
            {
                "organizationId": org,
                "counteragent": args.counteragent,
                "date": format_iiko_invoice_datetime(now),
                "number": f"PROBE-A-{stamp}",
                "items": [
                    {
                        "num": 1,
                        "product": args.service,
                        "amount": 1,
                        "price": 1,
                        "sum": 1,
                        "revenueAccount": staff,
                        "vatPercent": 0,
                    }
                ],
            },
        )
        act = extract_document_id(response)
        print("акт create:", status, response)
        if act:
            print(f"СОЗДАН incoming_service PROBE-A-{stamp} {act}", flush=True)
        if act:
            print(
                "акт post:",
                _call(
                    "/api/finance/v1/incoming_service/post",
                    {"organizationId": org, "documentId": act},
                ),
            )
    for label, doc in (("исходная", x), ("сторно", r), ("«(испр.)»", y), ("акт", act)):
        if doc:
            print(f"\n{label} {doc}:", *_legs(doc, accounts), sep="\n  ")
    # Счёт с поставщиком целиком: проводки дня по «Задолженности» и «Авансам» с его GUID
    # (``account-posting/list`` 25.10 отвечал 502 — читаем ``account_transactions/list``).
    day = now.date().isoformat()
    for name in ("Задолженность перед поставщиками", "Авансы поставщиков"):
        status, response = _call(
            "/api/finance/v1/account_transactions/list",
            {"organizationId": org, "accountId": by_name.get(name), "from": day, "to": day},
        )
        items = response.get("items") if isinstance(response, dict) else None
        mine = [i for i in items or [] if i.get("counteragent") == args.counteragent]
        total = sum((Decimal(str(i.get("sum") or 0)) for i in mine), Decimal("0"))
        print(f"\n«{name}» по поставщику за {day}: HTTP {status}, строк {len(mine)}, сумма {total}")


async def main() -> None:
    """Креды из vault — и проба в треде (сеть синхронная)."""
    args = _parse()
    async with AsyncSessionLocal() as session:
        await _load_source_credential_env(session)
    await asyncio.to_thread(_run, args)


if __name__ == "__main__":
    asyncio.run(main())
