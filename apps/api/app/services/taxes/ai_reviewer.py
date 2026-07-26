"""ИИ-ревьюер налоговых документов: Claude помогает там, где парсер не справился.

Решение владельца 26.07.2026: две кнопки. «ИИ-разбор» на документе — модель читает файл,
объясняет, что это, и заполняет нераспознанные поля; «ИИ-ревизия» — проход по всем
документам, требующим внимания, плюс общий вердикт по сверке («есть ли несостыковки»).

Принципы (детерминизм главнее магии):

* **Парсер первичен, ИИ — второй слой.** Оборотки и ведомости разбирает детерминированный
  код; ИИ их только объясняет. Правки полей ИИ применяет ТОЛЬКО к платёжкам и ТОЛЬКО
  той же дорогой, что ручная форма проверки (``set_intake_review``) — с той же валидацией.
* **Уверенные правки применяются, неуверенные — объясняются.** Порог ``_APPLY_CONFIDENCE``;
  ниже — документ остаётся владельцу, но с человеческим объяснением ИИ в карточке.
* **Каждый след ИИ помечен**: в ``recognition['ai_review']`` лежат вердикт, уверенность,
  модель и время — в интерфейсе видно, что поле заполнил ИИ, а не человек.
* **Суммы не выдумываются**: модель проинструктирована брать цифры только из документа;
  применение дополнительно требует, чтобы сумма была положительным числом.

Паттерн вызова — как в ``invoice_recognition`` («Страница на оплату», проверен в проде):
структурированный вывод через forced tool_use, graceful-отказ без ключа.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models.tax import TaxDocumentIntake
from app.services.taxes.document_ingest import REVIEW_TAX_KINDS, set_intake_review
from app.services.taxes.document_parser import _docx_text

logger = logging.getLogger(__name__)

# Порог автоприменения правок: ниже — только объясняем, решает владелец.
_APPLY_CONFIDENCE = 0.8
# Статусы, документы в которых ИИ-ревизия разбирает без явного клика по документу.
ATTENTION_STATUSES: tuple[str, ...] = ("needs_review", "error", "unsupported")

_MAX_DOC_CHARS = 20_000  # объёма текста платёжки/оборотки хватает с запасом


class TaxAiError(RuntimeError):
    """Осмысленный отказ ИИ-ревьюера (нет ключа, SDK, ответа) — роут переведёт в 422."""


@dataclass
class AiDocumentReview:
    intake_id: str
    filename: str
    summary: str
    confidence: float
    document_type: str | None
    applied: bool  # правки записаны в recognition и документ переведён в parsed
    needs_human: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class AiAuditFinding:
    severity: str  # 'info' | 'warning' | 'alert'
    title: str
    detail: str


@dataclass
class AiAuditReport:
    verdict: str
    findings: list[AiAuditFinding] = field(default_factory=list)
    documents: list[AiDocumentReview] = field(default_factory=list)


_SYSTEM = (
    "Ты — налоговый ревьюер управленческой системы ИП Шокиной (УСН «Доходы» 6 %, общепит, "
    "Волгодонск). Бухгалтер-аутсорс присылает на почту документы; система разбирает их "
    "автоматически, ты помогаешь с теми, что не распознались, и проверяешь картину целиком.\n\n"
    "Какие документы приходят:\n"
    "— платёжные поручения (docx, форма 0401060) и «Форма ПД (налог)» (xls) — уплата налогов;\n"
    "— сальдо-оборотные ведомости по зарплате «ОБОРОТКА MM.xls» — начисления по сотрудникам;\n"
    "— платёжные ведомости Т-53 «ВЕД-N …xls» — выплаты сотрудникам на руки;\n"
    "— кадровые приказы и трудовые договоры — НЕ платёжные документы, полей не имеют.\n\n"
    "Виды налоговых платежей (tax_kind):\n"
    "— usn_advance — авансовый платёж УСН (платится поквартально);\n"
    "— enp_payroll — зарплатный ЕНП: НДФЛ + взносы за работников ОДНОЙ суммой, срок 28 числа;\n"
    "— contrib_extra_1pct — допвзнос 1 % с дохода свыше 300 000;\n"
    "— contrib_fixed — фиксированные взносы ИП «за себя»;\n"
    "— contrib_injury — взносы на травматизм 0,2 % (платятся в СФР, не в ЕНС);\n"
    "— ndfl, contrib_employees — составные части ЕНП (отдельной платёжкой почти не приходят).\n\n"
    "Периоды (period_hint): q1, h1 (доплата за II квартал), 9m, year — для УСН и взносов; "
    "YYYY-MM — для помесячных.\n\n"
    "Жёсткие правила: суммы и даты бери ТОЛЬКО из текста документа — не вычисляй и не "
    "придумывай; не уверен — оставь поле пустым и подними needs_human. Пиши по-русски, "
    "для владельца-непрофессионала: без кодов, коротко и по делу."
)

_REVIEW_TOOL = {
    "name": "record_review",
    "description": "Записать результат разбора налогового документа.",
    "input_schema": {
        "type": "object",
        "properties": {
            "document_type": {
                "type": "string",
                "enum": ["payment_order", "turnover_statement", "payroll_statement", "other"],
                "description": "Тип документа. Приказы/договоры/письма — other.",
            },
            "tax_kind": {
                "type": "string",
                "enum": list(REVIEW_TAX_KINDS),
                "description": "Вид платежа — только для платёжного поручения.",
            },
            "amount": {"type": "string", "description": "Сумма платежа числом, как в документе."},
            "due_date": {"type": "string", "description": "Срок уплаты, YYYY-MM-DD."},
            "period_hint": {
                "type": "string",
                "description": "Период: q1|h1|9m|year либо YYYY-MM.",
            },
            "summary": {
                "type": "string",
                "description": "1–3 предложения по-русски: что это за документ и что с ним делать.",
            },
            "confidence": {"type": "number", "description": "Уверенность 0..1."},
            "needs_human": {
                "type": "boolean",
                "description": "true — нужен человек (файл нечитаем, противоречия, мало данных).",
            },
            "reasons": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Почему нужен человек — по-русски.",
            },
        },
        "required": ["document_type", "summary", "confidence", "needs_human"],
    },
}

_AUDIT_TOOL = {
    "name": "record_audit",
    "description": "Записать вердикт общей ревизии налогового контура.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "description": "Общий вывод 2–4 предложения по-русски: сходится ли картина.",
            },
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "severity": {"type": "string", "enum": ["info", "warning", "alert"]},
                        "title": {"type": "string", "description": "Короткий заголовок."},
                        "detail": {"type": "string", "description": "Суть и что делать."},
                    },
                    "required": ["severity", "title", "detail"],
                },
            },
        },
        "required": ["verdict", "findings"],
    },
}


async def _call_claude(
    settings: Settings, *, tool: dict, prompt: str, max_tokens: int = 2048
) -> dict:
    """Один структурированный вызов Claude (forced tool_use). Бросает TaxAiError."""
    if not settings.anthropic_api_key:
        raise TaxAiError(
            "ИИ-ревьюер не настроен: добавьте ANTHROPIC_API_KEY в окружение API."
        )
    try:
        from anthropic import AsyncAnthropic
    except Exception as exc:  # noqa: BLE001 - до пересборки образа пакета может не быть
        raise TaxAiError("Пакет anthropic недоступен в этом окружении.") from exc

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    try:
        message = await client.messages.create(
            model=settings.tax_ai_reviewer_model,
            max_tokens=max_tokens,
            system=_SYSTEM,
            tools=[tool],
            tool_choice={"type": "tool", "name": str(tool["name"])},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001 - сеть/лимиты не должны ронять страницу
        logger.warning("ИИ-ревьюер: вызов Claude не удался", exc_info=True)
        raise TaxAiError(f"Не удалось получить ответ модели: {exc}") from exc

    for block in message.content:
        if getattr(block, "type", None) == "tool_use":
            return dict(block.input)  # type: ignore[arg-type]
    raise TaxAiError("Модель не вернула структурированный ответ.")


def _extract_text(intake: TaxDocumentIntake) -> str | None:
    """Best-effort текст документа. None — содержимое недоступно (нет байтов/формат)."""
    data = intake.content
    if not data:
        return None
    name = (intake.filename or "").lower()
    try:
        if name.endswith(".docx"):
            return _docx_text(data)[:_MAX_DOC_CHARS]
        if name.endswith(".xls"):
            import xlrd  # локальный импорт: тяжёлый только для .xls-веток

            book = xlrd.open_workbook(file_contents=data)
            lines: list[str] = []
            for sheet in book.sheets():
                lines.append(f"=== лист «{sheet.name}» ===")
                for r in range(sheet.nrows):
                    cells = [str(sheet.cell_value(r, c)).strip() for c in range(sheet.ncols)]
                    if any(cells):
                        lines.append(" | ".join(cells))
            return "\n".join(lines)[:_MAX_DOC_CHARS]
    except Exception:  # noqa: BLE001 - нечитаемый файл — не повод падать, судим по метаданным
        logger.warning("ИИ-ревьюер: не удалось извлечь текст %s", intake.filename, exc_info=True)
    return None


def _document_prompt(intake: TaxDocumentIntake, text: str | None) -> str:
    parts = [
        "Разбери документ бухгалтера и вызови record_review.",
        f"Имя файла: {intake.filename!r}",
        f"Тема письма: {intake.subject!r}" if intake.subject else "",
        f"Текущий статус в системе: {intake.status}",
    ]
    rec = intake.recognition or {}
    if rec:
        known = {k: rec[k] for k in ("tax_kind", "amount", "due_date", "period_hint") if rec.get(k)}
        if known:
            parts.append(f"Что система уже распознала (может быть неполно/неверно): {known}")
        if rec.get("review_reasons"):
            parts.append(f"Почему система не уверена: {rec['review_reasons']}")
    if text:
        parts.append(f"Содержимое документа:\n---\n{text}\n---")
    else:
        parts.append(
            "Содержимое файла извлечь не удалось (формат не читается) — суди по имени файла "
            "и метаданным; поля в этом случае не заполняй, объясни, что это за документ."
        )
    return "\n".join(p for p in parts if p)


def _valid_amount(raw: object) -> Decimal | None:
    try:
        value = Decimal(str(raw).replace(" ", "").replace(",", "."))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value > 0 else None


async def review_document(
    session: AsyncSession,
    intake: TaxDocumentIntake,
    *,
    settings: Settings | None = None,
    call=_call_claude,
) -> AiDocumentReview:
    """ИИ-разбор одного документа: объяснение всегда, правки — только уверенные платёжки.

    ``call`` инъектируется в тестах (как ``fetch`` в ingest) — реальный API не дёргается.
    """
    settings = settings or get_settings()
    text = _extract_text(intake)
    payload = await call(settings, tool=_REVIEW_TOOL, prompt=_document_prompt(intake, text))

    summary = str(payload.get("summary") or "").strip() or "Модель не дала объяснения."
    try:
        confidence = float(payload.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    needs_human = bool(payload.get("needs_human"))
    reasons = [str(r) for r in payload.get("reasons") or []]
    doc_type = payload.get("document_type")

    applied = False
    if (
        not needs_human
        and confidence >= _APPLY_CONFIDENCE
        and doc_type == "payment_order"
        and intake.status in ATTENTION_STATUSES
        and payload.get("tax_kind") in REVIEW_TAX_KINDS
        and _valid_amount(payload.get("amount")) is not None
    ):
        # Файл, который парсер не смог классифицировать, ИИ опознал платёжкой —
        # выравниваем тип, иначе set_intake_review откажет в overrides.
        if intake.document_type != "payment_order":
            intake.document_type = "payment_order"
        overrides = {
            key: payload.get(key)
            for key in ("tax_kind", "amount", "due_date", "period_hint")
            if payload.get(key)
        }
        await set_intake_review(session, intake, status="parsed", overrides=overrides)
        applied = True

    # След ИИ — всегда, даже без правок: владелец видит объяснение в карточке документа.
    updated = dict(intake.recognition or {})
    updated["ai_review"] = {
        "summary": summary,
        "confidence": round(confidence, 2),
        "document_type": doc_type,
        "needs_human": needs_human,
        "reasons": reasons,
        "applied": applied,
        "model": settings.tax_ai_reviewer_model,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    if applied:
        # Честная маркировка: поля дозаполнил ИИ, а не человек (ручная форма ставит
        # manually_reviewed — здесь поверх пишем, кто проверял на самом деле).
        updated["manually_reviewed"] = False
        updated["reviewed_by"] = "ai"
    intake.recognition = updated
    await session.flush()

    return AiDocumentReview(
        intake_id=str(intake.id),
        filename=intake.filename or "",
        summary=summary,
        confidence=confidence,
        document_type=str(doc_type) if doc_type else None,
        applied=applied,
        needs_human=needs_human,
        reasons=reasons,
    )


def _audit_prompt(snapshot: str) -> str:
    return (
        "Проверь налоговый контур целиком и вызови record_audit. Ниже — снимок системы: "
        "строки сверки (расчёт × документ бухгалтера × факт уплаты из банка), обязательства "
        "«к уплате», расчётный ЕНС-кошелёк и сводка по документам. Найди несостыковки: "
        "расхождения сумм, просроченные или подозрительно отсутствующие платежи, документы, "
        "застрявшие в ошибках. Если картина сходится — так и скажи, не выдумывай проблем. "
        "Учитывай: жёлтое расхождение «документ больше расчёта» безопасно (переплата зачтётся "
        "на ЕНС); зарплатный ЕНП помесячно не совпадает с платёжкой из-за «окон» НДФЛ — это "
        "норма, а не ошибка.\n\n" + snapshot
    )


async def _build_snapshot(session: AsyncSession) -> str:
    """Компактный текстовый снимок контура для общего аудита."""
    from datetime import date

    from app.services.taxes.ens_wallet import compute_ens_wallet
    from app.services.taxes.obligations import list_payable_obligations
    from app.services.taxes.reconcile import build_reconciliation

    today = date.today()
    lines: list[str] = ["## Сверка (расчёт / документ / факт):"]
    recon = await build_reconciliation(session, as_of=today)
    for ln in recon.lines:
        lines.append(
            f"- {ln.label}: расчёт={ln.calculated} документ={ln.documented} "
            f"уплачено={ln.paid} вердикт={ln.verdict}/{ln.severity} срок={ln.due_date}"
        )

    lines.append("\n## Обязательства «к уплате»:")
    for ob in await list_payable_obligations(session, today=today):
        lines.append(f"- {ob.title}: {ob.amount} ₽, срок {ob.due_date}")

    wallet = await compute_ens_wallet(session, as_of=today)
    lines.append(
        f"\n## ЕНС-кошелёк (расчётный): уплачено {wallet.inflow} − признано "
        f"{wallet.recognized} → переплата {wallet.balance} (дефицит фактов {wallet.shortfall})"
    )

    lines.append("\n## Документы по статусам:")
    rows = (
        await session.execute(
            select(TaxDocumentIntake.status, TaxDocumentIntake.filename)
        )
    ).all()
    by_status: dict[str, list[str]] = {}
    for status, filename in rows:
        by_status.setdefault(status, []).append(filename or "?")
    for status, names in sorted(by_status.items()):
        shown = ", ".join(names[:8]) + ("…" if len(names) > 8 else "")
        lines.append(f"- {status}: {len(names)} шт. ({shown})")
    return "\n".join(lines)


async def review_all(
    session: AsyncSession,
    *,
    settings: Settings | None = None,
    call=_call_claude,
) -> AiAuditReport:
    """ИИ-ревизия: разобрать все документы, требующие внимания, и вынести общий вердикт."""
    settings = settings or get_settings()

    pending = (
        await session.scalars(
            select(TaxDocumentIntake)
            .where(TaxDocumentIntake.status.in_(ATTENTION_STATUSES))
            .order_by(TaxDocumentIntake.received_at.asc())
        )
    ).all()
    documents: list[AiDocumentReview] = []
    for intake in pending:
        documents.append(
            await review_document(session, intake, settings=settings, call=call)
        )

    snapshot = await _build_snapshot(session)
    payload = await call(
        settings, tool=_AUDIT_TOOL, prompt=_audit_prompt(snapshot), max_tokens=3000
    )
    findings = [
        AiAuditFinding(
            severity=str(item.get("severity") or "info"),
            title=str(item.get("title") or ""),
            detail=str(item.get("detail") or ""),
        )
        for item in payload.get("findings") or []
    ]
    verdict = str(payload.get("verdict") or "").strip() or "Модель не дала вердикта."
    return AiAuditReport(verdict=verdict, findings=findings, documents=documents)
