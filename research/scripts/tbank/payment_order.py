#!/usr/bin/env python3
"""Prepare T-Bank Business payment orders for owner signature.

The script is designed for agent/bot workflows:
1. Extract payment details from a document text/JSON.
2. Validate and store private bank payloads under research/private/tbank/payment_orders/.
3. Send the payment order to the T-Business work queue only through an explicit
   submit command and a separately enabled H2H/mTLS/signature setup.

The default target is "Платежи в работе -> На подпись", not "Черновики".
Whether a submitted order lands on signature or goes further depends on the
T-Business signing rules and certificates configured for the integration.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import email.utils
import hashlib
import hmac
import json
import mimetypes
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.error
import urllib.request
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

try:
    from . import payment_parsers
except ImportError:
    import payment_parsers


PROJECT_ROOT = Path(__file__).resolve().parents[3]
PRIVATE_ROOT = PROJECT_ROOT / "research/private/tbank/payment_orders"
DEFAULT_REQUEST_DIR = PRIVATE_ROOT / "requests"
DEFAULT_RESPONSE_DIR = PRIVATE_ROOT / "responses"
DEFAULT_UPLOAD_DIR = PRIVATE_ROOT / "uploads"
DEFAULT_INTAKE_DB = PRIVATE_ROOT / "payment_intake.sqlite3"
MACOS_OCR_SCRIPT = PROJECT_ROOT / "research/scripts/tbank/ocr_image_macos.swift"
SEQUENCE_PATH = PRIVATE_ROOT / "document_number_sequence.json"
DEFAULT_BASE_URL = "https://business.tbank.ru/openapi"
DEFAULT_ORDER_BASE_URL = "https://secured-openapi.tbank.ru"
PAYMENT_DRAFT_PATH = "/api/v1/payment/create"
PAYMENT_ORDER_SUBMIT_PATH = "/api/v1/payments-orders/payments-by-requisites/submit"
PAYMENT_ORDER_REQUEST_TARGET = "post /api/v1/payments-orders/payments-by-requisites/submit?"

TRUTHY = {"1", "true", "yes", "y", "on"}
SUPPORTED_UPLOAD_EXTENSIONS = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".tif",
    ".tiff",
    ".txt",
    ".md",
    ".csv",
    ".rtf",
}

REQUIRED_PAYMENT_FIELDS = (
    "documentNumber",
    "amount",
    "recipientName",
    "inn",
    "kpp",
    "bankAcnt",
    "bankBik",
    "accountNumber",
    "paymentPurpose",
    "executionOrder",
    "taxPayerStatus",
    "kbk",
    "oktmo",
    "taxEvidence",
    "taxPeriod",
    "uin",
    "taxDocNumber",
    "taxDocDate",
)

DEFAULT_TAX_FIELDS = {
    "taxPayerStatus": "0",
    "kbk": "0",
    "oktmo": "0",
    "taxEvidence": "0",
    "taxPeriod": "0",
    "uin": "0",
    "taxDocNumber": "0",
    "taxDocDate": "0",
}

FIELD_ALIASES = {
    "document_number": "documentNumber",
    "payment_order_number": "documentNumber",
    "amount_rub": "amount",
    "sum": "amount",
    "recipient": "recipientName",
    "recipient_name": "recipientName",
    "counterparty_name": "recipientName",
    "recipient_inn": "inn",
    "recipientInn": "inn",
    "recipient_kpp": "kpp",
    "recipientKpp": "kpp",
    "recipient_account": "bankAcnt",
    "recipientAccount": "bankAcnt",
    "bank_account": "bankAcnt",
    "rs": "bankAcnt",
    "checking_account": "bankAcnt",
    "bik": "bankBik",
    "bank_bik": "bankBik",
    "recipient_bik": "bankBik",
    "recipient_bank_name": "recipientBankName",
    "bank_name": "recipientBankName",
    "recipient_corr_account": "recipientCorrAccountNumber",
    "corr_account": "recipientCorrAccountNumber",
    "correspondent_account": "recipientCorrAccountNumber",
    "debit_account": "accountNumber",
    "from_account": "accountNumber",
    "payer_account": "accountNumber",
    "purpose": "paymentPurpose",
    "payment_purpose": "paymentPurpose",
    "paymentPurposeText": "paymentPurpose",
    "execution_order": "executionOrder",
    "source_document_number": "sourceDocumentNumber",
    "source_document_date": "sourceDocumentDate",
    "invoice_number": "sourceDocumentNumber",
    "invoice_date": "sourceDocumentDate",
    "document_id": "documentId",
    "payment_id": "documentId",
}


class TBankHTTPError(RuntimeError):
    def __init__(
        self,
        status: int | None,
        body: bytes,
        request_id: str,
        elapsed_seconds: float,
        message: str | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.request_id = request_id
        self.elapsed_seconds = elapsed_seconds
        self.message = message or body[:2000].decode("utf-8", "replace")
        super().__init__(f"T-Bank API error {status}: {sanitize_text(self.message)[:200]}")


def load_local_env() -> None:
    """Load .env/ENV without printing values and without overwriting real env."""

    for env_path in (PROJECT_ROOT / ".env", PROJECT_ROOT / "ENV"):
        if not env_path.exists():
            continue
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            if key and value and not os.environ.get(key):
                os.environ[key] = value


def env_value(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def required_env(name: str) -> str:
    value = env_value(name)
    if not value:
        raise SystemExit(f"{name} is missing. Add it to local .env first.")
    return value


def env_truthy(name: str) -> bool:
    return env_value(name).casefold() in TRUTHY


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def intake_store(args: argparse.Namespace | None = None) -> payment_parsers.PaymentIntakeStore:
    value = getattr(args, "intake_db", "") if args is not None else ""
    return payment_parsers.PaymentIntakeStore(resolve_project_path(value, DEFAULT_INTAKE_DB))


def resolve_project_path(value: str, default: Path | None = None) -> Path:
    if not value:
        if default is None:
            raise SystemExit("Path value is empty")
        return default
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def safe_filename(value: str, fallback: str = "upload") -> str:
    name = value.strip() or fallback
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:180] or fallback


def clean_digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def mask_account(value: Any) -> str:
    digits = clean_digits(value)
    if len(digits) < 4:
        return "<redacted>"
    return f"<account:...{digits[-4:]}>"


def mask_inn(value: Any) -> str:
    digits = clean_digits(value)
    if len(digits) < 4:
        return "<redacted>"
    return f"<inn:...{digits[-4:]}>"


def sanitize_text(value: str) -> str:
    text = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer <redacted>", value)
    text = re.sub(r"\b\d{20,22}\b", lambda m: mask_account(m.group(0)), text)
    text = re.sub(r"\b\d{12}\b", lambda m: mask_inn(m.group(0)), text)
    text = re.sub(r"\b\d{10}\b", lambda m: mask_inn(m.group(0)), text)
    return text[:2000]


def sanitize_payload(payload: Any) -> Any:
    if isinstance(payload, list):
        return [sanitize_payload(item) for item in payload]
    if not isinstance(payload, dict):
        return payload

    clean: dict[str, Any] = {}
    for key, value in payload.items():
        key_lower = key.casefold()
        if key_lower in {"authorization", "access_token", "token"}:
            clean[key] = "<redacted>"
        elif key_lower in {"inn", "recipientinn", "payeepersoninn"}:
            clean[key] = mask_inn(value)
        elif "account" in key_lower or key_lower in {"bankacnt", "payeraccountnumber"}:
            clean[key] = mask_account(value)
        elif key_lower in {"recipientname", "payeepersonname"}:
            clean[key] = "<recipient:set>" if value else ""
        elif key_lower in {"paymentpurpose"}:
            clean[key] = "<paymentPurpose:set>" if value else ""
        else:
            clean[key] = sanitize_payload(value)
    return clean


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_json_body(body: bytes) -> Any:
    text = body.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw_text": sanitize_text(text)}


def secret_bytes(secret: str, encoding: str) -> bytes:
    encoding = (encoding or "raw").casefold()
    if encoding == "base64":
        return base64.b64decode(secret)
    if encoding == "hex":
        return bytes.fromhex(secret)
    return secret.encode("utf-8")


def hmac_digest(value: bytes, secret: bytes, encoding: str) -> str:
    digest = hmac.new(secret, value, hashlib.sha256).digest()
    if encoding.casefold() == "hex":
        return digest.hex()
    return base64.b64encode(digest).decode("ascii")


def rfc1123_now() -> str:
    return email.utils.format_datetime(dt.datetime.now(dt.UTC), usegmt=True)


def signature_base(headers: list[str], values: dict[str, str]) -> str:
    lines = []
    for header in headers:
        key = header.strip().casefold()
        if key not in values:
            raise SystemExit(f"Cannot sign unknown header '{header}'")
        lines.append(f"{key}: {values[key]}")
    return "\n".join(lines)


def build_signature_header(*, key_id: str, secret: bytes, headers: list[str], values: dict[str, str]) -> str:
    base = signature_base(headers, values)
    signature = hmac_digest(
        base.encode("utf-8"),
        secret,
        env_value("TBANK_API_PAYMENT_ORDER_SIGNATURE_ENCODING", "base64") or "base64",
    )
    header_list = " ".join(header.strip().casefold() for header in headers)
    return (
        f'keyId="{key_id}",algorithm="HMAC-SHA256",'
        f'headers="{header_list}",signature="{signature}"'
    )


def money(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        return Decimal("0")
    text = str(value).strip()
    text = text.replace("\xa0", " ").replace(" ", "").replace(",", ".")
    text = re.sub(r"[^0-9.\-]", "", text)
    if text.count(".") > 1:
        parts = text.split(".")
        text = "".join(parts[:-1]) + "." + parts[-1]
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return Decimal("0")


def json_amount(value: Any) -> int | float:
    amount = money(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if amount == amount.to_integral():
        return int(amount)
    return float(amount)


def fmt_money(value: Any) -> str:
    amount = money(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return str(amount)


def normalize_date(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    match = re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2}|\d{4})", text)
    if match:
        day, month, year = match.groups()
        if len(year) == 2:
            year = f"20{year}"
        return f"{int(day):02d}.{int(month):02d}.{year}"
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        year, month, day = match.groups()
        return f"{day}.{month}.{year}"
    return text


def normalize_datetime(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return f"{text}T00:00:00+03:00"
    if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", text):
        day, month, year = text.split(".")
        return f"{year}-{month}-{day}T00:00:00+03:00"
    return text


def unwrap_payment_request(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SystemExit("Input JSON must be an object")
    for key in ("payment", "paymentDraft", "payment_order", "payload", "request"):
        value = payload.get(key)
        if isinstance(value, dict):
            return dict(value)
    return dict(payload)


def canonicalize_keys(raw: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in raw.items():
        canonical = FIELD_ALIASES.get(key, key)
        result[canonical] = value
    return result


def apply_env_defaults(payload: dict[str, Any]) -> None:
    if not payload.get("accountNumber"):
        payload["accountNumber"] = (
            env_value("TBANK_API_PAYMENT_ORDER_ACCOUNT_NUMBER")
            or env_value("TBANK_API_PAYMENT_DRAFT_ACCOUNT_NUMBER")
            or env_value("TBANK_API_ACCOUNT_NUMBER")
        )
    payload.setdefault("executionOrder", 5)
    for key, value in DEFAULT_TAX_FIELDS.items():
        payload.setdefault(key, value)


def normalize_payment_payload(raw: dict[str, Any]) -> dict[str, Any]:
    payload = canonicalize_keys(raw)
    apply_env_defaults(payload)

    for key in (
        "documentNumber",
        "inn",
        "kpp",
        "bankAcnt",
        "bankBik",
        "accountNumber",
        "recipientCorrAccountNumber",
    ):
        if key in payload and payload[key] is not None:
            payload[key] = clean_digits(payload[key])

    for key in ("recipientName", "recipientBankName", "paymentPurpose"):
        if key in payload and payload[key] is not None:
            payload[key] = re.sub(r"\s+", " ", str(payload[key])).strip()

    if payload.get("amount") not in (None, ""):
        payload["amount"] = json_amount(payload["amount"])

    if payload.get("executionOrder") not in (None, ""):
        try:
            payload["executionOrder"] = int(str(payload["executionOrder"]))
        except ValueError:
            payload["executionOrder"] = payload["executionOrder"]

    if payload.get("taxDocDate") and payload["taxDocDate"] != "0":
        payload["taxDocDate"] = normalize_date(str(payload["taxDocDate"]))
    if payload.get("sourceDocumentDate"):
        payload["sourceDocumentDate"] = normalize_date(str(payload["sourceDocumentDate"]))
    if payload.get("date"):
        payload["date"] = normalize_datetime(str(payload["date"]))

    return payload


def validate_payment_payload(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in REQUIRED_PAYMENT_FIELDS:
        value = payload.get(field)
        if value in (None, ""):
            errors.append(f"{field} is required")

    document_number = str(payload.get("documentNumber", ""))
    if document_number and not re.fullmatch(r"\d{1,6}", document_number):
        errors.append("documentNumber must contain 1-6 digits")

    amount = money(payload.get("amount"))
    if amount <= 0:
        errors.append("amount must be greater than zero")

    inn = str(payload.get("inn", ""))
    if inn and not re.fullmatch(r"\d{10}|\d{12}|0", inn):
        errors.append("inn must contain 10 or 12 digits, or 0")

    kpp = str(payload.get("kpp", ""))
    if kpp and not re.fullmatch(r"\d{9}|0", kpp):
        errors.append("kpp must contain 9 digits, or 0")

    bank_acnt = str(payload.get("bankAcnt", ""))
    if bank_acnt and not re.fullmatch(r"\d{20}|\d{22}", bank_acnt):
        errors.append("bankAcnt must contain 20 or 22 digits")

    bank_bik = str(payload.get("bankBik", ""))
    if bank_bik and not re.fullmatch(r"\d{9}", bank_bik):
        errors.append("bankBik must contain 9 digits")

    account_number = str(payload.get("accountNumber", ""))
    if account_number and not re.fullmatch(r"\d{20}|\d{22}", account_number):
        errors.append("accountNumber must contain 20 or 22 digits")

    corr_account = str(payload.get("recipientCorrAccountNumber", ""))
    if corr_account and not re.fullmatch(r"\d{20}", corr_account):
        errors.append("recipientCorrAccountNumber must contain 20 digits")

    purpose = str(payload.get("paymentPurpose", ""))
    if len(purpose) > 210:
        errors.append("paymentPurpose must be 210 characters or fewer")

    execution_order = payload.get("executionOrder")
    if not isinstance(execution_order, int) or execution_order < 1 or execution_order > 5:
        errors.append("executionOrder must be an integer from 1 to 5")

    return errors


def allocate_document_number(explicit: str = "") -> str:
    if explicit:
        number = clean_digits(explicit)
        if not re.fullmatch(r"\d{1,6}", number):
            raise SystemExit("--document-number must contain 1-6 digits")
        return number

    SEQUENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now()
    if SEQUENCE_PATH.exists():
        try:
            data = load_json(SEQUENCE_PATH)
            last = int(data.get("last_document_number", 0))
        except (ValueError, TypeError, json.JSONDecodeError):
            last = 0
    else:
        last = max(0, int(now.strftime("%H%M%S")) - 1)
    next_number = last + 1
    if next_number > 999999:
        next_number = 1
    write_json(
        SEQUENCE_PATH,
        {
            "last_document_number": next_number,
            "updated_at": now.replace(microsecond=0).isoformat(),
        },
    )
    return str(next_number)


def text_from_document(path: Path) -> str:
    suffix = path.suffix.casefold()
    if suffix in {".txt", ".md", ".csv", ".rtf"}:
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".pdf":
        text = text_from_pdf(path)
        if text.strip():
            return text
        raise SystemExit("Could not extract text from PDF. Run OCR first or pass --text-file.")
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}:
        text = text_from_image(path)
        if text.strip():
            return text
        raise SystemExit("Could not OCR image. Install/configure OCR or pass --text-file.")
    raise SystemExit(f"Unsupported document extension: {suffix}. Use --input-json or --text-file.")


def run_text_command(cmd: list[str]) -> str:
    try:
        result = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout


def text_from_pdf(path: Path) -> str:
    text = run_text_command(["pdftotext", "-layout", str(path), "-"])
    if text.strip():
        return text
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        return ""
    try:
        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return ""


def text_from_image(path: Path) -> str:
    if shutil.which("tesseract"):
        text = run_text_command(["tesseract", str(path), "stdout", "-l", "rus+eng"])
        bottom_text = text_from_jpeg_bottom_crop(path)
        if bottom_text.strip():
            text = "\n".join(part for part in (text, bottom_text) if part.strip())
        if text.strip():
            return text
    if sys.platform == "darwin" and shutil.which("swift") and MACOS_OCR_SCRIPT.exists():
        return run_text_command(["swift", str(MACOS_OCR_SCRIPT), str(path)])
    return ""


IMAGE_OCR_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"})


def write_text_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")


def text_from_vision_ocr(path: Path) -> str:
    """Run the macOS Vision OCR helper as an independent OCR engine.

    Vision usually returns column-major text for tabular invoices, which is
    poor for row-aligned regex but excellent for clean digit extraction.
    The water-utility parser has a dedicated column-major path that handles
    this layout.
    """
    if sys.platform != "darwin" or not shutil.which("swift") or not MACOS_OCR_SCRIPT.exists():
        return ""
    if path.suffix.casefold() not in IMAGE_OCR_EXTENSIONS:
        return ""
    return run_text_command(["swift", str(MACOS_OCR_SCRIPT), str(path)])


def text_from_upscaled_image(path: Path, target_width: int = 1800) -> str:
    """Rescue OCR pass — upscale the image first.

    At very low resolutions (≈ 139 DPI for Telegram-compressed photos)
    tesseract can read thin table borders as `1`s or row markers like `6`
    as `©`. Going to ≈ 270 DPI on a 1800-wide upscale stabilises those
    glyphs at the cost of occasionally breaking the column-major layout,
    which is why this is only invoked as a secondary pass.
    """
    if path.suffix.casefold() not in {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}:
        return ""
    if not shutil.which("tesseract") or not shutil.which("sips"):
        return ""
    width, _ = image_size_from_sips(path)
    if width <= 0 or width >= target_width:
        return ""
    with tempfile.TemporaryDirectory(prefix="payment_ocr_rescue_", dir=str(PROJECT_ROOT / "data" / "private")) as tmpdir:
        upscaled = Path(tmpdir) / f"{path.stem}_w{target_width}.jpg"
        result = run_text_command(
            ["sips", "-s", "format", "jpeg", "--resampleWidth", str(target_width), str(path), "--out", str(upscaled)]
        )
        if not upscaled.exists():
            return ""
        return run_text_command(["tesseract", str(upscaled), "stdout", "-l", "rus+eng"])


def _parse_is_better(candidate: dict[str, Any], baseline: dict[str, Any]) -> bool:
    """A rescue OCR's parse is better when it leaves strictly fewer required
    fields missing, or matches the baseline's missing set but lifts the status
    out of `owner_review`.
    """
    candidate_missing = len(candidate.get("missing_required_fields") or [])
    baseline_missing = len(baseline.get("missing_required_fields") or [])
    if candidate_missing < baseline_missing:
        return True
    if candidate_missing > baseline_missing:
        return False
    return (
        candidate.get("status") == "parsed_ready"
        and baseline.get("status") != "parsed_ready"
    )


def extract_text_with_rescue(
    stored_path: Path,
    prepared_dir: Path,
    metadata: dict[str, Any],
) -> tuple[str, dict[str, Any], str]:
    """OCR a document and, when the first parse misses required fields,
    retry with progressively more aggressive engines: tesseract on an
    upscaled copy, then macOS Vision (which returns column-major text the
    water-utility parser has a dedicated path for).

    Persists every OCR text next to the upload so failures can be replayed
    offline. Returns (final_text, parse_result, rescue_status). The status
    is one of `not_needed`, `non_image`, `used:<engine>`, `not_better`,
    `not_available`.
    """
    text = text_from_document(stored_path)
    parse_result = payment_parsers.parse_text(text, metadata=metadata)
    best_text, best_parse = text, parse_result

    write_text_file(prepared_dir / "ocr.txt", text)

    if stored_path.suffix.casefold() not in IMAGE_OCR_EXTENSIONS:
        return best_text, best_parse, "non_image"
    if best_parse.get("status") == "parsed_ready" or not best_parse.get("missing_required_fields"):
        return best_text, best_parse, "not_needed"

    rescue_attempts = (
        ("upscaled", lambda: text_from_upscaled_image(stored_path)),
        ("vision", lambda: text_from_vision_ocr(stored_path)),
    )
    used: list[str] = []
    for engine, fetch in rescue_attempts:
        rescue_text = fetch()
        if not rescue_text.strip():
            continue
        write_text_file(prepared_dir / f"ocr_{engine}.txt", rescue_text)
        rescue_parse = payment_parsers.parse_text(rescue_text, metadata=metadata)
        if _parse_is_better(rescue_parse, best_parse):
            best_text, best_parse = rescue_text, rescue_parse
            used.append(engine)
            if best_parse.get("status") == "parsed_ready" and not best_parse.get("missing_required_fields"):
                break

    if used:
        return best_text, best_parse, "used:" + "+".join(used)
    # We tried at least one rescue but none improved the parse.
    if any(
        (prepared_dir / f"ocr_{engine}.txt").exists()
        for engine, _ in rescue_attempts
    ):
        return best_text, best_parse, "not_better"
    return best_text, best_parse, "not_available"


def image_size_from_sips(path: Path) -> tuple[int, int]:
    if not shutil.which("sips"):
        return 0, 0
    output = run_text_command(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)])
    width_match = re.search(r"pixelWidth:\s*(\d+)", output)
    height_match = re.search(r"pixelHeight:\s*(\d+)", output)
    if not width_match or not height_match:
        return 0, 0
    return int(width_match.group(1)), int(height_match.group(1))


def text_from_jpeg_bottom_crop(path: Path) -> str:
    if path.suffix.casefold() not in {".jpg", ".jpeg"}:
        return ""
    if not shutil.which("jpegtran"):
        return ""
    width, height = image_size_from_sips(path)
    if width <= 0 or height <= 0:
        return ""
    crop_height = max(320, int(height * 0.42))
    crop_height = min(crop_height, height)
    crop_y = max(0, height - crop_height)
    with tempfile.TemporaryDirectory(prefix="payment_ocr_") as tmpdir:
        crop_path = Path(tmpdir) / "bottom.jpg"
        try:
            with crop_path.open("wb") as output:
                result = subprocess.run(
                    ["jpegtran", "-crop", f"{width}x{crop_height}+0+{crop_y}", str(path)],
                    check=False,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    timeout=30,
                )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if result.returncode != 0 or not crop_path.exists():
            return ""
        texts = [
            run_text_command(["tesseract", str(crop_path), "stdout", "-l", "rus+eng", "--psm", mode])
            for mode in ("11", "6")
        ]
    return "\n".join(text for text in texts if text.strip())


def apply_payment_overrides(raw: dict[str, Any], args: argparse.Namespace) -> None:
    overrides = {
        "documentNumber": getattr(args, "document_number", ""),
        "amount": getattr(args, "amount", ""),
        "recipientName": getattr(args, "recipient_name", ""),
        "inn": getattr(args, "inn", ""),
        "kpp": getattr(args, "kpp", ""),
        "bankAcnt": getattr(args, "bank_acnt", ""),
        "bankBik": getattr(args, "bank_bik", ""),
        "recipientBankName": getattr(args, "recipient_bank_name", ""),
        "recipientCorrAccountNumber": getattr(args, "recipient_corr_account", ""),
        "accountNumber": getattr(args, "account_number", ""),
        "paymentPurpose": getattr(args, "payment_purpose", ""),
        "documentId": getattr(args, "document_id", ""),
        "date": getattr(args, "execution_date", ""),
        "sourceDocumentNumber": getattr(args, "source_document_number", ""),
        "sourceDocumentDate": getattr(args, "source_document_date", ""),
    }
    for key, value in overrides.items():
        if value not in (None, ""):
            raw[key] = value

    if getattr(args, "execution_order", None):
        raw["executionOrder"] = args.execution_order


def build_raw_request_from_args(args: argparse.Namespace) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    if getattr(args, "input_json", ""):
        raw.update(unwrap_payment_request(load_json(resolve_project_path(args.input_json))))

    text = ""
    if getattr(args, "text_file", ""):
        text = resolve_project_path(args.text_file).read_text(encoding="utf-8", errors="replace")
    elif getattr(args, "document_file", ""):
        text = text_from_document(resolve_project_path(args.document_file))
    if text:
        metadata: dict[str, Any] = {}
        if getattr(args, "text_file", ""):
            metadata["source_file"] = rel(resolve_project_path(args.text_file))
        elif getattr(args, "document_file", ""):
            metadata["source_file"] = rel(resolve_project_path(args.document_file))
        parse_result = payment_parsers.parse_text(text, metadata=metadata)
        extracted = payment_parsers.payment_basis_to_raw_payment(parse_result["payment_basis"])
        extracted["recognition"] = {
            "parser_name": parse_result["payment_basis"].get("parser_name"),
            "source_type": parse_result["payment_basis"].get("source_type"),
            "confidence": parse_result["payment_basis"].get("recognition_confidence"),
            "missing_fields": parse_result.get("missing_required_fields", []),
            "requires_owner_review": parse_result.get("requires_owner_review", False),
            "owner_review_reasons": parse_result.get("owner_review_reasons", []),
        }
        extracted["intake_status"] = parse_result.get("status", "")
        extracted["intake_requires_owner_review"] = parse_result.get("requires_owner_review", False)
        if getattr(args, "intake_db", ""):
            ids = intake_store(args).save_parse_result(parse_result, text=text, metadata=metadata)
            extracted["parsed_id"] = ids["parsed_id"]
            extracted["candidate_id"] = ids["candidate_id"]
            extracted["intake_status"] = ids["status"]
            if ids["status"] in {"owner_review", "low_confidence", "duplicate"}:
                extracted["intake_requires_owner_review"] = True
        raw.update({key: value for key, value in extracted.items() if value not in (None, "")})

    apply_payment_overrides(raw, args)

    return raw


def payment_draft_api_payload(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = set(REQUIRED_PAYMENT_FIELDS) | {"date", "recipientCorrAccountNumber", "revenueTypeCode"}
    return {key: payload[key] for key in allowed if payload.get(key) not in (None, "")}


def payment_amount_string(value: Any) -> str:
    amount = money(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(amount, "f")


def ensure_document_id(payload: dict[str, Any]) -> str:
    value = str(payload.get("documentId") or "").strip()
    if value:
        return value[:64]
    seed = "|".join(
        str(payload.get(key, ""))
        for key in ("documentNumber", "amount", "inn", "bankAcnt", "paymentPurpose")
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"pay-{dt.datetime.now().strftime('%Y%m%d%H%M%S')}-{digest}"[:64]


def payment_work_queue_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Map the normalized payment to T-Bank payment-order submit schema.

    This is the endpoint family that can put the order into "Платежи в работе".
    Non-tax counterparty payments use zeros for UIP/UIN-like fields.
    """

    order: dict[str, Any] = {
        "documentId": ensure_document_id(payload),
        "payerAccountNumber": str(payload["accountNumber"]),
        "payeePersonName": str(payload["recipientName"]),
        "payeePersonInn": str(payload["inn"]),
        "payeePersonKpp": str(payload["kpp"]),
        "payeePersonBankBic": str(payload["bankBik"]),
        "payeePersonAccountNumber": str(payload["bankAcnt"]),
        "paymentPurpose": str(payload["paymentPurpose"]),
        "paymentAmount": payment_amount_string(payload["amount"]),
        "documentNumber": int(str(payload["documentNumber"])),
        "paymentExecutionOrder": int(payload.get("executionOrder") or 5),
        "isExpressPayment": bool(payload.get("isExpressPayment", False)),
        "budgetPaymentUin": str(payload.get("uin") or "0"),
        "counterpartyUip": str(payload.get("counterpartyUip") or "0"),
    }
    if payload.get("date"):
        order["paymentExecutionDate"] = payload["date"]
    if payload.get("recipientBankName"):
        order["payeePersonBankName"] = payload["recipientBankName"]
    if payload.get("recipientCorrAccountNumber"):
        order["payeePersonBankCorrespondentAccount"] = payload["recipientCorrAccountNumber"]
    if payload.get("revenueTypeCode"):
        try:
            order["revenueTypeCode"] = int(str(payload["revenueTypeCode"]))
        except ValueError:
            order["revenueTypeCode"] = payload["revenueTypeCode"]
    return order


def request_file_payload(payload: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    stamp = dt.datetime.now().replace(microsecond=0).isoformat()
    validation_errors = validate_payment_payload(payload)
    intake_status = str(payload.get("intake_status") or "")
    requires_owner_review = bool(payload.get("intake_requires_owner_review"))
    if intake_status in {"owner_review", "low_confidence", "duplicate"}:
        requires_owner_review = True
    metadata: dict[str, Any] = {
        "created_at": stamp,
        "source": "research/scripts/tbank/payment_order.py",
        "target_ui_bucket": "Платежи в работе -> На подпись",
        "api_endpoint": f"POST {PAYMENT_ORDER_SUBMIT_PATH}",
        "fallback_draft_endpoint": f"POST {PAYMENT_DRAFT_PATH}",
        "note": (
            "Private payment order payload. The target API requires H2H mTLS and "
            "cryptographic signature. Owner signature behavior depends on T-Business signing rules."
        ),
    }
    for attr in ("input_json", "text_file", "document_file"):
        value = getattr(args, attr, "")
        if value:
            metadata[attr] = rel(resolve_project_path(value))
    if isinstance(payload.get("recognition"), dict):
        metadata["recognition"] = payload["recognition"]
    parsed_id = getattr(args, "parsed_id", "") or payload.get("parsed_id")
    candidate_id = getattr(args, "candidate_id", "") or payload.get("candidate_id")
    if parsed_id:
        metadata["parsed_id"] = parsed_id
    if candidate_id:
        metadata["candidate_id"] = candidate_id
    if intake_status:
        metadata["intake_status"] = intake_status
    metadata["requires_owner_review"] = requires_owner_review
    metadata["validation_errors"] = validation_errors
    if requires_owner_review:
        metadata["bank_payload_status"] = "blocked_owner_review"
    elif validation_errors:
        metadata["bank_payload_status"] = "blocked_validation"
    else:
        metadata["bank_payload_status"] = "ready_for_bank_signature"

    prepared: dict[str, Any] = {
        "metadata": metadata,
        "payment": payload,
    }
    if not validation_errors and not requires_owner_review:
        prepared["work_queue_payload"] = payment_work_queue_payload(payload)
        prepared["draft_api_payload"] = payment_draft_api_payload(payload)
    return prepared


def raw_payment_from_parsed_id(args: argparse.Namespace) -> dict[str, Any]:
    store = intake_store(args)
    try:
        parsed = store.get_parsed(args.parsed_id)
    except KeyError as exc:
        raise SystemExit(str(exc)) from exc
    basis = dict(parsed["payment_basis"])
    raw = payment_parsers.payment_basis_to_raw_payment(basis)
    raw["parsed_id"] = args.parsed_id
    raw["intake_status"] = parsed.get("status", "")
    raw["intake_requires_owner_review"] = bool(parsed.get("requires_owner_review"))
    raw["recognition"] = {
        "parser_name": basis.get("parser_name"),
        "source_type": basis.get("source_type"),
        "confidence": basis.get("recognition_confidence"),
        "missing_fields": payment_parsers.missing_ready_fields(basis),
        "requires_owner_review": bool(parsed.get("requires_owner_review")),
        "owner_review_reasons": basis.get("owner_review_reasons", []),
    }
    candidate = store.get_candidate_by_parsed_id(args.parsed_id)
    if candidate:
        raw["candidate_id"] = candidate["candidate_id"]
    apply_payment_overrides(raw, args)
    return raw


def prepare_raw_payment(raw: dict[str, Any], args: argparse.Namespace) -> tuple[int, Path, dict[str, Any], list[str]]:
    if not raw:
        raise SystemExit("Provide --input-json, --text-file, --document-file, or manual payment fields.")

    if not raw.get("documentNumber"):
        raw["documentNumber"] = allocate_document_number()

    payload = normalize_payment_payload(raw)
    errors = validate_payment_payload(payload)
    output_dir = resolve_project_path(args.output_dir, DEFAULT_REQUEST_DIR)
    request_id = str(uuid.uuid4())[:8]
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"payment_order_request_{stamp}_{request_id}.json"
    write_json(output_path, request_file_payload(payload, args))
    prepared = load_json(output_path)
    metadata = prepared.get("metadata", {}) if isinstance(prepared, dict) else {}
    requires_owner_review = bool(metadata.get("requires_owner_review"))

    print(f"request_file={rel(output_path)}")
    print("target_ui_bucket=payments_in_work_to_signature")
    print(f"payment_order_number={payload.get('documentNumber')}")
    amount_text = fmt_money(payload.get("amount")) if payload.get("amount") not in (None, "") else ""
    print(f"amount={amount_text}")
    print(f"api_amount={amount_text}")
    print(f"dds_amount={amount_text}")
    print(f"recipient={sanitize_payload({'recipientName': payload.get('recipientName')})['recipientName']}")
    print(f"recipient_inn={mask_inn(payload.get('inn'))}")
    print(f"recipient_account={mask_account(payload.get('bankAcnt'))}")
    print(f"debit_account={mask_account(payload.get('accountNumber'))}")
    if isinstance(payload.get("recognition"), dict):
        recognition = payload["recognition"]
        print(f"recognition_confidence={recognition.get('confidence')}")
        print(f"recognition_parser={recognition.get('parser_name')}")
        print(f"recognition_missing={','.join(recognition.get('missing_fields', [])) or 'none'}")
    if payload.get("intake_status"):
        print(f"intake_status={payload.get('intake_status')}")
    print(f"requires_owner_review={str(requires_owner_review).lower()}")

    if errors:
        print("validation_status=failed")
        for error in errors:
            print(f"validation_error={error}")
        return 2, output_path, payload, errors

    if requires_owner_review:
        print("validation_status=owner_review")
        print("bank_submit=blocked_owner_review")
        return 0, output_path, payload, errors

    print("validation_status=ok")
    print("bank_submit=not_requested")
    return 0, output_path, payload, errors


def prepare_payment(args: argparse.Namespace) -> int:
    raw = raw_payment_from_parsed_id(args) if getattr(args, "parsed_id", "") else build_raw_request_from_args(args)
    result, output_path, payload, errors = prepare_raw_payment(raw, args)
    parsed_id = getattr(args, "parsed_id", "") or payload.get("parsed_id", "")
    candidate_id = getattr(args, "candidate_id", "") or payload.get("candidate_id", "")
    if parsed_id or candidate_id:
        metadata = load_json(output_path).get("metadata", {})
        status = "ready_for_bank_signature"
        requires_owner_review = bool(metadata.get("requires_owner_review")) or bool(errors)
        if metadata.get("intake_status") in {"owner_review", "low_confidence", "duplicate"}:
            status = str(metadata.get("intake_status"))
        elif requires_owner_review:
            status = "owner_review"
        intake_store(args).mark_candidate_request(
            candidate_id=candidate_id or None,
            parsed_id=parsed_id or None,
            request_file=rel(output_path),
            status=status,
            requires_owner_review=requires_owner_review,
        )
    return result


def upload_file_metadata(args: argparse.Namespace, source_path: Path, stored_path: Path) -> dict[str, Any]:
    return {
        "upload_id": stored_path.parent.name,
        "created_at": dt.datetime.now().replace(microsecond=0).isoformat(),
        "source": "research/scripts/tbank/payment_order.py upload",
        "source_channel": args.source_channel,
        "sender": args.sender,
        "chat_id": args.chat_id,
        "message_id": args.message_id,
        "caption": args.caption,
        "original_file_name": source_path.name,
        "stored_file": rel(stored_path),
        "mime_type": mimetypes.guess_type(str(stored_path))[0] or "application/octet-stream",
        "target_ui_bucket": "Платежи в работе -> На подпись",
    }


def prepare_args_for_uploaded_file(
    args: argparse.Namespace,
    stored_path: Path,
    output_dir: Path,
    parsed_id: str = "",
    candidate_id: str = "",
) -> argparse.Namespace:
    return argparse.Namespace(
        input_json="",
        text_file="",
        document_file="" if parsed_id else str(stored_path),
        parsed_id=parsed_id,
        candidate_id=candidate_id,
        intake_db=args.intake_db,
        document_number=args.document_number,
        amount=args.amount,
        recipient_name=args.recipient_name,
        inn=args.inn,
        kpp=args.kpp,
        bank_acnt=args.bank_acnt,
        bank_bik=args.bank_bik,
        recipient_bank_name=args.recipient_bank_name,
        recipient_corr_account=args.recipient_corr_account,
        account_number=args.account_number,
        payment_purpose=args.payment_purpose,
        document_id=args.document_id,
        execution_order=args.execution_order,
        execution_date=args.execution_date,
        source_document_number=args.source_document_number,
        source_document_date=args.source_document_date,
        output_dir=str(output_dir),
    )


def upload_document(args: argparse.Namespace) -> int:
    upload_root = resolve_project_path(args.upload_dir, DEFAULT_UPLOAD_DIR)
    uploaded_count = 0
    prepared_count = 0
    failed_count = 0

    for file_value in args.file:
        source_path = resolve_project_path(file_value)
        if not source_path.exists() or not source_path.is_file():
            print(f"upload_error=file_not_found:{source_path}")
            failed_count += 1
            continue
        if source_path.suffix.casefold() not in SUPPORTED_UPLOAD_EXTENSIONS:
            print(f"upload_error=unsupported_extension:{source_path.suffix}")
            failed_count += 1
            continue

        upload_id = f"{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        upload_dir = upload_root / upload_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        stored_name = safe_filename(source_path.name, f"upload{source_path.suffix.casefold()}")
        stored_path = upload_dir / stored_name
        shutil.copy2(source_path, stored_path)

        metadata = upload_file_metadata(args, source_path, stored_path)
        metadata["source_file_sha256"] = payment_parsers.file_sha256(stored_path)
        metadata_path = upload_dir / "upload_metadata.json"
        write_json(metadata_path, metadata)
        store = intake_store(args)
        store.save_upload(metadata)
        uploaded_count += 1

        print(f"upload_id={upload_id}")
        print(f"stored_file={rel(stored_path)}")
        print(f"metadata_file={rel(metadata_path)}")

        try:
            parse_metadata = {
                **metadata,
                "source_file": rel(stored_path),
                "upload_id": upload_id,
            }
            text, parse_result, rescue_status = extract_text_with_rescue(
                stored_path,
                upload_dir / "prepared",
                parse_metadata,
            )
            print(f"ocr_rescue={rescue_status}")
            ids = store.save_parse_result(
                parse_result,
                text=text,
                metadata=parse_metadata,
            )
        except SystemExit as exc:
            write_json(
                upload_dir / "parse_error.json",
                {
                    "upload_id": upload_id,
                    "stored_file": rel(stored_path),
                    "error": str(exc),
                },
            )
            print(f"parse_status=failed:{sanitize_text(str(exc))}")
            failed_count += 1
            continue

        print(f"parsed_id={ids['parsed_id']}")
        print(f"candidate_id={ids['candidate_id']}")
        print(f"parser_name={parse_result['payment_basis'].get('parser_name')}")
        print(f"recognition_confidence={parse_result['payment_basis'].get('recognition_confidence')}")
        print(f"candidate_status={ids['status']}")
        basis = parse_result["payment_basis"]
        if basis.get("parser_name") == "electricity_act_v1":
            print(f"electricity_period_amount={basis.get('electricity_period_amount') or ''}")
            print(f"electricity_paid_advance_amount={basis.get('electricity_paid_advance_amount') or ''}")
            print(f"electricity_amount_due={basis.get('electricity_amount_due') or ''}")
            print(f"electricity_payment_amount={basis.get('electricity_payment_amount') or ''}")

        if args.no_prepare:
            print("prepare_status=skipped")
            continue

        prepared_dir = upload_dir / "prepared"
        prepare_args = prepare_args_for_uploaded_file(
            args,
            stored_path,
            prepared_dir,
            parsed_id=ids["parsed_id"],
            candidate_id=ids["candidate_id"],
        )
        try:
            result = prepare_payment(prepare_args)
        except SystemExit as exc:
            write_json(
                upload_dir / "prepare_error.json",
                {
                    "upload_id": upload_id,
                    "stored_file": rel(stored_path),
                    "error": str(exc),
                },
            )
            print(f"prepare_status=failed:{sanitize_text(str(exc))}")
            failed_count += 1
            continue
        if result == 0:
            prepared_count += 1
            if parse_result.get("requires_owner_review"):
                print("prepare_status=owner_review")
            else:
                print("prepare_status=ok")
        elif parse_result.get("requires_owner_review"):
            prepared_count += 1
            print(f"prepare_status=owner_review:{result}")
        else:
            failed_count += 1
            print(f"prepare_status=failed:{result}")

    print(f"uploaded_count={uploaded_count}")
    print(f"prepared_count={prepared_count}")
    print(f"failed_count={failed_count}")
    return 1 if failed_count else 0


class TBankDraftClient:
    def __init__(self) -> None:
        self.base_url = (
            env_value("TBANK_API_PAYMENT_DRAFT_BASE_URL")
            or env_value("TBANK_API_BASE_URL")
            or DEFAULT_BASE_URL
        ).rstrip("/")
        self.access_token = required_env("TBANK_API_PAYMENT_DRAFT_TOKEN")
        self.timeout = float(
            env_value("TBANK_API_PAYMENT_DRAFT_TIMEOUT_SECONDS")
            or env_value("TBANK_API_TIMEOUT_SECONDS", "90")
            or 90
        )

    def post_json(self, path: str, payload: dict[str, Any]) -> tuple[int, Any, str, float]:
        url = f"{self.base_url}{path}"
        request_id = str(uuid.uuid4())
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Request-Id": request_id,
            },
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw_body = response.read()
                elapsed = time.perf_counter() - started
                return response.status, parse_json_body(raw_body), request_id, elapsed
        except urllib.error.HTTPError as exc:
            raw_body = exc.read()
            elapsed = time.perf_counter() - started
            raise TBankHTTPError(exc.code, raw_body, request_id, elapsed) from exc
        except urllib.error.URLError as exc:
            elapsed = time.perf_counter() - started
            raise TBankHTTPError(None, str(exc).encode("utf-8"), request_id, elapsed) from exc


class TBankPaymentOrderClient:
    def __init__(self) -> None:
        self.base_url = (
            env_value("TBANK_API_PAYMENT_ORDER_BASE_URL")
            or DEFAULT_ORDER_BASE_URL
        ).rstrip("/")
        self.access_token = required_env("TBANK_API_PAYMENT_ORDER_TOKEN")
        signature_key_id = env_value("TBANK_API_PAYMENT_ORDER_SIGNATURE_KEY_ID")
        signature_secret_raw = env_value("TBANK_API_PAYMENT_ORDER_SIGNATURE_SECRET")
        if not signature_key_id or not signature_secret_raw:
            raise SystemExit(
                "TBANK_API_PAYMENT_ORDER_SIGNATURE_KEY_ID and "
                "TBANK_API_PAYMENT_ORDER_SIGNATURE_SECRET are required for the "
                "payments-in-work submit endpoint."
            )
        self.signature_key_id = signature_key_id
        self.signature_secret = secret_bytes(
            signature_secret_raw,
            env_value("TBANK_API_PAYMENT_ORDER_SIGNATURE_SECRET_ENCODING", "raw") or "raw",
        )
        self.signature_headers = (
            env_value("TBANK_API_PAYMENT_ORDER_SIGNATURE_HEADERS", "request-target date data")
            or "request-target date data"
        ).split()
        self.data_encoding = env_value("TBANK_API_PAYMENT_ORDER_DATA_ENCODING", "base64") or "base64"
        self.timeout = float(
            env_value("TBANK_API_PAYMENT_ORDER_TIMEOUT_SECONDS")
            or env_value("TBANK_API_TIMEOUT_SECONDS", "90")
            or 90
        )
        self.context = self._ssl_context()

    def _ssl_context(self) -> ssl.SSLContext:
        ca_bundle = env_value("TBANK_API_PAYMENT_ORDER_CA_BUNDLE_PATH")
        cert_path = env_value("TBANK_API_PAYMENT_ORDER_TLS_CERT_PATH")
        key_path = env_value("TBANK_API_PAYMENT_ORDER_TLS_KEY_PATH")
        context = (
            ssl.create_default_context(cafile=str(resolve_project_path(ca_bundle)))
            if ca_bundle
            else ssl.create_default_context()
        )
        if not cert_path or not key_path:
            raise SystemExit(
                "TBANK_API_PAYMENT_ORDER_TLS_CERT_PATH and "
                "TBANK_API_PAYMENT_ORDER_TLS_KEY_PATH are required for the "
                "payments-in-work submit endpoint."
            )
        context.load_cert_chain(
            certfile=str(resolve_project_path(cert_path)),
            keyfile=str(resolve_project_path(key_path)),
        )
        return context

    def post_json(self, path: str, payload: Any) -> tuple[int, Any, str, float]:
        url = f"{self.base_url}{path}"
        request_id = str(uuid.uuid4())
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Request-Id": request_id,
        }
        if self.signature_key_id and self.signature_secret:
            date_value = rfc1123_now()
            data_value = hmac_digest(body, self.signature_secret, self.data_encoding)
            values = {
                "request-target": PAYMENT_ORDER_REQUEST_TARGET,
                "date": date_value,
                "data": data_value,
            }
            signature = build_signature_header(
                key_id=self.signature_key_id,
                secret=self.signature_secret,
                headers=self.signature_headers,
                values=values,
            )
            headers["date"] = date_value
            headers["data"] = data_value
            headers["Signature"] = signature
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers=headers,
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self.context
            ) as response:
                raw_body = response.read()
                elapsed = time.perf_counter() - started
                return response.status, parse_json_body(raw_body), request_id, elapsed
        except urllib.error.HTTPError as exc:
            raw_body = exc.read()
            elapsed = time.perf_counter() - started
            raise TBankHTTPError(exc.code, raw_body, request_id, elapsed) from exc
        except urllib.error.URLError as exc:
            elapsed = time.perf_counter() - started
            raise TBankHTTPError(None, str(exc).encode("utf-8"), request_id, elapsed) from exc


def check_env(_: argparse.Namespace) -> int:
    draft_base_url = env_value("TBANK_API_PAYMENT_DRAFT_BASE_URL") or env_value("TBANK_API_BASE_URL") or DEFAULT_BASE_URL
    order_base_url = env_value("TBANK_API_PAYMENT_ORDER_BASE_URL") or DEFAULT_ORDER_BASE_URL
    account = (
        env_value("TBANK_API_PAYMENT_ORDER_ACCOUNT_NUMBER")
        or env_value("TBANK_API_PAYMENT_DRAFT_ACCOUNT_NUMBER")
        or env_value("TBANK_API_ACCOUNT_NUMBER")
    )
    print("tbank_payment_order_env_ok=true")
    print(f"target_ui_bucket=payments_in_work_to_signature")
    print(f"order_base_url={order_base_url}")
    print(f"draft_base_url={draft_base_url}")
    print(f"payment_order_enabled={env_truthy('TBANK_API_PAYMENT_ORDER_ENABLED')}")
    print(f"payment_order_token={'set' if env_value('TBANK_API_PAYMENT_ORDER_TOKEN') else 'missing'}")
    print(
        "payment_order_mtls="
        f"{'set' if env_value('TBANK_API_PAYMENT_ORDER_TLS_CERT_PATH') and env_value('TBANK_API_PAYMENT_ORDER_TLS_KEY_PATH') else 'missing'}"
    )
    print(
        "payment_order_signature="
        f"{'set' if env_value('TBANK_API_PAYMENT_ORDER_SIGNATURE_KEY_ID') and env_value('TBANK_API_PAYMENT_ORDER_SIGNATURE_SECRET') else 'missing'}"
    )
    print(f"payment_draft_enabled={env_truthy('TBANK_API_PAYMENT_DRAFT_ENABLED')}")
    print(f"payment_draft_token={'set' if env_value('TBANK_API_PAYMENT_DRAFT_TOKEN') else 'missing'}")
    print(f"debit_account={mask_account(account) if account else 'missing'}")
    return 0


def read_prepared_payment(path: Path, *, target: str = "work_queue") -> dict[str, Any]:
    raw = load_json(path)
    if not isinstance(raw, dict):
        raise SystemExit("Prepared JSON must be an object")
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    if metadata:
        if metadata.get("requires_owner_review"):
            raise SystemExit("Prepared payment is blocked by owner review; resolve intake fields first.")
        if metadata.get("bank_payload_status") in {"blocked_owner_review", "blocked_validation"}:
            raise SystemExit(f"Prepared payment is not submit-ready: {metadata.get('bank_payload_status')}")
    if target == "work_queue" and isinstance(raw.get("work_queue_payload"), dict):
        return dict(raw["work_queue_payload"])
    if target == "draft" and isinstance(raw.get("draft_api_payload"), dict):
        return dict(raw["draft_api_payload"])

    if isinstance(raw.get("api_payload"), dict):
        payload = dict(raw["api_payload"])
    elif isinstance(raw.get("payment"), dict):
        payload = dict(raw["payment"])
    else:
        payload = unwrap_payment_request(raw)
    if not payload.get("documentNumber"):
        payload["documentNumber"] = allocate_document_number()
    payload = normalize_payment_payload(payload)
    errors = validate_payment_payload(payload)
    if errors:
        for error in errors:
            print(f"validation_error={error}")
        raise SystemExit("Prepared payment did not pass validation")
    if target == "draft":
        return payment_draft_api_payload(payload)
    return payment_work_queue_payload(payload)


def find_document_ids(payload: Any) -> list[str]:
    ids: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key.casefold() in {"documentid", "document_id"} and value:
                ids.append(str(value))
            else:
                ids.extend(find_document_ids(value))
    elif isinstance(payload, list):
        for item in payload:
            ids.extend(find_document_ids(item))
    return sorted(set(ids))


def submit_payment(args: argparse.Namespace) -> int:
    if not env_truthy("TBANK_API_PAYMENT_ORDER_ENABLED"):
        raise SystemExit(
            "TBANK_API_PAYMENT_ORDER_ENABLED must be true/1 in local .env before sending payment orders "
            "to 'Платежи в работе'."
        )

    input_path = resolve_project_path(args.input_json)
    payload = read_prepared_payment(input_path, target="work_queue")
    client = TBankPaymentOrderClient()
    output_dir = resolve_project_path(args.output_dir, DEFAULT_RESPONSE_DIR)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    response_path = output_dir / f"payment_order_submit_response_{stamp}.json"
    error_path = output_dir / f"payment_order_submit_error_{stamp}.json"

    # T-API expects an ARRAY of orders (up to 100 per request); wrap single
    # payment for batch transport.
    submit_payload = [payload] if isinstance(payload, dict) else payload
    try:
        status, response_payload, request_id, elapsed = client.post_json(PAYMENT_ORDER_SUBMIT_PATH, submit_payload)
    except TBankHTTPError as exc:
        write_json(
            error_path,
            {
                "endpoint": f"POST {PAYMENT_ORDER_SUBMIT_PATH}",
                "status": exc.status,
                "request_id": exc.request_id,
                "elapsed_seconds": round(exc.elapsed_seconds, 3),
                "input_file": rel(input_path),
                "api_payload_sanitized": [sanitize_payload(item) for item in submit_payload] if isinstance(submit_payload, list) else sanitize_payload(submit_payload),
                "error": parse_json_body(exc.body),
            },
        )
        print(f"status={exc.status}")
        print(f"elapsed_seconds={exc.elapsed_seconds:.3f}")
        print(f"request_id={exc.request_id}")
        print(f"error_file={rel(error_path)}")
        print(f"error={sanitize_text(exc.message)}")
        return 1

    write_json(
        response_path,
        {
            "endpoint": f"POST {PAYMENT_ORDER_SUBMIT_PATH}",
            "status": status,
            "request_id": request_id,
            "elapsed_seconds": round(elapsed, 3),
            "input_file": rel(input_path),
            "api_payload_sanitized": [sanitize_payload(item) for item in submit_payload] if isinstance(submit_payload, list) else sanitize_payload(submit_payload),
            "response": response_payload,
        },
    )
    document_ids = find_document_ids(response_payload)
    print(f"status={status}")
    print(f"elapsed_seconds={elapsed:.3f}")
    print(f"request_id={request_id}")
    print(f"response_file={rel(response_path)}")
    print(f"document_ids={','.join(document_ids) if document_ids else 'not_found_in_response'}")
    print("target_ui_bucket=payments_in_work_to_signature")
    print("owner_action=open_t_business_payments_in_work_to_signature")
    return 0


def create_draft_payment(args: argparse.Namespace) -> int:
    if not env_truthy("TBANK_API_PAYMENT_DRAFT_ENABLED"):
        raise SystemExit(
            "TBANK_API_PAYMENT_DRAFT_ENABLED must be true/1 in local .env before creating bank drafts."
        )

    input_path = resolve_project_path(args.input_json)
    payload = read_prepared_payment(input_path, target="draft")
    client = TBankDraftClient()
    output_dir = resolve_project_path(args.output_dir, DEFAULT_RESPONSE_DIR)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    response_path = output_dir / f"payment_draft_create_response_{stamp}.json"
    error_path = output_dir / f"payment_draft_create_error_{stamp}.json"

    try:
        status, response_payload, request_id, elapsed = client.post_json(PAYMENT_DRAFT_PATH, payload)
    except TBankHTTPError as exc:
        write_json(
            error_path,
            {
                "endpoint": f"POST {PAYMENT_DRAFT_PATH}",
                "status": exc.status,
                "request_id": exc.request_id,
                "elapsed_seconds": round(exc.elapsed_seconds, 3),
                "input_file": rel(input_path),
                "api_payload_sanitized": sanitize_payload(payload),
                "error": parse_json_body(exc.body),
            },
        )
        print(f"status={exc.status}")
        print(f"elapsed_seconds={exc.elapsed_seconds:.3f}")
        print(f"request_id={exc.request_id}")
        print(f"error_file={rel(error_path)}")
        print(f"error={sanitize_text(exc.message)}")
        return 1

    write_json(
        response_path,
        {
            "endpoint": f"POST {PAYMENT_DRAFT_PATH}",
            "status": status,
            "request_id": request_id,
            "elapsed_seconds": round(elapsed, 3),
            "input_file": rel(input_path),
            "api_payload_sanitized": sanitize_payload(payload),
            "response": response_payload,
        },
    )
    document_ids = find_document_ids(response_payload)
    print(f"status={status}")
    print(f"elapsed_seconds={elapsed:.3f}")
    print(f"request_id={request_id}")
    print(f"response_file={rel(response_path)}")
    print(f"document_ids={','.join(document_ids) if document_ids else 'not_found_in_response'}")
    print("target_ui_bucket=drafts_payments")
    return 0


def status_payload_from_args(args: argparse.Namespace) -> dict[str, Any]:
    document_ids = list(args.document_id or [])
    if args.input_json:
        raw = load_json(resolve_project_path(args.input_json))
        document_ids.extend(find_document_ids(raw))
    document_ids = sorted(set(str(value) for value in document_ids if value))
    if not document_ids:
        raise SystemExit("Provide --document-id or --input-json with documentId values")
    return {"documentIds": document_ids}


def check_status(args: argparse.Namespace) -> int:
    if not env_truthy("TBANK_API_PAYMENT_DRAFT_ENABLED"):
        raise SystemExit(
            "TBANK_API_PAYMENT_DRAFT_ENABLED must be true/1 in local .env before calling draft status API."
        )
    payload = status_payload_from_args(args)
    client = TBankDraftClient()
    output_dir = resolve_project_path(args.output_dir, DEFAULT_RESPONSE_DIR)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    response_path = output_dir / f"payment_status_response_{stamp}.json"

    try:
        status, response_payload, request_id, elapsed = client.post_json("/api/v1/payment/status", payload)
    except TBankHTTPError as exc:
        error_path = output_dir / f"payment_status_error_{stamp}.json"
        write_json(
            error_path,
            {
                "endpoint": "POST /api/v1/payment/status",
                "status": exc.status,
                "request_id": exc.request_id,
                "elapsed_seconds": round(exc.elapsed_seconds, 3),
                "payload": payload,
                "error": parse_json_body(exc.body),
            },
        )
        print(f"status={exc.status}")
        print(f"request_id={exc.request_id}")
        print(f"error_file={rel(error_path)}")
        print(f"error={sanitize_text(exc.message)}")
        return 1

    write_json(
        response_path,
        {
            "endpoint": "POST /api/v1/payment/status",
            "status": status,
            "request_id": request_id,
            "elapsed_seconds": round(elapsed, 3),
            "request_payload": payload,
            "response": response_payload,
        },
    )
    print(f"status={status}")
    print(f"elapsed_seconds={elapsed:.3f}")
    print(f"request_id={request_id}")
    print(f"response_file={rel(response_path)}")
    return 0


def print_parse_result(ids: dict[str, str], parse_result: dict[str, Any]) -> None:
    basis = parse_result["payment_basis"]
    print(f"parsed_id={ids['parsed_id']}")
    print(f"candidate_id={ids['candidate_id']}")
    print(f"candidate_status={ids['status']}")
    print(f"parser_name={basis.get('parser_name')}")
    print(f"source_type={basis.get('source_type')}")
    print(f"recognition_confidence={basis.get('recognition_confidence')}")
    print(f"requires_owner_review={str(parse_result.get('requires_owner_review', False)).lower()}")
    if parse_result.get("owner_review_reasons"):
        print(f"owner_review_reasons={','.join(parse_result['owner_review_reasons'])}")
    print(f"amount={basis.get('amount') or ''}")
    print(f"recipient={sanitize_payload({'recipientName': basis.get('counterparty_name')})['recipientName']}")
    print(f"recipient_inn={mask_inn(basis.get('inn')) if basis.get('inn') else ''}")
    print(f"recipient_account={mask_account(basis.get('bank_account')) if basis.get('bank_account') else ''}")


def parse_file(args: argparse.Namespace) -> int:
    input_path = resolve_project_path(args.file)
    if not input_path.exists() or not input_path.is_file():
        raise SystemExit(f"File not found: {input_path}")
    text = text_from_document(input_path)
    metadata = {
        "source_file": rel(input_path),
        "original_file_name": input_path.name,
        "source_file_sha256": payment_parsers.file_sha256(input_path),
    }
    parse_result = payment_parsers.parse_text(text, metadata=metadata)
    ids = intake_store(args).save_parse_result(parse_result, text=text, metadata=metadata)
    print_parse_result(ids, parse_result)
    return 0


def upload_metadata_from_disk(upload_id: str) -> dict[str, Any]:
    upload_dir = DEFAULT_UPLOAD_DIR / upload_id
    metadata_path = upload_dir / "upload_metadata.json"
    if not metadata_path.exists():
        raise SystemExit(f"Upload metadata not found for upload_id={upload_id}")
    return load_json(metadata_path)


def parse_upload(args: argparse.Namespace) -> int:
    store = intake_store(args)
    upload = store.get_upload(args.upload_id)
    metadata = dict(upload["metadata"]) if upload else upload_metadata_from_disk(args.upload_id)
    stored_file = metadata.get("stored_file") or upload.get("stored_file") if upload else metadata.get("stored_file")
    if not stored_file:
        raise SystemExit(f"Stored file not found in upload metadata for upload_id={args.upload_id}")
    stored_path = resolve_project_path(str(stored_file))
    if not stored_path.exists():
        raise SystemExit(f"Stored file does not exist: {stored_path}")
    metadata["upload_id"] = args.upload_id
    metadata["source_file"] = rel(stored_path)
    metadata.setdefault("source_file_sha256", payment_parsers.file_sha256(stored_path))
    store.save_upload(metadata)
    upload_dir = stored_path.parent
    text, parse_result, rescue_status = extract_text_with_rescue(
        stored_path,
        upload_dir / "prepared",
        metadata,
    )
    print(f"ocr_rescue={rescue_status}")
    ids = store.save_parse_result(parse_result, text=text, metadata=metadata)
    print_parse_result(ids, parse_result)
    return 0


def list_candidates(args: argparse.Namespace) -> int:
    rows = intake_store(args).list_candidates(status=args.status or "", limit=args.limit)
    print(f"candidate_count={len(rows)}")
    for row in rows:
        print(
            "candidate "
            f"id={row['candidate_id']} "
            f"parsed_id={row['parsed_id']} "
            f"status={row['status']} "
            f"amount={row.get('amount') or ''} "
            f"recipient={sanitize_payload({'recipientName': row.get('counterparty_name')})['recipientName']} "
            f"inn={mask_inn(row.get('inn')) if row.get('inn') else ''} "
            f"account={mask_account(row.get('bank_account')) if row.get('bank_account') else ''} "
            f"request_file={row.get('request_file') or ''}"
        )
    return 0


def raw_payment_from_candidate_id(args: argparse.Namespace) -> dict[str, Any]:
    store = intake_store(args)
    try:
        candidate = store.get_candidate(args.id)
    except KeyError as exc:
        raise SystemExit(str(exc)) from exc
    basis = dict(candidate["payment_basis"])
    raw = payment_parsers.payment_basis_to_raw_payment(basis)
    raw["candidate_id"] = args.id
    raw["parsed_id"] = candidate["parsed_id"]
    raw["intake_status"] = candidate.get("status", "")
    raw["intake_requires_owner_review"] = bool(candidate.get("requires_owner_review"))
    raw["recognition"] = {
        "parser_name": basis.get("parser_name"),
        "source_type": basis.get("source_type"),
        "confidence": basis.get("recognition_confidence"),
        "missing_fields": payment_parsers.missing_ready_fields(basis),
        "requires_owner_review": bool(candidate.get("requires_owner_review")),
        "owner_review_reasons": basis.get("owner_review_reasons", []),
    }
    apply_payment_overrides(raw, args)
    return raw


def export_candidate(args: argparse.Namespace) -> int:
    raw = raw_payment_from_candidate_id(args)
    result, output_path, payload, errors = prepare_raw_payment(raw, args)
    metadata = load_json(output_path).get("metadata", {})
    requires_owner_review = bool(metadata.get("requires_owner_review")) or bool(errors)
    status = "ready_for_bank_signature"
    if metadata.get("intake_status") in {"owner_review", "low_confidence", "duplicate"}:
        status = str(metadata.get("intake_status"))
    elif requires_owner_review:
        status = "owner_review"
    intake_store(args).mark_candidate_request(
        candidate_id=args.id,
        request_file=rel(output_path),
        status=status,
        requires_owner_review=requires_owner_review,
    )
    return result


def add_payment_override_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--document-number", help="Payment order number, 1-6 digits. Auto-allocated if omitted.")
    parser.add_argument("--amount", help="Payment amount in RUB")
    parser.add_argument("--recipient-name")
    parser.add_argument("--inn", help="Recipient INN")
    parser.add_argument("--kpp", help="Recipient KPP or 0")
    parser.add_argument("--bank-acnt", help="Recipient settlement account")
    parser.add_argument("--bank-bik", help="Recipient bank BIK")
    parser.add_argument("--recipient-bank-name")
    parser.add_argument("--recipient-corr-account")
    parser.add_argument("--account-number", help="Debit account in T-Bank")
    parser.add_argument("--payment-purpose")
    parser.add_argument("--document-id", help="External payment id for H2H payment-order submit")
    parser.add_argument("--execution-order", type=int, default=5)
    parser.add_argument("--execution-date", help="Optional execution date: YYYY-MM-DD or ISO date-time")
    parser.add_argument("--source-document-number")
    parser.add_argument("--source-document-date")


def add_common_prepare_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input-json", help="JSON with payment fields or a prepared request")
    parser.add_argument("--text-file", help="Text already extracted from invoice/receipt/payment order")
    parser.add_argument("--document-file", help="PDF/image/text document; PDF/image extraction uses local tools if installed")
    parser.add_argument("--parsed-id", help="Use a parsed document from the private intake SQLite database")
    parser.add_argument("--intake-db", default=str(DEFAULT_INTAKE_DB), help="Private SQLite intake database")
    add_payment_override_args(parser)
    parser.add_argument("--output-dir", default=str(DEFAULT_REQUEST_DIR))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare T-Bank payment orders for signature")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check-env", help="Show sanitized payment-order env status")
    check.set_defaults(func=check_env)

    prepare = subparsers.add_parser("prepare", help="Prepare and validate a payment order without bank call")
    add_common_prepare_args(prepare)
    prepare.set_defaults(func=prepare_payment)

    upload = subparsers.add_parser(
        "upload",
        help="Copy uploaded documents into the private inbox and prepare payment orders",
    )
    upload.add_argument("--file", action="append", required=True, help="Uploaded PDF/image/text file")
    upload.add_argument("--upload-dir", default=str(DEFAULT_UPLOAD_DIR))
    upload.add_argument("--source-channel", default="manual")
    upload.add_argument("--sender", default="")
    upload.add_argument("--chat-id", default="")
    upload.add_argument("--message-id", default="")
    upload.add_argument("--caption", default="")
    upload.add_argument("--no-prepare", action="store_true")
    upload.add_argument("--intake-db", default=str(DEFAULT_INTAKE_DB))
    add_payment_override_args(upload)
    upload.set_defaults(func=upload_document)

    parse = subparsers.add_parser("parse", help="Parse a single document and store payment_basis in intake DB")
    parse.add_argument("--file", required=True, help="PDF/image/text document to parse")
    parse.add_argument("--intake-db", default=str(DEFAULT_INTAKE_DB))
    parse.set_defaults(func=parse_file)

    parse_up = subparsers.add_parser("parse-upload", help="Parse an already saved private upload by upload_id")
    parse_up.add_argument("--upload-id", required=True)
    parse_up.add_argument("--intake-db", default=str(DEFAULT_INTAKE_DB))
    parse_up.set_defaults(func=parse_upload)

    list_cmd = subparsers.add_parser("list-candidates", help="List parsed payment-order candidates from intake DB")
    list_cmd.add_argument("--status", default="", help="Filter: owner_review, ready, parsed_ready, low_confidence, duplicate")
    list_cmd.add_argument("--limit", type=int, default=50)
    list_cmd.add_argument("--intake-db", default=str(DEFAULT_INTAKE_DB))
    list_cmd.set_defaults(func=list_candidates)

    export = subparsers.add_parser("export-candidate", help="Export a candidate to a prepared payment request JSON")
    export.add_argument("--id", required=True, help="Candidate id")
    export.add_argument("--intake-db", default=str(DEFAULT_INTAKE_DB))
    export.add_argument("--output-dir", default=str(DEFAULT_REQUEST_DIR))
    add_payment_override_args(export)
    export.set_defaults(func=export_candidate)

    submit = subparsers.add_parser(
        "submit",
        help="Send a prepared payment order to 'Платежи в работе' through H2H/mTLS/signature API",
    )
    submit.add_argument("--input-json", required=True, help="Prepared JSON produced by the prepare command")
    submit.add_argument("--output-dir", default=str(DEFAULT_RESPONSE_DIR))
    submit.set_defaults(func=submit_payment)

    draft = subparsers.add_parser(
        "create-draft",
        help="Fallback: create a regular T-Bank draft under 'Черновики -> Платежи'",
    )
    draft.add_argument("--input-json", required=True, help="Prepared JSON produced by the prepare command")
    draft.add_argument("--output-dir", default=str(DEFAULT_RESPONSE_DIR))
    draft.set_defaults(func=create_draft_payment)

    status = subparsers.add_parser("status", help="Check draft document status in T-Bank")
    status.add_argument("--document-id", action="append", default=[])
    status.add_argument("--input-json", help="Response JSON containing documentId values")
    status.add_argument("--output-dir", default=str(DEFAULT_RESPONSE_DIR))
    status.set_defaults(func=check_status)

    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    load_local_env()
    args = parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
