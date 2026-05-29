#!/usr/bin/env python3
"""Read-only Sber API statement export.

Reads Sber API credentials and mTLS paths from ENV/.env, writes raw bank
responses only under research/private/sber/, and prints a sanitized run summary.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "research/private/sber/statement"


class SberHTTPError(RuntimeError):
    def __init__(self, status: int | None, body: bytes, message: str | None = None) -> None:
        self.status = status
        self.body = body
        self.message = message or body[:500].decode("utf-8", "replace")
        super().__init__(f"Sber API error {status}: {sanitize_text(self.message)[:200]}")


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


def parse_date(value: str) -> dt.date:
    return dt.datetime.strptime(value, "%Y-%m-%d").date()


def date_range(start: dt.date, end: dt.date) -> list[dt.date]:
    if end < start:
        raise SystemExit("--end-date must be greater than or equal to --start-date")
    days = (end - start).days + 1
    return [start + dt.timedelta(days=offset) for offset in range(days)]


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


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is missing")
    return value


def mask_account(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) < 8:
        return "<invalid>"
    return f"{digits[:4]}...{digits[-4:]}"


def sanitize_text(value: str) -> str:
    text = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer <redacted>", value)
    text = re.sub(r"\b\d{20}\b", lambda m: mask_account(m.group(0)), text)
    text = re.sub(r"([?&]accountNumber=)\d{20}", lambda m: f"{m.group(1)}<redacted>", text)
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


def transaction_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get("transactions")
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]
    return []


def has_next_link(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    links = payload.get("_links")
    if isinstance(links, dict):
        return bool(links.get("next"))
    if isinstance(links, list):
        for link in links:
            if not isinstance(link, dict):
                continue
            rel_value = str(link.get("rel") or link.get("name") or link.get("type") or "").casefold()
            if rel_value == "next":
                return True
    return False


class SberClient:
    def __init__(self) -> None:
        self.base_url = required_env("SBER_API_BASE_URL").rstrip("/")
        self.access_token = required_env("SBER_API_ACCESS_TOKEN")
        self.account_number = required_env("SBER_API_ACCOUNT_NUMBER")
        if not re.fullmatch(r"\d{20}", self.account_number):
            raise SystemExit("SBER_API_ACCOUNT_NUMBER must be exactly 20 digits without separators")
        self.timeout = float(os.environ.get("SBER_API_TIMEOUT_SECONDS") or 90)
        self.context = self._ssl_context()

    def _ssl_context(self) -> ssl.SSLContext:
        ca_bundle = os.environ.get("SBER_API_CA_BUNDLE_PATH", "").strip()
        cert_path = os.environ.get("SBER_API_TLS_CERT_PATH", "").strip()
        key_path = os.environ.get("SBER_API_TLS_KEY_PATH", "").strip()

        context = (
            ssl.create_default_context(cafile=str(resolve_project_path(ca_bundle)))
            if ca_bundle
            else ssl.create_default_context()
        )
        if not cert_path or not key_path:
            raise SystemExit(
                "SBER_API_TLS_CERT_PATH and SBER_API_TLS_KEY_PATH are required. "
                "Extract them locally from the .p12 container first."
            )
        context.load_cert_chain(
            certfile=str(resolve_project_path(cert_path)),
            keyfile=str(resolve_project_path(key_path)),
        )
        return context

    def request_json(self, path: str, params: dict[str, Any]) -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        query = urllib.parse.urlencode(params)
        if query:
            url = f"{url}?{query}"
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self.context
            ) as response:
                body = response.read()
                return response.status, parse_json_body(body)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            raise SberHTTPError(exc.code, body) from exc
        except urllib.error.URLError as exc:
            raise SberHTTPError(None, str(exc).encode("utf-8")) from exc


def fetch_summary(
    client: SberClient,
    day: dt.date,
    day_dir: Path,
    manifest: list[dict[str, Any]],
) -> None:
    path = day_dir / "summary.json"
    try:
        status, payload = client.request_json(
            "/v2/statement/summary",
            {"accountNumber": client.account_number, "statementDate": day.isoformat()},
        )
        write_json(path, payload)
        remove_stale_error(day_dir / "summary_error.json")
        manifest.append(
            {
                "date": day.isoformat(),
                "endpoint": "/v2/statement/summary",
                "status": status,
                "file": rel(path),
                "keys": sorted(payload.keys()) if isinstance(payload, dict) else [],
            }
        )
    except SberHTTPError as exc:
        error_path = day_dir / "summary_error.json"
        write_json(error_path, parse_json_body(exc.body))
        manifest.append(
            {
                "date": day.isoformat(),
                "endpoint": "/v2/statement/summary",
                "status": exc.status,
                "file": rel(error_path),
                "error": sanitize_text(exc.message),
            }
        )


def fetch_transactions(
    client: SberClient,
    day: dt.date,
    day_dir: Path,
    manifest: list[dict[str, Any]],
    *,
    max_pages: int,
    sleep_seconds: float,
) -> int:
    total = 0
    page = 1
    while page <= max_pages:
        path = day_dir / f"transactions_page_{page:03d}.json"
        try:
            status, payload = client.request_json(
                "/v2/statement/transactions",
                {
                    "accountNumber": client.account_number,
                    "statementDate": day.isoformat(),
                    "page": page,
                },
            )
            write_json(path, payload)
            remove_stale_error(day_dir / f"transactions_page_{page:03d}_error.json")
            rows = transaction_rows(payload)
            total += len(rows)
            manifest.append(
                {
                    "date": day.isoformat(),
                    "endpoint": "/v2/statement/transactions",
                    "page": page,
                    "status": status,
                    "transaction_count": len(rows),
                    "file": rel(path),
                    "has_next": has_next_link(payload),
                }
            )
            if not has_next_link(payload):
                break
        except SberHTTPError as exc:
            error_path = day_dir / f"transactions_page_{page:03d}_error.json"
            write_json(error_path, parse_json_body(exc.body))
            manifest.append(
                {
                    "date": day.isoformat(),
                    "endpoint": "/v2/statement/transactions",
                    "page": page,
                    "status": exc.status,
                    "file": rel(error_path),
                    "error": sanitize_text(exc.message),
                }
            )
            break
        page += 1
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    return total


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export Sber statement raw JSON files")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--date", help="Single statement date, YYYY-MM-DD")
    group.add_argument("--start-date", help="Start statement date, YYYY-MM-DD")
    parser.add_argument("--end-date", help="End statement date, YYYY-MM-DD")
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--sleep-seconds", type=float, default=0.25)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    return parser.parse_args(argv)


def requested_days(args: argparse.Namespace) -> list[dt.date]:
    if args.date:
        return [parse_date(args.date)]
    if args.start_date:
        if not args.end_date:
            raise SystemExit("--end-date is required with --start-date")
        return date_range(parse_date(args.start_date), parse_date(args.end_date))
    today = dt.date.today()
    return [today]


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    load_local_env()
    client = SberClient()
    output_dir = resolve_project_path(args.output_dir)
    manifest: list[dict[str, Any]] = []

    print(f"account={mask_account(client.account_number)}")
    for day in requested_days(args):
        day_dir = output_dir / day.isoformat()
        fetch_summary(client, day, day_dir, manifest)
        if args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)
        total = fetch_transactions(
            client,
            day,
            day_dir,
            manifest,
            max_pages=args.max_pages,
            sleep_seconds=args.sleep_seconds,
        )
        print(f"{day.isoformat()}: transactions={total}")
        if args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    manifest_path = output_dir / f"run_manifest_{stamp}.json"
    write_json(manifest_path, manifest)
    print(f"manifest={rel(manifest_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
