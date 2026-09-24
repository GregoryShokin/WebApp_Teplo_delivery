"""Адресно перегасить закрывающий документ указанным авансом.

ЗАЧЕМ. Зачёт закрывающего по авансам — разметка, а не платёж: денег он не двигает, но решает,
ЧЬЯ дебиторка закрыта. Две ошибки лестницы адресности (исправлены в коде тем же релизом) успели
закрыть документы не своими деньгами, и штатного способа переподобрать уже погашенный документ
нет — обратный порядок (``reconcile_bill_prepayment``) перебирает только НЕОПЛАЧЕННЫЕ
закрывающие, а массовая пересборка (``resettle_closings_by_date``) трогает весь контур разом.
Нужен точечный инструмент: «вот этот документ — вот этим авансом», с проверками и контролем
нетто.

ПРОД-ПАРЫ, РАДИ КОТОРЫХ СКРИПТ НАПИСАН (разведка 24.09.2026, только чтение):

* ``7c462cae-324f-47ab-adb1-e0a17bb8bcae:be0ff243-f925-43ec-aeed-97ad5ca4b423`` — Лема, УПД
  32108 за 08.2026 (СБИС, основания не называет) погасил аванс 4a43e1e9 за СЕНТЯБРЬ (счёт
  73163/1/У, ранг ``amount``), а августовский be0ff243 (счёт 70221/1/У) остался открытым. В
  ОПиУ — ложное «ждём документ» за август и спрятанное законное ожидание в сентябре.
* ``71d49b83-cb6b-4e9c-bda0-3adbf3229512:b41cf10d-1ff6-45eb-a70a-0b87d311bd54`` — Синапсис,
  УПД 521921 за 08.2026 погасил сентябрьский f31b414e вместо своего b41cf10d. Та же механика:
  ровная абонентка, счёт за M+1 оплачен раньше УПД за M.
* ``42d6dfe5-9cff-4797-85b9-c484658a4f6e:3321f346-27fc-4825-98d2-76ea5937d582`` — вода
  Станислава Юрьевича за 08.2026 (9 429,75 ₽). Акт из бота коммуналки 01.09 погасился арендным
  авансом 3d5b61a1 («Аренда вперёд», хронология), а когда в тот же день счёт оплатили из Сейфа,
  его ДЗ 3321f346 повисла открытой навсегда.

ПОРЯДОК НА ПРОДЕ. Сначала выкатка исправлений лестницы и акта коммуналки (F1 + F2) — иначе
следующий такой же месяц снова ляжет крест-накрест. Потом этот скрипт — вхолостую, сверить
печать, затем ``--apply``. Успеть ДО 01.10 00:10 МСК: в эту минуту активируется «Аренда
09.2026», и открытая водяная ДЗ 3321f346 уйдёт на аренду — перекрёст станет двойным.

ЧТО ДЕЛАЕТ С КАЖДОЙ ПАРОЙ. Снимает с документа ТОЛЬКО его ``prepayment``-зачёты (наличные и
банковские оплаты не трогает), пересчитывает статус и проводит штатный авто-зачёт, ограниченный
одним целевым авансом. Начисления не трогает. Все пары — одна транзакция: откажет хоть одна,
не применится ни одна. Отказ — до любых изменений, с объяснением; контроль после зачёта
(документ закрыт целиком целевым авансом, период документа не сдвинулся, нетто ДЗ − КЗ по
контрагенту то же) при расхождении откатывает всё.

    python -m app.scripts.readdress_closing_settlements --pair <закрывающий>:<аванс> ...
    python -m app.scripts.readdress_closing_settlements --pair <закрывающий>:<аванс> --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models import (
    InvoicePaymentAllocation,
    SupplierInvoice,
    SupplierPrepayment,
    invoice_binds_settlement,
)
from app.services import accounting_periods
from app.services.counterparty_matching import _invoice_remaining, _recompute_status
from app.services.supplier_prepayments import (
    AUTO_SETTLEMENT_OPERATIONAL_SCOPE,
    BILL_PREPAYMENT_KIND,
    EARMARKED_PREPAYMENT_KINDS,
    OPEN_PREPAYMENT_STATUSES,
    _basis_bill_ids,
    _money,
    _periods_overlap,
    auto_settle_invoice_from_open_prepayments,
    release_invoice_prepayment_allocations,
)

# Статусы аванса, которые перегашение может задеть. ``settled`` — законный случай: аванс мог
# быть израсходован именно этим документом (перегашение тогда ничего не меняет по сути) или
# другим — это отсечёт проверка остатка. Возвращённый/отменённый аванс денег у поставщика
# больше не держит.
_TARGET_STATUSES = frozenset(OPEN_PREPAYMENT_STATUSES) | {"settled"}


class ReaddressRefused(RuntimeError):
    """Пара не прошла проверку — ничего не меняем, объясняем почему."""


@dataclass(frozen=True)
class ReaddressResult:
    closing_id: uuid.UUID
    prepayment_id: uuid.UUID
    released: Decimal
    settled: Decimal
    net_before: Decimal
    net_after: Decimal
    changed: bool


def _period_text(start, end) -> str:
    if start is None or end is None:
        return "—"
    if start.day == 1 and start.month == end.month and start.year == end.year:
        return f"{start:%m.%Y}"
    return f"{start:%d.%m.%Y}–{end:%d.%m.%Y}"


async def _allocations(
    session: AsyncSession, invoice_id: uuid.UUID
) -> list[InvoicePaymentAllocation]:
    return list(
        (
            await session.scalars(
                select(InvoicePaymentAllocation)
                .where(InvoicePaymentAllocation.invoice_id == invoice_id)
                .order_by(InvoicePaymentAllocation.created_at)
            )
        ).all()
    )


async def counterparty_net(session: AsyncSession, counterparty_id: uuid.UUID) -> Decimal:
    """ДЗ − КЗ по контрагенту: свободный остаток открытых авансов минус непогашенные закрывающие.

    Перегашение — разметка: оно меняет, НА КАКОМ авансе висит остаток, но не сколько его. Если
    нетто сдвинулось, значит зачёт взял или вернул лишнее, и применять такое нельзя.

    КЗ — только по документам, которые связывают расчёты (``invoice_binds_settlement``). Будущий
    (``pending``) и справочный (``informational``) документ долгом не являются, и в нетто их
    сумма была бы мнимой КЗ: авто-зачёт, погасивший такой документ, уменьшает ДЗ и эту мнимую
    КЗ на одно и то же, нетто сходится — и контроль молча пропускает зачёт, которого канон не
    допускает (скептик S7). Без них тот же зачёт сдвигает нетто, и контроль его ловит."""
    receivable = sum(
        (
            _money(p.amount) - _money(p.amount_settled)
            for p in (
                await session.scalars(
                    select(SupplierPrepayment).where(
                        SupplierPrepayment.counterparty_id == counterparty_id,
                        SupplierPrepayment.status.in_(OPEN_PREPAYMENT_STATUSES),
                    )
                )
            ).all()
        ),
        Decimal("0.00"),
    )
    payable = Decimal("0.00")
    for closing in (
        await session.scalars(
            select(SupplierInvoice).where(
                SupplierInvoice.counterparty_id == counterparty_id,
                SupplierInvoice.doc_kind == "closing",
                SupplierInvoice.payment_status != "void",
                invoice_binds_settlement(),
            )
        )
    ).all():
        payable += await _invoice_remaining(session, closing)
    return receivable - payable


async def print_state(
    session: AsyncSession,
    counterparty_id: uuid.UUID,
    closing: SupplierInvoice,
    *,
    title: str,
    out: Callable[[str], None] = print,
) -> None:
    """Все авансы контрагента и сам документ — чтобы глазами сверить, что куда переехало."""
    out(f"  [{title}]")
    prepayments = (
        await session.scalars(
            select(SupplierPrepayment)
            .where(SupplierPrepayment.counterparty_id == counterparty_id)
            .order_by(
                SupplierPrepayment.service_period_start.nulls_last(),
                SupplierPrepayment.created_at,
            )
        )
    ).all()
    for p in prepayments:
        period = _period_text(p.service_period_start, p.service_period_end)
        out(
            f"    аванс {str(p.id)[:8]} {p.kind:14} период {period:10} "
            f"{_money(p.amount):>11} / погашено {_money(p.amount_settled):>11}  {p.status}"
        )
    out(
        f"    документ № {closing.number or '—'} ({str(closing.id)[:8]}) "
        f"период {_period_text(closing.service_period_start, closing.service_period_end)} "
        f"сумма {_money(closing.amount)} — {closing.payment_status}"
    )
    for alloc in await _allocations(session, closing.id):
        source = str(alloc.prepayment_id)[:8] if alloc.prepayment_id else "—"
        out(
            f"      ← {alloc.source_kind:10} {source:8} {_money(alloc.amount):>11}  "
            f"основание {alloc.match_basis or '—'}"
        )
    out(f"    нетто ДЗ − КЗ: {await counterparty_net(session, counterparty_id)}")


async def readdress_closing(
    session: AsyncSession,
    *,
    closing_id: uuid.UUID,
    prepayment_id: uuid.UUID,
    out: Callable[[str], None] = print,
) -> ReaddressResult:
    """Перегасить закрывающий документ целевым авансом. Без коммита — решает вызывающий.

    Все проверки, которые могут отказать ДО изменений, стоят в начале: отказ оставляет сессию
    нетронутой. Контроль ПОСЛЕ зачёта тоже бросает ``ReaddressRefused`` — тогда вызывающий
    обязан откатить транзакцию (``main`` так и делает)."""
    closing = await session.scalar(
        select(SupplierInvoice).where(SupplierInvoice.id == closing_id).with_for_update()
    )
    if closing is None:
        raise ReaddressRefused(f"документ {closing_id} не найден")
    if closing.doc_kind != "closing":
        raise ReaddressRefused(
            f"документ № {closing.number} — не закрывающий (doc_kind={closing.doc_kind})"
        )
    # КАНОН: ДЗ гасит только документ, который связывает расчёты (``invoice_binds_settlement``).
    # Будущий документ (правило 4) ещё не долг — его погасит активация в свою дату; справочный
    # по договору не долг вовсе. Штатный авто-зачёт такие документы не выбирает, а скрипт
    # называет документ по id и мимо этих фильтров проходил: перегашение закрыло бы авансом
    # документ, который канон к деньгам не подпускает (скептик S7).
    if closing.activation_status != "active":
        raise ReaddressRefused(
            f"документ № {closing.number} ещё не вступил в силу "
            f"(activation_status={closing.activation_status}) — ДЗ он не гасит"
        )
    if closing.informational:
        raise ReaddressRefused(
            f"документ № {closing.number} справочный (по договору) — ДЗ он не гасит"
        )
    if closing.draft_id is not None:
        # Документ в банковском черновике: платёж в пути, и зачёт закрыл бы его дважды.
        raise ReaddressRefused(f"документ № {closing.number} отправлен в банк (draft_id)")
    if closing.barter_role is not None:
        raise ReaddressRefused(
            f"документ № {closing.number} бартерный ({closing.barter_role}) — у бартера свой зачёт"
        )
    if closing.payment_status == "void":
        raise ReaddressRefused(f"документ № {closing.number} аннулирован")
    if closing.operational_scope != AUTO_SETTLEMENT_OPERATIONAL_SCOPE:
        # Складскую накладную авто-зачёт не трогает вовсе — перегасить её этим путём нельзя.
        raise ReaddressRefused(
            f"документ № {closing.number} не финансовый ({closing.operational_scope})"
        )

    target = await session.scalar(
        select(SupplierPrepayment).where(SupplierPrepayment.id == prepayment_id).with_for_update()
    )
    if target is None:
        raise ReaddressRefused(f"аванс {prepayment_id} не найден")
    if target.counterparty_id != closing.counterparty_id:
        raise ReaddressRefused(
            f"аванс {str(target.id)[:8]} другого контрагента, чем документ № {closing.number}"
        )
    if target.kind in EARMARKED_PREPAYMENT_KINDS:
        raise ReaddressRefused(
            f"аванс {str(target.id)[:8]} целевой ({target.kind}) — закрывающим не гасится"
        )
    if target.status not in _TARGET_STATUSES:
        raise ReaddressRefused(f"аванс {str(target.id)[:8]} в статусе {target.status}")

    # АДРЕСНОСТЬ ДОЛЖНА БЫТЬ ДОКАЗУЕМОЙ. Скрипт существует, чтобы исправлять угаданное на
    # подтверждённое, а не наоборот: либо периоды документа и аванса пересекаются, либо аванс —
    # ДЗ того самого счёта, который документ называет своим основанием.
    by_basis = (
        target.kind == BILL_PREPAYMENT_KIND
        and target.bill_invoice_id is not None
        and target.bill_invoice_id in await _basis_bill_ids(session, closing)
    )
    if not (_periods_overlap(target, closing) or by_basis):
        raise ReaddressRefused(
            f"аванс {str(target.id)[:8]} (период "
            f"{_period_text(target.service_period_start, target.service_period_end)}) не относится "
            f"к документу № {closing.number} (период "
            f"{_period_text(closing.service_period_start, closing.service_period_end)}): "
            "ни пересечения периодов, ни счёта-основания"
        )

    # Замок закрытого месяца: и месяц документа, и все месяцы его периода — единица у замка и у
    # отчёта одна (см. ``assert_period_open``).
    action = f"перегашение документа № {closing.number}"
    try:
        if closing.invoice_date is not None:
            await accounting_periods.assert_month_open(session, closing.invoice_date, action=action)
        await accounting_periods.assert_period_open(
            session, closing.service_period_start, closing.service_period_end, action=action
        )
    except accounting_periods.PeriodClosed as exc:
        raise ReaddressRefused(str(exc)) from exc

    allocations = await _allocations(session, closing.id)
    prepayment_allocs = [a for a in allocations if a.source_kind == "prepayment"]
    other_paid = sum(
        (_money(a.amount) for a in allocations if a.source_kind != "prepayment"), Decimal("0.00")
    )
    need = _money(closing.amount) - other_paid
    if need <= 0:
        raise ReaddressRefused(
            f"документ № {closing.number} закрыт деньгами целиком — перегашать нечего"
        )
    if sum((_money(a.amount) for a in prepayment_allocs), Decimal("0.00")) == need and all(
        a.prepayment_id == target.id for a in prepayment_allocs
    ):
        out(f"  документ № {closing.number} уже погашен авансом {str(target.id)[:8]} — пропуск")
        net = await counterparty_net(session, closing.counterparty_id)
        return ReaddressResult(
            closing.id, target.id, Decimal("0.00"), Decimal("0.00"), net, net, changed=False
        )
    freed_on_target = sum(
        (_money(a.amount) for a in prepayment_allocs if a.prepayment_id == target.id),
        Decimal("0.00"),
    )
    available = _money(target.amount) - _money(target.amount_settled) + freed_on_target
    if available < need:
        raise ReaddressRefused(
            f"у аванса {str(target.id)[:8]} свободно {available} ₽, а документу № "
            f"{closing.number} нужно {need} ₽"
        )

    period_before = (
        closing.service_period_start,
        closing.service_period_end,
        closing.service_period_status,
    )
    await print_state(session, closing.counterparty_id, closing, title="до", out=out)
    net_before = await counterparty_net(session, closing.counterparty_id)

    released = await release_invoice_prepayment_allocations(session, closing)
    await _recompute_status(session, closing)
    await session.flush()
    settled = await auto_settle_invoice_from_open_prepayments(
        session, closing, allowed_prepayment_ids={target.id}
    )
    await session.flush()

    after = await _allocations(session, closing.id)
    after_prepayment = [a for a in after if a.source_kind == "prepayment"]
    remaining = await _invoice_remaining(session, closing)
    if remaining != 0 or any(a.prepayment_id != target.id for a in after_prepayment):
        raise ReaddressRefused(
            f"документ № {closing.number} не закрылся целевым авансом целиком "
            f"(погашено {settled}, остаток {remaining})"
        )
    # Период наследуется авто-зачётом только у документа без своего периода — и тянет за собой
    # начисление. Скрипт начисления не трогает: такой документ разбирается руками.
    if (
        closing.service_period_start,
        closing.service_period_end,
        closing.service_period_status,
    ) != period_before:
        raise ReaddressRefused(
            f"документ № {closing.number} перенял бы период аванса — это сдвигает начисление, "
            "разберите вручную"
        )
    net_after = await counterparty_net(session, closing.counterparty_id)
    if net_after != net_before:
        raise ReaddressRefused(f"нетто ДЗ − КЗ контрагента сдвинулось: {net_before} → {net_after}")
    await print_state(session, closing.counterparty_id, closing, title="после", out=out)
    return ReaddressResult(
        closing.id, target.id, released, settled, net_before, net_after, changed=True
    )


def parse_pair(raw: str) -> tuple[uuid.UUID, uuid.UUID]:
    try:
        closing_raw, prepayment_raw = raw.split(":")
        return uuid.UUID(closing_raw.strip()), uuid.UUID(prepayment_raw.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"ожидается <uuid закрывающего>:<uuid аванса>, получено {raw!r}"
        ) from exc


async def main(*, pairs: list[tuple[uuid.UUID, uuid.UUID]], apply: bool) -> int:
    async with AsyncSessionLocal() as session:
        for closing_id, prepayment_id in pairs:
            print(f"\nПара {closing_id} ← {prepayment_id}")
            try:
                await readdress_closing(session, closing_id=closing_id, prepayment_id=prepayment_id)
            except ReaddressRefused as exc:
                await session.rollback()
                print(f"  ОТКАЗ: {exc}\nНичего не изменено (откачены все пары).")
                return 1
        if not apply:
            await session.rollback()
            print("\nПробный прогон, изменения откачены. Чтобы применить — запустите с --apply")
            return 0
        await session.commit()
        print("\nПрименено.")
        return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--pair",
        dest="pairs",
        action="append",
        type=parse_pair,
        required=True,
        help="<uuid закрывающего>:<uuid целевого аванса>; можно несколько",
    )
    parser.add_argument("--apply", action="store_true", help="закоммитить (иначе — вхолостую)")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(pairs=args.pairs, apply=args.apply)))
