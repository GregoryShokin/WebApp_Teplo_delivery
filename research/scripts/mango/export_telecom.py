#!/usr/bin/env python3
"""Mango Office product-expenses headless export.

This script does two things:

1. **resume**: if an authenticated session cookie jar is already present in
   ``research/private/mango/session.json`` (e.g. exported from a browser), it
   reuses those cookies, drives the report request/status/result pipeline and
   writes monthly totals to ``research/processed/iiko/telecom/``.

2. **login attempt**: if no session is available, it walks the public OIDC
   discovery chain (LK landing → /sso/oidc/authorize → /sso/ SPA) but stops
   immediately before posting credentials. The Mango auth SPA gates login
   behind Yandex SmartCaptcha (verified on 2026-05-20: `/sso/oidc/auth/vpbx`
   returns ``{result: 1101}`` without a valid `captcha_token`). Headless
   browsers cannot solve SmartCaptcha, so the script does **not** attempt to.

Allowed POSTs (per project rules):

- ``/sso/oidc/auth/vpbx``  (login form; only if running with ``--attempt-login`` flag)
- ``/sso/oidc/session/start``  (finalises the OIDC dance)
- ``/<tenant>/<pbx>/product-expenses/report-{request,status,result}``  (read-only async report)

Everything else is GET-only. Secrets are never logged in cleartext.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs, urlunparse, urlencode

import requests
from bs4 import BeautifulSoup

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PRIVATE_DIR = PROJECT_ROOT / "research/private/mango"
RAW_DIR = PRIVATE_DIR / "raw"
SESSION_PATH = PRIVATE_DIR / "session.json"
PROCESSED_DIR = PROJECT_ROOT / "research/processed/iiko/telecom"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

MONTHS = ("2025-11", "2025-12", "2026-01", "2026-02", "2026-03", "2026-04")
TARGET_PRODUCT = "Мой продуктовый набор №1"
APRIL_CONTROL = Decimal("10189.52")
IHC_RU_SERVER = Decimal("600")
MIKROEL_INTERNET = Decimal("3230")

SECRET_HEADERS = {"set-cookie", "cookie", "authorization"}
SECRET_QUERY_KEYS = {"code", "state", "nonce", "id_token", "access_token",
                     "code_challenge", "code_verifier", "session_state",
                     "auth_code", "captcha_token"}


class StopCondition(RuntimeError):
    """A project-defined stop condition."""


# ---------------------------------------------------------------------------
# Helpers: masking, env, raw dumps
# ---------------------------------------------------------------------------

def mask_url(url: str) -> str:
    if not url:
        return url
    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    masked: dict[str, list[str]] = {}
    for k, v in qs.items():
        if k.lower() in SECRET_QUERY_KEYS:
            masked[k] = ["<redacted>"]
        else:
            masked[k] = v
    new_q = urlencode(masked, doseq=True)
    path = re.sub(r"/\d{6,}/\d{6,}", "/<tenant>/<pbx>", parsed.path)
    return urlunparse((parsed.scheme, parsed.netloc, path,
                       parsed.params, new_q, parsed.fragment))


def mask_header(name: str, value: str) -> str:
    if name.lower() in SECRET_HEADERS:
        return "<redacted>"
    if name.lower() == "location":
        return mask_url(value)
    return value


def mask_text(text: str) -> str:
    text = re.sub(r'(?i)(password=)[^&\s]+', r'\1<redacted>', text)
    text = re.sub(r'(?i)(username=)[^&\s]+', r'\1<redacted>', text)
    text = re.sub(r'(?i)(login=)[^&\s]+', r'\1<redacted>', text)
    text = re.sub(r'(?i)(user=)[^&\s]+', r'\1<redacted>', text)
    text = re.sub(r'(?i)(account=)[^&\s]+', r'\1<redacted>', text)
    text = re.sub(r'(?i)(captcha_token=)[^&\s]+', r'\1<redacted>', text)
    text = re.sub(r'(?i)(auth_code=)[^&\s]+', r'\1<redacted>', text)
    text = re.sub(r'(?i)(code_challenge=)[^&\s]+', r'\1<redacted>', text)
    text = re.sub(r'(?i)(state=)[^&\s]+', r'\1<redacted>', text)
    text = re.sub(r'(?i)("password"\s*:\s*")[^"]+(")', r'\1<redacted>\2', text)
    text = re.sub(r'(?i)("username"\s*:\s*")[^"]+(")', r'\1<redacted>\2', text)
    text = re.sub(r'(?i)("auth_code"\s*:\s*")[^"]+(")', r'\1<redacted>\2', text)
    text = re.sub(r'(?i)("captcha_token"\s*:\s*")[^"]+(")', r'\1<redacted>\2', text)
    return text


def mask_login(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) >= 4:
        return f"<{digits[:1]}***{digits[-3:]}>"
    return "<redacted>"


def load_env() -> None:
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in {'"', "'"}:
            v = v[1:-1]
        if k and not os.environ.get(k):
            os.environ[k] = v


def require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise StopCondition(f"environment variable {name} is missing")
    return val


def dump_raw(idx: int, label: str, response: requests.Response,
             *, request_body: bytes | None = None) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    base = RAW_DIR / f"{idx:02d}_{label}"
    lines = [f"HTTP {response.status_code} {response.reason}",
             f"URL: {mask_url(response.url)}"]
    if request_body is not None:
        lines.append(f"REQUEST_BODY: {mask_text(request_body.decode('utf-8', 'replace'))[:600]}")
    for k, v in response.raw.headers.items():
        lines.append(f"{k}: {mask_header(k, v)}")
    base.with_suffix(".headers").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if response.content:
        ct = response.headers.get("content-type", "").lower()
        body = response.content
        if ct.startswith("application/json") or ct.startswith("text"):
            body = mask_text(body.decode("utf-8", "replace")).encode("utf-8")
        base.with_suffix(".body").write_bytes(body)
    loc = response.headers.get("Location") or ""
    print(f"[{idx:02d}] {label}: HTTP {response.status_code} url={mask_url(response.url)}"
          + (f"  Location: {mask_url(loc)}" if loc else ""))


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
    })
    return s


def save_session(session: requests.Session) -> None:
    PRIVATE_DIR.mkdir(parents=True, exist_ok=True)
    cookies = []
    for c in session.cookies:
        cookies.append({
            "name": c.name, "value": c.value, "domain": c.domain,
            "path": c.path, "secure": c.secure,
            "expires": c.expires,
        })
    SESSION_PATH.write_text(json.dumps({"cookies": cookies}, indent=2), encoding="utf-8")
    os.chmod(SESSION_PATH, 0o600)


def load_session(session: requests.Session) -> bool:
    if not SESSION_PATH.exists():
        return False
    try:
        data = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
    except Exception:
        return False
    for c in data.get("cookies", []):
        session.cookies.set(
            name=c["name"], value=c["value"],
            domain=c.get("domain"), path=c.get("path", "/"),
            secure=c.get("secure", False),
        )
    return True


def try_resume_session(session: requests.Session, dashboard_url: str) -> bool:
    if not load_session(session):
        return False
    try:
        r = session.get(dashboard_url, allow_redirects=False, timeout=30,
                        headers={"Accept": "text/html"})
    except Exception:
        return False
    return r.status_code == 200 and b"product-expenses" in r.content


# ---------------------------------------------------------------------------
# OIDC login (best-effort; expected to STOP because of SmartCaptcha)
# ---------------------------------------------------------------------------

def oidc_login(session: requests.Session, *, allow_attempt: bool = False) -> None:
    if not allow_attempt:
        raise StopCondition(
            "No saved session and OIDC login disabled. Re-run with --attempt-login "
            "after solving SmartCaptcha manually in a browser, or paste session.json."
        )

    login_value = require_env("MANGO_OFFICE_LOGIN")
    password_value = require_env("MANGO_OFFICE_PASSWORD")

    r1 = session.get("https://lk.mango-office.ru/", allow_redirects=False,
                     headers={"Accept": "text/html,*/*;q=0.8",
                              "Upgrade-Insecure-Requests": "1"},
                     timeout=30)
    dump_raw(1, "lk_root", r1)
    loc = r1.headers.get("Location", "")
    if not loc:
        raise StopCondition("LK landing did not 302 to authorize")

    qs = parse_qs(urlparse(loc).query, keep_blank_values=True)
    client_id = qs.get("client_id", [""])[0]
    state = qs.get("state", [""])[0]
    code_challenge = qs.get("code_challenge", [""])[0]
    code_challenge_method = qs.get("code_challenge_method", ["S256"])[0]
    redirect_uri = qs.get("redirect_uri", [""])[0]
    if not (client_id and state and code_challenge and redirect_uri):
        raise StopCondition("authorize URL missing required params")

    time.sleep(1)
    r2 = session.get(loc, allow_redirects=False,
                     headers={"Accept": "text/html,*/*;q=0.8"},
                     timeout=30)
    dump_raw(2, "oidc_authorize", r2)
    sso_url = r2.headers.get("Location", "")
    if not sso_url:
        raise StopCondition("authorize did not redirect to SPA")

    time.sleep(1)
    r3 = session.get(sso_url, allow_redirects=False,
                     headers={"Accept": "text/html,*/*;q=0.8"},
                     timeout=30)
    dump_raw(3, "sso_spa", r3)

    time.sleep(1)
    body_params = {
        "username": login_value,
        "password": password_value,
        "client_id": client_id,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "state": state,
        # captcha_token intentionally omitted; we cannot produce a valid token.
    }
    body = urlencode(body_params)
    r4 = session.post(
        "https://auth.mango-office.ru/sso/oidc/auth/vpbx",
        data=body,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://auth.mango-office.ru",
            "Referer": sso_url,
            "X-Requested-With": "XMLHttpRequest",
        },
        allow_redirects=False, timeout=30,
    )
    dump_raw(4, "vpbx_login", r4, request_body=body.encode())

    if r4.status_code != 200:
        raise StopCondition(f"vpbx login HTTP {r4.status_code}")
    try:
        vp = r4.json()
    except Exception:
        raise StopCondition("vpbx response not JSON")

    result_code = vp.get("result")
    auth_code = vp.get("auth_code") or vp.get("code")
    if result_code in (1101, 1102, 1107, 1115):
        raise StopCondition(
            f"vpbx returned result={result_code} (invalid_params); SmartCaptcha "
            "validation refused our request. Manual browser login required."
        )
    if not auth_code:
        raise StopCondition(
            f"vpbx returned no auth_code (result={result_code}); cannot continue."
        )

    time.sleep(1)
    start_body = urlencode({
        "client_id": client_id,
        "state": state,
        "redirect_uri": redirect_uri,
        "auth_code": auth_code,
    })
    r5 = session.post(
        "https://auth.mango-office.ru/sso/oidc/session/start",
        data=start_body,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://auth.mango-office.ru",
            "Referer": sso_url,
            "X-Requested-With": "XMLHttpRequest",
        },
        allow_redirects=False, timeout=30,
    )
    dump_raw(5, "session_start", r5, request_body=start_body.encode())
    if r5.status_code not in (200, 204):
        raise StopCondition(f"session/start HTTP {r5.status_code}")

    save_session(session)


# ---------------------------------------------------------------------------
# Report pipeline
# ---------------------------------------------------------------------------

def month_bounds(month: str) -> tuple[dt.date, dt.date]:
    start = dt.datetime.strptime(month, "%Y-%m").date()
    if start.month == 12:
        next_month = dt.date(start.year + 1, 1, 1)
    else:
        next_month = dt.date(start.year, start.month + 1, 1)
    return start, next_month - dt.timedelta(days=1)


def report_base_url(dashboard_url: str) -> str:
    parsed = urlparse(dashboard_url)
    base = urlunparse((parsed.scheme, parsed.netloc, parsed.path.rsplit("/", 1)[0], "", "", ""))
    return base


def request_report(session: requests.Session, dashboard_url: str,
                   period_from: dt.date, period_to: dt.date) -> tuple[str, dict[str, Any]]:
    base = report_base_url(dashboard_url)
    range_str = f"{period_from.strftime('%d.%m.%Y')} - {period_to.strftime('%d.%m.%Y')}"
    body = urlencode({
        "export": "",
        "period": "arbitrary",
        "date-range": range_str,
        "grouping": "0",
    })
    r = session.post(
        f"{base}/report-request",
        data=body,
        headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://lk.mango-office.ru",
            "Referer": dashboard_url,
            "X-Requested-With": "XMLHttpRequest",
        },
        allow_redirects=False, timeout=60,
    )
    return body, r


def poll_status(session: requests.Session, dashboard_url: str, hash_value: str,
                timeout_s: int = 120) -> dict[str, Any]:
    base = report_base_url(dashboard_url)
    body = urlencode({"hash": hash_value, "status": "1"})
    deadline = time.time() + timeout_s
    last = {}
    while time.time() < deadline:
        time.sleep(1.5)
        r = session.post(
            f"{base}/report-status",
            data=body,
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": "https://lk.mango-office.ru",
                "Referer": dashboard_url,
                "X-Requested-With": "XMLHttpRequest",
            },
            allow_redirects=False, timeout=30,
        )
        if r.status_code != 200:
            raise StopCondition(f"report-status HTTP {r.status_code}")
        try:
            last = r.json()
        except Exception as exc:
            raise StopCondition(f"report-status not JSON: {exc}") from exc
        status = last.get("status")
        if status == "complete":
            return last
        if status in {"concurrent-request", "error"}:
            raise StopCondition(f"report-status returned status={status} code={last.get('code')}")
    raise StopCondition("report-status timed out before completion")


def fetch_result(session: requests.Session, dashboard_url: str,
                 hash_value: str) -> dict[str, Any]:
    base = report_base_url(dashboard_url)
    body = urlencode({"hash": hash_value, "type": "get-report",
                      "view": "product-expenses/records-dataset"})
    r = session.post(
        f"{base}/report-result",
        data=body,
        headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://lk.mango-office.ru",
            "Referer": dashboard_url,
            "X-Requested-With": "XMLHttpRequest",
        },
        allow_redirects=False, timeout=120,
    )
    if r.status_code != 200:
        raise StopCondition(f"report-result HTTP {r.status_code}")
    try:
        return r.json()
    except Exception as exc:
        raise StopCondition(f"report-result not JSON: {exc}") from exc


def decimal_from(text: str) -> Decimal | None:
    cleaned = text.replace(" ", " ").replace(" ", "").replace(",", ".")
    cleaned = re.sub(r"[^0-9.\-]", "", cleaned)
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def parse_top_total(html_table: str) -> Decimal:
    """Extract the `pe-tree-l1` row total for ``Мой продуктовый набор №1``."""
    soup = BeautifulSoup(html_table, "lxml")
    # Strategy 1: find row with class pe-tree-l1 whose .title text contains TARGET_PRODUCT
    for row in soup.find_all(attrs={"class": re.compile(r"\bpe-tree-l1\b")}):
        title = row.find(attrs={"class": re.compile(r"\btitle\b")})
        if not title or TARGET_PRODUCT not in title.get_text(" ", strip=True):
            continue
        cost_el = row.find(attrs={"class": re.compile(r"\bcost\b")})
        if cost_el is None:
            continue
        data_cost = cost_el.get("data-cost")
        if data_cost:
            amount = decimal_from(data_cost)
            if amount is not None:
                return amount.quantize(Decimal("0.01"))
        text = cost_el.get_text(" ", strip=True)
        amount = decimal_from(text)
        if amount is not None:
            return amount.quantize(Decimal("0.01"))
    # Strategy 2: pick any element with data-cost adjacent to target text
    for el in soup.find_all(attrs={"data-cost": True}):
        parent = el
        for _ in range(5):
            if parent is None:
                break
            text = parent.get_text(" ", strip=True)
            if TARGET_PRODUCT in text:
                amount = decimal_from(el.get("data-cost"))
                if amount is not None:
                    return amount.quantize(Decimal("0.01"))
            parent = parent.parent
    raise StopCondition(f"top total for {TARGET_PRODUCT!r} was not found in html_table")


def fetch_month_amount(session: requests.Session, dashboard_url: str,
                       month: str, idx: int) -> tuple[Decimal, dict[str, Any]]:
    period_from, period_to = month_bounds(month)
    body, r_req = request_report(session, dashboard_url, period_from, period_to)
    dump_raw(idx, f"report_request_{month}", r_req, request_body=body.encode())
    if r_req.status_code in (401, 403):
        raise StopCondition(f"report-request HTTP {r_req.status_code}; session lost")
    if r_req.status_code != 200:
        raise StopCondition(f"report-request HTTP {r_req.status_code}")
    try:
        req_payload = r_req.json()
    except Exception as exc:
        raise StopCondition(f"report-request not JSON: {exc}") from exc
    hash_value = req_payload.get("hash")
    if not hash_value:
        raise StopCondition(f"report-request returned no hash: {req_payload}")

    if req_payload.get("status") != "complete":
        poll_payload = poll_status(session, dashboard_url, hash_value)
    else:
        poll_payload = req_payload

    result_payload = fetch_result(session, dashboard_url, hash_value)
    raw_path = RAW_DIR / f"{idx+1:02d}_report_result_{month}.json"
    raw_path.write_text(json.dumps(result_payload, ensure_ascii=False)[:5_000_000], encoding="utf-8")

    html_table = result_payload.get("html_table", "")
    if TARGET_PRODUCT not in html_table:
        raise StopCondition(f"{TARGET_PRODUCT!r} missing in html_table for {month}")
    amount = parse_top_total(html_table)
    meta = {"raw_path": str(raw_path.relative_to(PROJECT_ROOT))}
    return amount, meta


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def write_mango_csv(rows: list[dict[str, str]]) -> None:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    path = PROCESSED_DIR / "mango_office_monthly.csv"
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "month", "mango_office_amount", "source", "period_from", "period_to",
            "captured_at", "control_status", "raw_path", "notes",
        ])
        writer.writeheader()
        writer.writerows(rows)


def write_telecom_csv(rows: list[dict[str, str]]) -> None:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    path = PROCESSED_DIR / "telecom_monthly.csv"
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "month", "mango_office", "ihc_ru_server",
            "mikroel_internet", "telecom_total", "source",
        ])
        writer.writeheader()
        for row in rows:
            mango = Decimal(row["mango_office_amount"])
            total = mango + IHC_RU_SERVER + MIKROEL_INTERNET
            writer.writerow({
                "month": row["month"],
                "mango_office": f"{mango:.2f}",
                "ihc_ru_server": f"{IHC_RU_SERVER:.2f}",
                "mikroel_internet": f"{MIKROEL_INTERNET:.2f}",
                "telecom_total": f"{total:.2f}",
                "source": "Mango Office LK + fixed iHC.ru 600 + fixed Mikroel 3230",
            })


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--attempt-login", action="store_true",
                    help="Try OIDC login (expected to fail because of SmartCaptcha).")
    ap.add_argument("--discover-only", action="store_true",
                    help="Just walk the public OIDC discovery chain and stop.")
    args = ap.parse_args(argv)

    load_env()
    dashboard_url = require_env("MANGO_OFFICE_LK_URL")
    print(f"Dashboard URL: {mask_url(dashboard_url)}")

    session = make_session()
    try:
        if args.discover_only:
            r = session.get("https://lk.mango-office.ru/", allow_redirects=False,
                            timeout=30,
                            headers={"Accept": "text/html"})
            dump_raw(1, "lk_root", r)
            print("Discovery only; see research/private/mango/raw/01_lk_root.* for details.")
            return 0

        if not try_resume_session(session, dashboard_url):
            if not args.attempt_login:
                raise StopCondition(
                    "No usable session. Browser login is required because the Mango "
                    "auth SPA gates `/sso/oidc/auth/vpbx` behind Yandex SmartCaptcha. "
                    "Either:\n"
                    "  (a) log in via a real browser, then export the lk.mango-office.ru "
                    "cookies into research/private/mango/session.json and rerun; or\n"
                    "  (b) rerun with --attempt-login to confirm the SmartCaptcha block "
                    "in the raw artefacts."
                )
            try:
                oidc_login(session, allow_attempt=True)
            except StopCondition as exc:
                raise StopCondition(
                    f"OIDC login attempt stopped: {exc}. "
                    "Headless login is blocked by SmartCaptcha and/or MFA per design."
                )

        print("Session ok; pulling 6 monthly reports.")
        rows: list[dict[str, str]] = []
        captured_at = dt.datetime.now(dt.timezone.utc).isoformat()
        for i, month in enumerate(MONTHS):
            if i:
                time.sleep(2)
            print(f"  -> {month}")
            amount, meta = fetch_month_amount(session, dashboard_url, month, idx=10 + i * 2)
            period_from, period_to = month_bounds(month)
            status = "captured"
            notes = ""
            if month == "2026-04":
                diff = abs(amount - APRIL_CONTROL)
                if diff <= Decimal("1"):
                    status = "passed_control"
                else:
                    status = "mismatch"
                    notes = f"Expected approx {APRIL_CONTROL:.2f}, diff {diff}"
            rows.append({
                "month": month,
                "mango_office_amount": f"{amount:.2f}",
                "source": "Mango Office LK (headless POST via OIDC)",
                "period_from": period_from.isoformat(),
                "period_to": period_to.isoformat(),
                "captured_at": captured_at,
                "control_status": status,
                "raw_path": meta.get("raw_path", ""),
                "notes": notes,
            })
            print(f"     amount={amount}, status={status}")

        write_mango_csv(rows)
        write_telecom_csv(rows)
        print("Saved CSVs under research/processed/iiko/telecom/.")
        return 0

    except StopCondition as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
