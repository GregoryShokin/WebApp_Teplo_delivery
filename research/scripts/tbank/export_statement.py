#!/usr/bin/env python3
"""Read-only T-Bank Business Open API statement export.

The script reads credentials from local ENV/.env, writes raw API responses only
under research/private/tbank/, and prints a sanitized run summary.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "research/private/tbank"


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
        self.message = message or body[:1000].decode("utf-8", "replace")
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


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is missing")
    return value


def optional_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def parse_iso_utc(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        raise SystemExit(f"Expected ISO 8601 UTC value like 2026-05-19T00:00:00Z: {value}")
    return value


def date_label(value: str) -> str:
    return parse_iso_utc(value)[:10]


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def resolve_project_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def mask_account(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) < 4:
        return "<redacted>"
    return f"<account:...{digits[-4:]}>"


def mask_inn(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) < 4:
        return "<redacted>"
    return f"<inn:...{digits[-4:]}>"


def sanitize_text(value: str) -> str:
    text = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer <redacted>", value)
    text = re.sub(r"([?&]accountNumber=)\d{20}", lambda m: f"{m.group(1)}<redacted>", text)
    text = re.sub(r"\b\d{20}\b", lambda m: mask_account(m.group(0)), text)
    text = re.sub(r"\b\d{12}\b", lambda m: mask_inn(m.group(0)), text)
    text = re.sub(r"\b\d{10}\b", lambda m: mask_inn(m.group(0)), text)
    return text[:2000]


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def remove_stale_error(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def parse_json_body(body: bytes) -> Any:
    text = body.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw_text": sanitize_text(text)}


def nested_get(payload: Any, path: tuple[str, ...]) -> Any:
    current = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def next_cursor(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    candidates = [
        payload.get("nextCursor"),
        payload.get("next_cursor"),
        nested_get(payload, ("paging", "nextCursor")),
        nested_get(payload, ("page", "nextCursor")),
        nested_get(payload, ("result", "nextCursor")),
        nested_get(payload, ("data", "nextCursor")),
    ]
    for value in candidates:
        if value:
            return str(value)
    return ""


def operation_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    candidates = [
        payload.get("operations"),
        payload.get("transactions"),
        payload.get("items"),
        nested_get(payload, ("statement", "operations")),
        nested_get(payload, ("result", "operations")),
        nested_get(payload, ("data", "operations")),
        nested_get(payload, ("payload", "operations")),
    ]
    for value in candidates:
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def page_path(output_dir: Path, prefix: str, page: int, suffix_mode: str) -> Path:
    if suffix_mode == "first-plain" and page == 1:
        return output_dir / f"{prefix}.json"
    return output_dir / f"{prefix}_p{page:02d}.json"


class TBankClient:
    def __init__(self) -> None:
        self.base_url = required_env("TBANK_API_BASE_URL").rstrip("/")
        self.access_token = required_env("TBANK_API_ACCESS_TOKEN")
        self.account_number = required_env("TBANK_API_ACCOUNT_NUMBER")
        if not re.fullmatch(r"\d{20}", self.account_number):
            raise SystemExit("TBANK_API_ACCOUNT_NUMBER must be exactly 20 digits without separators")
        self.organization_inn = optional_env("TBANK_API_ORGANIZATION_INN")
        if self.organization_inn and not re.fullmatch(r"\d{10}|\d{12}", self.organization_inn):
            raise SystemExit("TBANK_API_ORGANIZATION_INN must be 10 or 12 digits without separators")
        self.timeout = float(optional_env("TBANK_API_TIMEOUT_SECONDS", "90") or 90)

    def request_json(self, path: str, params: dict[str, Any]) -> tuple[int, Any, str, float]:
        url = f"{self.base_url}{path}"
        query = urllib.parse.urlencode(params, doseq=True)
        if query:
            url = f"{url}?{query}"
        request_id = str(uuid.uuid4())
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Accept": "application/json",
                "X-Request-Id": request_id,
            },
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                elapsed = time.perf_counter() - started
                return response.status, parse_json_body(body), request_id, elapsed
        except urllib.error.HTTPError as exc:
            body = exc.read()
            elapsed = time.perf_counter() - started
            raise TBankHTTPError(exc.code, body, request_id, elapsed) from exc
        except urllib.error.URLError as exc:
            elapsed = time.perf_counter() - started
            raise TBankHTTPError(None, str(exc).encode("utf-8"), request_id, elapsed) from exc


def base_params(args: argparse.Namespace, client: TBankClient) -> dict[str, Any]:
    params: dict[str, Any] = {
        "accountNumber": client.account_number,
        "from": parse_iso_utc(args.from_utc),
        "to": parse_iso_utc(args.to_utc),
        "limit": args.limit,
    }
    if args.operation_status:
        params["operationStatus"] = args.operation_status
    if args.ucid:
        params["ucid"] = args.ucid
    if args.category:
        params["categories"] = args.category
    if args.inn:
        params["inns"] = args.inn
    return params


def sanitized_params(params: dict[str, Any]) -> dict[str, Any]:
    clean = dict(params)
    if "accountNumber" in clean:
        clean["accountNumber"] = mask_account(str(clean["accountNumber"]))
    if "inns" in clean:
        clean["inns"] = [mask_inn(str(value)) for value in clean["inns"]]
    return clean


def check_env(client: TBankClient) -> None:
    print("tbank_env_ok=true")
    print(f"base_url={client.base_url}")
    print(f"account={mask_account(client.account_number)}")
    print(f"access_token={'set' if client.access_token else 'missing'}")
    print(f"organization_inn={'set' if client.organization_inn else 'missing'}")
    print(f"timeout_seconds={client.timeout:g}")


def export_statement(args: argparse.Namespace) -> int:
    client = TBankClient()
    if args.check_env:
        check_env(client)
        return 0

    output_dir = resolve_project_path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = args.file_prefix or f"statement_{date_label(args.from_utc)}_{date_label(args.to_utc)}"
    manifest: list[dict[str, Any]] = []
    params = base_params(args, client)

    cursor = ""
    total_operations = 0
    page = 1
    while page <= args.max_pages:
        request_params = dict(params)
        if cursor:
            request_params["cursor"] = cursor
        if args.with_balances and page == 1:
            request_params["withBalances"] = "true"

        raw_path = page_path(output_dir, prefix, page, args.page_suffix_mode)
        error_path = raw_path.with_name(f"{raw_path.stem}_error.json")
        try:
            status, payload, request_id, elapsed = client.request_json(
                "/api/v1/statement", request_params
            )
            write_json(raw_path, payload)
            remove_stale_error(error_path)
            rows = operation_rows(payload)
            total_operations += len(rows)
            cursor = next_cursor(payload)
            manifest.append(
                {
                    "endpoint": "/api/v1/statement",
                    "status": status,
                    "page": page,
                    "request_id": request_id,
                    "elapsed_seconds": round(elapsed, 3),
                    "file": rel(raw_path),
                    "operation_count": len(rows),
                    "has_next_cursor": bool(cursor),
                    "params": sanitized_params(request_params),
                    "top_level_fields": sorted(payload.keys()) if isinstance(payload, dict) else [],
                }
            )
            print(
                f"page={page} status={status} operations={len(rows)} "
                f"elapsed={elapsed:.3f}s file={rel(raw_path)} next_cursor={'yes' if cursor else 'no'}"
            )
            if not cursor:
                break
        except TBankHTTPError as exc:
            error_payload = parse_json_body(exc.body)
            write_json(error_path, error_payload)
            manifest.append(
                {
                    "endpoint": "/api/v1/statement",
                    "status": exc.status,
                    "page": page,
                    "request_id": exc.request_id,
                    "elapsed_seconds": round(exc.elapsed_seconds, 3),
                    "file": rel(error_path),
                    "error": sanitize_text(exc.message),
                    "params": sanitized_params(request_params),
                }
            )
            print(
                f"page={page} status={exc.status} elapsed={exc.elapsed_seconds:.3f}s "
                f"error={sanitize_text(exc.message)} file={rel(error_path)}"
            )
            break

        page += 1
        if args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    manifest_path = output_dir / f"{prefix}_manifest_{stamp}.json"
    write_json(manifest_path, manifest)
    print(f"account={mask_account(client.account_number)}")
    print(f"total_operations={total_operations}")
    print(f"manifest={rel(manifest_path)}")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export T-Bank statement raw JSON files")
    parser.add_argument("--from-utc", default="2026-05-18T00:00:00Z")
    parser.add_argument("--to-utc", default="2026-05-19T00:00:00Z")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--sleep-seconds", type=float, default=0.25)
    parser.add_argument("--with-balances", action="store_true")
    parser.add_argument("--operation-status", choices=["All", "Authorization", "Transaction"], default="All")
    parser.add_argument("--ucid")
    parser.add_argument("--category", action="append", default=[])
    parser.add_argument("--inn", action="append", default=[])
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--file-prefix")
    parser.add_argument("--page-suffix-mode", choices=["always", "first-plain"], default="always")
    parser.add_argument("--check-env", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    load_local_env()
    args = parse_args(argv)
    if args.limit < 1 or args.limit > 5000:
        raise SystemExit("--limit must be between 1 and 5000")
    return export_statement(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
