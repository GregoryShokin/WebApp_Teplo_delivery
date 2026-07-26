"""Факт-слой налогового контура: выписка банка → ``TaxPayment`` (source_kind='bank_statement').

Критичный для прода контур (без него вычет УСН пуст и налог завышен): реальные списания
в ФНС и СФР из ``bank_operations`` превращаются в строки фактов, которые кормят вычет
(repository), сверку (слой «Факт»), «Платежи», ЕНС-кошелёк и гашение обязательств.

Как распознаём налоговое списание. TPL-метки в налоговых платёжках нет (назначение
фиксированное «Единый налоговый платеж»), поэтому признак — РЕКВИЗИТЫ получателя из
owner-locked констант: ИНН Казначейства/ФНС и ОСФР либо их казначейские счета. Урок
контура ДЗ/КЗ: матчить по ИНН из реквизитов операции, а не по тексту назначения.

Как определяем вид и период (по убыванию доверия):

1. **Наш черновик** (``TaxBankDraft`` in_bank, точная сумма + получатель) — платёж ушёл
   из системы, вид/период известны; черновик доводится до ``paid``.
2. **Плановое обязательство** (``TaxPayment`` planned из продвинутой платёжки, ±1 ₽).
3. **Платёжка бухгалтера** (``tax_document_intake``, ±1 ₽) — путь для зарплатного ЕНП,
   который в обязательства не продвигается.
4. **Эвристика получателя**: всё в СФР — травматизм; нераспознанный платёж в ФНС —
   ``kind='other'`` + ``requires_review`` (в вычет НЕ попадает — консервативно, налог
   не занижаем; «прочее» видно на «Платежах» и дозревает следующим прогоном).

Зарплатный ЕНП разносится по видам ОБЯЗАТЕЛЬНО (``kind='enp_payroll'`` запрещён CHECK'ом):
взносы месяца N из оборотки (``tax_payroll_ledger``), НДФЛ — остаток платежа. Без оборотки
месяца разнос невозможен — платёж ждёт её в ``other``/``requires_review`` и пересобирается,
когда оборотка приходит («дозревание»).

Идемпотентность: операция обрабатывается один раз (дедуп по ``bank_operation_id``);
пере-разнос касается ТОЛЬКО собственных строк ``other``/``requires_review``. Строки,
введённые руками или сидом (без ``bank_operation_id``), сервис не трогает.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dds import BankOperation
from app.models.tax import TaxBankDraft, TaxDocumentIntake, TaxPayment, TaxPayrollLedger
from app.services.banking.fns_enp_requisites import TREASURY_ENP_REQUISITES
from app.services.banking.sfr_injury_requisites import TREASURY_INJURY_REQUISITES

logger = logging.getLogger(__name__)

ZERO = Decimal("0")
# Допуск матчинга с планом/платёжкой: налог округляется до рубля (как в obligations).
TOLERANCE = Decimal("1")

FNS_INN = str(TREASURY_ENP_REQUISITES["inn"])
FNS_ACCOUNT = str(TREASURY_ENP_REQUISITES["bankAcnt"])
SFR_INN = str(TREASURY_INJURY_REQUISITES["inn"])
SFR_ACCOUNT = str(TREASURY_INJURY_REQUISITES["bankAcnt"])


@dataclass
class TaxFactsSyncReport:
    """Итог прогона: что создано, что дозрело, что ждёт проверки."""

    operations_seen: int = 0
    bundles_created: int = 0
    bundles_ripened: int = 0  # пере-разнесённые из other/requires_review
    drafts_paid: int = 0
    review_pending: int = 0  # операций, оставшихся в other/requires_review
    details: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "operations_seen": self.operations_seen,
            "bundles_created": self.bundles_created,
            "bundles_ripened": self.bundles_ripened,
            "drafts_paid": self.drafts_paid,
            "review_pending": self.review_pending,
            "details": self.details,
        }


def _recipient_for_operation(op: BankOperation) -> str | None:
    """'fns' | 'sfr' по реквизитам получателя; None — операция не налоговая."""
    inn = (op.counterparty_inn_raw or "").strip()
    account = (op.counterparty_account_raw or "").strip()
    if inn == FNS_INN or account == FNS_ACCOUNT:
        return "fns"
    if inn == SFR_INN or account == SFR_ACCOUNT:
        return "sfr"
    return None


def _month_from_due(due: date) -> tuple[int, int]:
    """Обратная к ``payroll_enp_due``: срок 28.N+1 → (год, месяц N) базы ЕНП."""
    if due.month == 1:
        return due.year - 1, 12
    return due.year, due.month - 1


def _year_for(kind_period: str | None, fallback: date) -> int:
    if kind_period and len(kind_period) == 7 and kind_period[4] == "-":
        return int(kind_period[:4])
    return fallback.year


@dataclass(frozen=True)
class _Resolution:
    """Во что разнести операцию: строки (kind, amount, quality) + период."""

    rows: tuple[tuple[str, Decimal, str], ...]
    for_period: str | None
    for_year: int
    note: str


async def _payroll_contributions(
    session: AsyncSession, *, year: int, month: int
) -> Decimal | None:
    """Взносы за работников за месяц из оборотки; None — оборотки за месяц нет."""
    rows = (
        await session.execute(
            select(TaxPayrollLedger.contributions).where(
                TaxPayrollLedger.year == year, TaxPayrollLedger.month == month
            )
        )
    ).scalars().all()
    if not rows:
        return None
    return sum((Decimal(str(v)) for v in rows if v is not None), ZERO)


async def _split_enp(
    session: AsyncSession, *, amount: Decimal, year: int, month: int
) -> _Resolution | None:
    """Разнос зарплатного ЕНП: взносы месяца из оборотки + НДФЛ остатком.

    Разнос расчётный (состав платежа из выписки не виден — один КБК на всё),
    поэтому качество строк — ``reconstructed``, а не ``confirmed``.
    """
    contributions = await _payroll_contributions(session, year=year, month=month)
    if contributions is None:
        return None
    period = f"{year}-{month:02d}"
    contrib_part = min(contributions, amount)
    ndfl_part = amount - contrib_part
    rows: list[tuple[str, Decimal, str]] = []
    if contrib_part > ZERO:
        rows.append(("contrib_employees", contrib_part, "reconstructed"))
    if ndfl_part > ZERO:
        rows.append(("ndfl", ndfl_part, "reconstructed"))
    return _Resolution(
        rows=tuple(rows),
        for_period=period,
        for_year=year,
        note=f"разнос ЕНП по оборотке {period}: взносы {contrib_part} + НДФЛ {ndfl_part}",
    )


async def _resolve_via_kind(
    session: AsyncSession,
    *,
    kind: str,
    amount: Decimal,
    for_period: str | None,
    for_year: int | None,
    due_date: date | None,
    op_date: date,
    quality: str,
    note: str,
) -> _Resolution | None:
    """Свести известный вид (из черновика/плана/платёжки) к строкам факта.

    Обычный вид — одна строка как есть. Зарплатный ЕНП — обязательный разнос по
    оборотке; месяц берём из периода ('YYYY-MM') либо из срока уплаты (28.N+1 → N).
    """
    if kind != "enp_payroll":
        return _Resolution(
            rows=((kind, amount, quality),),
            for_period=for_period,
            for_year=for_year or _year_for(for_period, op_date),
            note=note,
        )
    if for_period and len(for_period) == 7 and for_period[4] == "-":
        year, month = int(for_period[:4]), int(for_period[5:])
    elif due_date is not None:
        year, month = _month_from_due(due_date)
    else:
        return None
    return await _split_enp(session, amount=amount, year=year, month=month)


async def _match_draft(
    session: AsyncSession, *, amount: Decimal, recipient: str, op_date: date
) -> TaxBankDraft | None:
    """Наш черновик in_bank с точной суммой и тем же получателем.

    При нескольких кандидатах берём с ближайшим сроком — детерминированно; остальные
    доведёт следующая операция (суммы у нас нетипизированные, коллизии редки).
    """
    drafts = (
        await session.scalars(
            select(TaxBankDraft).where(TaxBankDraft.status == "in_bank")
        )
    ).all()
    candidates = []
    for d in drafts:
        d_recipient = "sfr" if d.tax_kind == "contrib_injury" else "fns"
        if d_recipient != recipient or Decimal(str(d.amount)) != amount:
            continue
        candidates.append(d)
    if not candidates:
        return None
    candidates.sort(key=lambda d: (abs(((d.due_date or op_date) - op_date).days), str(d.id)))
    return candidates[0]


async def _match_planned(
    session: AsyncSession, *, amount: Decimal, recipient: str
) -> TaxPayment | None:
    """Плановое обязательство (продвинутая платёжка) с суммой ±1 ₽ и тем же получателем."""
    rows = (
        await session.scalars(
            select(TaxPayment).where(
                TaxPayment.status == "planned", TaxPayment.recipient == recipient
            )
        )
    ).all()
    candidates = [r for r in rows if abs(Decimal(str(r.amount)) - amount) <= TOLERANCE]
    if not candidates:
        return None
    candidates.sort(key=lambda r: (r.paid_on, str(r.id)))
    return candidates[0]


async def _match_intake(
    session: AsyncSession, *, amount: Decimal, recipient: str
) -> dict | None:
    """Платёжка бухгалтера с суммой ±1 ₽ — путь для непродвигаемых видов (ЕНП).

    Возвращает recognition-словарь. При нескольких кандидатах — самая свежая по
    received_at (бухгалтер мог прислать исправление).
    """
    rows = (
        await session.scalars(
            select(TaxDocumentIntake)
            .where(
                TaxDocumentIntake.document_type == "payment_order",
                TaxDocumentIntake.status.in_(("parsed", "needs_review", "promoted")),
            )
            .order_by(TaxDocumentIntake.received_at.asc())
        )
    ).all()
    best: dict | None = None
    for row in rows:
        rec = row.recognition or {}
        raw_amount = rec.get("amount")
        if raw_amount is None:
            continue
        if abs(Decimal(str(raw_amount)) - amount) > TOLERANCE:
            continue
        kind = rec.get("tax_kind")
        if not kind:
            continue
        rec_recipient = "sfr" if kind == "contrib_injury" else "fns"
        if rec_recipient != recipient:
            continue
        best = rec  # последняя по received_at побеждает
    return best


async def _resolve_operation(
    session: AsyncSession, op: BankOperation, recipient: str
) -> tuple[_Resolution, TaxBankDraft | None]:
    """Классифицировать операцию по слоям доверия. Всегда возвращает решение:
    последний фолбэк — ``other``/``requires_review`` (дозреет или уйдёт в ручную проверку).
    """
    amount = Decimal(str(op.amount))

    draft = await _match_draft(
        session, amount=amount, recipient=recipient, op_date=op.operation_date
    )
    if draft is not None:
        resolution = await _resolve_via_kind(
            session,
            kind=draft.tax_kind,
            amount=amount,
            for_period=draft.for_period,
            for_year=draft.for_year,
            due_date=draft.due_date,
            op_date=op.operation_date,
            quality="confirmed",
            note=f"по нашему черновику «{draft.title or draft.tax_kind}»",
        )
        if resolution is not None:
            return resolution, draft
        # Черновик есть, но разнести нечем (нет оборотки) — ждём её, черновик не трогаем.

    planned = await _match_planned(session, amount=amount, recipient=recipient)
    if planned is not None:
        resolution = await _resolve_via_kind(
            session,
            kind=planned.kind,
            amount=amount,
            for_period=planned.for_period,
            for_year=planned.for_year,
            due_date=planned.paid_on,  # у плановой строки в paid_on лежит срок уплаты
            op_date=op.operation_date,
            quality="confirmed",
            note="по плановому обязательству из платёжки бухгалтера",
        )
        if resolution is not None:
            return resolution, None

    intake = await _match_intake(session, amount=amount, recipient=recipient)
    if intake is not None:
        due_raw = intake.get("due_date")
        resolution = await _resolve_via_kind(
            session,
            kind=str(intake["tax_kind"]),
            amount=amount,
            for_period=intake.get("period_hint"),
            for_year=None,
            due_date=date.fromisoformat(due_raw) if due_raw else None,
            op_date=op.operation_date,
            quality="confirmed",
            note="по платёжке бухгалтера",
        )
        if resolution is not None:
            return resolution, None

    if recipient == "sfr":
        # В СФР у нас уходит только травматизм; привязки к документу нет — разнос расчётный.
        return (
            _Resolution(
                rows=(("contrib_injury", amount, "reconstructed"),),
                for_period=None,
                for_year=op.operation_date.year,
                note="платёж в СФР без документа — травматизм по эвристике получателя",
            ),
            None,
        )

    return (
        _Resolution(
            rows=(("other", amount, "requires_review"),),
            for_period=None,
            for_year=op.operation_date.year,
            note="платёж в ФНС не сопоставлен ни с черновиком, ни с платёжкой — нужна проверка",
        ),
        None,
    )


def _build_rows(
    op: BankOperation, recipient: str, resolution: _Resolution
) -> list[TaxPayment]:
    bundle_id = uuid.uuid4()
    rows: list[TaxPayment] = []
    for kind, amount, quality in resolution.rows:
        # CHECK ck_tax_payment_recipient_kind: травматизм — только 'sfr'; в 'sfr'
        # кроме него могут уходить лишь пени/прочее; всё остальное — 'fns'.
        if kind == "contrib_injury" or (recipient == "sfr" and kind in ("penalty", "other")):
            row_recipient = "sfr"
        else:
            row_recipient = "fns"
        rows.append(
            TaxPayment(
                id=uuid.uuid4(),
                bundle_id=bundle_id,
                paid_on=op.operation_date,
                kind=kind,
                amount=amount,
                recipient=row_recipient,
                for_year=resolution.for_year,
                for_period=resolution.for_period,
                status="paid",
                source_kind="bank_statement",
                quality_status=quality,
                bank_operation_id=op.id,
                cashflow_transaction_id=op.cashflow_transaction_id,
                document_number=op.document_number,
                purpose=op.payment_purpose,
                note=resolution.note,
            )
        )
    return rows


async def _tax_operations_without_facts(
    session: AsyncSession,
) -> list[BankOperation]:
    """Списания на реквизиты ФНС/СФР, по которым фактов ещё нет."""
    processed = (
        select(TaxPayment.bank_operation_id)
        .where(TaxPayment.bank_operation_id.isnot(None))
        .scalar_subquery()
    )
    ops = (
        await session.scalars(
            select(BankOperation).where(
                BankOperation.direction == "out",
                BankOperation.id.notin_(processed),
            )
        )
    ).all()
    return [op for op in ops if _recipient_for_operation(op) is not None]


async def _ripen_review_bundles(
    session: AsyncSession, report: TaxFactsSyncReport
) -> None:
    """Пере-разнос собственных строк ``other``/``requires_review``.

    Платёж, не разнесённый из-за отсутствия оборотки/платёжки, дозревает, когда данные
    приходят. Трогаем ТОЛЬКО свои строки: kind='other' + requires_review + привязка
    к операции. Ручные и сид-строки (без bank_operation_id) неприкосновенны.
    """
    stale = (
        await session.scalars(
            select(TaxPayment).where(
                TaxPayment.source_kind == "bank_statement",
                TaxPayment.kind == "other",
                TaxPayment.quality_status == "requires_review",
                TaxPayment.bank_operation_id.isnot(None),
            )
        )
    ).all()
    for row in stale:
        op = await session.get(BankOperation, row.bank_operation_id)
        if op is None:
            continue
        recipient = _recipient_for_operation(op)
        if recipient is None:
            continue
        resolution, draft = await _resolve_operation(session, op, recipient)
        if len(resolution.rows) == 1 and resolution.rows[0][0] == "other":
            report.review_pending += 1
            continue  # данных всё ещё нет
        await session.delete(row)
        await session.flush()
        for new_row in _build_rows(op, recipient, resolution):
            session.add(new_row)
        if draft is not None:
            draft.status = "paid"
            report.drafts_paid += 1
        report.bundles_ripened += 1
        report.details.append(
            f"{op.operation_date} {op.amount} ₽: дозрел — {resolution.note}"
        )
    await session.flush()


async def sync_tax_facts_from_bank(session: AsyncSession) -> TaxFactsSyncReport:
    """Однопроходный синк: новые налоговые списания → факты; зревшие — пересобрать.

    Вызывается из банковского ингеста после классификации (каждый вебхук/поллинг),
    из CLI для бэкфилла и безопасен к повторному запуску в любой момент.
    """
    report = TaxFactsSyncReport()

    await _ripen_review_bundles(session, report)

    operations = await _tax_operations_without_facts(session)
    report.operations_seen = len(operations)
    for op in sorted(operations, key=lambda o: (o.operation_date, str(o.id))):
        recipient = _recipient_for_operation(op)
        if recipient is None:  # защита от гонки — отбор уже отфильтровал
            continue
        resolution, draft = await _resolve_operation(session, op, recipient)
        for row in _build_rows(op, recipient, resolution):
            session.add(row)
        if draft is not None:
            draft.status = "paid"
            report.drafts_paid += 1
        report.bundles_created += 1
        if len(resolution.rows) == 1 and resolution.rows[0][0] == "other":
            report.review_pending += 1
        report.details.append(f"{op.operation_date} {op.amount} ₽: {resolution.note}")
    await session.flush()
    return report
