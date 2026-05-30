#!/usr/bin/env python3
"""Telegram intake bot for T-Bank payment order documents.

The bot only receives documents/photos/text, runs deterministic intake parsing,
and creates private payment-order candidates. It never submits payments to the
bank; the H2H submit path remains an explicit CLI action.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    from . import payment_order
except ImportError:
    import payment_order


PROJECT_ROOT = Path(__file__).resolve().parents[3]
PRIVATE_ROOT = PROJECT_ROOT / "research/private/tbank/payment_orders"
TELEGRAM_TMP_DIR = PRIVATE_ROOT / "telegram_tmp"
DEFAULT_MAX_FILE_MB = 20


class TelegramAPIError(RuntimeError):
    pass


def env_value(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def required_env(name: str) -> str:
    value = env_value(name)
    if not value:
        raise SystemExit(f"{name} is missing in local .env")
    return value


def api_request(token: str, method: str, payload: dict[str, Any] | None = None) -> Any:
    url = f"https://api.telegram.org/bot{token}/{method}"
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            raw = response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TelegramAPIError(str(exc)) from exc
    data = json.loads(raw.decode("utf-8", "replace"))
    if not data.get("ok"):
        description = str(data.get("description") or "Telegram API request failed")
        raise TelegramAPIError(description)
    return data.get("result")


def download_file(token: str, file_id: str, destination: Path) -> None:
    file_info = api_request(token, "getFile", {"file_id": file_id})
    file_path = str(file_info.get("file_path") or "")
    if not file_path:
        raise TelegramAPIError("Telegram did not return file_path")
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            destination.write_bytes(response.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TelegramAPIError(str(exc)) from exc


def allowed_chat_ids() -> set[str]:
    raw = env_value("TELEGRAM_PAYMENT_BOT_ALLOWED_CHAT_IDS")
    return {part.strip() for part in raw.split(",") if part.strip()}


def is_allowed_chat(chat_id: int | str) -> bool:
    allowed = allowed_chat_ids()
    return not allowed or str(chat_id) in allowed


def chat_title(chat: dict[str, Any]) -> str:
    for key in ("username", "first_name", "title"):
        value = str(chat.get(key) or "").strip()
        if value:
            return f"@{value}" if key == "username" else value
    return str(chat.get("id") or "")


def send_message(
    token: str,
    chat_id: int | str,
    text: str,
    *,
    reply_markup: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text[:4000],
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return api_request(token, "sendMessage", payload)


def edit_message_text(
    token: str,
    chat_id: int | str,
    message_id: int,
    text: str,
    *,
    reply_markup: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text[:4000],
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        api_request(token, "editMessageText", payload)
    except TelegramAPIError as exc:
        # Telegram returns an error when the new text is identical; safe to ignore.
        if "message is not modified" in str(exc).lower():
            return
        raise


def answer_callback_query(token: str, callback_query_id: str, text: str = "") -> None:
    payload: dict[str, Any] = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text[:200]
    try:
        api_request(token, "answerCallbackQuery", payload)
    except TelegramAPIError:
        # Non-fatal: spinner will time out after a few seconds anyway.
        pass


def max_file_bytes() -> int:
    raw = env_value("TELEGRAM_PAYMENT_BOT_MAX_FILE_MB")
    try:
        return int(raw or DEFAULT_MAX_FILE_MB) * 1024 * 1024
    except ValueError:
        return DEFAULT_MAX_FILE_MB * 1024 * 1024


def empty_upload_args(
    *,
    file_path: Path,
    chat: dict[str, Any],
    message: dict[str, Any],
    caption: str,
) -> argparse.Namespace:
    sender = chat_title(chat)
    return argparse.Namespace(
        file=[str(file_path)],
        upload_dir=str(payment_order.DEFAULT_UPLOAD_DIR),
        source_channel="telegram",
        sender=sender,
        chat_id=str(chat.get("id") or ""),
        message_id=str(message.get("message_id") or ""),
        caption=caption,
        no_prepare=False,
        intake_db=str(payment_order.DEFAULT_INTAKE_DB),
        document_number="",
        amount="",
        recipient_name="",
        inn="",
        kpp="",
        bank_acnt="",
        bank_bik="",
        recipient_bank_name="",
        recipient_corr_account="",
        account_number=env_value("TELEGRAM_PAYMENT_BOT_ACCOUNT_NUMBER"),
        payment_purpose="",
        document_id="",
        execution_order=5,
        execution_date="",
        source_document_number="",
        source_document_date="",
    )


def parse_cli_output(output: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def run_upload(file_path: Path, chat: dict[str, Any], message: dict[str, Any], caption: str) -> dict[str, str]:
    args = empty_upload_args(file_path=file_path, chat=chat, message=message, caption=caption)
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        result = payment_order.upload_document(args)
    parsed = parse_cli_output(stream.getvalue())
    parsed["exit_code"] = str(result)
    return parsed


def load_candidate_basis(candidate_id: str) -> dict[str, Any]:
    if not candidate_id:
        return {}
    try:
        candidate = payment_order.intake_store(None).get_candidate(candidate_id)
    except Exception:
        return {}
    return dict(candidate.get("payment_basis") or {})


def load_expense_accrual(parsed_id: str) -> dict[str, Any]:
    if not parsed_id:
        return {}
    try:
        accrual = payment_order.intake_store(None).get_expense_accrual_by_parsed_id(parsed_id)
    except Exception:
        return {}
    return dict(accrual or {})


PARSER_HEADERS: dict[str, tuple[str, str]] = {
    "water_utility_invoice_v1": ("💧", "Водоканал"),
    "electricity_act_v1": ("⚡", "Электроэнергия"),
}


def format_money(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        # Source values are strings like "9878.79" produced by fmt_money.
        amount = float(raw)
    except ValueError:
        return raw
    rubles, kop = divmod(round(amount * 100), 100)
    grouped = f"{int(rubles):,}".replace(",", " ")
    return f"{grouped},{int(kop):02d} ₽"


def mask_card_number(card: str) -> str:
    digits = "".join(ch for ch in str(card or "") if ch.isdigit())
    if len(digits) < 4:
        return ""
    return f"•••• {digits[-4:]}"


def utility_card_destination() -> tuple[str, str, str]:
    """Returns (masked_card, raw_card, agreement_number) from env. Either may be empty."""
    raw_card = env_value("TBANK_API_PERSONAL_CARD_NUMBER")
    agreement = env_value("TBANK_API_PERSONAL_CARD_AGREEMENT")
    return mask_card_number(raw_card), raw_card, agreement


_COUNTERPARTY_PREFIX_RE = re.compile(
    r"^\s*(?:Покупатель|Поставщик|Получатель|Плательщик|Продавец|Исполнитель)\s*[:\-]\s*",
    re.IGNORECASE,
)


def clean_counterparty_name(name: str) -> str:
    """Strip stray label prefixes ("Покупатель:", "Поставщик:" etc.) the
    parser sometimes captures when OCR reorders the invoice header.
    """
    cleaned = str(name or "").strip()
    while True:
        new_value = _COUNTERPARTY_PREFIX_RE.sub("", cleaned, count=1).strip()
        if new_value == cleaned:
            return cleaned
        cleaned = new_value


def reply_header(basis: dict[str, Any], parser_name: str) -> str:
    icon, default_label = PARSER_HEADERS.get(parser_name, ("📄", "Платёж"))
    label = str(basis.get("counterparty_alias") or default_label)
    period = str(basis.get("service_period_label") or "").strip()
    if period:
        return f"{icon} {label} · {period}"
    return f"{icon} {label}"


def reply_lines_for_ready(basis: dict[str, Any], fields: dict[str, str]) -> list[str]:
    parser_name = str(basis.get("parser_name") or fields.get("parser_name") or "")
    lines = [reply_header(basis, parser_name), ""]

    amount = basis.get("amount") or fields.get("api_amount") or fields.get("amount") or ""
    vat = basis.get("vat_amount") or ""
    money_line = f"💰 Сумма: {format_money(amount)}" if amount else "💰 Сумма: не распознана"
    lines.append(money_line)
    if vat:
        lines.append(f"   в т.ч. НДС {format_money(vat)}")

    doc_type = str(basis.get("document_type") or "").strip()
    doc_number = str(basis.get("document_number") or "").strip()
    doc_date = str(basis.get("document_date") or "").strip()
    doc_parts: list[str] = []
    if doc_type:
        doc_parts.append(doc_type.capitalize())
    if doc_number:
        doc_parts.append(f"№ {doc_number}")
    if doc_date:
        doc_parts.append(f"от {doc_date}")
    if doc_parts:
        lines.append(f"📄 {' '.join(doc_parts)}")

    counterparty = clean_counterparty_name(basis.get("counterparty_name") or "")
    if counterparty:
        # Trim long legal names so the reply stays scannable.
        if len(counterparty) > 80:
            counterparty = counterparty[:77] + "…"
        lines.append(f"🏢 {counterparty}")

    period_label = str(basis.get("service_period_label") or "").strip()
    pnl_article = str(basis.get("pnl_article_candidate") or "").strip()
    if period_label or pnl_article:
        accrual_parts = [part for part in (period_label, pnl_article) if part]
        lines.append("📊 Расход для ОПиУ: " + " · ".join(accrual_parts))

    if parser_name in PARSER_HEADERS:
        masked, _, agreement = utility_card_destination()
        if masked:
            destination = f"💳 Перевод на карту {masked}"
            if not agreement:
                destination += " (нет agreementNumber в .env — soft-submit)"
            lines.append(destination)

    lines.extend(["", f"🔑 candidate_id: {fields.get('candidate_id', 'unknown')}"])
    return lines


def reply_lines_for_review(basis: dict[str, Any], fields: dict[str, str]) -> list[str]:
    lines = ["⚠️ Документ принят, нужна проверка владельца.", ""]

    parser_name = str(basis.get("parser_name") or fields.get("parser_name") or "")
    if parser_name:
        header = reply_header(basis, parser_name)
        lines.append(header)
        lines.append("")

    amount = basis.get("amount") or ""
    lines.append(f"💰 Сумма: {format_money(amount)}" if amount else "💰 Сумма: НЕ РАСПОЗНАНА")

    doc_number = str(basis.get("document_number") or "").strip()
    doc_date = str(basis.get("document_date") or "").strip()
    if doc_number or doc_date:
        lines.append(f"📄 Документ № {doc_number or '?'} от {doc_date or '?'}")

    counterparty = str(basis.get("counterparty_name") or "").strip()
    if counterparty:
        if len(counterparty) > 80:
            counterparty = counterparty[:77] + "…"
        lines.append(f"🏢 {counterparty}")

    missing = [
        part.strip()
        for part in str(basis.get("owner_review_reasons") or "").split(",")
        if part.strip()
    ]
    reasons = list(basis.get("owner_review_reasons") or [])
    if isinstance(reasons, list) and reasons:
        lines.append("")
        lines.append("Причины:")
        for reason in reasons[:5]:
            lines.append(f"  • {reason}")

    lines.extend(["", f"🔑 candidate_id: {fields.get('candidate_id', 'unknown')}"])
    lines.append(f"🗂  upload_id: {fields.get('upload_id', 'unknown')}")
    return lines


def candidate_reply(fields: dict[str, str]) -> str:
    parse_status = fields.get("parse_status") or ""
    if parse_status.startswith("failed"):
        return "\n".join(
            [
                "❌ Файл сохранён, но распознавание не прошло.",
                f"upload_id: {fields.get('upload_id', 'unknown')}",
                f"parse_status: {parse_status}",
                "Пришли документ заново или текстовый PDF.",
            ]
        )

    status = fields.get("candidate_status") or fields.get("intake_status") or "unknown"
    prepare_status = fields.get("prepare_status") or ""
    ready = (
        status in {"parsed_ready", "ready_for_bank_signature"}
        and prepare_status != "owner_review"
    )
    basis = load_candidate_basis(fields.get("candidate_id", ""))
    lines = reply_lines_for_ready(basis, fields) if ready else reply_lines_for_review(basis, fields)
    return "\n".join(lines)


def candidate_keyboard(fields: dict[str, str]) -> dict[str, Any] | None:
    status = fields.get("candidate_status") or fields.get("intake_status") or ""
    prepare_status = fields.get("prepare_status") or ""
    candidate_id = fields.get("candidate_id", "")
    if not candidate_id or candidate_id == "unknown":
        return None
    if status not in {"parsed_ready", "ready_for_bank_signature"}:
        return None
    if prepare_status == "owner_review":
        return None
    return {
        "inline_keyboard": [
            [{"text": "✅ Отправить в банк", "callback_data": f"submit:{candidate_id}"}],
        ]
    }


TO_SIGN_DIR = PRIVATE_ROOT / "to_sign"


def soft_submit_candidate(candidate_id: str) -> dict[str, Any]:
    """Soft-submit: copy prepared payload to to_sign/ and mark candidate.

    Returns a dict describing what happened, with keys:
      `status` (`ok`/`already_submitted`/`not_ready`/`no_request_file`/`error`),
      `message`, `to_sign_file`, `amount`, `candidate_id`.
    """
    result: dict[str, Any] = {"candidate_id": candidate_id, "status": "error", "message": ""}
    try:
        store = payment_order.intake_store(None)
        candidate = store.get_candidate(candidate_id)
    except KeyError:
        result["message"] = "candidate не найден"
        return result
    except Exception as exc:  # pragma: no cover - defensive
        result["message"] = f"intake error: {payment_order.sanitize_text(str(exc))}"
        return result

    current_status = str(candidate.get("status") or "")
    if current_status == "awaiting_bank_signature":
        result["status"] = "already_submitted"
        result["message"] = "Уже отмечен как awaiting_bank_signature."
        result["amount"] = (candidate.get("payment_basis") or {}).get("amount", "")
        return result
    if current_status not in {"parsed_ready", "ready_for_bank_signature"}:
        result["status"] = "not_ready"
        result["message"] = f"candidate в статусе {current_status!r}, отправка не разрешена."
        return result

    request_file = str(candidate.get("request_file") or "").strip()
    if not request_file:
        result["status"] = "no_request_file"
        result["message"] = "Подготовленный JSON не найден; перезапусти upload."
        return result

    source_path = payment_order.resolve_project_path(request_file)
    if not source_path.exists():
        result["status"] = "no_request_file"
        result["message"] = f"Подготовленный JSON отсутствует на диске: {request_file}"
        return result

    TO_SIGN_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    target_path = TO_SIGN_DIR / f"{candidate_id}_{stamp}.json"

    try:
        payload = payment_order.load_json(source_path)
    except Exception as exc:
        result["message"] = f"не смог прочитать JSON: {payment_order.sanitize_text(str(exc))}"
        return result

    metadata = dict(payload.get("metadata") or {})
    metadata.update(
        {
            "soft_submitted_at": dt.datetime.now().replace(microsecond=0).isoformat(),
            "soft_submit_target_bucket": "Платежи в работе -> На подпись",
            "soft_submit_note": (
                "Bot soft-submit: H2H credentials не настроены; владелец проводит платёж "
                "вручную в T-Business UI по этим реквизитам."
            ),
        }
    )
    masked_card, raw_card, agreement = utility_card_destination()
    parser_name = str((candidate.get("payment_basis") or {}).get("parser_name") or "")
    if parser_name in PARSER_HEADERS and (raw_card or agreement):
        metadata["card_destination"] = {
            "card_last4": masked_card.split()[-1] if masked_card else "",
            "agreement_number": agreement,
            "note": "Перевод выполняется на карту физлица; компания компенсирует расход.",
        }
    payload["metadata"] = metadata

    try:
        payment_order.write_json(target_path, payload)
        store.mark_candidate_request(
            candidate_id=candidate_id,
            request_file=payment_order.rel(target_path),
            status="awaiting_bank_signature",
            requires_owner_review=False,
        )
    except Exception as exc:
        result["message"] = f"не удалось сохранить: {payment_order.sanitize_text(str(exc))}"
        return result

    result.update(
        {
            "status": "ok",
            "message": "OK",
            "to_sign_file": payment_order.rel(target_path),
            "amount": (candidate.get("payment_basis") or {}).get("amount", ""),
        }
    )
    return result


def submit_confirmation_reply(result: dict[str, Any]) -> str:
    candidate_id = result.get("candidate_id", "unknown")
    if result["status"] == "ok":
        amount = format_money(result.get("amount") or "")
        masked, _, agreement = utility_card_destination()
        lines = [
            "✅ Платёж подготовлен к отправке в банк.",
            "",
            f"💰 Сумма: {amount}" if amount else "💰 Сумма: —",
        ]
        if masked:
            destination = f"💳 Назначение: карта {masked}"
            if not agreement:
                destination += " (нет agreementNumber)"
            lines.append(destination)
        lines.extend(
            [
                "",
                "H2H API ещё не настроен — это soft-submit.",
                "Открой T-Business → Платежи → новый платёж и проведи вручную по реквизитам из JSON ниже.",
                "",
                f"📁 {result.get('to_sign_file', '')}",
                f"🔑 {candidate_id}",
            ]
        )
        return "\n".join(lines)
    if result["status"] == "already_submitted":
        return f"ℹ️ Этот candidate уже помечен как awaiting_bank_signature.\n🔑 {candidate_id}"
    if result["status"] == "not_ready":
        return f"⚠️ {result['message']}\n🔑 {candidate_id}"
    if result["status"] == "no_request_file":
        return f"❌ {result['message']}\n🔑 {candidate_id}"
    return f"❌ Ошибка: {result.get('message') or 'unknown'}\n🔑 {candidate_id}"


def save_text_message(message: dict[str, Any]) -> Path:
    text = str(message.get("text") or "")
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    message_id = str(message.get("message_id") or "message")
    path = TELEGRAM_TMP_DIR / f"{stamp}_{message_id}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def file_from_message(token: str, message: dict[str, Any]) -> tuple[Path, str] | tuple[None, str]:
    if message.get("document"):
        document = message["document"]
        size = int(document.get("file_size") or 0)
        if size and size > max_file_bytes():
            return None, "Файл слишком большой для intake."
        file_name = payment_order.safe_filename(str(document.get("file_name") or "telegram_upload"))
        path = TELEGRAM_TMP_DIR / file_name
        download_file(token, str(document["file_id"]), path)
        return path, str(message.get("caption") or "")

    if message.get("photo"):
        photos = list(message["photo"])
        photo = photos[-1]
        size = int(photo.get("file_size") or 0)
        if size and size > max_file_bytes():
            return None, "Фото слишком большое для intake."
        unique = str(photo.get("file_unique_id") or photo.get("file_id") or "photo")
        path = TELEGRAM_TMP_DIR / payment_order.safe_filename(f"telegram_photo_{unique}.jpg")
        download_file(token, str(photo["file_id"]), path)
        return path, str(message.get("caption") or "")

    text = str(message.get("text") or "").strip()
    if text and not text.startswith("/"):
        return save_text_message(message), ""

    return None, "Отправьте счет, УПД, накладную, чек, платежку или текст с реквизитами."


def help_text() -> str:
    return "\n".join(
        [
            "Пришлите счет, УПД, накладную, чек, платежку или текст с реквизитами.",
            "Я сохраню файл в private inbox, запущу deterministic parser и создам payment_order_candidate.",
            "Платежи в банк этим ботом не отправляются.",
            "",
            "Команды:",
            "/help - помощь",
            "/candidates ready - готовые кандидаты",
            "/candidates review - кандидаты на проверку",
        ]
    )


def candidates_text(status: str) -> str:
    mapped_status = "ready" if status == "ready" else "owner_review"
    rows = payment_order.intake_store(None).list_candidates(status=mapped_status, limit=10)
    if not rows:
        return f"Кандидатов со статусом {mapped_status} нет."
    lines = [f"Последние кандидаты: {mapped_status}"]
    for row in rows:
        lines.append(
            " ".join(
                [
                    str(row["candidate_id"]),
                    f"status={row['status']}",
                    f"amount={row.get('amount') or ''}",
                    f"parsed_id={row['parsed_id']}",
                ]
            )
        )
    return "\n".join(lines)


def handle_message(token: str, message: dict[str, Any]) -> None:
    chat = dict(message.get("chat") or {})
    chat_id = chat.get("id")
    if chat_id is None:
        return
    if not is_allowed_chat(chat_id):
        send_message(token, chat_id, "Этот чат не разрешен для платежного intake.")
        return

    text = str(message.get("text") or "").strip()
    if text in {"/start", "/help"}:
        send_message(token, chat_id, help_text())
        return
    if text.startswith("/candidates"):
        parts = text.split()
        status = parts[1] if len(parts) > 1 else "review"
        send_message(token, chat_id, candidates_text("ready" if status == "ready" else "review"))
        return

    try:
        file_path, note = file_from_message(token, message)
        if file_path is None:
            send_message(token, chat_id, note)
            return
        fields = run_upload(file_path, chat, message, note)
        send_message(
            token,
            chat_id,
            candidate_reply(fields),
            reply_markup=candidate_keyboard(fields),
        )
    except Exception as exc:
        send_message(token, chat_id, f"Не получилось обработать документ: {payment_order.sanitize_text(str(exc))}")


def handle_callback_query(token: str, callback_query: dict[str, Any]) -> None:
    query_id = str(callback_query.get("id") or "")
    data = str(callback_query.get("data") or "")
    message = callback_query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")

    if chat_id is None or not is_allowed_chat(chat_id):
        answer_callback_query(token, query_id, "Чат не разрешён.")
        return

    if not data.startswith("submit:"):
        answer_callback_query(token, query_id, "Неизвестная команда.")
        return

    candidate_id = data.split(":", 1)[1].strip()
    if not candidate_id:
        answer_callback_query(token, query_id, "candidate_id отсутствует.")
        return

    try:
        result = soft_submit_candidate(candidate_id)
    except Exception as exc:
        answer_callback_query(token, query_id, "Ошибка soft-submit.")
        send_message(
            token,
            chat_id,
            f"❌ Soft-submit упал: {payment_order.sanitize_text(str(exc))}\n🔑 {candidate_id}",
        )
        return

    toast = {
        "ok": "✅ Отправлено",
        "already_submitted": "Уже отправлено",
        "not_ready": "Не готов",
        "no_request_file": "JSON не найден",
        "error": "Ошибка",
    }.get(result["status"], "")
    answer_callback_query(token, query_id, toast)

    # Remove the inline button so it can't be pressed twice and update text.
    if message_id and result["status"] in {"ok", "already_submitted"}:
        try:
            original_text = str(message.get("text") or "")
            marker = "✅ Отправлено в банк" if result["status"] == "ok" else "ℹ️ Уже отправлено"
            new_text = original_text + f"\n\n{marker}"
            edit_message_text(token, chat_id, int(message_id), new_text, reply_markup={"inline_keyboard": []})
        except Exception:
            pass

    send_message(token, chat_id, submit_confirmation_reply(result))


def poll(token: str) -> None:
    offset = 0
    timeout = int(env_value("TELEGRAM_PAYMENT_BOT_POLL_TIMEOUT_SECONDS", "50") or "50")
    print("telegram_payment_bot=running", flush=True)
    while True:
        try:
            updates = api_request(token, "getUpdates", {"offset": offset, "timeout": timeout})
        except TelegramAPIError as exc:
            print(f"telegram_poll_error={payment_order.sanitize_text(str(exc))}", flush=True)
            time.sleep(5)
            continue
        for update in updates:
            offset = max(offset, int(update["update_id"]) + 1)
            message = update.get("message") or update.get("edited_message")
            if message:
                handle_message(token, message)
            callback_query = update.get("callback_query")
            if callback_query:
                handle_callback_query(token, callback_query)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Telegram bot for T-Bank payment-order intake")
    parser.add_argument("--check", action="store_true", help="Call Telegram getMe and print sanitized bot identity")
    parser.add_argument("--once", action="store_true", help="Fetch and process currently pending updates once")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    payment_order.load_local_env()
    args = parse_args(argv)
    token = required_env("TELEGRAM_BOT_TOKEN")
    if args.check:
        info = api_request(token, "getMe")
        print("telegram_bot_check=ok")
        print(f"telegram_bot_id={info.get('id')}")
        print(f"telegram_bot_username=@{info.get('username')}")
        return 0
    if args.once:
        updates = api_request(token, "getUpdates", {"timeout": 0})
        for update in updates:
            message = update.get("message") or update.get("edited_message")
            if message:
                handle_message(token, message)
        print(f"processed_updates={len(updates)}")
        return 0
    poll(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
