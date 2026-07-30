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
   из системы, вид/период известны; черновик доводится до ``paid`` и запоминает операцию,
   которая его закрыла. Кандидатом он остаётся не вечно: не подтверждённая владельцем
   платёжка протухает через ``DRAFT_MATCH_TTL_DAYS`` и больше не ловит чужие списания
   по сумме (по своему номеру документа — ловит по-прежнему), см. ``_match_draft``.
2. **Плановое обязательство** (``TaxPayment`` planned из продвинутой платёжки, ±1 ₽).
3. **Платёжка бухгалтера** (``tax_document_intake``, ±1 ₽) — путь для зарплатного ЕНП,
   который в обязательства не продвигается.
4. **Эвристика получателя**: всё в СФР — травматизм; нераспознанный платёж в ФНС —
   ``kind='other'`` + ``requires_review`` (в вычет НЕ попадает — консервативно, налог
   не занижаем; «прочее» видно на «Платежах» и дозревает следующим прогоном).

Зарплатный ЕНП разносится по видам ОБЯЗАТЕЛЬНО (``kind='enp_payroll'`` запрещён CHECK'ом):
взносы месяца N — из первого доступного основания, НДФЛ — остаток платежа. Основания взносов
по убыванию точности (см. ``_contributions_basis``):

1. **оборотка** бухгалтера (``tax_payroll_ledger``) — взносы стоят цифрой;
2. **платёжка травматизма** того же месяца: 0,2 % от ФОТ → база = сумма / 0,002 → взносы по
   тарифу МСП. Так разносятся месяцы, за которые оборотку не присылали (январь–февраль 2026:
   травматизм 100 ₽ → база 50 000 → взносы 13 595,93 — сверено с ЕНП копейка в копейку);
3. **официальный контур** сотрудника (оклад × тариф) — если и травматизма за месяц нет.

Без всех трёх разнос невозможен — платёж ждёт данные в ``other``/``requires_review`` и
пересобирается, когда они приходят («дозревание»).

Идемпотентность: операция обрабатывается один раз (дедуп по ``bank_operation_id``);
пере-разнос касается ТОЛЬКО собственных строк ``other``/``requires_review``. Строки,
введённые руками или сидом (без ``bank_operation_id``), сервис не трогает.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dds import BankOperation
from app.models.tax import TaxBankDraft, TaxDocumentIntake, TaxPayment, TaxPayrollLedger
from app.services.banking.fns_enp_requisites import TREASURY_ENP_REQUISITES
from app.services.banking.sfr_injury_requisites import TREASURY_INJURY_REQUISITES
from app.services.taxes.concurrency import insert_or_reread
from app.services.taxes.engine import money
from app.services.taxes.official_payroll import (
    injury_due,
    month_contributions,
    official_month_accrual,
)
from app.services.taxes.repository import year_config

logger = logging.getLogger(__name__)

ZERO = Decimal("0")
# Допуск матчинга с планом/платёжкой: налог округляется до рубля (как в obligations).
TOLERANCE = Decimal("1")
# Сколько дней черновик, отправленный в банк, ловит списания ПО СУММЕ (по своему номеру
# документа — бессрочно, см. ``_match_draft``). Срок — налоговый цикл: ЕНП платится 28-го
# каждого месяца, и черновик, переживший месяц, конкурирует уже с платежами следующего
# периода. Не подтверждённая владельцем платёжка так перестаёт быть вечным кандидатом.
DRAFT_MATCH_TTL_DAYS = 30

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


def _aware(value: datetime | None) -> datetime:
    """Отметка времени как aware-UTC. ``None`` (черновик без обеих дат) — «бесконечно старый»:
    свежести он не подтверждает, значит по сумме ловиться не должен."""
    if value is None:
        return datetime.min.replace(tzinfo=UTC)
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


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


async def payroll_facts_without_turnover(
    session: AsyncSession, *, year: int
) -> dict[int, tuple[Decimal, Decimal]]:
    """Месяцы БЕЗ оборотки, но с уплаченным и разнесённым зарплатным ЕНП.

    Возвращает ``{месяц: (взносы, НДФЛ)}`` по банковским фактам. Нужен и сверке, и сводке
    «начислено/уплачено»: январь и февраль 2026 оплачены (21 783,93 и 19 460,93), их взносы
    в вычете, но начисления по ним оборотка не покрывает — без этой добавки месяц выпадал
    из таблиц МОЛЧА, а «уплачено» оказывалось больше «начислено» на те же суммы.
    """
    covered = set(
        (
            await session.scalars(
                select(TaxPayrollLedger.month)
                .where(TaxPayrollLedger.year == year)
                .distinct()
            )
        ).all()
    )
    rows = (
        await session.execute(
            select(
                TaxPayment.for_period,
                TaxPayment.kind,
                func.coalesce(func.sum(TaxPayment.amount), 0),
            )
            .where(
                TaxPayment.status == "paid",
                TaxPayment.for_year == year,
                TaxPayment.source_kind != "tax_notice",
                TaxPayment.kind.in_(("contrib_employees", "ndfl")),
                TaxPayment.for_period.is_not(None),
            )
            .group_by(TaxPayment.for_period, TaxPayment.kind)
        )
    ).all()
    out: dict[int, tuple[Decimal, Decimal]] = {}
    for period, kind, total in rows:
        if not period or len(period) != 7 or period[4] != "-":
            continue
        month = int(period[5:])
        if month in covered:
            continue
        contributions, ndfl = out.get(month, (ZERO, ZERO))
        if kind == "contrib_employees":
            contributions += Decimal(str(total))
        else:
            ndfl += Decimal(str(total))
        out[month] = (contributions, ndfl)
    return out


async def injury_paid_for_month(
    session: AsyncSession, *, year: int, month: int
) -> Decimal:
    """Травматизм, уплаченный за месяц N (платится внутри N, крайний срок 15.(N+1))."""
    total = await session.scalar(
        select(func.coalesce(func.sum(TaxPayment.amount), 0)).where(
            TaxPayment.kind == "contrib_injury",
            TaxPayment.status == "paid",
            TaxPayment.source_kind != "tax_notice",
            TaxPayment.paid_on >= date(year, month, 1),
            TaxPayment.paid_on <= injury_due(year, month),
        )
    )
    return Decimal(str(total or 0))


def _amounts_match(document: Decimal, operation: Decimal) -> bool:
    """Совпадают ли суммы документа и списания с допуском на рублёвое округление.

    Допуск ±1 ₽ нужен потому, что налог в платёжке округлён до рубля, но для КОПЕЕЧНЫХ сумм
    он абсурден: добор 0,90 ₽ к ЕНП попадал в допуск нулевой платёжки «УСН 2 кв» (Наталья
    прислала её пустой и на следующий день заменила) и уезжал в УСН. Поэтому обе суммы
    должны быть больше допуска, иначе требуем точного совпадения.
    """
    if document <= TOLERANCE or operation <= TOLERANCE:
        return document == operation
    return abs(document - operation) <= TOLERANCE


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


async def _injury_payroll_base(
    session: AsyncSession, *, year: int, month: int
) -> Decimal | None:
    """ФОТ месяца, восстановленный из уплаченного травматизма: сумма / 0,2 %.

    Травматизм бухгалтер считает от того же ФОТ, что и взносы, и платит его ОТДЕЛЬНОЙ
    платёжкой в СФР внутри месяца N (не в составе ЕНП) — значит, он читается из выписки
    прямо и годится основанием, когда оборотки за месяц нет. Окно поиска — месяц N плюс
    срок 15.(N+1) (125-ФЗ, ст. 22).
    """
    cfg = year_config(year)
    if not cfg.injury_rate:
        return None
    window_start = date(year, month, 1)
    window_end = injury_due(year, month)
    paid = (
        await session.scalars(
            select(TaxPayment.amount).where(
                TaxPayment.kind == "contrib_injury",
                TaxPayment.status == "paid",
                TaxPayment.paid_on >= window_start,
                TaxPayment.paid_on <= window_end,
            )
        )
    ).all()
    total = sum((Decimal(str(v)) for v in paid if v is not None), ZERO)
    if total <= ZERO:
        return None
    return money(total / cfg.injury_rate)


async def _contributions_basis(
    session: AsyncSession, *, year: int, month: int
) -> tuple[Decimal, str] | None:
    """Взносы за работников за месяц + человекочитаемое основание. None — считать нечем."""
    from_ledger = await _payroll_contributions(session, year=year, month=month)
    if from_ledger is not None:
        return from_ledger, f"по оборотке {year}-{month:02d}"

    cfg = year_config(year)
    base = await _injury_payroll_base(session, year=year, month=month)
    if base is not None:
        return month_contributions(base, cfg), (
            f"по травматизму {year}-{month:02d} (ФОТ {base} восстановлен из 0,2 %)"
        )

    accrual = await official_month_accrual(session, year=year, month=month, cfg=cfg)
    if accrual is not None:
        return accrual.contributions, f"по официальному контуру {year}-{month:02d}"
    return None


async def _split_enp(
    session: AsyncSession, *, amount: Decimal, year: int, month: int, op_date: date
) -> _Resolution | None:
    """Разнос зарплатного ЕНП: взносы месяца + НДФЛ остатком.

    Разнос расчётный (состав платежа из выписки не виден — один КБК на всё),
    поэтому качество строк — ``reconstructed``, а не ``confirmed``.
    """
    basis = await _contributions_basis(session, year=year, month=month)
    if basis is None:
        return None
    contributions, basis_note = basis
    period = f"{year}-{month:02d}"
    if amount < contributions:
        # Платёж МЕНЬШЕ взносов месяца — значит взносов в нём нет вовсе: это отдельный НДФЛ
        # (платёжка «ЕНП-НДФЛ с аванса», 1 733 ₽ при взносах 13 595,93) или добор копеек.
        # Относим целиком в НДФЛ месяцем удержания = месяцем платежа. Раньше такой платёж
        # съедал часть взносов месяца и завышал вычет УСН.
        return _Resolution(
            rows=(("ndfl", amount, "reconstructed"),),
            for_period=f"{op_date.year}-{op_date.month:02d}",
            for_year=op_date.year,
            note=f"платёж меньше взносов месяца {period} — НДФЛ без взносов",
        )
    ndfl_part = amount - contributions
    rows: list[tuple[str, Decimal, str]] = [
        ("contrib_employees", contributions, "reconstructed")
    ]
    if ndfl_part > ZERO:
        rows.append(("ndfl", ndfl_part, "reconstructed"))
    return _Resolution(
        rows=tuple(rows),
        for_period=period,
        for_year=year,
        note=f"разнос ЕНП {basis_note}: взносы {contributions} + НДФЛ {ndfl_part}",
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

    Обычный вид — одна строка как есть. Зарплатный ЕНП — обязательный разнос.

    Месяц ЕНП — это месяц НАЧИСЛЕНИЯ (N при сроке 28.N+1), и срок уплаты для него
    приоритетнее подсказки периода из документа: в платёжке «ЕНП сроком до 28.04» подсказка
    читается парсером как апрель, а взносы внутри — за март. Если верить подсказке, платежи
    за март и за апрель занимают ОДИН слот 2026-04 — вычет задваивается на одном месяце и
    теряется на другом.
    """
    if kind != "enp_payroll":
        return _Resolution(
            rows=((kind, amount, quality),),
            for_period=for_period,
            for_year=for_year or _year_for(for_period, op_date),
            note=note,
        )
    if due_date is not None:
        year, month = _month_from_due(due_date)
    elif for_period and len(for_period) == 7 and for_period[4] == "-":
        year, month = int(for_period[:4]), int(for_period[5:])
    else:
        return None
    return await _split_enp(
        session, amount=amount, year=year, month=month, op_date=op_date
    )


async def _match_draft(
    session: AsyncSession,
    *,
    amount: Decimal,
    recipient: str,
    op_date: date,
    provider: str | None = None,
    document_number: str | None = None,
) -> TaxBankDraft | None:
    """Наш черновик с точной суммой и тем же получателем.

    Номер платёжного документа — ПРИОРИТЕТ, а не фильтр. Ключ детерминирован: черновик
    коммитит ``document_id`` до вызова банка, а ``tbank._document_number`` выводит из него
    номер, который уходит в платёжке и возвращается в выписке. Это разводит по своим
    операциям черновики одной суммы, которые законно висят одновременно (травматизм 100 ₽
    за разные месяцы — частичный уникум слота уникален по виду+периоду).

    Фильтром номер делать НЕЛЬЗЯ: платёжку, подготовленную у нас, владелец или бухгалтер
    могли оплатить руками из банк-клиента — там номер свой. Жёсткий фильтр увёл бы такой
    платёж в ``other``/``requires_review``, то есть мимо вычета УСН, и налог был бы завышен.

    Из этого послабления растёт риск, который закрывают два ограничения ниже.

    **Свежесть.** Черновик, отправленный в банк больше ``DRAFT_MATCH_TTL_DAYS`` назад, по
    сумме и получателю больше НЕ ловится: не подтверждённая владельцем платёжка иначе висела
    бы активной вечно и однажды перехватила бы постороннее списание той же суммы в ФНС/СФР.
    Срок — налоговый цикл (месяц): переживший его черновик уже конкурирует с платежами
    следующего периода. По СВОЕМУ номеру документа протухший черновик находится по-прежнему,
    поэтому запоздалая оплата собственной платёжки не теряется. Отсчёт — от отправки в банк
    (``sent_to_bank_at``); у строк до миграции 0221 его нет — падаем на ``created_at``.

    **Одноразовость.** Закрытый черновик (``settled_operation_id``) из разбора выбывает:
    при пере-разносе строк (``_ripen_review_bundles``) он иначе достался бы второй операции.

    Кроме ``in_bank`` берём ``failed`` — но ТОЛЬКО по точному совпадению номера. Отказ банка
    мог случиться после того, как платёжка в нём уже создалась (``document_id`` коммитится до
    вызова банка), и такое списание раньше не подхватывалось вовсе. Послабление «по сумме»
    для ``failed`` не годится: у него нет подтверждения, что платёжка вообще существует.

    При прочих равных берём черновик с ближайшим сроком — детерминированно; остальные
    доведёт следующая операция.
    """
    from app.services.banking.tbank import _document_number

    # Возраст берём ВЫРАЖЕНИЕМ, а не с атрибута: ``created_at`` заполняется server_default,
    # и у только что вставленной строки обращение к нему ушло бы в ленивую догрузку — в
    # асинхронной сессии это MissingGreenlet.
    rows = (
        await session.execute(
            select(
                TaxBankDraft,
                func.coalesce(TaxBankDraft.sent_to_bank_at, TaxBankDraft.created_at),
            ).where(TaxBankDraft.status.in_(("in_bank", "failed")))
        )
    ).all()
    raw_number = (document_number or "").strip()

    def _by_number(draft: TaxBankDraft) -> bool:
        return bool(
            raw_number and draft.document_id and _document_number(draft.document_id) == raw_number
        )

    fresh_before = datetime.now(UTC) - timedelta(days=DRAFT_MATCH_TTL_DAYS)
    candidates = []
    for d, sent_at in rows:
        # Черновик уходил в свой банк: сберовское списание той же суммы — не он.
        if provider is not None and d.bank_provider != provider:
            continue
        d_recipient = "sfr" if d.tax_kind == "contrib_injury" else "fns"
        if d_recipient != recipient or Decimal(str(d.amount)) != amount:
            continue
        if d.settled_operation_id is not None:
            continue
        if not _by_number(d):
            # Без совпадения номера черновик обязан быть отправленным и свежим.
            if d.status != "in_bank":
                continue
            if _aware(sent_at) < fresh_before:
                continue
        candidates.append(d)
    if not candidates:
        return None

    def _rank(draft: TaxBankDraft) -> tuple[int, int, str]:
        return (
            0 if _by_number(draft) else 1,
            abs(((draft.due_date or op_date) - op_date).days),
            str(draft.id),
        )

    candidates.sort(key=_rank)
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
    candidates = [
        r for r in rows if _amounts_match(Decimal(str(r.amount)), amount)
    ]
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
            # Тай-брейк по id: вложения одного письма несут один received_at.
            .order_by(TaxDocumentIntake.received_at.asc(), TaxDocumentIntake.id.asc())
        )
    ).all()
    best: dict | None = None
    for row in rows:
        rec = row.recognition or {}
        raw_amount = rec.get("amount")
        if raw_amount is None:
            continue
        if not _amounts_match(Decimal(str(raw_amount)), amount):
            continue
        kind = rec.get("tax_kind")
        if not kind:
            continue
        rec_recipient = "sfr" if kind == "contrib_injury" else "fns"
        if rec_recipient != recipient:
            continue
        best = rec  # последняя по received_at побеждает
    return best


async def _match_enp_topup(
    session: AsyncSession, *, amount: Decimal, recipient: str
) -> _Resolution | None:
    """Добор к уже уплаченной платёжке ЕНП: платёж ровно на недостающую разницу.

    Реальный случай: платёжка ЕНП за март была на 20 559,93, ушло 20 559,03, и бухгалтер
    написала «по платежке 286 не доплатили 90 копеек» — на следующий день ушли эти 90 копеек.
    Отдельной платёжки на них нет, по сумме такой платёж не матчится ни с чем и оставался
    «прочим» (в вычет не идёт, висит «нужна проверка»). Ищем документ, у которого факт
    меньше ровно на эту сумму, и относим добор туда же — в НДФЛ: взносы месяца предыдущий
    платёж уже закрыл полностью (разнос кладёт их первыми).

    Совпадение требуется ТОЧНОЕ: допуск тут опасен — под «почти подходит» попадёт любой
    мелкий платёж.
    """
    if recipient != "fns" or amount <= ZERO:
        return None
    docs = (
        await session.scalars(
            select(TaxDocumentIntake)
            .where(
                TaxDocumentIntake.document_type == "payment_order",
                TaxDocumentIntake.status.in_(("parsed", "needs_review", "promoted")),
            )
            .order_by(TaxDocumentIntake.received_at.asc(), TaxDocumentIntake.id.asc())
        )
    ).all()
    for row in docs:
        rec = row.recognition or {}
        if rec.get("tax_kind") != "enp_payroll":
            continue
        raw_amount, raw_due = rec.get("amount"), rec.get("due_date")
        if raw_amount is None or not raw_due:
            continue
        year, month = _month_from_due(date.fromisoformat(raw_due))
        period = f"{year}-{month:02d}"
        paid = await session.scalar(
            select(func.coalesce(func.sum(TaxPayment.amount), 0)).where(
                TaxPayment.status == "paid",
                TaxPayment.for_year == year,
                TaxPayment.for_period == period,
                TaxPayment.kind.in_(("contrib_employees", "ndfl")),
                TaxPayment.source_kind != "tax_notice",
            )
        )
        already_paid = Decimal(str(paid or 0))
        if already_paid <= ZERO:
            continue  # по платёжке ещё вообще не платили — это не добор, а первый платёж
        shortfall = Decimal(str(raw_amount)) - already_paid
        if shortfall == amount:
            return _Resolution(
                rows=(("ndfl", amount, "reconstructed"),),
                for_period=period,
                for_year=year,
                note=f"добор к платёжке ЕНП за {period}: недоплаченный НДФЛ",
            )
    return None


async def _resolve_operation(
    session: AsyncSession, op: BankOperation, recipient: str
) -> tuple[_Resolution, TaxBankDraft | None]:
    """Классифицировать операцию по слоям доверия. Всегда возвращает решение:
    последний фолбэк — ``other``/``requires_review`` (дозреет или уйдёт в ручную проверку).
    """
    amount = Decimal(str(op.amount))

    draft = await _match_draft(
        session,
        amount=amount,
        recipient=recipient,
        op_date=op.operation_date,
        provider=op.provider,
        document_number=op.document_number,
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

    topup = await _match_enp_topup(session, amount=amount, recipient=recipient)
    if topup is not None:
        return topup, None

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
                # Ссылку ставит проектор ДДС (services/taxes/dds_projection) — на СВОЮ
                # проводку каждой строки. Слепая копия якоря операции здесь врала бы: после
                # дозревания строка пересоздаётся, и «факт без проводки» — единственный
                # признак, по которому проектор понимает, что разнос надо пересобрать.
                cashflow_transaction_id=None,
                document_number=op.document_number,
                purpose=op.payment_purpose,
                note=resolution.note,
            )
        )
    return rows


async def _existing_operation_row(
    session: AsyncSession, operation_id, kind: str
) -> TaxPayment | None:
    """Строка факта по слоту (операция, вид) — перечит для проигравшего гонку прогона."""
    return (
        await session.scalars(
            select(TaxPayment).where(
                TaxPayment.bank_operation_id == operation_id,
                TaxPayment.kind == kind,
            )
        )
    ).first()


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


def _settle_draft(draft: TaxBankDraft, op: BankOperation) -> None:
    """Закрыть черновик операцией, которая его оплатила.

    Ссылка на операцию — не украшение: она и есть признак «этот черновик уже отработан».
    Статуса ``paid`` для этого мало, потому что ``failed``-черновик тоже может быть закрыт
    (платёжка в банке создалась до отказа), а пере-разнос строк переоткрывает разбор.
    """
    draft.status = "paid"
    draft.settled_operation_id = op.id


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
            select(TaxPayment)
            .where(
                TaxPayment.source_kind == "bank_statement",
                TaxPayment.kind == "other",
                TaxPayment.quality_status == "requires_review",
                TaxPayment.bank_operation_id.isnot(None),
            )
            # Порядок дозревания детерминирован: каждая итерация меняет состояние
            # (черновик paid, план погашен), от которого зависит матчинг следующих —
            # без ORDER BY «кому достанется черновик» решал порядок строк Postgres.
            .order_by(TaxPayment.paid_on.asc(), TaxPayment.id.asc())
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
            _settle_draft(draft, op)
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

    operations = await _tax_operations_without_facts(session)
    report.operations_seen = len(operations)
    for op in sorted(operations, key=lambda o: (o.operation_date, str(o.id))):
        recipient = _recipient_for_operation(op)
        if recipient is None:  # защита от гонки — отбор уже отфильтровал
            continue
        resolution, draft = await _resolve_operation(session, op, recipient)
        # Вебхук и почасовой поллинг могут прийти на одну операцию одновременно —
        # дубль факта задвоил бы вычет. Гарантия — уникальный слот (операция, вид):
        # проигравший прогон просто пропускает уже созданную строку (миграция 0215).
        raced = False
        for row in _build_rows(op, recipient, resolution):
            _, created = await insert_or_reread(
                session,
                row,
                reread=lambda r=row: _existing_operation_row(session, r.bank_operation_id, r.kind),
            )
            raced = raced or not created
        if raced:
            continue  # операцию уже разнёс параллельный прогон — счётчики не двигаем
        if draft is not None:
            _settle_draft(draft, op)
            report.drafts_paid += 1
        report.bundles_created += 1
        report.details.append(f"{op.operation_date} {op.amount} ₽: {resolution.note}")
    await session.flush()

    # Дозревание — ПОСЛЕ разбора новых операций, а не до. Основание разноса может появиться
    # в этом же проходе: травматизм за январь уплачен 26.01, а НДФЛ-ЕНП — 21.01, и при
    # обратном порядке платёж 21.01 оставался «нужна проверка» до следующего нажатия кнопки.
    # Итог прогона не должен зависеть от того, сколько раз его запустили.
    await _ripen_review_bundles(session, report)
    report.review_pending = await _review_pending_count(session)
    return report


async def _review_pending_count(session: AsyncSession) -> int:
    """Сколько платежей всё ещё ждут разноса — считаем по факту, а не накоплением счётчика."""
    rows = await session.scalars(
        select(TaxPayment.id).where(
            TaxPayment.kind == "other",
            TaxPayment.quality_status == "requires_review",
            TaxPayment.bank_operation_id.is_not(None),
        )
    )
    return len(rows.all())
