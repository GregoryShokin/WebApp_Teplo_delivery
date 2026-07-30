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
from app.services.anthropic_client import LlmCallError, call_failure_reason, call_tool
from app.services.taxes.document_ingest import REVIEW_TAX_KINDS, set_intake_review
from app.services.taxes.document_parser import _docx_text

logger = logging.getLogger(__name__)

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
    # Предложенные поля платёжки (tax_kind/amount/due_date/period_hint) — ИИ сам их
    # НЕ применяет: владелец подтверждает кнопкой «Применить» в окне разбора.
    proposal: dict | None
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
    """Один структурированный вызов Claude (forced tool_use). Бросает TaxAiError.

    Сам вызов живёт в общем модуле ``app.services.anthropic_client``: раньше блок создания
    клиента был скопирован сюда и в распознавание счетов ДОСЛОВНО, байт в байт. Цена
    расхождения высокая — клиент без заголовка релея на проде получает 403, и владелец идёт
    искать проблему в ключе. Здесь остаётся только перевод ошибки в доменную.
    """
    try:
        return await call_tool(
            settings,
            tool=tool,
            prompt=prompt,
            model=settings.tax_ai_reviewer_model,
            system=_SYSTEM,
            max_tokens=max_tokens,
        )
    except LlmCallError as exc:
        logger.warning("ИИ-ревьюер: вызов Claude не удался", exc_info=True)
        raise TaxAiError(str(exc)) from exc


# Причина отказа общая для всех сервисов; имя оставлено прежним — на него ссылаются тесты.
_call_failure_reason = call_failure_reason


def _extract_text(intake: TaxDocumentIntake) -> str | None:
    """Best-effort текст документа. None — содержимое недоступно (нет байтов/формат)."""
    data = intake.content
    if not data:
        return None
    name = (intake.filename or "").lower()
    try:
        if name.endswith(".docx"):
            return _docx_text(data)[:_MAX_DOC_CHARS]
        if name.endswith((".xls", ".xlsx")):
            from app.services.taxes.document_parser import open_workbook

            book = open_workbook(data)
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
        # Конкретная причина нечитаемости (старый .doc, новый .xlsx…) уже определена
        # разбором — отдаём её модели, чтобы объяснение владельцу называло причину, а
        # не ограничивалось «содержимое не читается».
        why = rec.get("reason") or intake.error or "формат не читается"
        parts.append(
            f"Содержимое файла извлечь не удалось. Причина: {why} "
            "Суди по имени файла и метаданным; поля в этом случае не заполняй, "
            "объясни, что это за документ, и назови эту причину владельцу."
        )
    return "\n".join(p for p in parts if p)


def _valid_amount(raw: object) -> Decimal | None:
    try:
        value = Decimal(str(raw).replace(" ", "").replace(",", "."))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value > 0 else None


def _build_proposal(payload: dict, intake: TaxDocumentIntake) -> dict | None:
    """Предложение полей платёжки из ответа модели. None — предлагать нечего.

    Решение владельца 26.07.2026: ИИ НИЧЕГО не применяет сам — только предлагает.
    Владелец смотрит предложение во всплывающем окне и подтверждает кнопкой; применение
    идёт той же ручкой, что ручная проверка. Предложение имеет смысл только для платёжек
    (у ведомостей/оборотк поля другие) и только у документов, ждущих внимания.
    """
    if payload.get("document_type") != "payment_order":
        return None
    if intake.status not in (*ATTENTION_STATUSES, "parsed"):
        return None
    proposal: dict[str, str] = {}
    if payload.get("tax_kind") in REVIEW_TAX_KINDS:
        proposal["tax_kind"] = str(payload["tax_kind"])
    amount = _valid_amount(payload.get("amount"))
    if amount is not None:
        proposal["amount"] = str(amount)
    for key in ("due_date", "period_hint"):
        if payload.get(key):
            proposal[key] = str(payload[key])
    return proposal or None


async def review_document(
    session: AsyncSession,
    intake: TaxDocumentIntake,
    *,
    settings: Settings | None = None,
    call=_call_claude,
) -> AiDocumentReview:
    """ИИ-разбор одного документа: объяснение + ПРЕДЛОЖЕНИЕ полей, без применения.

    Данные не трогаются: разбор сохраняется в ``recognition['ai_review']`` (окно на
    фронте показывает его по клику), предложенные поля владелец применяет сам кнопкой
    «Применить» — через тот же роут, что ручная проверка.

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
    proposal = _build_proposal(payload, intake)

    updated = dict(intake.recognition or {})
    updated["ai_review"] = {
        "summary": summary,
        "confidence": round(confidence, 2),
        "document_type": doc_type,
        "needs_human": needs_human,
        "reasons": reasons,
        "proposal": proposal,
        "model": settings.tax_ai_reviewer_model,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    intake.recognition = updated
    await session.flush()

    return AiDocumentReview(
        intake_id=str(intake.id),
        filename=intake.filename or "",
        summary=summary,
        confidence=confidence,
        document_type=str(doc_type) if doc_type else None,
        proposal=proposal,
        needs_human=needs_human,
        reasons=reasons,
    )


async def apply_ai_proposal(session: AsyncSession, intake: TaxDocumentIntake) -> None:
    """Владелец подтвердил предложение ИИ («да, делай») — применить сохранённые поля.

    Применяется РОВНО то, что лежит в ``recognition['ai_review']['proposal']`` (никакой
    подмены полей с фронта), той же дорогой, что ручная проверка. Документ, который парсер
    не классифицировал (error/unsupported), при подтверждении выравнивается в платёжку —
    иначе ``set_intake_review`` откажет в overrides.
    """
    review = (intake.recognition or {}).get("ai_review") or {}
    proposal = review.get("proposal")
    if not proposal:
        raise ValueError("У этого документа нет предложения ИИ — сначала запустите разбор.")
    if intake.document_type != "payment_order":
        intake.document_type = "payment_order"
    await set_intake_review(session, intake, status="parsed", overrides=dict(proposal))

    updated = dict(intake.recognition or {})
    ai_review = dict(updated.get("ai_review") or {})
    ai_review["applied"] = True
    ai_review["applied_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    updated["ai_review"] = ai_review
    # Поля предложил ИИ, подтвердил владелец — фиксируем обе стороны.
    updated["reviewed_by"] = "ai_confirmed"
    intake.recognition = updated
    await session.flush()


def _audit_prompt(snapshot: str) -> str:
    return (
        "Проверь налоговый контур целиком и вызови record_audit. Ниже — снимок системы: "
        "строки сверки (расчёт × документ бухгалтера × факт уплаты из банка), обязательства "
        "«к уплате», расчётный ЕНС-кошелёк и сводка по документам. Найди несостыковки: "
        "просроченные или подозрительно отсутствующие платежи, документы, застрявшие в "
        "ошибках, противоречия между разделами. Если картина сходится — так и скажи, не "
        "выдумывай проблем.\n\n"
        "ВАЖНО ПРО ЦИФРЫ: все разницы сумм и их направления УЖЕ посчитаны системой — они "
        "приведены в «пояснениях системы» у каждой строки, и вердикт строки "
        "(ok/due/doc_mismatch/overdue) первичен. НЕ вычисляй разницы сам, НЕ выводи "
        "собственных сумм расхождений — цитируй готовые числа из пояснений дословно. "
        "Учитывай: «документ больше расчёта» безопасно (переплата зачтётся на ЕНС); "
        "зарплатный ЕНП помесячно не совпадает с платёжкой из-за «окон» НДФЛ — это норма, "
        "а не ошибка.\n\n" + snapshot
    )


def _fmt_diff(documented: Decimal | None, calculated: Decimal | None) -> str | None:
    """Готовая дельта «документ − расчёт» со словами — чтобы модель не считала сама."""
    if documented is None or calculated is None:
        return None
    diff = documented - calculated
    if diff == 0:
        return "документ равен расчёту"
    if diff > 0:
        return (
            f"документ БОЛЬШЕ расчёта на {diff} ₽ — безопасная сторона (переплата зачтётся на ЕНС)"
        )
    return f"документ МЕНЬШЕ расчёта на {-diff} ₽ — сторона риска недоплаты"


async def _build_snapshot(session: AsyncSession) -> str:
    """Компактный текстовый снимок контура для общего аудита.

    Все арифметические выводы (разницы, направления) кладём в снимок ГОТОВЫМИ — LLM
    ненадёжна в арифметике, её дело — интерпретация и приоритизация, не вычисления.
    """
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
        # Готовую дельту даём только там, где сама сверка зафиксировала расхождение
        # документа с ожиданием. Допвзнос 1% исключён: его платёжка приростная, прямая
        # разница с накопленным расчётом врёт — специфику объясняют messages строки.
        if ln.verdict == "doc_mismatch" and ln.tax_kind != "contrib_extra_1pct":
            diff_note = _fmt_diff(ln.documented, ln.calculated)
            if diff_note:
                lines.append(f"  разница (посчитана системой): {diff_note}")
        if ln.messages:
            lines.append(f"  пояснения системы: {' '.join(ln.messages)}")
        if ln.action:
            lines.append(f"  рекомендация системы: {ln.action}")

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
        await session.execute(select(TaxDocumentIntake.status, TaxDocumentIntake.filename))
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
        documents.append(await review_document(session, intake, settings=settings, call=call))

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
