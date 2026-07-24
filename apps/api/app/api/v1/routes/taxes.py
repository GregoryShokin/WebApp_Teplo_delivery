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

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.api.deps import CurrentActor, get_current_actor, require_any_permission
from app.core.config import Settings, get_settings
from app.db.session import get_session
from app.models.tax import (
    TAX_INTAKE_STATUSES,
    TAX_PAYMENT_KINDS,
    TaxDocumentIntake,
    TaxPayment,
)
from app.schemas.taxes import (
    ReconciliationRead,
    ReconLineRead,
    TaxBankDraftInput,
    TaxBankDraftResultRead,
    TaxCalendarItemRead,
    TaxCalendarRead,
    TaxDocumentListRead,
    TaxDocumentRead,
    TaxDocumentReviewInput,
    TaxOverviewRead,
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
from app.services.taxes.bank_draft import TaxDraftError, create_tax_bank_draft
from app.services.taxes.document_ingest import ingest_tax_documents, set_intake_review
from app.services.taxes.engine import TaxComputationError, TaxState, compute_tax_state
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
    return TaxOverviewRead(
        as_of=moment,
        year=state.year,
        period_code=state.period_code,
        state=TaxStateRead.model_validate(state),
        reconciliation=_reconciliation_read(recon),
        alert_count=len(recon.alerts),
        is_blocked=bool(state.blocking),
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

    items: list[TaxCalendarItemRead] = []
    planned_total = ZERO
    paid_total = ZERO
    overdue_total = ZERO
    overdue_count = 0

    for row in rows:
        is_planned = row.status == "planned"
        # У плановой строки в `paid_on` лежит СРОК уплаты (так её заполняет продвижение
        # документа), у уплаченной — фактическая дата списания. Раскладываем явно.
        due_date = row.paid_on if is_planned else None
        is_overdue = is_planned and row.paid_on < today

        if is_planned:
            planned_total += row.amount
            if is_overdue:
                overdue_total += row.amount
                overdue_count += 1
        else:
            paid_total += row.amount

        item = TaxCalendarItemRead.model_validate(row)
        items.append(item.model_copy(update={"due_date": due_date, "is_overdue": is_overdue}))

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
    payment_status: Annotated[
        str | None,
        Query(alias="status", description="Статус строки: planned | paid | cancelled."),
    ] = None,
) -> TaxPaymentListRead:
    """Реестр платежей в бюджет за год — расшифровка того, из чего сложилась нагрузка.

    Одна строка = одно НАЗНАЧЕНИЕ внутри перевода, а не один перевод. ЕНП уходит единой
    суммой, но внутри и НДФЛ (в вычет не идёт), и взносы за работников (идут) — без
    разложения вычет посчитать нельзя, поэтому реестр ведётся по назначениям.

    ``totals_by_kind`` считается только по УПЛАЧЕННЫМ строкам: это ответ на вопрос
    «сколько реально ушло по каждому виду», плановые обязательства его бы исказили.
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
    if payment_status is not None and payment_status not in ("planned", "paid", "cancelled"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Неизвестный статус платежа: {payment_status!r}. "
                f"Допустимые: planned, paid, cancelled."
            ),
        )

    stmt = select(TaxPayment).where(TaxPayment.for_year == target_year)
    if kind is not None:
        stmt = stmt.where(TaxPayment.kind == kind)
    if payment_status is not None:
        stmt = stmt.where(TaxPayment.status == payment_status)

    rows = (
        await session.scalars(stmt.order_by(TaxPayment.paid_on.desc(), TaxPayment.kind))
    ).all()

    paid_total = ZERO
    planned_total = ZERO
    totals_by_kind: dict[str, Decimal] = {}
    for row in rows:
        if row.status == "paid":
            paid_total += row.amount
            totals_by_kind[row.kind] = totals_by_kind.get(row.kind, ZERO) + row.amount
        elif row.status == "planned":
            planned_total += row.amount

    return TaxPaymentListRead(
        year=target_year,
        items=[TaxPaymentRead.model_validate(row) for row in rows],
        total=len(rows),
        paid_total=paid_total,
        planned_total=planned_total,
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
    stmt = select(TaxDocumentIntake).options(defer(TaxDocumentIntake.content))
    if document_status is not None:
        stmt = stmt.where(TaxDocumentIntake.status == document_status)

    # `id` в сортировке — не украшение: при равных `received_at` (пачка вложений из одного
    # письма приходит одной секундой) порядок без него не определён, и строки под `limit`
    # могут прыгать между запросами.
    rows = (
        await session.scalars(
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

    return TaxDocumentListRead(
        items=[TaxDocumentRead.model_validate(row) for row in rows],
        total=total,
        status_counts=status_counts,
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
    """Забрать новые документы из почты бухгалтера в staging «Документы».

    На проде почта забирается фоново по расписанию; эта кнопка — «проверить прямо сейчас».
    Если почта не настроена (превью), вернётся статус ``not_configured``.
    """
    return await ingest_tax_documents(session)


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
    (``ignored``). Так владелец подтверждает то, что автоматика оставила на проверку."""
    intake = await session.get(TaxDocumentIntake, intake_id)
    if intake is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Документ не найден.")
    try:
        await set_intake_review(session, intake, status=body.status)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    await session.commit()
    return TaxDocumentRead.model_validate(intake)


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
