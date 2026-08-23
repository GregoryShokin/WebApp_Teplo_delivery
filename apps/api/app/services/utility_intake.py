"""Фотография коммунальной платёжки → строки «Страницы на оплату».

ПОЧЕМУ ЗДЕСЬ, А НЕ НА СВОЁМ ЭКРАНЕ. Первая версия завела коммуналке отдельную страницу со своим
хранилищем — и это была ошибка (решение владельца 02.08.2026). Документ становился виден только
тому, кто знает про специальный экран, а заплатить по нему из очереди оплат было нечем: счёт
живёт там, где живут все остальные основания платежей. Поэтому снимок квитанции приходит в тот же
журнал ``email_invoice_intake``, что и счета из почты и ЭДО, — третьим источником, с
``mailbox='photo'``.

ОДИН ДОКУМЕНТ — ОДНА СТРОКА, ДАЖЕ ЕСЛИ ФАЙЛ ОДИН. Энергетик привозит за визит два акта:
фактический за прошедший месяц и авансовый на следующий. Пришли они двумя снимками или одним —
это два разных документа с разными периодами и разными ролями, и в очереди оплат они обязаны быть
двумя строками: у них не совпадает ни период, ни смысл (факт признаёт расход, аванс только
платится). Слить их в одну строку значило бы показать сумму, которой нет ни в одной бумаге.
Ключ идемпотентности для второй и следующих строк выводится из хэша файла (``_document_sha``) —
колонка остаётся честным 64-символьным дайджестом, а повторная загрузка того же файла упирается
в те же ключи.

РЕКВИЗИТЫ ИЗ КВИТАНЦИИ В ПЛАТЁЖ НЕ ИДУТ. В бумаге стоят реквизиты ресурсника (Водоканал,
энергосбыт), а платим мы не ему: договор заключал арендодатель, и деньги уходят ему в возмещение.
Кому платить, на какую статью и в какое помещение относить расход — знает только поток
(``UtilityAccount``), поэтому контрагент берётся оттуда, а распознанные реквизиты остаются в
приёмке справкой. Подставить их в платёж значило бы отправить деньги постороннему юрлицу.

ЧЕЛОВЕК РЕШАЕТ ПЛАТИТЬ, А НЕ ПЕРЕНАБИРАТЬ. Разбор, прошедший проверку, сразу заводит документы:
суммы у парсеров детерминированы и закрыты golden-тестами на настоящих актах. Метки вида «ждём
парный акт» из ``owner_review_reasons`` едут наружу как ПОДСКАЗКА, а не как блокировка: деньги
всё равно не уходят сами — отправку в банк нажимает человек. На ручной разбор строка уходит
только тогда, когда действовать нечем: не собралась сумма, не определился период, не найден
поток.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import (
    UTILITY_KIND_LABELS,
    Counterparty,
    EmailInvoiceIntake,
    UtilityAccount,
)
from app.services import utility_charges, utility_ocr
from app.services.document_uploads import MAX_UPLOAD_BYTES, sniff_mime
from app.services.utility_recognition import (
    LOW_CONFIDENCE_THRESHOLD,
    UtilityRecognition,
    recognize_utility_documents,
)

logger = logging.getLogger(__name__)

__all__ = [
    "PHOTO_MAILBOX",
    "UtilityIntakeError",
    "confirm_utility_intake",
    "ingest_document",
]

# Откуда пришла бумага. Поле ``mailbox`` изначально означало почтовый ящик, но давно им не
# ограничено: СБИС кладёт сюда 'sbis'. Фотография — такой же источник, и своей колонки не заводит.
PHOTO_MAILBOX = "photo"

# Расшифровки машинных причин: их видит человек в списке и в окне разбора. Причина без
# расшифровки покажется как есть — это лучше, чем молчание, и сразу видно, что забыли перевести.
REVIEW_REASON_LABELS: dict[str, str] = {
    "electricity_pair_waiting_for_advance": "Есть только акт за факт — ждём авансовый",
    "electricity_pair_waiting_for_actual": "Есть только авансовый акт — ждём акт за факт",
    "electricity_act_kind_not_detected": "Не понятно, факт это или аванс",
    "electricity_amount_due_not_found": "Не нашли сумму к оплате",
    "electricity_period_amount_not_found": "Не нашли сумму расхода за период",
    "electricity_advance_amount_not_found": "Не нашли сумму аванса",
    "amount_not_found": "Не нашли сумму",
    "service_period_not_found": "Не нашли период оказания услуги",
    "utility_account_missing": "Не заведён поток для этого ресурса",
    "utility_account_ambiguous": "Потоков этого ресурса несколько — выберите нужный",
    "utility_account_inactive": "Поток отключён",
    "utility_payable_not_positive": "Сумма к оплате нулевая или отрицательная",
    "low_confidence": "Разбор неуверенный — проверьте суммы",
    "not_a_utility_document": "Не похоже на коммунальную платёжку",
    "ocr_failed": "Не удалось прочитать текст со снимка",
}

_ELECTRICITY_PAIR_REASON_CODES = frozenset(
    {"electricity_pair_waiting_for_advance", "electricity_pair_waiting_for_actual"}
)
_ELECTRICITY_PAIR_HINTS = frozenset(
    REVIEW_REASON_LABELS[reason] for reason in _ELECTRICITY_PAIR_REASON_CODES
)


class UtilityIntakeError(RuntimeError):
    """Файл принять нельзя. Текст показывается человеку как есть."""


def _document_sha(base_sha: str, index: int) -> str:
    """Ключ идемпотентности документа внутри файла.

    Первый документ живёт под хэшем самого файла — так повторная загрузка того же снимка
    упирается в обычную проверку дубля. Для второго и следующих ключ выводится из первого:
    колонка остаётся 64-символьным дайджестом (её ширина ровно под это), значение
    детерминировано, и повторная загрузка файла даёт те же ключи, а не новые строки.
    """
    if index == 0:
        return base_sha
    return hashlib.sha256(f"{base_sha}#{index}".encode()).hexdigest()


def _decimal_or_none(raw: object) -> Decimal | None:
    if raw in (None, ""):
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    return value


def _reason_labels(reasons: list[str]) -> list[str]:
    return [REVIEW_REASON_LABELS.get(reason, reason) for reason in reasons]


def _electricity_pair_fields(intake: EmailInvoiceIntake) -> dict[str, Any] | None:
    """Поля, по которым два снимка можно без догадки назвать одной парой актов."""
    recognition = intake.recognition or {}
    utility = recognition.get("utility") or {}
    if (
        intake.status != "linked"
        or intake.utility_account_id is None
        or utility.get("kind") != "electricity"
        or utility.get("act_kind") not in ("actual", "advance")
        or utility.get("paired_intake_id")
    ):
        return None
    try:
        period_start = date.fromisoformat(str(recognition.get("service_period_start") or ""))
        period_end = date.fromisoformat(str(recognition.get("service_period_end") or ""))
    except ValueError:
        return None
    document_date = str(utility.get("document_date") or "")
    payable_amount = _decimal_or_none(utility.get("payable_amount"))
    if not document_date or payable_amount is None or payable_amount <= 0:
        return None
    return {
        "act_kind": str(utility["act_kind"]),
        "document_date": document_date,
        "period_start": period_start,
        "period_end": period_end,
        "payable_amount": payable_amount,
    }


def _is_electricity_pair(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Факт прошлого месяца + аванс следующего, выписанные в один день."""
    if left["act_kind"] == right["act_kind"]:
        return False
    actual = left if left["act_kind"] == "actual" else right
    advance = right if right["act_kind"] == "advance" else left
    return bool(
        actual["document_date"] == advance["document_date"]
        and actual["period_end"] + date.resolution == advance["period_start"]
    )


def _mark_electricity_pair(
    intake: EmailInvoiceIntake,
    paired: EmailInvoiceIntake,
    *,
    total: Decimal,
) -> None:
    recognition = dict(intake.recognition or {})
    utility = dict(recognition.get("utility") or {})
    hints = [hint for hint in utility.get("hints") or [] if hint not in _ELECTRICITY_PAIR_HINTS]
    pair_hint = f"Пара собрана — общий платёж {total.quantize(Decimal('0.01'))} ₽"
    if pair_hint not in hints:
        hints.append(pair_hint)
    if utility.get("act_kind") == "advance":
        lifecycle_hint = "Расход по этому авансу признает акт за факт за тот же месяц"
        if lifecycle_hint not in hints:
            hints.append(lifecycle_hint)
    utility.update(
        {
            "hints": hints,
            "raw_reasons": [
                reason
                for reason in utility.get("raw_reasons") or []
                if reason not in _ELECTRICITY_PAIR_REASON_CODES
            ],
            "paired_intake_id": str(paired.id),
            "pair_payable_amount": str(total.quantize(Decimal("0.01"))),
            "pair_status": "ready",
        }
    )
    recognition["utility"] = utility
    intake.recognition = recognition


async def _pair_electricity_intakes(
    session: AsyncSession,
    created: list[EmailInvoiceIntake],
) -> None:
    """Связать два последовательных фото одного визита, не смешивая месяцы и отправителей.

    Совпадение по одному контрагенту недостаточно: у арендодателя одновременно живут аренда,
    вода и электричество. Даже одного потока мало — июньский аванс и июльский факт относятся к
    одному месяцу, но это документы РАЗНЫХ визитов. Поэтому пара считается доказанной только
    при четырёх признаках: один поток, один отправитель, одна дата актов и соседние периоды
    «факт → аванс». Если кандидатов несколько, не угадываем и оставляем обе строки раздельными.
    """
    for intake in created:
        fields = _electricity_pair_fields(intake)
        if fields is None:
            continue
        sender_filter = (
            EmailInvoiceIntake.from_addr.is_(None)
            if intake.from_addr is None
            else EmailInvoiceIntake.from_addr == intake.from_addr
        )
        candidates = (
            await session.scalars(
                select(EmailInvoiceIntake)
                .where(
                    EmailInvoiceIntake.id != intake.id,
                    EmailInvoiceIntake.mailbox == PHOTO_MAILBOX,
                    EmailInvoiceIntake.status == "linked",
                    EmailInvoiceIntake.utility_account_id == intake.utility_account_id,
                    sender_filter,
                )
                .order_by(EmailInvoiceIntake.created_at.desc())
                .limit(24)
            )
        ).all()
        matches: list[tuple[EmailInvoiceIntake, dict[str, Any]]] = []
        for candidate in candidates:
            candidate_fields = _electricity_pair_fields(candidate)
            if candidate_fields is not None and _is_electricity_pair(fields, candidate_fields):
                matches.append((candidate, candidate_fields))
        if len(matches) != 1:
            continue
        paired, paired_fields = matches[0]
        total = fields["payable_amount"] + paired_fields["payable_amount"]
        _mark_electricity_pair(intake, paired, total=total)
        _mark_electricity_pair(paired, intake, total=total)
        await session.flush()


def _split_amounts(
    document: UtilityRecognition,
) -> tuple[Decimal | None, Decimal | None, str | None]:
    """Разложить документ на «расход периода» и «к оплате». Третий элемент — причина отказа.

    У воды и газа это одно и то же число: счёт за месяц и есть расход месяца. У электричества
    они расходятся, и это не тонкость учёта, а содержание бумаги: в фактическом акте из
    потребления с потерями вычтен ранее внесённый аванс, поэтому расход июня 95 402 ₽, а
    доплатить надо 30 402 ₽. Авансовый акт расхода не несёт вовсе — его признает будущий факт.
    """
    raw = document.raw or {}
    if document.kind == "electricity":
        act_kind = str(raw.get("electricity_act_kind") or "")
        if act_kind == "advance":
            if document.amount is None:
                return None, None, "electricity_advance_amount_not_found"
            return None, document.amount, None
        if act_kind != "actual":
            return None, None, "electricity_act_kind_not_detected"
        expense = _decimal_or_none(raw.get("electricity_period_amount"))
        if expense is None:
            return None, None, "electricity_period_amount_not_found"
        if document.amount is None:
            return None, None, "electricity_amount_due_not_found"
        return expense, document.amount, None
    if document.amount is None:
        return None, None, "amount_not_found"
    return document.amount, document.amount, None


async def _resolve_account(
    session: AsyncSession,
    *,
    kind: str,
    explicit_id: uuid.UUID | None,
) -> tuple[UtilityAccount | None, str | None]:
    """Найти поток, к которому относится документ. Вторым элементом — причина, если не вышло.

    Выбор человека сильнее распознанного: он может грузить квитанцию второй точки, где ресурс
    тот же. Без явного выбора поток определяется по ресурсу — и только если такой ровно один.
    Гадать при нескольких нельзя: ошибка отправит деньги другому арендодателю и посадит расход
    на чужое помещение.
    """
    if explicit_id is not None:
        account = await session.get(UtilityAccount, explicit_id)
        if account is None:
            raise UtilityIntakeError("Коммунальный поток не найден")
        if not account.is_active:
            return None, "utility_account_inactive"
        return account, None
    accounts = (
        await session.scalars(
            select(UtilityAccount).where(
                UtilityAccount.kind == kind,
                UtilityAccount.is_active.is_(True),
            )
        )
    ).all()
    if not accounts:
        return None, "utility_account_missing"
    if len(accounts) > 1:
        return None, "utility_account_ambiguous"
    return accounts[0], None


def _recognition_payload(
    document: UtilityRecognition,
    *,
    ocr_method: str,
    text: str | None,
    expense_amount: Decimal | None,
    payable_amount: Decimal | None,
    account: UtilityAccount | None,
    counterparty: Counterparty | None,
    title: str | None,
    blocking_reasons: list[str],
) -> dict[str, Any]:
    """Собрать ``recognition`` так, чтобы существующий экран прочитал его без исключений.

    Плоские ключи (``amount``, ``invoice_number``, периоды) — контракт «Страницы на оплату», их
    читает список и окно разбора. Коммунальная специфика лежит отдельным блоком ``utility``:
    расход и сумма к оплате у электричества разные, и показать надо оба числа, а не одно.

    ``recipient_name`` — тот, КОМУ уходят деньги (арендодатель из потока), а не тот, кто
    напечатан в квитанции. Реквизиты ресурсника остаются в ``utility.source_org`` справкой: они
    настоящие, но платить по ним нельзя — договор с Водоканалом заключал не бизнес.
    """
    raw = document.raw or {}
    return {
        "amount": str(payable_amount) if payable_amount is not None else None,
        "invoice_number": title,
        "invoice_date": document.document_date.isoformat() if document.document_date else None,
        "service_period_start": (
            document.period_start.isoformat() if document.period_start else None
        ),
        "service_period_end": document.period_end.isoformat() if document.period_end else None,
        "service_period_source": utility_charges.UTILITY_INVOICE_SOURCE,
        "recipient_name": counterparty.name if counterparty is not None else None,
        "inn": counterparty.inn if counterparty is not None else None,
        # Пусто намеренно: банковские реквизиты платежа берутся из карточки контрагента, а не
        # из бумаги ресурсника. Пустой словарь — то, что ждёт экран, когда счёт их не дал.
        "requisites": {},
        "utility": {
            "kind": document.kind,
            "kind_label": UTILITY_KIND_LABELS.get(document.kind, document.kind),
            "act_kind": raw.get("electricity_act_kind") or None,
            "account_id": str(account.id) if account is not None else None,
            "expense_amount": str(expense_amount) if expense_amount is not None else None,
            "payable_amount": str(payable_amount) if payable_amount is not None else None,
            "period_label": raw.get("service_period_label") or None,
            "document_number": document.document_number,
            "document_date": (
                document.document_date.isoformat() if document.document_date else None
            ),
            "source_org": {
                "name": raw.get("counterparty_name") or None,
                "inn": raw.get("inn") or None,
            },
            "energy_amount": raw.get("electricity_energy_amount") or None,
            "losses_amount": raw.get("electricity_losses_amount") or None,
            "paid_advance_amount": raw.get("electricity_paid_advance_amount") or None,
            "ocr_method": ocr_method,
            "confidence": document.confidence,
            # Подсказки («ждём парный акт») и причины отказа лежат врозь: первые информируют,
            # вторые объясняют, почему строка ждёт рук. Смешать их — значит перестать различать
            # «всё готово, но знай» и «без тебя не проведём».
            "hints": _reason_labels(
                [
                    reason
                    for reason in document.reasons
                    if reason in REVIEW_REASON_LABELS and reason not in blocking_reasons
                ]
            ),
            "blocking": _reason_labels(blocking_reasons),
            "raw_reasons": list(document.reasons),
        },
        "raw_text": text,
    }


def _new_intake(
    *,
    sha: str,
    filename: str | None,
    mime: str,
    content: bytes,
    ocr_method: str,
    confidence: float | None,
    source_label: str | None = None,
) -> EmailInvoiceIntake:
    return EmailInvoiceIntake(
        mailbox=PHOTO_MAILBOX,
        # «От кого» — та же колонка, в которой у почтовых строк стоит адрес отправителя, и тот
        # же столбец на экране. У бота белого списка нет (решение владельца 02.08.2026), и
        # отправитель — единственное, что отвечает на вопрос «чей это документ»: у документа с
        # чужой суммой должно быть имя, иначе разбираться будет не с кем.
        from_addr=source_label,
        attachment_filename=filename,
        attachment_mime=mime,
        attachment_sha256=sha,
        attachment_size=len(content),
        pdf_bytes=content,
        status="new",
        engine=f"utility:{ocr_method}"[:24],
        confidence=(
            Decimal(str(round(max(0.0, min(1.0, confidence)), 3)))
            if confidence is not None
            else None
        ),
        recognition={},
    )


async def _materialize(
    session: AsyncSession,
    intake: EmailInvoiceIntake,
    *,
    account: UtilityAccount,
    period_start: date,
    period_end: date,
    expense_amount: Decimal | None,
    payable_amount: Decimal,
    actor_user_id: uuid.UUID | None,
) -> None:
    """Завести по разобранному документу счёт и (если он несёт расход) закрывающий.

    Статусы приёмки те же, что у почты, и означают то же самое: ``linked`` — счёт в очереди
    оплат, ``duplicate`` — ту же бумагу принесли второй раз, ``needs_review`` — провести нечем.
    """
    try:
        bill, closing = await utility_charges.build_utility_documents(
            session,
            account,
            period_start=period_start,
            period_end=period_end,
            expense_amount=expense_amount,
            payable_amount=payable_amount,
            actor_user_id=actor_user_id,
        )
    except utility_charges.UtilityMonthTakenError as exc:
        # Пересняли ту же квитанцию: байты другие, дедуп по файлу не сработал, а долг тот же.
        # Ведём себя как с дублем письма — привязываем к существующему документу и уводим из
        # очереди оплат, чтобы месяц не всплыл к оплате дважды.
        intake.invoice_id = exc.invoice.id
        intake.status = "duplicate"
        intake.error = str(exc)[:1000]
        return
    except utility_charges.UtilityChargeError as exc:
        intake.status = "needs_review"
        intake.error = str(exc)[:1000]
        return

    intake.invoice_id = bill.id
    intake.companion_invoice_id = closing.id if closing is not None else None
    intake.status = "linked"
    intake.error = None


async def ingest_document(
    session: AsyncSession,
    *,
    content: bytes,
    filename: str | None,
    settings: Settings,
    account_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    source_label: str | None = None,
) -> list[EmailInvoiceIntake]:
    """Принять файл и завести по нему строки «Страницы на оплату». Не коммитит.

    Возвращает созданные строки — по одной на документ в файле. Отказ приёма (формат, размер,
    повтор файла) — ``UtilityIntakeError``: это разговор с человеком, а не сбой обработки.
    Неудача разбора отказом НЕ является: строка создаётся всегда, со снимком внутри, — иначе
    принесённый документ исчезал бы бесследно, и о нём вспоминали бы по звонку арендодателя.
    """
    if not content:
        raise UtilityIntakeError("Файл пустой")
    if len(content) > MAX_UPLOAD_BYTES:
        raise UtilityIntakeError(
            f"Файл больше {MAX_UPLOAD_BYTES // (1024 * 1024)} МБ — сожмите снимок"
        )
    mime = sniff_mime(content)
    if mime is None:
        raise UtilityIntakeError(
            "Такой формат не принимаем. Подойдёт фотография (JPEG, PNG, HEIC) или PDF"
        )

    base_sha = hashlib.sha256(content).hexdigest()
    existing = await session.scalar(
        select(EmailInvoiceIntake).where(EmailInvoiceIntake.attachment_sha256 == base_sha)
    )
    if existing is not None:
        raise UtilityIntakeError("Этот файл уже загружали — строка по нему есть в списке")

    text, ocr_method = await utility_ocr.extract_text(content, mime=mime, settings=settings)
    documents = recognize_utility_documents(text) if text else []

    if not documents:
        # Прочитать не смогли или это не коммунальная бумага. Строку всё равно заводим: человек
        # видит снимок, вводит поля руками и подтверждает — обычным путём «Страницы на оплату».
        reason = "ocr_failed" if not text else "not_a_utility_document"
        intake = _new_intake(
            sha=base_sha,
            filename=filename,
            mime=mime,
            content=content,
            ocr_method=ocr_method,
            confidence=None,
            source_label=source_label,
        )
        intake.status = "needs_review"
        intake.recognition = {
            "requisites": {},
            "utility": {
                "ocr_method": ocr_method,
                "hints": [],
                "blocking": _reason_labels([reason]),
                "raw_reasons": [reason],
            },
            "raw_text": text,
        }
        session.add(intake)
        await session.flush()
        return [intake]

    created: list[EmailInvoiceIntake] = []
    for index, document in enumerate(documents):
        sha = _document_sha(base_sha, index)
        if await session.scalar(
            select(EmailInvoiceIntake.id).where(EmailInvoiceIntake.attachment_sha256 == sha)
        ):
            continue
        intake = _new_intake(
            sha=sha,
            filename=filename,
            mime=mime,
            content=content,
            ocr_method=ocr_method,
            confidence=document.confidence,
            source_label=source_label,
        )
        session.add(intake)
        await session.flush()

        expense_amount, payable_amount, amount_reason = _split_amounts(document)
        account, account_reason = await _resolve_account(
            session, kind=document.kind, explicit_id=account_id
        )
        period_start, period_end = document.period_start, document.period_end
        blocking = [reason for reason in (amount_reason, account_reason) if reason]
        if period_start is None or period_end is None:
            blocking.append("service_period_not_found")
        if payable_amount is not None and payable_amount <= 0:
            blocking.append("utility_payable_not_positive")
        if document.confidence < LOW_CONFIDENCE_THRESHOLD:
            blocking.append("low_confidence")

        counterparty = (
            await session.get(Counterparty, account.counterparty_id)
            if account is not None
            else None
        )
        title = (
            utility_charges.invoice_title(document.kind, document.period_end.replace(day=1))
            if document.period_end is not None
            else None
        )
        intake.utility_account_id = account.id if account is not None else None
        intake.counterparty_id = account.counterparty_id if account is not None else None
        intake.recognition = _recognition_payload(
            document,
            ocr_method=ocr_method,
            text=text,
            expense_amount=expense_amount,
            payable_amount=payable_amount,
            account=account,
            counterparty=counterparty,
            title=title,
            blocking_reasons=blocking,
        )

        if (
            blocking
            or account is None
            or payable_amount is None
            or period_start is None
            or period_end is None
        ):
            intake.status = "needs_review"
        else:
            await _materialize(
                session,
                intake,
                account=account,
                period_start=period_start,
                period_end=period_end,
                expense_amount=expense_amount,
                payable_amount=payable_amount,
                actor_user_id=actor_user_id,
            )
        await session.flush()
        created.append(intake)

    if not created:
        raise UtilityIntakeError("Этот файл уже загружали — строка по нему есть в списке")
    await _pair_electricity_intakes(session, created)
    return created


async def confirm_utility_intake(
    session: AsyncSession,
    intake: EmailInvoiceIntake,
    *,
    account_id: uuid.UUID,
    period_start: date,
    period_end: date,
    expense_amount: Decimal | None,
    payable_amount: Decimal,
    actor_user_id: uuid.UUID | None = None,
) -> str:
    """Ручное проведение строки, которую разбор не осилил. Не коммитит.

    Отдельная дверь рядом с почтовым ``confirm_intake_with_review``, потому что вопросы у
    коммуналки другие: там оператор правит контрагента и реквизиты, здесь — поток, период и ДВЕ
    суммы (расход месяца и сумму к оплате, у электричества они не совпадают). Пустой
    ``expense_amount`` означает авансовый документ: он платится, но расхода не несёт.
    """
    if intake.status not in ("new", "needs_review", "failed"):
        raise UtilityIntakeError("Эта строка уже проведена")
    account = await session.get(UtilityAccount, account_id)
    if account is None:
        raise UtilityIntakeError("Коммунальный поток не найден")
    if payable_amount <= 0:
        raise UtilityIntakeError("Сумма к оплате должна быть больше нуля")

    counterparty = await session.get(Counterparty, account.counterparty_id)
    title = utility_charges.invoice_title(account.kind, period_end.replace(day=1))
    recognition = dict(intake.recognition or {})
    utility_block = dict(recognition.get("utility") or {})
    utility_block.update(
        {
            "kind": account.kind,
            "kind_label": UTILITY_KIND_LABELS.get(account.kind, account.kind),
            "account_id": str(account.id),
            "expense_amount": str(expense_amount) if expense_amount is not None else None,
            "payable_amount": str(payable_amount),
            # Провёл человек — причины отказа больше не актуальны, а подсказки уже прочитаны.
            "blocking": [],
            "confirmed_manually": True,
        }
    )
    recognition.update(
        {
            "amount": str(payable_amount),
            "invoice_number": title,
            "service_period_start": period_start.isoformat(),
            "service_period_end": period_end.isoformat(),
            "service_period_source": utility_charges.UTILITY_INVOICE_SOURCE,
            "recipient_name": counterparty.name if counterparty is not None else None,
            "inn": counterparty.inn if counterparty is not None else None,
            "requisites": recognition.get("requisites") or {},
            "utility": utility_block,
        }
    )
    intake.recognition = recognition
    intake.utility_account_id = account.id
    intake.counterparty_id = account.counterparty_id

    await _materialize(
        session,
        intake,
        account=account,
        period_start=period_start,
        period_end=period_end,
        expense_amount=expense_amount,
        payable_amount=payable_amount,
        actor_user_id=actor_user_id,
    )
    if intake.status == "needs_review" and intake.error:
        # Ручное проведение упёрлось в доменный отказ (период вне срока жизни потока, сумма
        # нулевая): человек ждёт ответа на своё действие, а не молчаливой строки в списке.
        raise UtilityIntakeError(intake.error)
    return intake.status
