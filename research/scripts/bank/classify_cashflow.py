#!/usr/bin/env python3
"""Classify local Sber and T-Bank statements into management cash-flow aggregates.

This script is intentionally local-only: it reads raw files from research/private/,
writes row-level classified statements back to research/private/, and publishes only
aggregate CSV/Markdown files under research/processed/cashflow/.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
PRIVATE_DIR = PROJECT_ROOT / "research/private"
PROCESSED_DIR = PROJECT_ROOT / "research/processed/cashflow"
SBER_RAW_DIR = PROJECT_ROOT / "research/private/sber/statement"
TBANK_RAW_FILE = PROJECT_ROOT / "research/private/tbank/statement_2026-02-01_2026-05-31_p01.json"
RULE_TEMPLATE = PROJECT_ROOT / "research/processed/bank_operation_rules_template.csv"
IIKO_REVENUE_FILE = PROJECT_ROOT / "research/processed/economic_block/iiko_monthly_gross_margin.csv"
SBER_OLD_RECON_FILE = PROJECT_ROOT / "research/processed/sber/bank_iiko_revenue_reconciliation.csv"

START_DATE = dt.date(2026, 2, 1)
END_DATE = dt.date(2026, 5, 19)
MAY_RECON_END = dt.date(2026, 5, 17)

Money = Decimal

SUPPLIER_OVERRIDES = [
    {
        "legal_contains": "тора",
        "working_name": "Амай",
        "dds_article": "Оплата поставщикам",
        "pnl_line": "",
        "note": "owner_speech_2026_05_19; DDS counterparty Амай; food/product supplier cash-flow article",
    },
    {
        "legal_contains": "альянс юг",
        "working_name": "Альянс Юг",
        "dds_article": "Оплата поставщикам",
        "pnl_line": "",
        "note": "owner_speech_2026_05_19; DDS article from matching counterparty",
    },
    {
        "legal_contains": "мяснофф-дон",
        "working_name": "Мяснов",
        "dds_article": "Оплата поставщикам",
        "pnl_line": "",
        "note": "owner_speech_2026_05_19; DDS counterparty Мяснов",
    },
    {
        "legal_contains": "мистерия",
        "working_name": "Мистерия",
        "dds_article": "Оплата поставщикам",
        "pnl_line": "",
        "note": "owner_speech_2026_05_19; DDS article from matching counterparty",
    },
    {
        "legal_contains": "о. о",
        "working_name": "Синапс",
        "dds_article": "Контекстная реклама",
        "pnl_line": "Контекстная реклама",
        "note": "owner_speech_2026_05_19; Yandex context/search ads setup",
    },
    {
        "legal_contains": "метро кэш энд керри",
        "working_name": "Метро",
        "dds_article": "Оплата поставщикам",
        "pnl_line": "",
        "note": "owner_speech_2026_05_19; food supplier",
    },
    {
        "legal_contains": "айко",
        "working_name": "iiko",
        "dds_article": "Оплаты систем автоматизации",
        "pnl_line": "Оплата систем автоматизации",
        "note": "DDS article from matching counterparty",
    },
    {
        "legal_contains": "ревви",
        "working_name": "Ревви",
        "dds_article": "Оплаты систем автоматизации",
        "pnl_line": "Оплата систем автоматизации",
        "note": "owner_speech_2026_05_19; feedback/reviews system",
    },
    {
        "legal_contains": "доксинбокс",
        "working_name": "Доксинбокс",
        "dds_article": "Оплаты систем автоматизации",
        "pnl_line": "Оплата систем автоматизации",
        "note": "owner_speech_2026_05_19; DDS article description includes DocsInbox",
    },
    {
        "legal_contains": "синапсис",
        "working_name": "Синопсис",
        "dds_article": "SEO-оптимизация",
        "pnl_line": "SEO - оптимизация",
        "note": "owner_speech_2026_05_19; DDS currently uses ИП Трубина И.О and should be renamed",
    },
    {
        "legal_contains": "экоцентр",
        "working_name": "Экоцентр",
        "dds_article": "Аренда торговых точек",
        "pnl_line": "Аренда торговой точки Черникова",
        "note": "owner_speech_2026_05_19; garbage removal supplier, mapped this way in DDS",
    },
    {
        "legal_contains": "суши принт",
        "working_name": "Суши Принт",
        "dds_article": "Оплата поставщикам",
        "pnl_line": "",
        "note": "owner_speech_2026_05_19; branded chopsticks for rolls",
    },
    {
        "legal_contains": "сити",
        "working_name": "Сити",
        "dds_article": "Баннерная реклама",
        "pnl_line": "Наружная реклама",
        "note": "owner_speech_2026_05_19; legacy banner ads supplier, no longer used",
    },
]


@dataclass
class Operation:
    bank: str
    operation_id: str
    operation_date: str
    direction: str
    amount: Money
    counterparty_name: str
    counterparty_inn: str
    counterparty_account: str
    counterparty_bank_name: str
    counterparty_bic: str
    description: str
    category_native_bank: str
    raw: dict[str, Any]
    source_file: str


@dataclass
class ClassifiedOperation:
    op: Operation
    rule_id: str
    flow_type: str
    dds_article: str
    pnl_line: str
    confidence: str
    requires_owner_review: str
    notes: str


def load_local_env() -> dict[str, str]:
    values: dict[str, str] = {}
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
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            values[key.strip()] = value
    return values


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def money(value: Any) -> Money:
    if isinstance(value, dict):
        value = value.get("amount") or value.get("value") or value.get("sum")
    if value is None or value == "":
        return Decimal("0")
    if isinstance(value, str):
        value = value.replace(" ", "").replace(",", ".")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def fmt_money(value: Money) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def clean_digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def first_date(value: str) -> str:
    match = re.search(r"\d{4}-\d{2}-\d{2}", value or "")
    return match.group(0) if match else ""


def parse_date(value: str) -> dt.date | None:
    try:
        return dt.datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def month_of(value: str) -> str:
    return value[:7]


def in_scope_date(day: str) -> bool:
    parsed = parse_date(day)
    return bool(parsed and START_DATE <= parsed <= END_DATE)


def sha(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def normalize_text(*parts: str) -> str:
    return " ".join(part for part in parts if part).casefold()


def supplier_override(op: Operation) -> dict[str, str] | None:
    name = normalize_text(op.counterparty_name)
    for override in SUPPLIER_OVERRIDES:
        if override["legal_contains"] in name:
            return override
    return None


def is_sber_bank(name: str, bic: str = "") -> bool:
    text = normalize_text(name, bic)
    return "сбер" in text or "044525225" in text or "046015602" in text


def is_tbank_bank(name: str, bic: str = "") -> bool:
    text = normalize_text(name, bic)
    return "тбанк" in text or "т-банк" in text or "тинькофф" in text or "044525974" in text


def contains_any(text: str, needles: tuple[str, ...]) -> bool:
    low = text.casefold()
    return any(needle in low for needle in needles)


def description_snippet(value: str) -> str:
    return " ".join((value or "").split())[:260]


def read_env_and_client_accounts() -> tuple[list[dict[str, str]], set[str], set[str]]:
    env = load_local_env()
    rows: list[dict[str, str]] = []
    own_accounts: set[str] = set()
    own_inns: set[str] = set()

    tbank_account = clean_digits(env.get("TBANK_API_ACCOUNT_NUMBER", ""))
    tbank_inn = clean_digits(env.get("TBANK_API_ORGANIZATION_INN", ""))
    if tbank_inn:
        own_inns.add(tbank_inn)
    if tbank_account:
        own_accounts.add(tbank_account)
        tbank_bic = ""
        if TBANK_RAW_FILE.exists():
            payload = load_json(TBANK_RAW_FILE)
            for operation in payload.get("operations", []) if isinstance(payload, dict) else []:
                if clean_digits(str(operation.get("accountNumber") or "")) == tbank_account:
                    tbank_bic = clean_digits(str(operation.get("bic") or ""))
                    break
        rows.append(
            {
                "bank": "tbank",
                "account_number": tbank_account,
                "account_role": "main_tbank",
                "bik": tbank_bic,
                "inn": tbank_inn,
                "kpp": clean_digits(env.get("TBANK_API_ORGANIZATION_KPP", "")),
                "note": "from .env; owner previously provided; confirm if additional T-Bank accounts exist",
            }
        )

    sber_env_account = clean_digits(env.get("SBER_API_ACCOUNT_NUMBER", ""))
    if sber_env_account:
        own_accounts.add(sber_env_account)
    client_files = sorted((PRIVATE_DIR / "sber").glob("client_info_*.json"))
    if client_files:
        payload = load_json(client_files[-1])
        sber_inn = clean_digits(str(payload.get("inn") or ""))
        if sber_inn:
            own_inns.add(sber_inn)
        for account in payload.get("accounts", []) if isinstance(payload, dict) else []:
            number = clean_digits(str(account.get("number") or ""))
            if not number:
                continue
            own_accounts.add(number)
            role = "main_sber" if number == sber_env_account else "secondary"
            account_name = str(account.get("name") or account.get("type") or "").casefold()
            if role == "secondary" and ("ссуд" in account_name or "loan" in account_name):
                role = "secondary_loan_sber"
            rows.append(
                {
                    "bank": "sber",
                    "account_number": number,
                    "account_role": role,
                    "bik": clean_digits(str(account.get("bic") or "")),
                    "inn": sber_inn,
                    "kpp": "",
                    "note": "from Sber client-info raw; confirm with owner before treating as full own-account list",
                }
            )

    if sber_env_account and not any(row["account_number"] == sber_env_account for row in rows):
        rows.append(
            {
                "bank": "sber",
                "account_number": sber_env_account,
                "account_role": "main_sber",
                "bik": "",
                "inn": "",
                "kpp": "",
                "note": "from .env; confirm with owner",
            }
        )

    # Deduplicate by bank/account, preserving the richer first row.
    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, str]] = []
    for row in rows:
        key = (row["bank"], row["account_number"])
        if key in seen:
            continue
        deduped.append(row)
        seen.add(key)

    write_csv(
        PRIVATE_DIR / "bank_own_accounts_registry.csv",
        deduped,
        ["bank", "account_number", "account_role", "bik", "inn", "kpp", "note"],
    )
    return deduped, own_accounts, own_inns


def build_private_rules(sber_operations: list[Operation], tbank_operations: list[Operation]) -> None:
    rows = [
        {
            "rule_id": "REV_ACQUIRING_SBER_MERCHANT",
            "bank": "sber",
            "direction": "credit",
            "source_fields": "paymentPurpose; payerBankName; operationCode",
            "match_type": "contains_any",
            "pattern": "эквайр|мерчант",
            "flow_type": "revenue_acquiring_sber",
            "dds_article_candidate": "Выручка эквайринг Sber",
            "pnl_line_candidate": "Выручка с учетом скидок Черникова",
            "requires_owner_review": "no",
            "priority": "10",
            "notes": "Observed Sber merchant/acquiring settlement pattern.",
        },
        {
            "rule_id": "REV_CONTRACT_TRANSFER_SBER_STARTERAPP",
            "bank": "sber",
            "direction": "credit",
            "source_fields": "paymentPurpose; payerBankName",
            "match_type": "contains_all",
            "pattern": "перевод средств;договор",
            "flow_type": "revenue_acquiring_sber",
            "dds_article_candidate": "Выручка интернет-эквайринг Sber / StarterApp",
            "pnl_line_candidate": "Выручка с учетом скидок Черникова",
            "requires_owner_review": "no",
            "priority": "11",
            "notes": "Owner confirmed StarterApp / payment acceptance contract channel.",
        },
        {
            "rule_id": "REV_ACQUIRING_TBANK_INCOMEPEOPLE_TBANK",
            "bank": "tbank",
            "direction": "credit",
            "source_fields": "category; payer.bankName; payPurpose; description",
            "match_type": "category_incomePeople_and_tbank_bank_and_acquiring_text",
            "pattern": "category=incomePeople; payer.bankName=АО \"ТБанк\"; text contains эквайр",
            "flow_type": "revenue_acquiring_tbank",
            "dds_article_candidate": "Выручка эквайринг T-Bank",
            "pnl_line_candidate": "Выручка с учетом скидок Черникова",
            "requires_owner_review": "yes",
            "priority": "10",
            "notes": "Hypothesis from raw fields; keep medium confidence until owner confirms T-Bank acquiring signature.",
        },
        {
            "rule_id": "INTERNAL_TRANSFER_SBER_TO_TBANK_DEBIT",
            "bank": "sber",
            "direction": "debit",
            "source_fields": "payeeBankName; payeeInn; payeeAccount; own_accounts_registry",
            "match_type": "payee_is_tbank_and_own_account",
            "pattern": "payee bank contains Тинькофф/ТБанк; payee account or INN is own",
            "flow_type": "internal_transfer_sber_to_tbank",
            "dds_article_candidate": "",
            "pnl_line_candidate": "",
            "requires_owner_review": "no",
            "priority": "1",
            "notes": "Sber side of the Sber -> T-Bank self-transfer chain.",
        },
        {
            "rule_id": "INTERNAL_TRANSFER_SBER_TO_TBANK_CREDIT",
            "bank": "tbank",
            "direction": "credit",
            "source_fields": "category; payer.bankName; payer.inn; payer.acct; own_accounts_registry",
            "match_type": "payer_is_sber_and_own_account",
            "pattern": "category=incomePeople; payer bank contains Сбер; payer account or INN is own",
            "flow_type": "internal_transfer_sber_to_tbank",
            "dds_article_candidate": "",
            "pnl_line_candidate": "",
            "requires_owner_review": "no",
            "priority": "2",
            "notes": "T-Bank side of the Sber -> T-Bank self-transfer chain.",
        },
        {
            "rule_id": "LOAN_OR_OVERDRAFT_ANY",
            "bank": "any",
            "direction": "both",
            "source_fields": "category; paymentPurpose; payPurpose; description",
            "match_type": "contains_or_category",
            "pattern": "кредит|овердрафт|category=incomeLoan|category=creditPaymentOuter",
            "flow_type": "loan_payment",
            "dds_article_candidate": "Кредиты / овердрафт",
            "pnl_line_candidate": "Комиссия за овердрафт",
            "requires_owner_review": "yes",
            "priority": "30",
            "notes": "Needs owner confirmation of active credit/overdraft products and principal/interest split.",
        },
        {
            "rule_id": "REFUND_ANY",
            "bank": "any",
            "direction": "both",
            "source_fields": "category; paymentPurpose; payPurpose; description",
            "match_type": "contains_or_category",
            "pattern": "возврат|category=refundIn",
            "flow_type": "refund",
            "dds_article_candidate": "Возвраты клиентам",
            "pnl_line_candidate": "Возвраты клиентам (с минусом)",
            "requires_owner_review": "yes",
            "priority": "35",
            "notes": "Separate customer refunds from supplier/bank refunds.",
        },
        {
            "rule_id": "TAX_ANY",
            "bank": "any",
            "direction": "debit",
            "source_fields": "category; paymentPurpose; payPurpose; description",
            "match_type": "contains_or_category",
            "pattern": "налог|взнос|ндфл|category=tax|category=budget",
            "flow_type": "tax_payment",
            "dds_article_candidate": "Налоги с З/п",
            "pnl_line_candidate": "Налоги с ЗП",
            "requires_owner_review": "yes",
            "priority": "40",
            "notes": "Split payroll taxes from other taxes in the next pass.",
        },
        {
            "rule_id": "BANK_FEE_ANY",
            "bank": "any",
            "direction": "debit",
            "source_fields": "category; operationCode; paymentPurpose; payPurpose; description",
            "match_type": "contains_or_category_or_code",
            "pattern": "комиссия|рко|category=fee|operationCode=02|operationCode=17",
            "flow_type": "bank_fee",
            "dds_article_candidate": "Прочие банковские комиссии",
            "pnl_line_candidate": "Прочие банковские комиссии",
            "requires_owner_review": "no",
            "priority": "50",
            "notes": "Acquiring fees are remapped to DДС article Эквайринг when acquiring text is present.",
        },
        {
            "rule_id": "PAYROLL_TBANK_PEOPLE",
            "bank": "tbank",
            "direction": "debit",
            "source_fields": "category; receiver",
            "match_type": "category",
            "pattern": "category=contragentPeople",
            "flow_type": "payroll_payment",
            "dds_article_candidate": "Расходы на персонал",
            "pnl_line_candidate": "Расходы на персонал",
            "requires_owner_review": "yes",
            "priority": "55",
            "notes": "Transfers to individuals; owner should confirm payroll vs owner withdrawals.",
        },
        {
            "rule_id": "SUPPLIER_PAYMENT_TBANK_CONTRAGENT",
            "bank": "tbank",
            "direction": "debit",
            "source_fields": "category; receiver; payPurpose; description",
            "match_type": "category",
            "pattern": "category=contragentOutcome",
            "flow_type": "supplier_payment",
            "dds_article_candidate": "Поставщики / требуется разметка",
            "pnl_line_candidate": "",
            "requires_owner_review": "yes",
            "priority": "60",
            "notes": "Mapped by heuristic text/known counterparty where possible; otherwise review.",
        },
    ]

    observed_tbank_acquirers: dict[tuple[str, str, str], list[Operation]] = defaultdict(list)
    observed_tbank_suppliers: dict[tuple[str, str], list[Operation]] = defaultdict(list)
    observed_sber_debit_counterparties: dict[tuple[str, str], list[Operation]] = defaultdict(list)

    for op in tbank_operations:
        if (
            op.direction == "credit"
            and op.category_native_bank == "incomePeople"
            and len(op.counterparty_inn) == 10
            and is_tbank_bank(op.counterparty_bank_name, op.counterparty_bic)
            and contains_any(normalize_text(op.description, op.counterparty_name), ("эквайр",))
        ):
            observed_tbank_acquirers[(op.counterparty_inn, op.counterparty_name, op.counterparty_bic)].append(op)
        if op.direction == "debit" and op.category_native_bank == "contragentOutcome" and len(op.counterparty_inn) == 10:
            observed_tbank_suppliers[(op.counterparty_inn, op.counterparty_name)].append(op)

    for op in sber_operations:
        if op.direction == "debit" and len(op.counterparty_inn) == 10:
            observed_sber_debit_counterparties[(op.counterparty_inn, op.counterparty_name)].append(op)

    for (inn, name, bic), group in sorted(
        observed_tbank_acquirers.items(),
        key=lambda kv: sum((op.amount for op in kv[1]), Decimal("0")),
        reverse=True,
    ):
        rows.append(
            {
                "rule_id": f"REV_ACQUIRING_TBANK_COUNTERPARTY_{sha(inn + name, 8).upper()}",
                "bank": "tbank",
                "direction": "credit",
                "source_fields": "category; payer.inn; payer.name; payer.bankName; payer.bicRu; payPurpose; description",
                "match_type": "counterparty_inn_category_bank_and_acquiring_text",
                "pattern": f"category=incomePeople; payer.inn={inn}; payer.name={name}; payer.bicRu={bic}; text contains эквайр",
                "flow_type": "revenue_acquiring_tbank",
                "dds_article_candidate": "Выручка эквайринг T-Bank",
                "pnl_line_candidate": "Выручка с учетом скидок Черникова",
                "requires_owner_review": "yes",
                "priority": "10",
                "notes": f"Observed {len(group)} operations; direct T-Bank acquiring hypothesis, owner confirmation required.",
            }
        )

    for (inn, name), group in sorted(
        observed_tbank_suppliers.items(),
        key=lambda kv: sum((op.amount for op in kv[1]), Decimal("0")),
        reverse=True,
    ):
        sample = group[0]
        dds, pnl, note = article_for_supplier(sample)
        has_override = supplier_override(sample) is not None
        rows.append(
            {
                "rule_id": f"SUPPLIER_TBANK_COUNTERPARTY_{sha(inn + name, 8).upper()}",
                "bank": "tbank",
                "direction": "debit",
                "source_fields": "category; receiver.inn; receiver.name; payPurpose; description",
                "match_type": "counterparty_inn_and_category",
                "pattern": f"category=contragentOutcome; receiver.inn={inn}; receiver.name={name}",
                "flow_type": "supplier_payment",
                "dds_article_candidate": dds,
                "pnl_line_candidate": pnl,
                "requires_owner_review": "no" if has_override else "yes",
                "priority": "59",
                "notes": f"Observed {len(group)} operations; {note}.",
            }
        )

    for (inn, name), group in sorted(
        observed_sber_debit_counterparties.items(),
        key=lambda kv: sum((op.amount for op in kv[1]), Decimal("0")),
        reverse=True,
    ):
        rows.append(
            {
                "rule_id": f"SBER_DEBIT_COUNTERPARTY_REVIEW_{sha(inn + name, 8).upper()}",
                "bank": "sber",
                "direction": "debit",
                "source_fields": "payee.inn; payee.name; paymentPurpose",
                "match_type": "counterparty_inn_review",
                "pattern": f"payee.inn={inn}; payee.name={name}",
                "flow_type": "other_outflow",
                "dds_article_candidate": "",
                "pnl_line_candidate": "",
                "requires_owner_review": "yes",
                "priority": "65",
                "notes": f"Observed {len(group)} Sber debit operations outside confirmed Sber->T-Bank transfer chain.",
            }
        )

    write_csv(
        PRIVATE_DIR / "bank_operation_rules.csv",
        rows,
        [
            "rule_id",
            "bank",
            "direction",
            "source_fields",
            "match_type",
            "pattern",
            "flow_type",
            "dds_article_candidate",
            "pnl_line_candidate",
            "requires_owner_review",
            "priority",
            "notes",
        ],
    )


def load_sber_operations() -> list[Operation]:
    operations: list[Operation] = []
    for path in sorted(SBER_RAW_DIR.glob("2026-*/transactions_page_*.json")):
        payload = load_json(path)
        for row in payload.get("transactions", []) if isinstance(payload, dict) else []:
            day = first_date(str(row.get("operationDate") or row.get("documentDate") or ""))
            if not in_scope_date(day):
                continue
            direction = str(row.get("direction") or "").lower()
            transfer = row.get("rurTransfer") if isinstance(row.get("rurTransfer"), dict) else {}
            if direction == "credit":
                cp_name = str(transfer.get("payerName") or "")
                cp_inn = clean_digits(str(transfer.get("payerInn") or ""))
                cp_account = clean_digits(str(transfer.get("payerAccount") or row.get("correspondingAccount") or ""))
                cp_bank = str(transfer.get("payerBankName") or "")
                cp_bic = clean_digits(str(transfer.get("payerBankBic") or ""))
            else:
                cp_name = str(transfer.get("payeeName") or "")
                cp_inn = clean_digits(str(transfer.get("payeeInn") or ""))
                cp_account = clean_digits(str(transfer.get("payeeAccount") or row.get("correspondingAccount") or ""))
                cp_bank = str(transfer.get("payeeBankName") or "")
                cp_bic = clean_digits(str(transfer.get("payeeBankBic") or ""))
            operations.append(
                Operation(
                    bank="sber",
                    operation_id=str(row.get("operationId") or row.get("uuid") or sha(json.dumps(row, sort_keys=True, ensure_ascii=False))),
                    operation_date=day,
                    direction=direction,
                    amount=abs(money(row.get("amountRub") or row.get("amount"))),
                    counterparty_name=cp_name,
                    counterparty_inn=cp_inn,
                    counterparty_account=cp_account,
                    counterparty_bank_name=cp_bank,
                    counterparty_bic=cp_bic,
                    description=str(row.get("paymentPurpose") or ""),
                    category_native_bank=str(row.get("operationCode") or ""),
                    raw=row,
                    source_file=rel(path),
                )
            )
    return operations


def load_tbank_operations() -> list[Operation]:
    payload = load_json(TBANK_RAW_FILE)
    operations: list[Operation] = []
    for row in payload.get("operations", []) if isinstance(payload, dict) else []:
        day = first_date(str(row.get("operationDate") or row.get("docDate") or row.get("chargeDate") or ""))
        if not in_scope_date(day):
            continue
        direction = "credit" if str(row.get("typeOfOperation") or "").casefold() == "credit" else "debit"
        payer = row.get("payer") if isinstance(row.get("payer"), dict) else {}
        receiver = row.get("receiver") if isinstance(row.get("receiver"), dict) else {}
        block = payer if direction == "credit" else receiver
        operations.append(
            Operation(
                bank="tbank",
                operation_id=str(row.get("operationId") or sha(json.dumps(row, sort_keys=True, ensure_ascii=False))),
                operation_date=day,
                direction=direction,
                amount=abs(money(row.get("rubleAmount") or row.get("accountAmount") or row.get("operationAmount"))),
                counterparty_name=str(block.get("name") or row.get("counterParty") or ""),
                counterparty_inn=clean_digits(str(block.get("inn") or "")),
                counterparty_account=clean_digits(str(block.get("acct") or "")),
                counterparty_bank_name=str(block.get("bankName") or ""),
                counterparty_bic=clean_digits(str(block.get("bicRu") or row.get("bic") or "")),
                description=str(row.get("payPurpose") or row.get("description") or ""),
                category_native_bank=str(row.get("category") or ""),
                raw=row,
                source_file=rel(TBANK_RAW_FILE),
            )
        )
    return operations


def article_for_supplier(op: Operation) -> tuple[str, str, str]:
    override = supplier_override(op)
    if override:
        return (
            override["dds_article"],
            override["pnl_line"],
            f"counterparty_override: working_name={override['working_name']}; {override['note']}",
        )

    text = normalize_text(op.counterparty_name, op.description, op.category_native_bank)
    rules = [
        (("транспорт", "логист", "доставк", "такси", "груз"), "Транспортные услуги", "Транспортные услуги"),
        (("питани", "столов", "обед"), "Расходы на питание персонала", "Расходы на питание персонала"),
        (("автоматизац", "iiko", "айко", "касс", "ofd", "фд", "нк"), "Оплаты систем автоматизации", "Оплата систем автоматизации"),
        (("реклам", "маркет", "контекст", "таргет", "seo"), "Прочие маркетинговые услуги", "Прочие маркетинговые услуги"),
        (("листов", "печат"), "Листовки", "Листовки"),
        (("персонал", "обучен", "найм", "hh.ru", "headhunter"), "Поиск и найм персонала", "Поиск и найм персонала"),
        (("связь", "телеком", "интернет", "сервер", "хостинг", "mango", "манго", "микроэл", "ihc"), "Телекоммуникации", "Телекоммуникации"),
        (("аренд",), "Аренда / требуется проверка", "Аренда торговой точки Черникова"),
        (("штраф", "пени"), "Штрафы и пени", "Штрафы и пени"),
    ]
    for needles, dds, pnl in rules:
        if contains_any(text, needles):
            return dds, pnl, "heuristic_text_match"
    return "Поставщики / требуется разметка", "", "needs_owner_mapping"


def working_name_for_supplier(op: Operation) -> str:
    override = supplier_override(op)
    return override["working_name"] if override else ""


def classify_operation(op: Operation, own_accounts: set[str], own_inns: set[str]) -> ClassifiedOperation:
    text = normalize_text(op.description, op.counterparty_name, op.counterparty_bank_name, op.category_native_bank)
    cp_is_own = bool(
        (op.counterparty_account and op.counterparty_account in own_accounts)
        or (op.counterparty_inn and op.counterparty_inn in own_inns)
    )

    if op.bank == "sber":
        if op.direction == "credit":
            if cp_is_own and is_tbank_bank(op.counterparty_bank_name, op.counterparty_bic):
                return ClassifiedOperation(op, "INTERNAL_TRANSFER_OWN_ACCOUNTS_REVERSE_REVIEW", "other_inflow", "", "", "low", "yes", "Own-account inflow to Sber; not part of Sber->T-Bank chain.")
            if contains_any(text, ("эквайр", "мерчант")):
                return ClassifiedOperation(op, "REV_ACQUIRING_SBER_MERCHANT", "revenue_acquiring_sber", "Выручка эквайринг Sber", "Выручка с учетом скидок Черникова", "high", "no", "Sber acquiring/merchant settlement.")
            if contains_any(text, ("перевод средств", "договор", "прием платеж", "приём платеж")):
                return ClassifiedOperation(op, "REV_CONTRACT_TRANSFER_SBER_STARTERAPP", "revenue_acquiring_sber", "Выручка интернет-эквайринг Sber / StarterApp", "Выручка с учетом скидок Черникова", "high", "no", "Owner-confirmed payment acceptance contract channel.")
            return ClassifiedOperation(op, "OTHER_SBER_CREDIT_REVIEW", "other_inflow", "", "", "low", "yes", "Sber credit without revenue signature.")

        if cp_is_own and is_tbank_bank(op.counterparty_bank_name, op.counterparty_bic):
            return ClassifiedOperation(op, "INTERNAL_TRANSFER_SBER_TO_TBANK_DEBIT", "internal_transfer_sber_to_tbank", "", "", "high", "no", "Own-account transfer from Sber to T-Bank.")
        if contains_any(text, ("кредит", "овердрафт")):
            return ClassifiedOperation(op, "LOAN_OR_OVERDRAFT_ANY", "loan_payment", "Кредиты / овердрафт", "Комиссия за овердрафт", "medium", "yes", "Loan/overdraft-like Sber debit.")
        if op.category_native_bank in {"02", "17"} or contains_any(text, ("комисс", "рко")):
            article = "Эквайринг" if contains_any(text, ("эквайр", "мерчант")) else "Прочие банковские комиссии"
            pnl = "Комиссия за эквайринг" if article == "Эквайринг" else "Прочие банковские комиссии"
            return ClassifiedOperation(op, "BANK_FEE_ANY", "bank_fee", article, pnl, "high", "no", "Sber fee/commission operation.")
        return ClassifiedOperation(op, "OTHER_SBER_DEBIT_REVIEW", "other_outflow", "", "", "low", "yes", "Sber debit not matched to internal transfer or fee.")

    category = op.category_native_bank
    if op.direction == "credit":
        if category == "incomePeople" and is_sber_bank(op.counterparty_bank_name, op.counterparty_bic) and cp_is_own:
            return ClassifiedOperation(op, "INTERNAL_TRANSFER_SBER_TO_TBANK_CREDIT", "internal_transfer_sber_to_tbank", "", "", "high", "no", "Sber -> T-Bank own-account transfer.")
        if category == "incomePeople" and is_tbank_bank(op.counterparty_bank_name, op.counterparty_bic) and contains_any(text, ("эквайр",)):
            return ClassifiedOperation(op, "REV_ACQUIRING_TBANK_INCOMEPEOPLE_TBANK", "revenue_acquiring_tbank", "Выручка эквайринг T-Bank", "Выручка с учетом скидок Черникова", "medium", "yes", "Hypothesis: direct T-Bank acquiring; owner confirmation required.")
        if category == "incomeLoan" or contains_any(text, ("кредит", "овердрафт")):
            return ClassifiedOperation(op, "LOAN_OR_OVERDRAFT_ANY", "loan_payment", "Кредиты / овердрафт", "Комиссия за овердрафт", "medium", "yes", "Loan/overdraft inflow.")
        if category == "refundIn" or contains_any(text, ("возврат",)):
            return ClassifiedOperation(op, "REFUND_ANY", "refund", "Возвраты клиентам", "Возвраты клиентам (с минусом)", "medium", "yes", "Refund-like inflow.")
        return ClassifiedOperation(op, "OTHER_TBANK_CREDIT_REVIEW", "other_inflow", "", "", "low", "yes", f"T-Bank credit category {category} requires review.")

    if category == "fee":
        article = "Эквайринг" if contains_any(text, ("эквайр", "мерчант")) else "Прочие банковские комиссии"
        pnl = "Комиссия за эквайринг" if article == "Эквайринг" else "Прочие банковские комиссии"
        return ClassifiedOperation(op, "BANK_FEE_ANY", "bank_fee", article, pnl, "high", "no", "T-Bank fee.")
    if category == "creditPaymentOuter" or contains_any(text, ("кредит", "овердрафт")):
        return ClassifiedOperation(op, "LOAN_OR_OVERDRAFT_ANY", "loan_payment", "Кредиты / овердрафт", "Комиссия за овердрафт", "medium", "yes", "Loan/overdraft payment.")
    if category in {"tax", "budget"} or contains_any(text, ("налог", "взнос", "ндфл", "пфр", "фсс", "фомс")):
        return ClassifiedOperation(op, "TAX_ANY", "tax_payment", "Налоги с З/п", "Налоги с ЗП", "medium", "yes", "Budget/tax payment; split payroll and non-payroll taxes later.")
    if category == "contragentPeople":
        return ClassifiedOperation(op, "PAYROLL_TBANK_PEOPLE", "payroll_payment", "Расходы на персонал", "Расходы на персонал", "medium", "yes", "Transfer to individuals; confirm payroll vs owner withdrawals.")
    if category == "contragentOutcome":
        dds, pnl, note = article_for_supplier(op)
        has_override = supplier_override(op) is not None
        confidence = "high" if has_override else ("medium" if dds != "Поставщики / требуется разметка" else "low")
        requires_review = "no" if has_override else "yes"
        return ClassifiedOperation(op, "SUPPLIER_PAYMENT_TBANK_CONTRAGENT", "supplier_payment", dds, pnl, confidence, requires_review, note)
    if category == "cardOperation":
        return ClassifiedOperation(op, "TBANK_CARD_OPERATION_REVIEW", "other_outflow", "Бизнес-карта / требуется разметка", "", "low", "yes", "Business-card operation; could be TT content, supplies, meals, etc.")
    if category == "selfTransferOuter":
        return ClassifiedOperation(op, "TBANK_SELF_TRANSFER_OUTER_REVIEW", "other_outflow", "", "", "low", "yes", "T-Bank outward self-transfer-like category; not Sber->T-Bank inflow.")
    return ClassifiedOperation(op, "UNCLASSIFIED", "unclassified", "", "", "low", "yes", "No rule matched.")


def classified_row(item: ClassifiedOperation) -> dict[str, Any]:
    op = item.op
    return {
        "bank": op.bank,
        "operation_id": op.operation_id,
        "operation_date": op.operation_date,
        "direction": op.direction,
        "amount": fmt_money(op.amount),
        "counterparty_name": op.counterparty_name,
        "counterparty_inn": op.counterparty_inn,
        "counterparty_account": op.counterparty_account,
        "description_snippet": description_snippet(op.description),
        "category_native_bank": op.category_native_bank,
        "rule_id_matched": item.rule_id,
        "flow_type": item.flow_type,
        "dds_article_candidate": item.dds_article,
        "pnl_line_candidate": item.pnl_line,
        "confidence": item.confidence,
        "requires_owner_review": item.requires_owner_review,
        "notes": item.notes,
        "source_file": op.source_file,
    }


def write_classified_rows(items: list[ClassifiedOperation]) -> None:
    fieldnames = [
        "bank",
        "operation_id",
        "operation_date",
        "direction",
        "amount",
        "counterparty_name",
        "counterparty_inn",
        "counterparty_account",
        "description_snippet",
        "category_native_bank",
        "rule_id_matched",
        "flow_type",
        "dds_article_candidate",
        "pnl_line_candidate",
        "confidence",
        "requires_owner_review",
        "notes",
        "source_file",
    ]
    by_bank: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        by_bank[item.op.bank].append(classified_row(item))
    write_csv(PRIVATE_DIR / "sber/statement_classified.csv", by_bank.get("sber", []), fieldnames)
    write_csv(PRIVATE_DIR / "tbank/statement_classified.csv", by_bank.get("tbank", []), fieldnames)


def aggregate_dds(items: list[ClassifiedOperation]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str, str], list[ClassifiedOperation]] = defaultdict(list)
    for item in items:
        month = month_of(item.op.operation_date)
        dds = item.dds_article or "not_applicable"
        pnl = item.pnl_line or ""
        groups[(month, dds, pnl, item.op.bank, item.flow_type)].append(item)
    rows: list[dict[str, Any]] = []
    for (month, dds, pnl, bank, flow), group in sorted(groups.items()):
        inflow = sum((item.op.amount for item in group if item.op.direction == "credit"), Decimal("0"))
        outflow = sum((item.op.amount for item in group if item.op.direction == "debit"), Decimal("0"))
        rows.append(
            {
                "month": month,
                "dds_article": dds,
                "pnl_line": pnl,
                "bank": bank,
                "flow_type": flow,
                "inflow_total": fmt_money(inflow),
                "outflow_total": fmt_money(outflow),
                "net_total": fmt_money(inflow - outflow),
                "operation_count": len(group),
                "sources": "; ".join(sorted({item.op.source_file for item in group})),
            }
        )
    return rows


def load_iiko_revenue() -> dict[str, Money]:
    rows = read_csv_dicts(IIKO_REVENUE_FILE)
    result: dict[str, Money] = {}
    for row in rows:
        period = row.get("period", "")
        if period in {"2026-02", "2026-03", "2026-04"}:
            result[period] = money(row.get("revenue"))
        elif period == "2026-05-01_2026-05-17":
            result["2026-05"] = money(row.get("revenue"))
    return result


def load_previous_sber_coverage() -> dict[str, Money]:
    result: dict[str, Money] = {}
    if not SBER_OLD_RECON_FILE.exists():
        return result
    for row in read_csv_dicts(SBER_OLD_RECON_FILE):
        period = row.get("period", "")
        key = "2026-05" if period.startswith("2026-05") else period
        if key in {"2026-02", "2026-03", "2026-04", "2026-05"}:
            result[key] = money(row.get("bank_inflows_pct_of_iiko_revenue"))
    return result


def read_csv_dicts(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def revenue_by_month(items: list[ClassifiedOperation], *, recon_scope: bool) -> dict[str, dict[str, Money]]:
    result: dict[str, dict[str, Money]] = defaultdict(lambda: defaultdict(lambda: Decimal("0")))
    for item in items:
        day = parse_date(item.op.operation_date)
        if not day:
            continue
        if recon_scope and day.month == 5 and day > MAY_RECON_END:
            continue
        if item.flow_type not in {"revenue_acquiring_sber", "revenue_acquiring_tbank"}:
            continue
        month = month_of(item.op.operation_date)
        result[month][item.flow_type] += item.op.amount
    return result


def build_revenue_outputs(items: list[ClassifiedOperation]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    iiko = load_iiko_revenue()
    previous = load_previous_sber_coverage()
    revenue = revenue_by_month(items, recon_scope=True)
    revenue_rows: list[dict[str, Any]] = []
    recon_rows: list[dict[str, Any]] = []
    for month in ["2026-02", "2026-03", "2026-04", "2026-05"]:
        sber = revenue[month].get("revenue_acquiring_sber", Decimal("0"))
        tbank = revenue[month].get("revenue_acquiring_tbank", Decimal("0"))
        total = sber + tbank
        iiko_value = iiko.get(month, Decimal("0"))
        gap_abs = total - iiko_value
        coverage = (total / iiko_value * Decimal("100")) if iiko_value else Decimal("0")
        previous_coverage = previous.get(month, Decimal("0"))
        revenue_rows.append(
            {
                "month": month,
                "revenue_acquiring_sber": fmt_money(sber),
                "revenue_acquiring_tbank": fmt_money(tbank),
                "total_bank_revenue": fmt_money(total),
                "iiko_revenue": fmt_money(iiko_value),
                "gap_abs": fmt_money(gap_abs),
                "gap_pct": fmt_money(coverage),
            }
        )
        recon_rows.append(
            {
                "month": month,
                "iiko_revenue": fmt_money(iiko_value),
                "bank_acquiring_sber": fmt_money(sber),
                "bank_acquiring_tbank": fmt_money(tbank),
                "sum_bank_acquiring": fmt_money(total),
                "gap_abs": fmt_money(gap_abs),
                "gap_pct": fmt_money(coverage),
                "previous_gap_pct_sber_only": fmt_money(previous_coverage),
                "delta_coverage_pct": fmt_money(coverage - previous_coverage),
            }
        )
    return revenue_rows, recon_rows


def build_internal_transfer_check(items: list[ClassifiedOperation]) -> list[dict[str, Any]]:
    sber_out: dict[str, Money] = defaultdict(lambda: Decimal("0"))
    tbank_in: dict[str, Money] = defaultdict(lambda: Decimal("0"))
    for item in items:
        if item.flow_type != "internal_transfer_sber_to_tbank":
            continue
        month = month_of(item.op.operation_date)
        if item.op.bank == "sber" and item.op.direction == "debit":
            sber_out[month] += item.op.amount
        if item.op.bank == "tbank" and item.op.direction == "credit":
            tbank_in[month] += item.op.amount
    rows: list[dict[str, Any]] = []
    for month in ["2026-02", "2026-03", "2026-04", "2026-05"]:
        out = sber_out.get(month, Decimal("0"))
        inc = tbank_in.get(month, Decimal("0"))
        diff = inc - out
        tolerance = max(abs(out), abs(inc)) * Decimal("0.01")
        status = "ok" if abs(diff) <= max(tolerance, Decimal("10000")) else "requires_owner_review"
        rows.append(
            {
                "month": month,
                "sber_to_tbank_outflow_from_sber": fmt_money(out),
                "sber_to_tbank_inflow_to_tbank": fmt_money(inc),
                "diff": fmt_money(diff),
                "status": status,
            }
        )
    return rows


def build_unclassified_summary(items: list[ClassifiedOperation]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str, str], list[ClassifiedOperation]] = defaultdict(list)
    for item in items:
        if item.flow_type != "unclassified" and item.requires_owner_review != "yes":
            continue
        text_hash = sha(normalize_text(item.op.description, item.op.counterparty_name), 12)
        key = (item.op.bank, item.op.direction, item.op.category_native_bank, item.flow_type, text_hash)
        groups[key].append(item)
    rows: list[dict[str, Any]] = []
    for (bank, direction, category, flow, signature), group in sorted(groups.items()):
        total = sum((item.op.amount for item in group), Decimal("0"))
        dates = sorted(item.op.operation_date for item in group)
        legal_count = sum(1 for item in group if len(item.op.counterparty_inn) == 10)
        person_count = sum(1 for item in group if len(item.op.counterparty_inn) == 12)
        rows.append(
            {
                "bank": bank,
                "direction": direction,
                "category_native_bank": category,
                "flow_type": flow,
                "description_signature": signature,
                "operation_count": len(group),
                "amount_total_abs": fmt_money(total),
                "first_date": dates[0],
                "last_date": dates[-1],
                "legal_counterparty_count": legal_count,
                "person_or_ip_counterparty_count": person_count,
                "rule_ids": "; ".join(sorted({item.rule_id for item in group})),
                "suggested_question": suggest_question(group),
            }
        )
    return rows


def suggest_question(group: list[ClassifiedOperation]) -> str:
    first = group[0]
    if first.flow_type == "revenue_acquiring_tbank":
        return "Подтвердить, что это прямой T-Bank эквайринг, а не внутренний перевод."
    if first.flow_type == "supplier_payment":
        return "Указать статью ДДС/ОПиУ для группы платежей этому контрагенту."
    if first.flow_type in {"other_inflow", "other_outflow", "unclassified"}:
        return "Определить экономический смысл группы операций."
    if first.flow_type == "loan_payment":
        return "Разделить тело кредита, проценты и комиссии."
    if first.flow_type == "tax_payment":
        return "Разделить налоги с З/п и прочие налоги."
    return "Подтвердить классификацию."


def write_report(
    items: list[ClassifiedOperation],
    dds_rows: list[dict[str, Any]],
    revenue_rows: list[dict[str, Any]],
    recon_rows: list[dict[str, Any]],
    transfer_rows: list[dict[str, Any]],
    unclassified_rows: list[dict[str, Any]],
) -> None:
    by_flow_count = Counter(item.flow_type for item in items)
    by_flow_amount: dict[str, Money] = defaultdict(lambda: Decimal("0"))
    for item in items:
        by_flow_amount[item.flow_type] += item.op.amount
    total_amount = sum(by_flow_amount.values(), Decimal("0"))
    review_count = sum(1 for item in items if item.requires_owner_review == "yes" or item.flow_type == "unclassified")
    classified_count = len(items) - sum(1 for item in items if item.flow_type == "unclassified")
    transfer_ok = all(row["status"] == "ok" for row in transfer_rows)
    latest_recon = [row for row in recon_rows if row["month"] in {"2026-02", "2026-03", "2026-04"}]
    avg_coverage = (
        sum((money(row["gap_pct"]) for row in latest_recon), Decimal("0")) / Decimal(len(latest_recon))
        if latest_recon
        else Decimal("0")
    )
    tbank_rev_total = sum((money(row["revenue_acquiring_tbank"]) for row in revenue_rows), Decimal("0"))
    sber_rev_total = sum((money(row["revenue_acquiring_sber"]) for row in revenue_rows), Decimal("0"))

    lines = [
        "# Bank Cashflow Classification Report",
        "",
        f"Дата сборки: {dt.date.today().isoformat()}.",
        "",
        "Источник: локальные raw-выписки Sber и T-Bank из `research/private/`; новых API-вызовов не выполнялось.",
        "Построчная классификация хранится только в `research/private/`; processed-файлы содержат только агрегаты.",
        "",
        "## Период",
        "",
        f"- Разметка операций: `{START_DATE.isoformat()}` - `{END_DATE.isoformat()}`.",
        "- Сверка iiko vs банк за май ограничена `2026-05-01` - `2026-05-17`, потому что iiko processed-снимок доступен только за этот период.",
        "",
        "## Классификация",
        "",
        f"- Операций всего: {len(items)}.",
        f"- Операций с не-`unclassified` flow_type: {classified_count}.",
        f"- Операций с `requires_owner_review=yes` или `unclassified`: {review_count}.",
        "",
        "| Flow type | Операций | Оборот abs | Доля оборота |",
        "| --- | ---: | ---: | ---: |",
    ]
    for flow, amount in sorted(by_flow_amount.items(), key=lambda kv: kv[1], reverse=True):
        share = (amount / total_amount * Decimal("100")) if total_amount else Decimal("0")
        lines.append(f"| `{flow}` | {by_flow_count[flow]} | {fmt_money(amount)} | {fmt_money(share)}% |")
    lines.extend(
        [
            "",
            "## Выручка",
            "",
            f"- Sber-эквайринг и договорные платежи: {fmt_money(sber_rev_total)} руб.",
            f"- T-Bank-эквайринг, гипотеза требует проверки: {fmt_money(tbank_rev_total)} руб.",
            f"- Среднее покрытие iiko банком за февраль-апрель после добавления T-Bank: {fmt_money(avg_coverage)}%.",
            "",
            "| Месяц | Sber | T-Bank | Банк всего | iiko | Покрытие |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in revenue_rows:
        lines.append(
            f"| {row['month']} | {row['revenue_acquiring_sber']} | {row['revenue_acquiring_tbank']} | "
            f"{row['total_bank_revenue']} | {row['iiko_revenue']} | {row['gap_pct']}% |"
        )
    lines.extend(
        [
            "",
            "## Внутренние Переводы",
            "",
            f"- Контроль Sber -> T-Bank: {'ok' if transfer_ok else 'requires_owner_review'}.",
            "",
            "| Месяц | Sber outflow | T-Bank inflow | Diff | Status |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
    )
    for row in transfer_rows:
        lines.append(
            f"| {row['month']} | {row['sber_to_tbank_outflow_from_sber']} | "
            f"{row['sber_to_tbank_inflow_to_tbank']} | {row['diff']} | {row['status']} |"
        )
    top_questions = sorted(unclassified_rows, key=lambda row: money(row.get("amount_total_abs")), reverse=True)[:5]
    lines.extend(
        [
            "",
            "## Открытые Вопросы",
            "",
        ]
    )
    for index, row in enumerate(top_questions, start=1):
        lines.append(
            f"{index}. `{row['bank']}` / `{row['flow_type']}` / `{row['category_native_bank']}`: "
            f"{row['operation_count']} операций, {row['amount_total_abs']} руб. - {row['suggested_question']}"
        )
    lines.extend(
        [
            "",
            "## Файлы",
            "",
            "- `research/private/bank_own_accounts_registry.csv`",
            "- `research/private/bank_operation_rules.csv`",
            "- `research/private/sber/statement_classified.csv`",
            "- `research/private/tbank/statement_classified.csv`",
            "- `research/processed/cashflow/dds_by_article_2026.csv`",
            "- `research/processed/cashflow/revenue_split.csv`",
            "- `research/processed/cashflow/iiko_vs_bank_reconciliation.csv`",
            "- `research/processed/cashflow/internal_transfer_check.csv`",
            "- `research/processed/cashflow/unclassified_operations_summary.csv`",
            "",
        ]
    )
    write_text(PROCESSED_DIR / "report.md", "\n".join(lines))


def write_counterparty_candidates(items: list[ClassifiedOperation]) -> None:
    groups: dict[tuple[str, str, str], list[ClassifiedOperation]] = defaultdict(list)
    for item in items:
        if item.op.bank != "tbank" or item.op.direction != "debit":
            continue
        if len(item.op.counterparty_inn) != 10:
            continue
        if item.flow_type not in {"supplier_payment", "bank_fee", "tax_payment"}:
            continue
        key = (item.op.counterparty_inn, item.op.counterparty_name, item.flow_type)
        groups[key].append(item)
    rows: list[dict[str, Any]] = []
    for (inn, name, flow), group in sorted(groups.items(), key=lambda kv: sum((item.op.amount for item in kv[1]), Decimal("0")), reverse=True):
        total = sum((item.op.amount for item in group), Decimal("0"))
        dds = Counter(item.dds_article for item in group).most_common(1)[0][0]
        pnl = Counter(item.pnl_line for item in group).most_common(1)[0][0]
        working_name = working_name_for_supplier(group[0].op)
        rows.append(
            {
                "counterparty_working_name": working_name,
                "counterparty_name": name,
                "counterparty_inn": inn,
                "flow_type": flow,
                "dds_article_candidate": dds,
                "pnl_line_candidate": pnl,
                "operation_count": len(group),
                "amount_total_abs": fmt_money(total),
                "first_date": min(item.op.operation_date for item in group),
                "last_date": max(item.op.operation_date for item in group),
                "status": "owner_confirmed_2026_05_19" if working_name else "requires_owner_confirmation",
            }
        )
    write_csv(
        PRIVATE_DIR / "bank_counterparty_candidates_private.csv",
        rows[:30],
        [
            "counterparty_working_name",
            "counterparty_name",
            "counterparty_inn",
            "flow_type",
            "dds_article_candidate",
            "pnl_line_candidate",
            "operation_count",
            "amount_total_abs",
            "first_date",
            "last_date",
            "status",
        ],
    )


def main() -> int:
    _, own_accounts, own_inns = read_env_and_client_accounts()
    sber_operations = load_sber_operations()
    tbank_operations = load_tbank_operations()
    build_private_rules(sber_operations, tbank_operations)
    classified = [classify_operation(op, own_accounts, own_inns) for op in [*sber_operations, *tbank_operations]]
    write_classified_rows(classified)
    write_counterparty_candidates(classified)

    dds_rows = aggregate_dds(classified)
    revenue_rows, recon_rows = build_revenue_outputs(classified)
    transfer_rows = build_internal_transfer_check(classified)
    unclassified_rows = build_unclassified_summary(classified)

    write_csv(
        PROCESSED_DIR / "dds_by_article_2026.csv",
        dds_rows,
        [
            "month",
            "dds_article",
            "pnl_line",
            "bank",
            "flow_type",
            "inflow_total",
            "outflow_total",
            "net_total",
            "operation_count",
            "sources",
        ],
    )
    write_csv(
        PROCESSED_DIR / "revenue_split.csv",
        revenue_rows,
        [
            "month",
            "revenue_acquiring_sber",
            "revenue_acquiring_tbank",
            "total_bank_revenue",
            "iiko_revenue",
            "gap_abs",
            "gap_pct",
        ],
    )
    write_csv(
        PROCESSED_DIR / "iiko_vs_bank_reconciliation.csv",
        recon_rows,
        [
            "month",
            "iiko_revenue",
            "bank_acquiring_sber",
            "bank_acquiring_tbank",
            "sum_bank_acquiring",
            "gap_abs",
            "gap_pct",
            "previous_gap_pct_sber_only",
            "delta_coverage_pct",
        ],
    )
    write_csv(
        PROCESSED_DIR / "internal_transfer_check.csv",
        transfer_rows,
        [
            "month",
            "sber_to_tbank_outflow_from_sber",
            "sber_to_tbank_inflow_to_tbank",
            "diff",
            "status",
        ],
    )
    write_csv(
        PROCESSED_DIR / "unclassified_operations_summary.csv",
        unclassified_rows,
        [
            "bank",
            "direction",
            "category_native_bank",
            "flow_type",
            "description_signature",
            "operation_count",
            "amount_total_abs",
            "first_date",
            "last_date",
            "legal_counterparty_count",
            "person_or_ip_counterparty_count",
            "rule_ids",
            "suggested_question",
        ],
    )
    write_report(classified, dds_rows, revenue_rows, recon_rows, transfer_rows, unclassified_rows)
    print(f"sber_operations={len(sber_operations)}")
    print(f"tbank_operations={len(tbank_operations)}")
    print(f"classified_operations={len(classified)}")
    print(f"processed_dir={rel(PROCESSED_DIR)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
