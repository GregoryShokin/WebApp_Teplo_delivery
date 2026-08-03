"""API экрана «Страница на оплату» (Фаза 2, MVP).

Журнал разбора счетов из почты (``email_invoice_intake``): список со статусами, превью
исходного PDF, подтверждение оператором (материализация накладной) и игнор. Редактирование
полей и применение реквизитов в профиль — следующий шаг. Права: чтение —
``counterparties.read`` ИЛИ ``finance.counterparties.read``, операции —
``counterparties.operate`` (экран в контуре контрагентов).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    CurrentActor,
    get_current_actor,
    require_any_permission,
    require_permission,
)
from app.core.config import Settings, get_settings
from app.db.session import get_session
from app.models import (
    Counterparty,
    CounterpartyPayableProfile,
    EmailInvoiceIntake,
    SupplierInvoice,
    User,
)
from app.models.email_invoice_intake import EMAIL_INTAKE_STATUSES
from app.services import counterparty_payments as payments
from app.services import document_uploads, utility_images, utility_intake
from app.services import email_invoice_ingest as ingest
from app.services import supplier_service_periods as service_periods
from app.services.banking.exceptions import BankCredentialsError, BankFetchError

router = APIRouter()

# Экран живёт в секции «counterparties», которая открывается ЛЮБЫМ из двух прав чтения
# (permissions.ts: counterparties → counterparties.read | finance.counterparties.read), и
# соседние роуты того же контура (finance_payments, sbis, counterparties) принимают оба.
# Здесь принималось только первое — finance-роль видела пункт меню и получала тихий 403,
# читая его как «счетов нет». Особенно важно после переезда экрана вкладкой на «Платежи».
READ = (Depends(require_any_permission(("counterparties.read", "finance.counterparties.read"))),)
OPERATE = (Depends(require_permission("counterparties.operate")),)


class IntakeRead(BaseModel):
    id: uuid.UUID
    mailbox: str
    from_addr: str | None
    subject: str | None
    received_at: datetime | None
    attachment_filename: str | None
    status: str
    engine: str | None
    confidence: float | None
    counterparty_id: uuid.UUID | None
    counterparty_name: str | None
    invoice_id: uuid.UUID | None
    # Пакет «счёт + УПД» одним файлом: закрывающий документ, заведённый вместе со счётом.
    companion_invoice_id: uuid.UUID | None
    companion_amount: str | None
    # Плоско вынесенные распознанные поля — для таблицы.
    recipient_name: str | None
    inn: str | None
    amount: str | None
    invoice_number: str | None
    invoice_date: str | None
    service_period_start: str | None
    service_period_end: str | None
    service_period_source: str | None
    service_period_status: str | None
    service_period_confidence: float | None
    service_period_required: bool
    # Режим признания услуг контрагента. Решает, спрашивать ли период в момент оплаты:
    # у «счёт + УПД» сумму расхода принесёт документ, и период там знает только оператор
    # услуги (Манго: платим 5 000, а расход по звонкам 372,08) — спрашивать бессмысленно.
    service_billing_mode: str | None = None
    # Распознанные банковские реквизиты (recipientName/inn/kpp/bankAcnt/bankBik/corr) —
    # снимок того, что стояло в самом PDF. Правки оператора сюда не пишутся: по этому полю
    # ищутся кандидаты в истории и сверяется, не сменил ли поставщик банк.
    requisites: dict[str, Any]
    # Правки оператора в окне разбора — приоритетный источник для формы при повторном заходе.
    reviewed_requisites: dict[str, Any]
    # Подтверждены ли реквизиты контрагента (нужно для отправки в банк).
    requisites_verified: bool
    # Состояние связанной накладной: оплачена/частично и заведён ли банк-черновик.
    invoice_payment_status: str | None
    invoice_in_draft: bool
    # Статья ДДС, выбранная для оплаты этого счёта (None → дефолтная «Оплата поставщикам»).
    invoice_dds_article_id: uuid.UUID | None
    # Закреплённая за контрагентом статья ДДС — предзаполняет окно оплаты.
    default_dds_article_id: uuid.UUID | None
    has_pdf: bool
    # Тип вложения: у почты это всегда PDF, у коммунальной платёжки — фотография. Фронту он
    # нужен, чтобы показать снимок картинкой, а не пустым местом в PDF-просмотрщике.
    attachment_mime: str | None
    # Коммунальная платёжка: поток и разложенные суммы. У воды расход и платёж совпадают, у
    # электричества нет — из потребления вычтен внесённый аванс, и показывать надо оба числа.
    utility_account_id: uuid.UUID | None
    utility_kind: str | None
    utility_kind_label: str | None
    # 'actual' — акт за факт (несёт расход), 'advance' — авансовый (только платится).
    utility_act_kind: str | None
    utility_expense_amount: str | None
    utility_payable_amount: str | None
    utility_period_label: str | None
    # Подсказки («есть только акт за факт — ждём авансовый») не мешают платить: они объясняют,
    # чего ждать дальше. Блокировки — то, из-за чего строка ждёт рук человека.
    utility_hints: list[str]
    utility_blocking: list[str]
    # Дата плановой авто-отправки в банк (ISO). None = отправка только вручную.
    scheduled_send_date: str | None
    created_at: datetime


class CounterpartyRequisitesRead(BaseModel):
    """Реквизиты карточки контрагента — фолбэк формы разбора, когда в счёте их не распознали."""

    counterparty_id: uuid.UUID
    name: str
    inn: str | None
    requisites: dict[str, Any]
    requisites_verified: bool


class ReviewRequisites(BaseModel):
    recipientName: str | None = None
    inn: str | None = None
    kpp: str | None = None
    bankAcnt: str | None = None
    bankBik: str | None = None
    recipientCorrAccountNumber: str | None = None


class ConfirmIn(BaseModel):
    # Существующий контрагент (пикер) ИЛИ создание нового по имени+ИНН. Если оба пустые —
    # берётся уже сматченный intake.counterparty_id.
    counterparty_id: uuid.UUID | None = None
    new_counterparty_name: str | None = None
    new_counterparty_inn: str | None = None
    # Правки распознанных полей (если None — оставляем как было).
    amount: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None
    service_period_start: str | None = None
    service_period_end: str | None = None
    requisites: ReviewRequisites | None = None
    # Перенести реквизиты в карточку контрагента и пометить проверенными.
    apply_requisites: bool = False


class ConfirmUtilityIn(BaseModel):
    """Ручное проведение коммунальной платёжки: поток, период и две суммы.

    Сумм именно две, потому что в бумаге их две. У воды они совпадают, у фактического акта
    электроэнергии — нет: расход месяца 95 402 ₽, а доплатить надо 30 402 ₽, потому что аванс
    уже внесён. Пустой ``expense_amount`` — авансовый документ: платится, но расхода не несёт.
    """

    utility_account_id: uuid.UUID
    period_start: date
    period_end: date
    expense_amount: Decimal | None = None
    payable_amount: Decimal


class SendToBankIn(BaseModel):
    # Статья ДДС для оплаты этого счёта (None → дефолтная «Оплата поставщикам» при гашении).
    dds_article_id: uuid.UUID | None = None
    # Закрепить выбранную статью за контрагентом (предзаполнит окно при следующей оплате).
    remember_for_counterparty: bool = False


class SendManyToBankIn(BaseModel):
    """Несколько счетов одним платежом. Статью здесь не спрашиваем: у пачки она уже проставлена
    на каждом счёте (у коммуналки — из потока), а одна общая перетёрла бы разные."""

    intake_ids: list[uuid.UUID]


class ScheduleSendIn(BaseModel):
    # Дата, к которой счёт автоматически уйдёт в банк (джоба отправляет, когда дата наступила).
    send_date: date
    # Статья ДДС / закрепление — как при немедленной отправке (счёт уйдёт позже с этой статьёй).
    dds_article_id: uuid.UUID | None = None
    remember_for_counterparty: bool = False


def _to_read(
    intake: EmailInvoiceIntake,
    counterparty_name: str | None,
    *,
    requisites_verified: bool | None = False,
    invoice_payment_status: str | None = None,
    invoice_draft_id: uuid.UUID | None = None,
    invoice_dds_article_id: uuid.UUID | None = None,
    default_dds_article_id: uuid.UUID | None = None,
    invoice_service_period_start: date | None = None,
    invoice_service_period_end: date | None = None,
    invoice_service_period_source: str | None = None,
    invoice_service_period_status: str | None = None,
    invoice_service_period_confidence: Any | None = None,
    service_period_required: bool | None = False,
    service_billing_mode: str | None = None,
) -> IntakeRead:
    rec: dict[str, Any] = intake.recognition or {}
    utility: dict[str, Any] = rec.get("utility") or {}
    period_start_value = (
        invoice_service_period_start.isoformat()
        if invoice_service_period_start
        else rec.get("service_period_start")
    )
    period_end_value = (
        invoice_service_period_end.isoformat()
        if invoice_service_period_end
        else rec.get("service_period_end")
    )
    period_status_value = invoice_service_period_status
    if period_status_value is None:
        if rec.get("service_period_ambiguous"):
            period_status_value = "ambiguous"
        elif period_start_value and period_end_value:
            period_status_value = "ready"
        else:
            period_status_value = "missing" if service_period_required else "not_required"
    return IntakeRead(
        id=intake.id,
        mailbox=intake.mailbox,
        from_addr=intake.from_addr,
        subject=intake.subject,
        received_at=intake.received_at,
        attachment_filename=intake.attachment_filename,
        status=intake.status,
        engine=intake.engine,
        confidence=float(intake.confidence) if intake.confidence is not None else None,
        counterparty_id=intake.counterparty_id,
        counterparty_name=counterparty_name,
        invoice_id=intake.invoice_id,
        companion_invoice_id=intake.companion_invoice_id,
        companion_amount=(rec.get("companion") or {}).get("amount")
        if isinstance(rec.get("companion"), dict)
        else None,
        recipient_name=rec.get("recipient_name"),
        inn=rec.get("inn"),
        amount=rec.get("amount"),
        invoice_number=rec.get("invoice_number"),
        invoice_date=rec.get("invoice_date"),
        service_period_start=period_start_value,
        service_period_end=period_end_value,
        service_period_source=invoice_service_period_source or rec.get("service_period_source"),
        service_period_status=period_status_value,
        service_period_confidence=(
            float(invoice_service_period_confidence)
            if invoice_service_period_confidence is not None
            else (
                float(rec["service_period_confidence"])
                if rec.get("service_period_confidence") is not None
                else None
            )
        ),
        service_period_required=bool(service_period_required),
        service_billing_mode=service_billing_mode,
        requisites=rec.get("requisites") or {},
        reviewed_requisites=rec.get("requisites_reviewed") or {},
        requisites_verified=bool(requisites_verified),
        invoice_payment_status=invoice_payment_status,
        invoice_in_draft=invoice_draft_id is not None,
        invoice_dds_article_id=invoice_dds_article_id,
        default_dds_article_id=default_dds_article_id,
        has_pdf=intake.pdf_bytes is not None,
        attachment_mime=intake.attachment_mime,
        utility_account_id=intake.utility_account_id,
        utility_kind=utility.get("kind"),
        utility_kind_label=utility.get("kind_label"),
        utility_act_kind=utility.get("act_kind"),
        utility_expense_amount=utility.get("expense_amount"),
        utility_payable_amount=utility.get("payable_amount"),
        utility_period_label=utility.get("period_label"),
        utility_hints=list(utility.get("hints") or []),
        utility_blocking=list(utility.get("blocking") or []),
        scheduled_send_date=(
            intake.scheduled_send_date.isoformat() if intake.scheduled_send_date else None
        ),
        created_at=intake.created_at,
    )


async def _uploader_label(session: AsyncSession, actor: CurrentActor) -> str:
    """«Кто принёс» для строки, загруженной кнопкой.

    У почтовой строки в этом столбце стоит адрес отправителя, у телеграмной — имя приславшего.
    Пустое место у загруженной руками читалось бы как потерянный источник, а вопрос «чей это
    документ» задают именно к строкам с чужими суммами.
    """
    if actor.user_id is None:
        return "Загружено вручную"
    user = await session.get(User, actor.user_id)
    who = (user.full_name or user.email) if user is not None else str(actor.user_id)
    return f"Загрузил: {who}"


async def _get_intake(session: AsyncSession, intake_id: uuid.UUID) -> EmailInvoiceIntake:
    intake = await session.get(EmailInvoiceIntake, intake_id)
    if intake is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Запись не найдена")
    return intake


async def _apply_article_choice(
    session: AsyncSession,
    intake: EmailInvoiceIntake,
    *,
    dds_article_id: uuid.UUID | None,
    remember_for_counterparty: bool,
) -> None:
    """Проставить выбранную статью ДДС на счёт (её берёт гашение ``apply_payment_status``) и
    опционально закрепить за контрагентом — предзаполнит окно при следующей оплате."""
    if dds_article_id is None:
        return
    if intake.invoice_id is not None:
        invoice = await session.get(SupplierInvoice, intake.invoice_id)
        if invoice is not None:
            invoice.dds_article_id = dds_article_id
            await service_periods.sync_invoice_accrual(session, invoice)
    if remember_for_counterparty and intake.counterparty_id is not None:
        profile = await session.scalar(
            select(CounterpartyPayableProfile).where(
                CounterpartyPayableProfile.counterparty_id == intake.counterparty_id
            )
        )
        if profile is None:
            profile = CounterpartyPayableProfile(counterparty_id=intake.counterparty_id)
            session.add(profile)
        profile.default_dds_article_id = dds_article_id
    await session.flush()


async def _load_read(session: AsyncSession, intake_id: uuid.UUID) -> IntakeRead:
    """Перечитать запись с присоединённым контекстом (контрагент, верификация реквизитов, статус
    накладной) — единый ответ для всех мутаций, как в списке."""
    row = (
        await session.execute(
            select(
                EmailInvoiceIntake,
                Counterparty.name,
                CounterpartyPayableProfile.requisites_verified,
                SupplierInvoice.payment_status,
                SupplierInvoice.draft_id,
                SupplierInvoice.dds_article_id,
                CounterpartyPayableProfile.default_dds_article_id,
                SupplierInvoice.service_period_start,
                SupplierInvoice.service_period_end,
                SupplierInvoice.service_period_source,
                SupplierInvoice.service_period_status,
                SupplierInvoice.service_period_confidence,
                CounterpartyPayableProfile.service_period_required,
                CounterpartyPayableProfile.service_billing_mode,
            )
            .outerjoin(Counterparty, Counterparty.id == EmailInvoiceIntake.counterparty_id)
            .outerjoin(
                CounterpartyPayableProfile,
                CounterpartyPayableProfile.counterparty_id == EmailInvoiceIntake.counterparty_id,
            )
            .outerjoin(SupplierInvoice, SupplierInvoice.id == EmailInvoiceIntake.invoice_id)
            .where(EmailInvoiceIntake.id == intake_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Запись не найдена")
    (
        intake,
        cp_name,
        verified,
        pay_status,
        draft_id,
        inv_article,
        default_article,
        period_start,
        period_end,
        period_source,
        period_status,
        period_confidence,
        period_required,
        billing_mode,
    ) = row
    return _to_read(
        intake,
        cp_name,
        requisites_verified=verified,
        invoice_payment_status=pay_status,
        invoice_draft_id=draft_id,
        invoice_dds_article_id=inv_article,
        default_dds_article_id=default_article,
        invoice_service_period_start=period_start,
        invoice_service_period_end=period_end,
        invoice_service_period_source=period_source,
        invoice_service_period_status=period_status,
        invoice_service_period_confidence=period_confidence,
        service_period_required=period_required,
        service_billing_mode=billing_mode,
    )


@router.get("/intakes", response_model=list[IntakeRead], dependencies=READ)
async def list_intakes(
    session: Annotated[AsyncSession, Depends(get_session)],
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[IntakeRead]:
    stmt = (
        select(
            EmailInvoiceIntake,
            Counterparty.name,
            CounterpartyPayableProfile.requisites_verified,
            SupplierInvoice.payment_status,
            SupplierInvoice.draft_id,
            SupplierInvoice.dds_article_id,
            CounterpartyPayableProfile.default_dds_article_id,
            SupplierInvoice.service_period_start,
            SupplierInvoice.service_period_end,
            SupplierInvoice.service_period_source,
            SupplierInvoice.service_period_status,
            SupplierInvoice.service_period_confidence,
            CounterpartyPayableProfile.service_period_required,
            CounterpartyPayableProfile.service_billing_mode,
        )
        .outerjoin(Counterparty, Counterparty.id == EmailInvoiceIntake.counterparty_id)
        .outerjoin(
            CounterpartyPayableProfile,
            CounterpartyPayableProfile.counterparty_id == EmailInvoiceIntake.counterparty_id,
        )
        .outerjoin(SupplierInvoice, SupplierInvoice.id == EmailInvoiceIntake.invoice_id)
        .order_by(EmailInvoiceIntake.created_at.desc())
        .limit(limit)
    )
    if status_filter:
        if status_filter not in EMAIL_INTAKE_STATUSES:
            raise HTTPException(status_code=400, detail="Неизвестный статус")
        stmt = stmt.where(EmailInvoiceIntake.status == status_filter)
    rows = (await session.execute(stmt)).all()
    return [
        _to_read(
            intake,
            cp_name,
            requisites_verified=verified,
            invoice_payment_status=pay_status,
            invoice_draft_id=draft_id,
            invoice_dds_article_id=inv_article,
            default_dds_article_id=default_article,
            invoice_service_period_start=period_start,
            invoice_service_period_end=period_end,
            invoice_service_period_source=period_source,
            invoice_service_period_status=period_status,
            invoice_service_period_confidence=period_confidence,
            service_period_required=period_required,
            service_billing_mode=billing_mode,
        )
        for (
            intake,
            cp_name,
            verified,
            pay_status,
            draft_id,
            inv_article,
            default_article,
            period_start,
            period_end,
            period_source,
            period_status,
            period_confidence,
            period_required,
            billing_mode,
        ) in rows
    ]


@router.get(
    "/counterparties/{counterparty_id}/requisites",
    response_model=CounterpartyRequisitesRead,
    dependencies=READ,
)
async def get_counterparty_requisites(
    counterparty_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CounterpartyRequisitesRead:
    """Реквизиты карточки контрагента для окна разбора счёта.

    Окно предзаполняет форму по цепочке «распознано из PDF → карточка → пусто», а контрагента
    в нём можно переключить — поэтому отдаём по выбранному id, а не только по сматченному.
    Лёгкий ответ вместо полной карточки (``/counterparties/{id}``): списку разбора нужны ровно
    шесть полей платёжки, а карточка тянет агрегаты ДЗ/КЗ и историю документов.
    """
    counterparty = await session.get(Counterparty, counterparty_id)
    if counterparty is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Контрагент не найден")
    profile = await session.scalar(
        select(CounterpartyPayableProfile).where(
            CounterpartyPayableProfile.counterparty_id == counterparty_id
        )
    )
    return CounterpartyRequisitesRead(
        counterparty_id=counterparty.id,
        name=counterparty.name,
        inn=counterparty.inn,
        requisites=(profile.requisites or {}) if profile else {},
        requisites_verified=bool(profile.requisites_verified) if profile else False,
    )


@router.get("/intakes/{intake_id}/pdf", dependencies=READ)
async def get_intake_pdf(
    intake_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Исходный документ строки: PDF из письма или фотография квитанции.

    Тип определяем ПО СОДЕРЖИМОМУ, а не по тому, что написал отправитель. Заявленному верить
    нельзя: 1С-рассылки и часть почтовых шлюзов подписывают PDF как ``application/octet-stream``,
    и с таким типом браузер не рисует документ во фрейме, а скачивает его на диск — окно разбора
    показывало пустой прямоугольник, а счёт уезжал в «Загрузки» (прод, «Назад в будущее», 03.08).
    Раньше здесь стояла константа ``application/pdf`` — она ломала показ снимков; заявленный тип
    исправил снимки, но принёс эту беду. HEIC с айфона конвертируем на лету: в браузерах, кроме
    Safari, он не показывается вовсе.
    """
    intake = await _get_intake(session, intake_id)
    if not intake.pdf_bytes:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Файл недоступен")
    raw = bytes(intake.pdf_bytes)
    content, media_type = utility_images.to_displayable(
        raw,
        utility_images.sniff_media_type(raw, intake.attachment_mime, fallback="application/pdf"),
    )
    # Имя файла кириллическое, а HTTP-заголовки только latin-1 → ASCII-фолбэк + RFC 5987
    # (filename*) с percent-encoding, иначе Starlette роняет ответ («Network Error» в браузере).
    raw_name = intake.attachment_filename or "invoice.pdf"
    ascii_name = raw_name.encode("ascii", "ignore").decode("ascii") or "invoice.pdf"
    disposition = f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(raw_name)}"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": disposition},
    )


@router.post(
    "/intakes/upload",
    response_model=list[IntakeRead],
    status_code=status.HTTP_201_CREATED,
    dependencies=OPERATE,
)
async def upload_intake(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    settings: Annotated[Settings, Depends(get_settings)],
    file: Annotated[UploadFile, File(description="Фотография квитанции или PDF")],
    utility_account_id: Annotated[uuid.UUID | None, Form()] = None,
) -> list[IntakeRead]:
    """Принести документ руками — третий источник «Страницы на оплату» рядом с почтой и ЭДО.

    Отвечает СПИСКОМ: один файл несёт столько документов, сколько в нём есть. Энергетик за
    визит выдаёт два акта — за факт и авансовый, — и это две строки очереди оплат с разными
    периодами, а не одна на общую сумму.

    Читаем с ограничением по объёму, а не целиком: 25 МБ — потолок для фотографии, и класть в
    память заведомо больший файл, чтобы потом его отвергнуть, незачем.
    """
    content = await file.read(document_uploads.MAX_UPLOAD_BYTES + 1)
    try:
        intakes = await utility_intake.ingest_document(
            session,
            content=content,
            filename=file.filename,
            settings=settings,
            account_id=utility_account_id,
            actor_user_id=actor.user_id,
            # «От кого» заполняем и здесь: у строки из почты в этом столбце стоит адрес
            # отправителя, и пустое место у принесённой руками выглядело бы как потерянный
            # источник. Кто загрузил — такой же ответ на вопрос «чей это документ».
            source_label=await _uploader_label(session, actor),
        )
    except utility_intake.UtilityIntakeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    ids = [intake.id for intake in intakes]
    await session.commit()
    return [await _load_read(session, intake_id) for intake_id in ids]


@router.post(
    "/intakes/{intake_id}/confirm-utility", response_model=IntakeRead, dependencies=OPERATE
)
async def confirm_utility(
    intake_id: uuid.UUID,
    body: ConfirmUtilityIn,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> IntakeRead:
    """Провести коммунальную платёжку руками, когда разбор не справился.

    Своя дверь рядом с ``/confirm``, потому что вопросы другие: там оператор правит контрагента
    и реквизиты, здесь — поток, период и ДВЕ суммы. Пустой ``expense_amount`` означает авансовый
    документ: он платится, но расхода не несёт — его признает будущий акт за факт.
    """
    intake = await _get_intake(session, intake_id)
    try:
        await utility_intake.confirm_utility_intake(
            session,
            intake,
            account_id=body.utility_account_id,
            period_start=body.period_start,
            period_end=body.period_end,
            expense_amount=body.expense_amount,
            payable_amount=body.payable_amount,
            actor_user_id=actor.user_id,
        )
    except utility_intake.UtilityIntakeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    return await _load_read(session, intake_id)


@router.get("/intakes/{intake_id}", response_model=IntakeRead, dependencies=READ)
async def get_intake(
    intake_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> IntakeRead:
    return await _load_read(session, intake_id)


@router.post("/intakes/{intake_id}/confirm", response_model=IntakeRead, dependencies=OPERATE)
async def confirm_intake(
    intake_id: uuid.UUID,
    body: ConfirmIn,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> IntakeRead:
    intake = await _get_intake(session, intake_id)
    if intake.status == "ignored":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Запись помечена как не счёт"
        )
    # Признак коммунальной строки — опознанный РЕСУРС, а не сам факт загрузки файлом: снимок
    # обычного счёта поставщика тоже приходит сюда, и его как раз проводят этим путём.
    if ((intake.recognition or {}).get("utility") or {}).get("kind"):
        # Коммунальную платёжку этот путь провёл бы как обычный счёт из письма: контрагентом
        # стал бы ресурсник из бумаги (платим-то мы арендодателю), расход осел бы без помещения,
        # а закрывающий документ не появился бы вовсе — то есть платёж без признания расхода.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Коммунальную платёжку проводят через окно коммунальных услуг",
        )
    try:
        await ingest.confirm_intake_with_review(
            session,
            intake,
            actor_user_id=actor.user_id,
            counterparty_id=body.counterparty_id,
            new_counterparty_name=body.new_counterparty_name,
            new_counterparty_inn=body.new_counterparty_inn,
            amount=body.amount,
            invoice_number=body.invoice_number,
            invoice_date=body.invoice_date,
            service_period_start=body.service_period_start,
            service_period_end=body.service_period_end,
            requisites=body.requisites.model_dump(exclude_none=True) if body.requisites else None,
            apply_requisites=body.apply_requisites,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    return await _load_read(session, intake_id)


@router.post("/intakes/{intake_id}/ignore", response_model=IntakeRead, dependencies=OPERATE)
async def ignore_intake(
    intake_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> IntakeRead:
    intake = await _get_intake(session, intake_id)
    if intake.status == "linked":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="По записи уже создана накладная — игнор недоступен",
        )
    intake.status = "ignored"
    await session.commit()
    await session.refresh(intake)
    return _to_read(intake, None)


@router.post("/intakes/{intake_id}/send-to-bank", response_model=IntakeRead, dependencies=OPERATE)
async def send_to_bank(
    intake_id: uuid.UUID,
    body: SendToBankIn,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> IntakeRead:
    """Отправить подтверждённый счёт в банк — банк-черновик (как у накладных). Деньги не
    списываются: уходит на подтверждение. В dev (mock-режим) реального вызова банка нет."""
    intake = await _get_intake(session, intake_id)
    await _apply_article_choice(
        session,
        intake,
        dds_article_id=body.dds_article_id,
        remember_for_counterparty=body.remember_for_counterparty,
    )
    try:
        await ingest.send_intake_to_bank(session, intake, actor_user_id=actor.user_id)
    except payments.RequisitesNotVerifiedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Реквизиты контрагента не подтверждены — откройте «Разобрать» и подтвердите их",
        ) from exc
    except payments.CounterpartyPaymentError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (BankFetchError, BankCredentialsError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Банк недоступен, попробуйте позже"
        ) from exc
    return await _load_read(session, intake_id)


@router.post("/intakes/send-to-bank", response_model=list[IntakeRead], dependencies=OPERATE)
async def send_many_to_bank(
    body: SendManyToBankIn,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list[IntakeRead]:
    """Отправить несколько счетов ОДНИМ платежом.

    Владелец платит энергетику один перевод на два акта — доплату за прошлый месяц и аванс за
    текущий. Раньше пришлось бы делать два перевода: очередь оплат умела отправлять только по
    одному счёту. Периоды у счетов при этом разные, и это нормально — период живёт на счёте, а
    не на платеже.
    """
    intakes = [await _get_intake(session, intake_id) for intake_id in body.intake_ids]
    try:
        await ingest.send_intakes_to_bank(session, intakes, actor_user_id=actor.user_id)
    except payments.RequisitesNotVerifiedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Реквизиты контрагента не подтверждены — откройте «Разобрать» и подтвердите их",
        ) from exc
    except payments.CounterpartyPaymentError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (BankFetchError, BankCredentialsError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Банк недоступен, попробуйте позже"
        ) from exc
    return [await _load_read(session, intake.id) for intake in intakes]


@router.post("/intakes/{intake_id}/schedule-send", response_model=IntakeRead, dependencies=OPERATE)
async def schedule_send(
    intake_id: uuid.UUID,
    body: ScheduleSendIn,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> IntakeRead:
    """Запланировать авто-отправку счёта в банк к заданной дате (джоба отправит, когда дата
    наступит). Те же условия, что и при ручной отправке: счёт подтверждён, реквизиты проверены,
    ещё не в банке."""
    intake = await _get_intake(session, intake_id)
    if intake.status != "linked" or intake.invoice_id is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Счёт ещё не подтверждён")
    invoice = await session.get(SupplierInvoice, intake.invoice_id)
    if invoice is None or invoice.draft_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Счёт уже отправлен в банк"
        )
    profile = await session.scalar(
        select(CounterpartyPayableProfile).where(
            CounterpartyPayableProfile.counterparty_id == invoice.counterparty_id
        )
    )
    if profile and profile.service_period_required and invoice.service_period_status != "ready":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Для этого контрагента обязателен период оказания услуги",
        )
    verified = await session.scalar(
        select(CounterpartyPayableProfile.requisites_verified).where(
            CounterpartyPayableProfile.counterparty_id == intake.counterparty_id
        )
    )
    if not verified:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Реквизиты контрагента не подтверждены — откройте «Разобрать» и подтвердите их",
        )
    await _apply_article_choice(
        session,
        intake,
        dds_article_id=body.dds_article_id,
        remember_for_counterparty=body.remember_for_counterparty,
    )
    intake.scheduled_send_date = body.send_date
    await session.commit()
    return await _load_read(session, intake_id)


@router.post(
    "/intakes/{intake_id}/cancel-schedule", response_model=IntakeRead, dependencies=OPERATE
)
async def cancel_schedule(
    intake_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> IntakeRead:
    """Отменить плановую авто-отправку (счёт остаётся готовым к ручной отправке)."""
    intake = await _get_intake(session, intake_id)
    intake.scheduled_send_date = None
    await session.commit()
    return await _load_read(session, intake_id)


@router.post("/intakes/{intake_id}/exclude", response_model=IntakeRead, dependencies=OPERATE)
async def exclude_intake(
    intake_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> IntakeRead:
    """Исключить счёт из рабочего инбокса в корзину «Исключённые» (ручное «не платим этот»)."""
    intake = await _get_intake(session, intake_id)
    try:
        await ingest.exclude_intake(session, intake)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    return await _load_read(session, intake_id)


@router.post("/intakes/{intake_id}/restore", response_model=IntakeRead, dependencies=OPERATE)
async def restore_intake(
    intake_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> IntakeRead:
    """Вернуть счёт из «Исключённых» обратно в рабочий инбокс."""
    intake = await _get_intake(session, intake_id)
    try:
        await ingest.restore_intake(session, intake)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    return await _load_read(session, intake_id)


@router.delete("/intakes/{intake_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=OPERATE)
async def delete_intake(
    intake_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Удалить исключённый счёт НАВСЕГДА (вместе с накладной, если она не в банке/оплате)."""
    intake = await _get_intake(session, intake_id)
    try:
        await ingest.delete_intake_forever(session, intake)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
