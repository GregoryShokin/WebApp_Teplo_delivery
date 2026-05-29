#!/usr/bin/env python3
"""Read-only discovery/export for iiko Finance -> Chart of accounts.

The script performs only GET requests against data endpoints. The shared
IikoClient may refresh an expired token through POST /auth; secrets are loaded
from .env/ENV and are never printed or written to outputs.

Raw files under research/raw/ may contain full iiko comments and user identifiers.
Processed outputs contain only field metadata and aggregates.
"""

from __future__ import annotations

import calendar
import csv
import datetime as dt
import json
import re
import sys
import time
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from export_orders_delivery import (  # noqa: E402
    IikoClient,
    IikoHTTPError,
    PROJECT_ROOT,
    clean_text,
    load_local_env,
    parse_rows,
    rel,
    write_json,
)


RAW_DIR = PROJECT_ROOT / "research/raw/iiko/finance_chart_research"
PROCESSED_DIR = PROJECT_ROOT / "research/processed/iiko/finance_chart"
MAIN_CASH_ACCOUNT = "Главная касса"
MAIN_CASH_ACCOUNT_ID = "8ccc8f0f-24f6-64d2-5eea-04f829ba381f"
MAIN_CASH_ACCOUNT_ID_REDACTED = "<main_cash_account_id_redacted>"
SOURCE_ENDPOINT = "/reports/olap"
RUN_DATE = dt.date(2026, 5, 20)

OLAP_GROUPS = [
    "DateTime.DateTyped",
    "Account.Name",
    "Contr-Account.Name",
    "TransactionSide",
    "TransactionType",
    "Document",
    "CashFlowCategory",
    "Comment",
]
OLAP_AGGREGATES = ["Sum.ResignedSum", "Sum.Incoming", "Sum.Outgoing"]
OLAP_EXPECTED_FIELDS = OLAP_GROUPS + OLAP_AGGREGATES

WADL_KEYWORDS = [
    "finance",
    "account",
    "chartOfAccount",
    "chart-of-account",
    "cashflow",
    "cash",
    "mainCash",
    "accounting",
    "glAccount",
    "journal",
    "transaction",
    "posting",
    "balance",
    "report",
    "olap",
]

CSV_ENDPOINT_FIELDS = [
    "endpoint",
    "method",
    "status_code",
    "returned_records",
    "date_format",
    "mandatory_params",
    "notes",
]

CSV_SCHEMA_FIELDS = [
    "endpoint",
    "field_path",
    "type",
    "nullable",
    "example_redacted",
    "business_meaning",
    "target_app_column",
]

CSV_CORR_FIELDS = [
    "corr_account",
    "debit_count",
    "debit_sum_abs",
    "credit_count",
    "credit_sum_abs",
    "hypothesized_dds_article",
    "status",
]

CSV_MONTHLY_FIELDS = [
    "month",
    "corr_account",
    "debit_sum",
    "credit_sum",
    "operation_count",
    "source_endpoint",
]


def month_periods() -> list[tuple[str, dt.date, dt.date]]:
    """Target period: full Feb-Apr 2026 and May through the run date."""

    periods: list[tuple[str, dt.date, dt.date]] = []
    for month in (2, 3, 4, 5):
        start = dt.date(2026, month, 1)
        last_day = calendar.monthrange(2026, month)[1]
        end = dt.date(2026, month, last_day)
        if month == RUN_DATE.month:
            end = RUN_DATE
        periods.append((f"2026-{month:02d}", start, end))
    return periods


def fmt_olap_date(value: dt.date) -> str:
    return value.strftime("%d.%m.%Y")


def money_decimal(value: Any) -> Decimal:
    text = clean_text(value).replace(" ", "").replace("\u2212", "-")
    if not text:
        return Decimal("0")
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    else:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal("0")


def money_text(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def money_human(value: Decimal) -> str:
    rounded = value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"{int(rounded):,}".replace(",", " ")


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def request_get(
    client: IikoClient,
    inventory: list[dict[str, Any]],
    endpoint: str,
    *,
    params: dict[str, Any] | None = None,
    raw_path: Path | None = None,
    expected_fields: list[str] | None = None,
    date_format: str = "",
    mandatory_params: str = "",
    notes: str = "",
) -> tuple[int | None, bytes, list[dict[str, Any]]]:
    try:
        status, data = client.request(endpoint, params=params or {})
        if raw_path:
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_bytes(data)
        rows = parse_rows(data, expected_fields)
        inventory.append(
            {
                "endpoint": endpoint,
                "method": "GET",
                "status_code": status,
                "returned_records": len(rows),
                "date_format": date_format,
                "mandatory_params": mandatory_params,
                "notes": notes,
            }
        )
        time.sleep(0.3)
        return status, data, rows
    except IikoHTTPError as exc:
        if raw_path:
            error_path = raw_path.with_name(raw_path.stem + "_error.json")
            write_json(
                error_path,
                {
                    "endpoint": endpoint,
                    "status": exc.status,
                    "message": clean_text(exc.message)[:1000],
                    "params_redacted": params or {},
                },
            )
        inventory.append(
            {
                "endpoint": endpoint,
                "method": "GET",
                "status_code": exc.status or "ERROR",
                "returned_records": 0,
                "date_format": date_format,
                "mandatory_params": mandatory_params,
                "notes": f"{notes}; error={clean_text(exc.message)[:250]}".strip("; "),
            }
        )
        time.sleep(0.3)
        return exc.status, exc.body, []


def join_path(base: str, part: str) -> str:
    if part.startswith("/"):
        return part
    if not base or base == "/":
        return "/" + part
    return base.rstrip("/") + "/" + part


def wadl_matches_from_text(text: str) -> list[tuple[str, str]]:
    """Small WADL path extractor that avoids XML parser runtime issues."""

    matches: list[tuple[str, str]] = []
    stack: list[str] = []
    resource_re = re.compile(r"<resource\b[^>]*\bpath=\"([^\"]+)\"")
    method_re = re.compile(r"<method\b[^>]*\bname=\"([A-Z]+)\"")

    for line in text.splitlines():
        for resource_match in resource_re.finditer(line):
            parent = stack[-1] if stack else ""
            stack.append(join_path(parent, resource_match.group(1)))

        current_path = stack[-1] if stack else "/"
        for method_match in method_re.finditer(line):
            haystack = f"{current_path}\n{line}".casefold()
            if any(keyword.casefold() in haystack for keyword in WADL_KEYWORDS):
                matches.append((method_match.group(1), current_path))

        for _ in range(line.count("</resource>")):
            if stack:
                stack.pop()

    result: list[tuple[str, str]] = []
    for item in matches:
        if item not in result:
            result.append(item)
    return result


def save_wadl_matches(data: bytes) -> None:
    text = data.decode("utf-8", "replace")
    matches = wadl_matches_from_text(text)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    with (RAW_DIR / "wadl_matches.txt").open("w", encoding="utf-8") as handle:
        for method, path in matches:
            handle.write(f"{method}\t{path}\n")


def payload_json(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def account_maps(rows: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    by_name: dict[str, dict[str, str]] = {}
    for row in rows:
        name = clean_text(row.get("name"))
        if not name:
            continue
        by_name[name] = {
            "id": clean_text(row.get("id")),
            "code": clean_text(row.get("code")),
            "type": clean_text(row.get("type")),
            "deleted": str(row.get("deleted", "")),
            "customTransactionsAllowed": str(row.get("customTransactionsAllowed", "")),
        }
    return by_name


def enrich_main_cash_rows(
    rows: list[dict[str, Any]],
    accounts_by_name: dict[str, dict[str, str]],
    source_file: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        if clean_text(row.get("Account.Name")) != MAIN_CASH_ACCOUNT:
            continue
        account = accounts_by_name.get(clean_text(row.get("Account.Name")), {})
        corr = accounts_by_name.get(clean_text(row.get("Contr-Account.Name")), {})
        enriched = {
            "DateTime.DateTyped": clean_text(row.get("DateTime.DateTyped")),
            "Account.Id": account.get("id", ""),
            "Account.Code": account.get("code", ""),
            "Account.Name": clean_text(row.get("Account.Name")),
            "Account.Type": account.get("type") or clean_text(row.get("Account.Type")),
            "Contr-Account.Id": corr.get("id", ""),
            "Contr-Account.Code": corr.get("code", ""),
            "Contr-Account.Name": clean_text(row.get("Contr-Account.Name")),
            "Contr-Account.Type": corr.get("type", ""),
            "TransactionSide": clean_text(row.get("TransactionSide")),
            "TransactionType": clean_text(row.get("TransactionType")),
            "Document": clean_text(row.get("Document")),
            "CashFlowCategory": clean_text(row.get("CashFlowCategory")),
            "Comment": clean_text(row.get("Comment")),
            "Sum.ResignedSum": clean_text(row.get("Sum.ResignedSum")),
            "Sum.Incoming": clean_text(row.get("Sum.Incoming")),
            "Sum.Outgoing": clean_text(row.get("Sum.Outgoing")),
            "_source_file": source_file,
        }
        result.append(enriched)
    return result


def export_month(
    client: IikoClient,
    inventory: list[dict[str, Any]],
    accounts_by_name: dict[str, dict[str, str]],
    label: str,
    start: dt.date,
    end: dt.date,
) -> list[dict[str, Any]]:
    params = {
        "report": "TRANSACTIONS",
        "summary": "false",
        "from": fmt_olap_date(start),
        "to": fmt_olap_date(end),
        "groupRow": OLAP_GROUPS,
        "agr": OLAP_AGGREGATES,
    }
    status, data, rows = request_get(
        client,
        inventory,
        SOURCE_ENDPOINT,
        params=params,
        raw_path=None,
        expected_fields=OLAP_EXPECTED_FIELDS,
        date_format="dd.MM.yyyy inclusive",
        mandatory_params="report, from, to, groupRow, agr",
        notes=f"canonical Главная касса export; period={start.isoformat()}..{end.isoformat()}",
    )
    raw_json = RAW_DIR / f"glavnaya_kassa_{label}.json"
    source_file = rel(raw_json)
    main_rows = enrich_main_cash_rows(rows, accounts_by_name, source_file)
    write_json(
        raw_json,
        {
            "endpoint": SOURCE_ENDPOINT,
            "method": "GET",
            "status": status,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "params_redacted": {
                "report": "TRANSACTIONS",
                "summary": "false",
                "from": fmt_olap_date(start),
                "to": fmt_olap_date(end),
                "groupRow": OLAP_GROUPS,
                "agr": OLAP_AGGREGATES,
            },
            "account_filter": MAIN_CASH_ACCOUNT,
            "records": main_rows,
        },
    )
    if inventory:
        inventory[-1]["returned_records"] = len(main_rows)
    return main_rows


def rule_guess(corr_account: str) -> tuple[str, str]:
    value = clean_text(corr_account).casefold()
    if corr_account == "Задолженность перед поставщиками":
        return "Оплата поставщикам", "iiko_auto_classified"
    if corr_account == "Торговые кассы":
        return "Наличная торговая выручка", "rule_candidate_sales_cash"
    if corr_account == "Депозиты сотрудников":
        return "Депозиты курьеров / deposit_account", "payroll_link_required"
    if corr_account in {"Текущие расчеты с сотрудниками", "Затраты на персонал"}:
        return "Расходы на персонал", "rule_candidate_owner_review"
    if "гагарин" in value:
        return "Историческая касса Гагарина", "legacy_requires_owner_review"
    if "прочие расходы" in value:
        return "Прочие операционные расходы", "owner_review"
    if "прочие доходы" in value:
        return "Прочие поступления", "owner_review"
    if "алиса" in value:
        return "Внутреннее движение наличных / агрегатор", "owner_review"
    if "актуализация" in value:
        return "Техническая корректировка", "owner_review"
    return "Не определено", "owner_review"


def build_corr_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        corr = clean_text(row.get("Contr-Account.Name")) or "(пусто)"
        item = grouped.setdefault(
            corr,
            {
                "corr_account": corr,
                "debit_count": 0,
                "debit_sum_abs": Decimal("0"),
                "credit_count": 0,
                "credit_sum_abs": Decimal("0"),
            },
        )
        incoming = money_decimal(row.get("Sum.Incoming"))
        outgoing = money_decimal(row.get("Sum.Outgoing"))
        if incoming != 0:
            item["debit_count"] += 1
            item["debit_sum_abs"] += abs(incoming)
        if outgoing != 0:
            item["credit_count"] += 1
            item["credit_sum_abs"] += abs(outgoing)

    output: list[dict[str, Any]] = []
    for corr, item in grouped.items():
        article, status = rule_guess(corr)
        output.append(
            {
                "corr_account": corr,
                "debit_count": item["debit_count"],
                "debit_sum_abs": money_text(item["debit_sum_abs"]),
                "credit_count": item["credit_count"],
                "credit_sum_abs": money_text(item["credit_sum_abs"]),
                "hypothesized_dds_article": article,
                "status": status,
            }
        )
    output.sort(
        key=lambda row: money_decimal(row["debit_sum_abs"]) + money_decimal(row["credit_sum_abs"]),
        reverse=True,
    )
    return output


def build_monthly(rows_by_month: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for month, rows in rows_by_month.items():
        for row in rows:
            corr = clean_text(row.get("Contr-Account.Name")) or "(пусто)"
            key = (month, corr)
            item = grouped.setdefault(
                key,
                {
                    "month": month,
                    "corr_account": corr,
                    "debit_sum": Decimal("0"),
                    "credit_sum": Decimal("0"),
                    "operation_count": 0,
                    "source_endpoint": SOURCE_ENDPOINT,
                },
            )
            item["debit_sum"] += money_decimal(row.get("Sum.Incoming"))
            item["credit_sum"] += money_decimal(row.get("Sum.Outgoing"))
            item["operation_count"] += 1
    output: list[dict[str, Any]] = []
    for item in grouped.values():
        output.append(
            {
                "month": item["month"],
                "corr_account": item["corr_account"],
                "debit_sum": money_text(item["debit_sum"]),
                "credit_sum": money_text(item["credit_sum"]),
                "operation_count": item["operation_count"],
                "source_endpoint": item["source_endpoint"],
            }
        )
    output.sort(key=lambda row: (row["month"], row["corr_account"]))
    return output


def build_field_schema(sample: dict[str, Any] | None) -> list[dict[str, Any]]:
    examples = sample or {}

    def example(field: str, fallback: str = "") -> str:
        if field == "Comment":
            return "<redacted PII/comment>"
        value = clean_text(examples.get(field))
        return value or fallback

    rows = [
        (
            "records[]",
            "array<object>",
            "false",
            "[]",
            "Список строк OLAP TRANSACTIONS после фильтра `Account.Name=Главная касса`.",
            "import_batch.source_records",
        ),
        (
            "records[].DateTime.DateTyped",
            "date-string",
            "false",
            example("DateTime.DateTyped", "Wed Apr 01 00:00:00 MSK 2026"),
            "Учетный день операции в iiko.",
            "cashflow_transactions.operation_date",
        ),
        (
            "records[].Account.Id",
            "uuid",
            "false",
            MAIN_CASH_ACCOUNT_ID_REDACTED,
            "ID счета Главная касса из справочника `/v2/entities/accounts/list`.",
            "cashflow_transactions.source_account_id",
        ),
        (
            "records[].Account.Name",
            "string",
            "false",
            MAIN_CASH_ACCOUNT,
            "Счет плана счетов, по которому строится wallet ТК Черникова.",
            "wallets.source_account_name",
        ),
        (
            "records[].Account.Type",
            "enum",
            "false",
            example("Account.Type", "CASH"),
            "Тип счета iiko.",
            "wallets.source_account_type",
        ),
        (
            "records[].Contr-Account.Id",
            "uuid",
            "true",
            "<corr_account_id_redacted>",
            "ID корреспондирующего счета; основной ключ для правил ДДС.",
            "cashflow_transactions.corr_account_id",
        ),
        (
            "records[].Contr-Account.Name",
            "string",
            "false",
            example("Contr-Account.Name", "Торговые кассы"),
            "Корсчет операции, главный признак статьи ДДС.",
            "cashflow_transactions.corr_account_name",
        ),
        (
            "records[].Contr-Account.Type",
            "enum",
            "true",
            example("Contr-Account.Type", "REVENUE"),
            "Тип корсчета из iiko.",
            "cashflow_transactions.corr_account_type",
        ),
        (
            "records[].TransactionSide",
            "enum",
            "false",
            example("TransactionSide", "DEBIT"),
            "Сторона операции по Главной кассе: дебет = поступление, кредит = расход.",
            "cashflow_transactions.direction",
        ),
        (
            "records[].TransactionType",
            "enum",
            "false",
            example("TransactionType", "PAYOUT"),
            "Тип транзакции iiko.",
            "cashflow_transactions.source_operation_type",
        ),
        (
            "records[].Document",
            "string",
            "true",
            example("Document", "1065"),
            "Номер документа iiko.",
            "source_documents.external_number",
        ),
        (
            "records[].CashFlowCategory",
            "string",
            "true",
            example("CashFlowCategory", "<empty>"),
            "Статья ДДС iiko; заполнена надежно только для отдельных корсчетов.",
            "cashflow_transactions.iiko_cashflow_category",
        ),
        (
            "records[].Comment",
            "string",
            "true",
            "<redacted PII/comment>",
            "Комментарий операции; может содержать ФИО курьера/сотрудника и не выносится в processed.",
            "cashflow_transactions.source_comment_private",
        ),
        (
            "records[].Sum.ResignedSum",
            "money-string",
            "false",
            example("Sum.ResignedSum", "9786.000000000"),
            "Сумма со знаком в представлении iiko.",
            "cashflow_transactions.amount_signed",
        ),
        (
            "records[].Sum.Incoming",
            "money-string",
            "false",
            example("Sum.Incoming", "9786.000000000"),
            "Дебет Главной кассы, поступление.",
            "cashflow_transactions.debit_amount",
        ),
        (
            "records[].Sum.Outgoing",
            "money-string",
            "false",
            example("Sum.Outgoing", "0"),
            "Кредит Главной кассы, расход.",
            "cashflow_transactions.credit_amount",
        ),
    ]
    return [
        {
            "endpoint": SOURCE_ENDPOINT,
            "field_path": field,
            "type": typ,
            "nullable": nullable,
            "example_redacted": ex,
            "business_meaning": meaning,
            "target_app_column": target,
        }
        for field, typ, nullable, ex, meaning, target in rows
    ]


def write_report(
    all_rows: list[dict[str, Any]],
    corr_summary: list[dict[str, Any]],
    monthly_rows: list[dict[str, Any]],
    inventory: list[dict[str, Any]],
) -> None:
    total_debit = sum(money_decimal(row.get("Sum.Incoming")) for row in all_rows)
    total_credit = sum(money_decimal(row.get("Sum.Outgoing")) for row in all_rows)
    top = corr_summary[:10]
    auto_supplier = next(
        (row for row in corr_summary if row["corr_account"] == "Задолженность перед поставщиками"),
        None,
    )
    lines = [
        "# iiko finance chart research report",
        "",
        "Дата разведки: 2026-05-20.",
        "",
        f"Канонический источник операций Главной кассы: `GET {SOURCE_ENDPOINT}` с `report=TRANSACTIONS`, `groupRow={', '.join(OLAP_GROUPS)}` и агрегатами `{', '.join(OLAP_AGGREGATES)}`.",
        "",
        f"Период: 2026-02-01..2026-05-{RUN_DATE.day:02d}. Операций Главной кассы: {len(all_rows)}. Уникальных корсчетов: {len(corr_summary)}.",
        "",
        f"Обороты: дебет {money_human(total_debit)} руб.; кредит {money_human(total_credit)} руб.",
        "",
        "## Артефакты",
        "",
        "- Raw: `research/raw/iiko/finance_chart_research/` (может содержать комментарии iiko и PII).",
        "- Processed: `research/processed/iiko/finance_chart/`.",
        "- Документ API: `docs/business-control/22-iiko-finance-chart-of-accounts.md`.",
        "",
        "## Top-10 корсчетов по обороту",
        "",
        "| Корсчет | Дебет | Кредит | Статус |",
        "| --- | ---: | ---: | --- |",
    ]
    for row in top:
        lines.append(
            f"| {row['corr_account']} | {money_human(money_decimal(row['debit_sum_abs']))} | {money_human(money_decimal(row['credit_sum_abs']))} | {row['status']} |"
        )
    lines.extend(
        [
            "",
            "## Автоклассификация iiko",
            "",
            "iiko не заполняет `CashFlowCategory` универсально для операций Главной кассы. Надежно выделенный бизнес-кейс: корсчет `Задолженность перед поставщиками` -> `Оплата поставщикам`; остальные корсчета требуют rule engine приложения.",
        ]
    )
    if auto_supplier:
        lines.append(
            f"За период по этому корсчету: дебет {auto_supplier['debit_sum_abs']}, кредит {auto_supplier['credit_sum_abs']}, строк {int(auto_supplier['debit_count']) + int(auto_supplier['credit_count'])}."
        )
    lines.extend(
        [
            "",
            "## Риски",
            "",
            "- `Comment` может содержать ФИО сотрудников/курьеров; в processed и Markdown он не выгружается.",
            "- `Депозиты сотрудников` не содержат проверенной связи с `employee_id` в OLAP-строке; для payroll потребуется ручное правило или отдельный процесс фиксации курьера.",
            "- `GET /v2/cashshifts/*` полезен для сверки наличной выручки, но не содержит полный журнал корсчетов Главной кассы.",
            "- Май 2026 выгружен частично: до 2026-05-20 включительно.",
            "",
            "## Проверенные endpoint'ы",
            "",
            "| Endpoint | Статус | Записей | Примечание |",
            "| --- | ---: | ---: | --- |",
        ]
    )
    for entry in inventory:
        lines.append(
            f"| `{entry['endpoint']}` | {entry['status_code']} | {entry['returned_records']} | {entry['notes']} |"
        )
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    (PROCESSED_DIR / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    load_local_env()
    client = IikoClient()
    inventory: list[dict[str, Any]] = []

    print("Fetching WADL...")
    _, wadl_data, _ = request_get(
        client,
        inventory,
        "/application.wadl",
        raw_path=RAW_DIR / "application.wadl",
        notes="WADL discovery; 276 method/path combinations observed in project docs",
    )
    if wadl_data:
        save_wadl_matches(wadl_data)

    print("Fetching TRANSACTIONS columns...")
    _, columns_data, _ = request_get(
        client,
        inventory,
        "/v2/reports/olap/columns",
        params={"reportType": "TRANSACTIONS"},
        raw_path=RAW_DIR / "olap_columns_transactions.json",
        date_format="none",
        mandatory_params="reportType",
        notes="column catalog for TRANSACTIONS",
    )
    columns_payload = payload_json(columns_data)
    selected_columns = {
        key: value
        for key, value in (columns_payload or {}).items()
        if any(
            marker.casefold() in (key + json.dumps(value, ensure_ascii=False)).casefold()
            for marker in ("Account", "Contr-Account", "CashFlow", "Sum", "Date", "Transaction", "Comment")
        )
    }
    write_json(RAW_DIR / "olap_columns_transactions_account_sum_date_subset.json", selected_columns)

    print("Fetching OLAP presets...")
    _, presets_data, presets_rows = request_get(
        client,
        inventory,
        "/v2/reports/olap/presets",
        raw_path=RAW_DIR / "olap_presets.json",
        notes="saved OLAP presets discovery",
    )
    preset_ids = {
        "dds": "8c13763a-35bf-9f27-017f-5468b1e70022",
        "dds_by_departments": "8c13763a-35bf-9f27-017f-5468b1e70023",
    }
    for label, preset_id in preset_ids.items():
        request_get(
            client,
            inventory,
            f"/v2/reports/olap/byPresetId/{preset_id}",
            params={"summary": "false", "dateFrom": "2026-04-01", "dateTo": "2026-05-01"},
            raw_path=RAW_DIR / f"olap_byPresetId_{label}_2026-04.json",
            date_format="yyyy-MM-dd; dateTo exclusive",
            mandatory_params="dateFrom, dateTo",
            notes=f"saved preset pilot; preset_label={label}",
        )

    print("Fetching accounts dictionary...")
    _, _, account_rows = request_get(
        client,
        inventory,
        "/v2/entities/accounts/list",
        params={"includeDeleted": "false"},
        raw_path=RAW_DIR / "entities_accounts_list.json",
        mandatory_params="includeDeleted optional",
        notes="chart of accounts dictionary; source for account_id/corr_account_id",
    )
    accounts_by_name = account_maps(account_rows)

    print("Checking direct cash/balance endpoints...")
    _, _, shift_rows = request_get(
        client,
        inventory,
        "/v2/cashshifts/list",
        params={"openDateFrom": "2026-04-01", "openDateTo": "2026-04-07", "status": "CLOSED"},
        raw_path=RAW_DIR / "cashshifts_list_2026-04-01_2026-04-07_closed.json",
        date_format="yyyy-MM-dd",
        mandatory_params="status",
        notes="cash shift summary; useful for cash-sales reconciliation, not full chart-of-accounts ledger",
    )
    first_session_id = clean_text(shift_rows[0].get("id")) if shift_rows else ""
    if first_session_id:
        request_get(
            client,
            inventory,
            f"/v2/cashshifts/payments/list/{first_session_id}",
            params={"hideAccepted": "false"},
            raw_path=RAW_DIR / "cashshifts_payments_2026-04-01_first_session.json",
            mandatory_params="sessionId path",
            notes="cash shift payments detail; contains payment records, not all main-cash postings",
        )
        if inventory:
            inventory[-1]["endpoint"] = "/v2/cashshifts/payments/list/{sessionId}"
    request_get(
        client,
        inventory,
        "/v2/reports/balance/counteragents",
        params={"timestamp": "2026-04-30T23:59:59", "account": MAIN_CASH_ACCOUNT_ID},
        raw_path=RAW_DIR / "balance_counteragents_glavnaya_kassa_2026-04-30.json",
        date_format="yyyy-MM-ddTHH:mm:ss",
        mandatory_params="timestamp; account optional but used",
        notes="balance by counteragent for Главная касса; useful for balance snapshots, not operation ledger",
    )
    request_get(
        client,
        inventory,
        "/v2/reports/balance/stores",
        params={"timestamp": "2026-04-30T23:59:59"},
        raw_path=RAW_DIR / "balance_stores_2026-04-30.json",
        date_format="yyyy-MM-ddTHH:mm:ss",
        mandatory_params="timestamp",
        notes="store balance endpoint; unrelated to Main Cash operations",
    )
    request_get(
        client,
        inventory,
        "/v2/documents/internalTransfer",
        params={"dateFrom": "2026-04-01", "dateTo": "2026-04-07", "status": "PROCESSED"},
        raw_path=RAW_DIR / "documents_internalTransfer_2026-04-01_2026-04-07_processed.json",
        date_format="yyyy-MM-dd",
        mandatory_params="dateFrom, dateTo, status",
        notes="direct document endpoint pilot; returned no useful Main Cash rows in pilot",
    )

    print("Exporting Главная касса monthly rows...")
    rows_by_month: dict[str, list[dict[str, Any]]] = {}
    all_rows: list[dict[str, Any]] = []
    for label, start, end in month_periods():
        rows = export_month(client, inventory, accounts_by_name, label, start, end)
        rows_by_month[label] = rows
        all_rows.extend(rows)
        print(f"{label}: {len(rows)} Главная касса rows")

    corr_summary = build_corr_summary(all_rows)
    monthly_rows = build_monthly(rows_by_month)
    sample = all_rows[0] if all_rows else None
    schema_rows = build_field_schema(sample)

    write_csv(PROCESSED_DIR / "endpoints_inventory.csv", inventory, CSV_ENDPOINT_FIELDS)
    write_csv(PROCESSED_DIR / "glavnaya_kassa_field_schema.csv", schema_rows, CSV_SCHEMA_FIELDS)
    write_csv(
        PROCESSED_DIR / "glavnaya_kassa_corraccounts_summary.csv",
        corr_summary,
        CSV_CORR_FIELDS,
    )
    write_csv(PROCESSED_DIR / "glavnaya_kassa_monthly.csv", monthly_rows, CSV_MONTHLY_FIELDS)
    write_report(all_rows, corr_summary, monthly_rows, inventory)

    print(
        "Done: "
        f"operations={len(all_rows)}, corr_accounts={len(corr_summary)}, "
        f"processed={rel(PROCESSED_DIR)}"
    )


if __name__ == "__main__":
    main()
