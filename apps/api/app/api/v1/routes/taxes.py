"""HTTP-слой модуля «Налоги»: расчёт УСН, сверка, календарь сроков, документы бухгалтера.

Роутер намеренно ТОНКИЙ: вся методология живёт в ``app/services/taxes`` и проверяется
тестами на числах без базы. Здесь только разбор параметров, права, перевод доменных ошибок
в HTTP и сборка ответа. Ни одна цифра налога в этом файле не считается.

Смысл модуля для владельца (он не бухгалтер) — СВЕРКА. На одно обязательство приходят три
независимых источника: наш расчёт из выручки iiko, платёжка бухгалтера и факт списания из
банка. Пока они сходятся, страница молчит. Разошлись — показывает это первым экраном.
Реальный случай, ради которого всё строилось: платёжка по УСН на 0 ₽ при расчёте
478 376 ₽ — такое должно ловиться автоматикой, а не глазами.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.api.deps import CurrentActor, get_current_actor, require_any_permission
from app.core.config import Settings, get_settings
from app.db.session import get_session
from app.models.tax import (
    TAX_INTAKE_STATUSES,
    TAX_PAYMENT_KINDS,
    TaxBankDraft,
    TaxDocumentIntake,
    TaxPayment,
)
from app.schemas.taxes import (
    AiAuditFindingRead,
    AiAuditReportRead,
    AiDocumentReviewRead,
    EnsWalletRead,
    LedgerRowRead,
    LedgerSummaryRead,
    ReconciliationRead,
    ReconLineRead,
    TaxBankDraftInput,
    TaxBankDraftResultRead,
    TaxCalendarItemRead,
    TaxCalendarRead,
    TaxDebtItemRead,
    TaxDebtRead,
    TaxDocumentListRead,
    TaxDocumentRead,
    TaxDocumentReviewInput,
    TaxDraftSendInput,
    TaxOverviewRead,
    TaxPaymentDraftInput,
    TaxPaymentDraftRead,
    TaxPaymentListRead,
    TaxPaymentRead,
    TaxPromotionResultRead,
    TaxPromotionSummaryRead,
    TaxSourceRead,
    TaxSourcesRead,
    TaxStateRead,
    VatMonthAccrualRead,
    VatThresholdInput,
    VatWageCriterionRead,
)
from app.services.banking.exceptions import BankCredentialsError, BankFetchError
from app.services.taxes.ai_reviewer import TaxAiError, apply_ai_proposal, review_all
from app.services.taxes.ai_reviewer import review_document as ai_review_document
from app.services.taxes.bank_draft import (
    TaxDraftError,
    cancel_tax_draft,
    create_tax_bank_draft,
    create_tax_payment_draft,
    send_tax_draft_to_bank,
)
from app.services.taxes.document_ingest import (
    ingest_tax_documents,
    resolve_tax_senders,
    set_intake_review,
)
from app.services.taxes.engine import TaxComputationError, TaxState, compute_tax_state
from app.services.taxes.ens_wallet import compute_ens_wallet
from app.services.taxes.ledger_summary import LedgerSummary, build_ledger_summary
from app.services.taxes.obligations import is_settled, list_payable_obligations
from app.services.taxes.promote import promote_ready_intakes
from app.services.taxes.reconcile import Reconciliation, build_reconciliation
from app.services.taxes.repository import load_tax_inputs
from app.services.taxes.sources import (
    load_source_registry,
    registry_to_payload,
    weakest_links,
)
from app.services.taxes.vat_monitor import (
    VatWageCriterion,
    evaluate_vat_wage_criterion,
    load_regional_wage,
    save_regional_wage,
)

router = APIRouter()

ZERO = Decimal("0")

# Право «manage» включает в себя чтение: тот, кто ведёт налоги, обязан их видеть.
# Отдельный код для чтения нужен, чтобы владелец мог открыть страницу, не получая
# возможности продвигать документы бухгалтера в обязательства.
TAXES_READ = (
    Depends(require_any_permission(("accounting.taxes.read", "accounting.taxes.manage"))),
)
TAXES_MANAGE = (Depends(require_any_permission(("accounting.taxes.manage",))),)

# Статусы обязательств, попадающих в календарь. Отменённые не показываем: они не создают
# ни срока, ни задолженности, и только зашумляют таймлайн.
CALENDAR_STATUSES = ("planned", "paid")


def _resolve_as_of(as_of: date | None) -> date:
    """Дата среза расчёта. По умолчанию — сегодня."""
    return as_of or date.today()


def _resolve_year(year: int | None) -> int:
    """Налоговый год. По умолчанию — текущий."""
    return year or date.today().year


async def _load_state(session: AsyncSession, as_of: date) -> TaxState:
    """Посчитать состояние на дату среза, переведя доменную ошибку в 422.

    ``TaxComputationError`` — это не сбой сервиса, а осмысленный отказ считать: нет
    параметров налогового года либо год вообще не на УСН (2025-й был на патенте).
    Тексты движка уже написаны по-русски и для человека, поэтому отдаём их как есть.
    """
    try:
        inputs = await load_tax_inputs(session, as_of=as_of)
        return compute_tax_state(inputs)
    except TaxComputationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


async def _load_ledger(
    session: AsyncSession, as_of: date, state: TaxState
) -> LedgerSummary:
    """Сводка «начислено / уплачено / осталось» на дату среза."""
    try:
        inputs = await load_tax_inputs(session, as_of=as_of)
        cfg = inputs.config
        return await build_ledger_summary(
            session, state=state, inputs=inputs, cfg=cfg, as_of=as_of
        )
    except TaxComputationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


async def _load_reconciliation(session: AsyncSession, as_of: date) -> Reconciliation:
    """Собрать сверку на дату среза, переведя доменную ошибку в 422."""
    try:
        return await build_reconciliation(session, as_of=as_of)
    except TaxComputationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


def _source_version(raw: object) -> int:
    """Версия реестра из настроек. Мусор в поле не должен ронять справочную страницу."""
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw)
    return 1


def _reconciliation_read(recon: Reconciliation) -> ReconciliationRead:
    return ReconciliationRead(
        year=recon.year,
        as_of=recon.as_of,
        lines=[ReconLineRead.model_validate(line) for line in recon.lines],
        alert_count=len(recon.alerts),
        has_alerts=recon.has_alerts,
    )


@router.get("/overview", response_model=TaxOverviewRead, dependencies=TAXES_READ)
async def get_overview(
    session: Annotated[AsyncSession, Depends(get_session)],
    as_of: Annotated[
        date | None,
        Query(description="Дата среза расчёта нарастающим итогом. По умолчанию — сегодня."),
    ] = None,
) -> TaxOverviewRead:
    """Первый экран: сколько должны бюджету и сходится ли это с бухгалтером и банком.

    Расчёт и сверка отдаются ОДНИМ ответом сознательно — они обязаны быть на одну дату
    среза. Разделив их на два запроса, легко получить состояние, где расчёт уже на новой
    выручке, а сверка ещё на старой, и показать владельцу расхождение, которого нет.

    ``alert_count`` — количество обязательств, требующих реакции прямо сейчас: платёжка
    меньше расчёта или срок прошёл без оплаты. ``is_blocked`` означает, что выручка
    загружена не за весь период, то есть цифру нельзя считать окончательной.
    """
    moment = _resolve_as_of(as_of)
    state = await _load_state(session, moment)
    recon = await _load_reconciliation(session, moment)
    ledger = await _load_ledger(session, moment, state)
    return TaxOverviewRead(
        as_of=moment,
        year=state.year,
        period_code=state.period_code,
        state=TaxStateRead.model_validate(state),
        reconciliation=_reconciliation_read(recon),
        alert_count=len(recon.alerts),
        is_blocked=bool(state.blocking),
        ledger=LedgerSummaryRead(
            year=ledger.year,
            as_of=ledger.as_of,
            rows=[LedgerRowRead.model_validate(row) for row in ledger.rows],
            accrued_total=ledger.accrued_total,
            paid_total=ledger.paid_total,
            left_total=ledger.left_total,
        ),
    )


@router.get("/reconciliation", response_model=ReconciliationRead, dependencies=TAXES_READ)
async def get_reconciliation(
    session: Annotated[AsyncSession, Depends(get_session)],
    as_of: Annotated[
        date | None, Query(description="Дата среза сверки. По умолчанию — сегодня.")
    ] = None,
) -> ReconciliationRead:
    """Только сверка трёх слоёв — для отдельной вкладки и для перепроверки после правок.

    Каждая строка — одно обязательство в трёх измерениях: расчёт, документ, факт. Пустое
    измерение (``null``) значит «источника нет», и это не то же самое, что ноль в нём.
    """
    recon = await _load_reconciliation(session, _resolve_as_of(as_of))
    return _reconciliation_read(recon)


@router.get("/debt", response_model=TaxDebtRead, dependencies=TAXES_READ)
async def get_tax_debt(
    session: Annotated[AsyncSession, Depends(get_session)],
    as_of: Annotated[
        date | None, Query(description="Дата среза. По умолчанию — сегодня.")
    ] = None,
) -> TaxDebtRead:
    """Расчёты с бюджетом для страницы «Учёт ДЗ/КЗ»: одна строка + детализация.

    Кредиторка — все неоплаченные обязательства из ``list_payable_obligations`` (тот же
    источник, что наполняет «Активные платежи»: одна цифра в трёх местах по построению).
    Черновик «в банке» долг НЕ гасит — гасит только факт списания из выписки, — но его
    статус показываем, чтобы владелец видел, что платёж уже в работе.

    Дебиторка — расчётное сальдо ЕНС (переплата в бюджет): факты уплаты минус признанные
    начисления. Считается, а не вводится руками, поэтому не протухает.
    """
    moment = _resolve_as_of(as_of)
    try:
        obligations = await list_payable_obligations(session, today=moment)
        wallet = await compute_ens_wallet(session, as_of=moment)
    except TaxComputationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    # Активные черновики — статусом на строку (вид+период; 1% матчится по одному виду:
    # сверка кодирует его годом, платёжка приходит на прирост квартала).
    drafts = (
        await session.scalars(
            select(TaxBankDraft).where(
                TaxBankDraft.status.in_(("ready_to_send", "in_bank"))
            )
        )
    ).all()

    def _draft_status(kind: str, period: str | None) -> str | None:
        for d in drafts:
            if d.tax_kind != kind:
                continue
            if d.for_period == period or kind == "contrib_extra_1pct":
                return d.status
        return None

    items = [
        TaxDebtItemRead(
            kind=ob.kind,
            title=ob.title,
            for_year=ob.for_year,
            for_period=ob.for_period,
            amount=ob.amount,
            due_date=ob.due_date,
            draft_status=None if ob.is_projection else _draft_status(ob.kind, ob.for_period),
            is_projection=ob.is_projection,
        )
        for ob in sorted(obligations, key=lambda o: (o.due_date or moment, -o.amount))
    ]
    return TaxDebtRead(
        as_of=moment,
        payable_total=sum((ob.amount for ob in obligations), ZERO),
        items=items,
        wallet=EnsWalletRead.model_validate(wallet),
    )


@router.get("/calendar", response_model=TaxCalendarRead, dependencies=TAXES_READ)
async def get_calendar(
    session: Annotated[AsyncSession, Depends(get_session)],
    year: Annotated[
        int | None,
        Query(ge=2000, le=2100, description="Налоговый год. По умолчанию — текущий."),
    ] = None,
) -> TaxCalendarRead:
    """Таймлайн обязательств за год: что уже уплачено и какой срок ближайший.

    Отбор идёт по ``for_year``, а НЕ по дате платежа: годовой налог за 2026-й уплачивается
    в апреле 2027-го и обязан остаться в календаре 2026 года, иначе год выглядит недоплаченным.

    Просрочка считается на СЕГОДНЯ, а не на дату среза расчёта: владельцу нужен ответ
    «горит ли сейчас», а не «горело ли на отчётную дату».
    """
    target_year = _resolve_year(year)
    today = date.today()

    rows = (
        await session.scalars(
            select(TaxPayment)
            .where(
                TaxPayment.for_year == target_year,
                TaxPayment.status.in_(CALENDAR_STATUSES),
            )
            .order_by(TaxPayment.paid_on, TaxPayment.kind)
        )
    ).all()

    # Обязательство, по которому уже есть ФАКТ уплаты, закрыто: плановая строка-«близнец»
    # (создана продвижением платёжки) — шум и ложная «просрочка», её не показываем — уплату
    # уже представляет paid-строка. Правило матчинга общее с окном «Активные платежи».
    paid_rows = [r for r in rows if r.status == "paid"]

    items: list[TaxCalendarItemRead] = []
    planned_total = ZERO
    paid_total = ZERO
    overdue_total = ZERO
    overdue_count = 0
    used_facts: set = set()  # один факт гасит одну плановую строку

    for row in rows:
        is_planned = row.status == "planned"
        if is_planned and is_settled(row, paid_rows, used_fact_ids=used_facts):
            continue
        # Разнос из оборотки (tax_notice) помечен «paid» по кассовой конвенции: дата = СРОК
        # уплаты. Пока срок не наступил, деньги ещё не ушли — для календаря это ПЛАН
        # («Зарплатный ЕНП за июль к 28.08»), а не свершившаяся уплата с будущей датой.
        planned_like = is_planned or (
            row.source_kind == "tax_notice" and row.status == "paid" and row.paid_on > today
        )
        # У плановой строки в `paid_on` лежит СРОК уплаты (так её заполняет продвижение
        # документа), у уплаченной — фактическая дата списания. Раскладываем явно.
        due_date = row.paid_on if planned_like else None
        # Просрочка — только у настоящих планов: разнос прошедшего срока считается
        # уплаченным по конвенции (факт сверяется отдельно, в сверке).
        is_overdue = is_planned and row.paid_on < today

        if planned_like:
            planned_total += row.amount
            if is_overdue:
                overdue_total += row.amount
                overdue_count += 1
        else:
            paid_total += row.amount

        item = TaxCalendarItemRead.model_validate(row)
        items.append(
            item.model_copy(
                update={
                    "due_date": due_date,
                    "is_overdue": is_overdue,
                    "status": "planned" if planned_like else row.status,
                }
            )
        )

    return TaxCalendarRead(
        year=target_year,
        today=today,
        items=items,
        planned_total=planned_total,
        paid_total=paid_total,
        overdue_total=overdue_total,
        overdue_count=overdue_count,
    )


@router.get("/payments", response_model=TaxPaymentListRead, dependencies=TAXES_READ)
async def list_payments(
    session: Annotated[AsyncSession, Depends(get_session)],
    year: Annotated[
        int | None,
        Query(ge=2000, le=2100, description="Налоговый год (for_year). По умолчанию — текущий."),
    ] = None,
    kind: Annotated[
        str | None,
        Query(description="Вид платежа: usn_advance, ndfl, contrib_* и т.д."),
    ] = None,
) -> TaxPaymentListRead:
    """Журнал ФАКТИЧЕСКИХ уплат в бюджет за год — что реально ушло из банка.

    Здесь ТОЛЬКО реальные списания (``status='paid'`` из выписки или ручного ввода).
    Плановые обязательства живут в «Календаре», разнос ЕНП по месяцам — в «Сводке»:
    раньше все три вида строк были свалены сюда вперемешку, и страница показывала
    «оплачено» с будущими датами (разнос по сроку) рядом с дублями планов — владелец
    справедливо не смог её прочитать (26.07.2026).

    Одна строка = одно НАЗНАЧЕНИЕ внутри перевода, а не один перевод. ЕНП уходит единой
    суммой, но внутри и НДФЛ (в вычет не идёт), и взносы за работников (идут) — без
    разложения вычет посчитать нельзя, поэтому журнал ведётся по назначениям.
    """
    target_year = _resolve_year(year)

    if kind is not None and kind not in TAX_PAYMENT_KINDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Неизвестный вид платежа: {kind!r}. "
                f"Допустимые: {', '.join(TAX_PAYMENT_KINDS)}."
            ),
        )

    stmt = select(TaxPayment).where(
        TaxPayment.for_year == target_year,
        TaxPayment.status == "paid",
        # Разнос из оборотки (tax_notice) — начисления для сверки, не движение денег.
        TaxPayment.source_kind.in_(("bank_statement", "manual")),
    )
    if kind is not None:
        stmt = stmt.where(TaxPayment.kind == kind)

    rows = (
        await session.scalars(stmt.order_by(TaxPayment.paid_on.desc(), TaxPayment.kind))
    ).all()

    paid_total = ZERO
    totals_by_kind: dict[str, Decimal] = {}
    for row in rows:
        paid_total += row.amount
        totals_by_kind[row.kind] = totals_by_kind.get(row.kind, ZERO) + row.amount

    return TaxPaymentListRead(
        year=target_year,
        items=[TaxPaymentRead.model_validate(row) for row in rows],
        total=len(rows),
        paid_total=paid_total,
        totals_by_kind=totals_by_kind,
    )


@router.get("/documents", response_model=TaxDocumentListRead, dependencies=TAXES_READ)
async def list_documents(
    session: Annotated[AsyncSession, Depends(get_session)],
    document_status: Annotated[
        str | None,
        Query(
            alias="status",
            description=(
                "Статус разбора: parsed | needs_review | promoted | "
                "unsupported | error | ignored."
            ),
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> TaxDocumentListRead:
    """Входящие документы бухгалтера: что распознано уверенно, а что ждёт владельца.

    Разбор намеренно НЕ создаёт факты налога сам — он складывает распознанное в staging.
    Так распознавание по рукописному имени файла остаётся под human-контролем и не уходит
    в расчёт молча. ``needs_review`` — это очередь на решение владельца.

    ``status_counts`` считается по ВСЕЙ таблице, а не по отфильтрованной выборке: счётчик
    в бейдже не должен обнуляться от того, что пользователь открыл конкретный фильтр.
    ``total`` — сколько строк подходит под фильтр ВСЕГО, а не сколько вернулось под
    ``limit``: иначе на 201-м документе счётчик молча замрёт на 200. Тело вложения наружу
    не отдаётся и из БД не поднимается.
    """
    if document_status is not None and document_status not in TAX_INTAKE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Неизвестный статус документа: {document_status!r}. "
                f"Допустимые: {', '.join(TAX_INTAKE_STATUSES)}."
            ),
        )

    received = func.coalesce(TaxDocumentIntake.received_at, TaxDocumentIntake.created_at)
    # Флаг наличия исходного файла считаем прямо в SQL (content IS NOT NULL) — сами байты
    # по-прежнему не поднимаем (defer), в список идёт только булев признак для кнопки «Открыть».
    has_file_expr = TaxDocumentIntake.content.isnot(None)
    stmt = select(TaxDocumentIntake, has_file_expr).options(defer(TaxDocumentIntake.content))
    if document_status is not None:
        stmt = stmt.where(TaxDocumentIntake.status == document_status)

    # `id` в сортировке — не украшение: при равных `received_at` (пачка вложений из одного
    # письма приходит одной секундой) порядок без него не определён, и строки под `limit`
    # могут прыгать между запросами.
    rows = (
        await session.execute(
            stmt.order_by(received.desc(), TaxDocumentIntake.id).limit(limit)
        )
    ).all()

    counts = (
        await session.execute(
            select(TaxDocumentIntake.status, func.count())
            .select_from(TaxDocumentIntake)
            .group_by(TaxDocumentIntake.status)
        )
    ).all()

    status_counts = {row_status: int(count) for row_status, count in counts}
    total = (
        status_counts.get(document_status, 0)
        if document_status is not None
        else sum(status_counts.values())
    )

    items = []
    for intake, intake_has_file in rows:
        read = TaxDocumentRead.model_validate(intake)
        read.has_file = bool(intake_has_file)
        items.append(read)

    return TaxDocumentListRead(
        items=items,
        total=total,
        status_counts=status_counts,
    )


@router.get("/documents/{intake_id}/file", dependencies=TAXES_READ)
async def get_document_file(
    intake_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Отдать исходный файл документа (платёжка/ведомость/оборотка) как есть.

    Основание в один клик из строки «Документы» — так же, как PDF-счёт открывается на
    «Странице на оплату». Байты лежат в staging (``content``); отдаём их только по этому
    прямому запросу, в списке — лишь флаг ``has_file``.
    """
    intake = await session.get(TaxDocumentIntake, intake_id)
    if intake is None or not intake.content:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Файл документа недоступен.")
    # Имена файлов бухгалтера кириллические, а HTTP-заголовки только latin-1 → ASCII-фолбэк
    # + RFC 5987 (filename*), иначе Starlette роняет ответ. Тот же приём, что в payment_page.
    raw_name = intake.filename or "document"
    ascii_name = raw_name.encode("ascii", "ignore").decode("ascii") or "document"
    disposition = f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(raw_name)}"
    return Response(
        content=bytes(intake.content),
        media_type=intake.mime or "application/octet-stream",
        headers={"Content-Disposition": disposition},
    )


@router.post(
    "/documents/promote",
    response_model=TaxPromotionSummaryRead,
    dependencies=TAXES_MANAGE,
)
async def promote_documents(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> TaxPromotionSummaryRead:
    """Продвинуть распознанные платёжки в плановые обязательства.

    Мост между «бухгалтер прислала платёжку» и календарём: документ становится
    обязательством, которое видно в сроках и участвует в сверке ЕЩЁ ДО того, как деньги
    ушли из банка. Именно поэтому расхождение ловится заранее, а не постфактум.

    Операция идемпотентна: повторный документ по тому же обязательству обновляет плановую
    сумму, а не плодит вторую строку. Непригодные документы (нулевая платёжка-заглушка,
    смешанный зарплатный ЕНП без разноса) пропускаются с причиной и НЕ роняют прогон —
    поэтому ответ всегда 200, а разбираться нужно по ``skipped``.
    """
    results = await promote_ready_intakes(session, actor_user_id=actor.user_id)
    await session.commit()

    return TaxPromotionSummaryRead(
        created=sum(1 for item in results if item.action == "created"),
        updated=sum(1 for item in results if item.action == "updated"),
        skipped=sum(1 for item in results if item.action == "skipped"),
        results=[TaxPromotionResultRead.model_validate(item) for item in results],
    )


@router.post("/documents/refresh", dependencies=TAXES_MANAGE)
async def refresh_documents(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, int | str]:
    """Забрать новые документы из почты бухгалтера в staging «Документы» прямо сейчас.

    Тянет живой IMAP настроенных ящиков (MAILRU_*), берёт вложения от отправителей из
    настройки ``tax_document_senders``, кладёт в staging с дедупом по SHA-256. Если почта не
    настроена (например, превью без кредов) — вернётся ``{"status": "not_configured"}``, а
    сообщение об этом покажет UI. Фоновый забор по расписанию делает планировщик
    (``tax_document_poll_enabled``); эта кнопка — ручная проверка в любой момент.
    """
    settings = get_settings()
    senders = resolve_tax_senders(settings.tax_document_senders)
    return await ingest_tax_documents(session, settings=settings, senders=senders)


@router.post(
    "/documents/{intake_id}/review",
    response_model=TaxDocumentRead,
    dependencies=TAXES_MANAGE,
)
async def review_document(
    session: Annotated[AsyncSession, Depends(get_session)],
    intake_id: uuid.UUID,
    body: TaxDocumentReviewInput,
) -> TaxDocumentRead:
    """Отметить документ проверенным (``parsed`` — готов к продвижению) или отклонить
    (``ignored``). Так владелец подтверждает то, что автоматика оставила на проверку.

    Вместе со статусом можно дозаполнить нераспознанные поля платёжки (срок, сумму,
    вид, период) — продвижение возьмёт исправленные значения.
    """
    intake = await session.get(TaxDocumentIntake, intake_id)
    if intake is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Документ не найден.")
    overrides = {
        "tax_kind": body.tax_kind,
        "amount": body.amount,
        "due_date": body.due_date.isoformat() if body.due_date else None,
        "period_hint": body.period_hint,
    }
    overrides = {k: v for k, v in overrides.items() if v is not None}
    try:
        await set_intake_review(
            session, intake, status=body.status, overrides=overrides or None
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    await session.commit()
    return TaxDocumentRead.model_validate(intake)


@router.post(
    "/documents/{intake_id}/ai-review",
    response_model=AiDocumentReviewRead,
    dependencies=TAXES_MANAGE,
)
async def ai_review_one(
    session: Annotated[AsyncSession, Depends(get_session)],
    intake_id: uuid.UUID,
) -> AiDocumentReviewRead:
    """ИИ-разбор одного документа: модель читает файл, объясняет и дозаполняет поля.

    Уверенные правки платёжек применяются той же дорогой, что ручная проверка (с той же
    валидацией), с пометкой «проверено ИИ». Неуверенные — только объяснение владельцу.
    """
    intake = await session.get(TaxDocumentIntake, intake_id)
    if intake is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Документ не найден.")
    try:
        result = await ai_review_document(session, intake)
    except TaxAiError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    await session.commit()
    return AiDocumentReviewRead.model_validate(result)


@router.post(
    "/documents/{intake_id}/ai-apply",
    response_model=TaxDocumentRead,
    dependencies=TAXES_MANAGE,
)
async def ai_apply(
    session: Annotated[AsyncSession, Depends(get_session)],
    intake_id: uuid.UUID,
) -> TaxDocumentRead:
    """Владелец подтвердил предложение ИИ в окне разбора — применить сохранённые поля.

    Применяется ровно то, что предлагал ИИ (из ``recognition.ai_review.proposal``), той же
    дорогой и с той же валидацией, что ручная проверка. Документ становится «распознан».
    """
    intake = await session.get(TaxDocumentIntake, intake_id)
    if intake is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Документ не найден.")
    try:
        await apply_ai_proposal(session, intake)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    await session.commit()
    return TaxDocumentRead.model_validate(intake)


@router.post("/ai-review", response_model=AiAuditReportRead, dependencies=TAXES_MANAGE)
async def ai_review_everything(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AiAuditReportRead:
    """ИИ-ревизия: разобрать все документы, требующие внимания, и проверить контур целиком.

    Модель получает снимок сверки, обязательств и ЕНС-кошелька и возвращает вердикт с
    замечаниями. Правки данных — только через тот же контур, что ручная проверка.
    """
    try:
        report = await review_all(session)
    except TaxAiError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    await session.commit()
    return AiAuditReportRead(
        verdict=report.verdict,
        findings=[AiAuditFindingRead.model_validate(f) for f in report.findings],
        documents=[AiDocumentReviewRead.model_validate(d) for d in report.documents],
    )


@router.get("/sources", response_model=TaxSourcesRead, dependencies=TAXES_READ)
async def get_sources(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TaxSourcesRead:
    """Откуда взялось каждое число и насколько ему можно верить.

    Расчёт собирается из шести разнородных входов, и они сильно отличаются по надёжности:
    доход подтверждён двумя независимыми сверками, а разнос ЕНП на НДФЛ и взносы — наша
    реконструкция, потому что банк отдаёт все бюджетные платежи под одним КБК и назначение
    из выписки прочитать нельзя.

    Без явного реестра через полгода никто не вспомнит, какая цифра первична, а какая
    выведена — поэтому слабые звенья вынесены отдельным списком и показываются владельцу
    рядом с расчётом, а не прячутся в документации.
    """
    payload = await load_source_registry(session)
    # Реестр редактируется через настройки (`app_setting`, ключ `taxes.sources`), то есть
    # его форму гарантирует не код, а человек. Справочник не имеет права уронить страницу
    # расчёта, поэтому неожиданная форма деградирует до сида кода, а битые записи молча
    # пропускаются — иначе одна кривая строка в JSON стоит владельцу всей страницы «Налоги».
    if not isinstance(payload, dict):
        payload = registry_to_payload()
    raw_sources = payload.get("sources")
    rows = (
        [item for item in raw_sources if isinstance(item, dict)]
        if isinstance(raw_sources, list)
        else []
    )

    return TaxSourcesRead(
        version=_source_version(payload.get("version")),
        sources=[TaxSourceRead.model_validate(item) for item in rows],
        weakest_links=[
            TaxSourceRead.model_validate(item) for item in weakest_links({"sources": rows})
        ],
    )


def _vat_read(criterion: VatWageCriterion) -> VatWageCriterionRead:
    return VatWageCriterionRead(
        year=criterion.year,
        active_employee=criterion.active_employee,
        active_tab=criterion.active_tab,
        months=[
            VatMonthAccrualRead(
                month=m.month, accrued=m.accrued, oklad=m.oklad, full_month=m.full_month
            )
            for m in criterion.months
        ],
        indicator_full=criterion.indicator_full,
        indicator_all=criterion.indicator_all,
        threshold=criterion.threshold,
        passes=criterion.passes,
        margin=criterion.margin,
        messages=criterion.messages,
    )


@router.get("/vat-criterion", response_model=VatWageCriterionRead, dependencies=TAXES_READ)
async def get_vat_criterion(
    session: Annotated[AsyncSession, Depends(get_session)],
    year: Annotated[int | None, Query(ge=2000, le=2100)] = None,
) -> VatWageCriterionRead:
    """Зарплатный критерий льготы по НДС: средние начисления действующего работника ↔ порог."""
    resolved = year or date.today().year
    threshold = await load_regional_wage(session, year=resolved)
    criterion = await evaluate_vat_wage_criterion(
        session, year=resolved, threshold=threshold
    )
    return _vat_read(criterion)


@router.post(
    "/vat-criterion/threshold",
    response_model=VatWageCriterionRead,
    dependencies=TAXES_MANAGE,
)
async def set_vat_threshold(
    session: Annotated[AsyncSession, Depends(get_session)],
    body: VatThresholdInput,
) -> VatWageCriterionRead:
    """Ввести региональный порог за год (Росстат/ЕМИСС) и пересчитать критерий."""
    if body.amount <= ZERO:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Порог должен быть больше нуля."
        )
    await save_regional_wage(session, year=body.year, amount=body.amount)
    await session.commit()
    threshold = await load_regional_wage(session, year=body.year)
    criterion = await evaluate_vat_wage_criterion(
        session, year=body.year, threshold=threshold
    )
    return _vat_read(criterion)


def _tax_bank_rejected(exc: BankFetchError) -> HTTPException:
    """Отказ банка при создании черновика → осмысленный HTTP вместо голой 500."""
    if isinstance(exc, BankCredentialsError):
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Ошибка авторизации в банке — обратитесь к администратору",
        )
    if exc.status_code is not None and 400 <= exc.status_code < 500:
        message = "Банк отклонил платёж по реквизитам получателя"
        if exc.detail:
            message = f"{message}: {exc.detail}"
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=message)
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="Банк временно недоступен — попробуйте позже",
    )


@router.post("/bank-draft", response_model=TaxBankDraftResultRead, dependencies=TAXES_MANAGE)
async def create_bank_draft(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    body: TaxBankDraftInput,
) -> TaxBankDraftResultRead:
    """Создать черновик платёжки ЕНП в Т-Банк. Черновик ≠ оплата — подтвердить в банк-клиенте.

    Получатель — фиксированные реквизиты ФНС; сумму подтверждает владелец. Отказ банка по
    реквизитам маппится в 422 с причиной, а не в голую 500.
    """
    if body.amount <= ZERO:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Сумма платежа должна быть больше нуля."
        )
    try:
        result = await create_tax_bank_draft(
            session, settings=settings, amount=body.amount, purpose=body.purpose
        )
    except TaxDraftError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except BankFetchError as exc:
        raise _tax_bank_rejected(exc) from exc

    return TaxBankDraftResultRead(
        document_id=result.document_id,
        status=result.status,
        provider_ref=result.provider_ref,
    )


@router.post(
    "/payment-drafts",
    response_model=TaxPaymentDraftRead,
    dependencies=TAXES_MANAGE,
)
async def create_payment_draft(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    body: TaxPaymentDraftInput,
) -> TaxPaymentDraftRead:
    """Подготовить налоговый платёж в очередь «Активные платежи».

    Кнопка «Отправить в банк» на «Налогах» больше не стреляет платёжкой напрямую и вслепую —
    она создаёт видимую строку: платёж появляется в окне активных платежей, где владелец
    сверяет сумму и назначение и уже оттуда отправляет в банк. Повторный клик по тому же
    обязательству не плодит дубликат — обновляет подготовленный платёж.
    """
    if body.amount <= ZERO:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Сумма платежа должна быть больше нуля."
        )
    try:
        draft = await create_tax_payment_draft(
            session,
            tax_kind=body.tax_kind,
            amount=body.amount,
            purpose=body.purpose,
            for_year=body.for_year,
            for_period=body.for_period,
            due_date=body.due_date,
            title=body.title,
            created_by=actor.user_id,
        )
    except TaxDraftError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    await session.commit()
    await session.refresh(draft)
    return TaxPaymentDraftRead.model_validate(draft)


@router.post(
    "/payment-drafts/{draft_id}/send-to-bank",
    response_model=TaxPaymentDraftRead,
    dependencies=TAXES_MANAGE,
)
async def send_payment_draft_to_bank(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    draft_id: uuid.UUID,
    body: TaxDraftSendInput,
) -> TaxPaymentDraftRead:
    """Отправить подготовленный налоговый платёж в банк (черновик платёжки ЕНП в Т-Банк).

    Сумму/назначение можно поправить перед отправкой; реквизиты получателя ФНС фиксированы.
    Отказ банка оставляет платёж в очереди со статусом ``failed`` — его можно отправить снова.
    """
    draft = await session.get(TaxBankDraft, draft_id)
    if draft is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Платёж не найден.")
    try:
        draft = await send_tax_draft_to_bank(
            session, draft, settings=settings, amount=body.amount, purpose=body.purpose
        )
    except TaxDraftError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except BankFetchError as exc:
        # Статус 'failed' и текст ошибки уже проставлены сервисом — сохраняем их до маппинга.
        await session.commit()
        raise _tax_bank_rejected(exc) from exc
    await session.commit()
    await session.refresh(draft)
    return TaxPaymentDraftRead.model_validate(draft)


@router.post(
    "/payment-drafts/{draft_id}/cancel",
    response_model=TaxPaymentDraftRead,
    dependencies=TAXES_MANAGE,
)
async def cancel_payment_draft(
    session: Annotated[AsyncSession, Depends(get_session)],
    draft_id: uuid.UUID,
) -> TaxPaymentDraftRead:
    """Убрать подготовленный налоговый платёж из очереди активных платежей."""
    draft = await session.get(TaxBankDraft, draft_id)
    if draft is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Платёж не найден.")
    try:
        draft = await cancel_tax_draft(session, draft)
    except TaxDraftError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    await session.commit()
    await session.refresh(draft)
    return TaxPaymentDraftRead.model_validate(draft)
