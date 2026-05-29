#!/usr/bin/env python3
"""Mail.ru mailbox connector over IMAP/SMTP.

The script keeps all downloaded message data under research/private/mail/.
Credentials are read from .env/ENV and are never printed.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import email.utils
import hashlib
import imaplib
import json
import mimetypes
import os
import re
import smtplib
import sqlite3
import ssl
import sys
import time
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB_PATH = PROJECT_ROOT / "research/private/mail/mail.sqlite3"
DEFAULT_ATTACHMENTS_DIR = PROJECT_ROOT / "research/private/mail/attachments"
DEFAULT_IMAP_HOST = "imap.mail.ru"
DEFAULT_SMTP_HOST = "smtp.mail.ru"
DEFAULT_IMAP_PORT = 993
DEFAULT_SMTP_PORT = 465
MAILRU_ACCOUNT_ENV = {
    "personal": ("MAILRU_EMAIL", "MAILRU_APP_PASSWORD"),
    "workmail": ("MAILRU_WORKMAIL", "MAILRU_WORKMAIL_PASSWORD"),
}
MAILRU_ACCOUNT_ALL = "all"


@dataclass(frozen=True)
class MailConfig:
    account: str
    email: str
    app_password: str
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    db_path: Path
    attachments_dir: Path


@dataclass(frozen=True)
class MailFolder:
    encoded_name: str
    display_name: str
    attributes: tuple[str, ...]


@dataclass(frozen=True)
class ParsedMessage:
    message_id: str
    in_reply_to: str
    references_header: str
    thread_key: str
    subject: str
    normalized_subject: str
    from_addr: str
    to_addrs: str
    cc_addrs: str
    date_utc: str
    body_text: str
    body_html: str
    raw_headers_json: str
    attachments: list[tuple[str, str, bytes]]


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


def resolve_project_path(value: str, default: Path) -> Path:
    if not value:
        return default
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def load_config(require_password: bool = True, account: str = "") -> MailConfig:
    load_local_env()
    account_name = (account or env_value("MAILRU_ACCOUNT", "personal") or "personal").strip().casefold()
    if account_name == MAILRU_ACCOUNT_ALL:
        raise SystemExit("--account all is supported only for read-only commands: check, folders, sync, summary, messages.")
    if account_name not in MAILRU_ACCOUNT_ENV:
        allowed = ", ".join(sorted(MAILRU_ACCOUNT_ENV))
        raise SystemExit(f"Unknown Mail.ru account '{account_name}'. Expected one of: {allowed}.")
    email_env, password_env = MAILRU_ACCOUNT_ENV[account_name]
    account_email = required_env(email_env)
    app_password = required_env(password_env) if require_password else env_value(password_env)
    return MailConfig(
        account=account_name,
        email=account_email,
        app_password=app_password,
        imap_host=env_value("MAILRU_IMAP_HOST", DEFAULT_IMAP_HOST),
        imap_port=int(env_value("MAILRU_IMAP_PORT", str(DEFAULT_IMAP_PORT))),
        smtp_host=env_value("MAILRU_SMTP_HOST", DEFAULT_SMTP_HOST),
        smtp_port=int(env_value("MAILRU_SMTP_PORT", str(DEFAULT_SMTP_PORT))),
        db_path=resolve_project_path(env_value("MAIL_DB_PATH"), DEFAULT_DB_PATH),
        attachments_dir=resolve_project_path(env_value("MAIL_ATTACHMENTS_DIR"), DEFAULT_ATTACHMENTS_DIR),
    )


def load_config_from_args(args: argparse.Namespace, require_password: bool = True) -> MailConfig:
    return load_config(require_password=require_password, account=getattr(args, "account", ""))


def load_configs_from_args(args: argparse.Namespace, require_password: bool = True) -> list[MailConfig]:
    load_local_env()
    account_name = (getattr(args, "account", "") or env_value("MAILRU_ACCOUNT", "personal") or "personal").strip().casefold()
    if account_name == MAILRU_ACCOUNT_ALL:
        return [load_config(require_password=require_password, account=name) for name in MAILRU_ACCOUNT_ENV]
    return [load_config(require_password=require_password, account=account_name)]


def account_prefix(configs: list[MailConfig], config: MailConfig) -> str:
    return f"[{config.account}] " if len(configs) > 1 else ""


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def safe_filename(value: str, fallback: str) -> str:
    name = value.strip() or fallback
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:180] or fallback


def safe_folder(value: str) -> str:
    return safe_filename(value.replace("/", "_"), "folder")


def ensure_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS mail_folders (
            id INTEGER PRIMARY KEY,
            account_email TEXT NOT NULL,
            encoded_name TEXT NOT NULL,
            display_name TEXT NOT NULL,
            attributes_json TEXT NOT NULL,
            uidvalidity TEXT,
            last_uid INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            UNIQUE(account_email, encoded_name)
        );

        CREATE TABLE IF NOT EXISTS mail_threads (
            id INTEGER PRIMARY KEY,
            account_email TEXT NOT NULL,
            thread_key TEXT NOT NULL,
            normalized_subject TEXT NOT NULL,
            first_message_at TEXT,
            last_message_at TEXT,
            message_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            UNIQUE(account_email, thread_key)
        );

        CREATE TABLE IF NOT EXISTS mail_messages (
            id INTEGER PRIMARY KEY,
            account_email TEXT NOT NULL,
            folder_encoded TEXT NOT NULL,
            folder_name TEXT NOT NULL,
            uidvalidity TEXT NOT NULL,
            uid INTEGER NOT NULL,
            message_id TEXT,
            thread_key TEXT NOT NULL,
            in_reply_to TEXT,
            references_header TEXT,
            subject TEXT,
            normalized_subject TEXT NOT NULL,
            from_addr TEXT,
            to_addrs TEXT,
            cc_addrs TEXT,
            date_utc TEXT,
            internaldate TEXT,
            flags_json TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            has_attachments INTEGER NOT NULL DEFAULT 0,
            body_text TEXT,
            body_html TEXT,
            raw_headers_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(account_email, folder_encoded, uidvalidity, uid)
        );

        CREATE INDEX IF NOT EXISTS idx_mail_messages_thread
            ON mail_messages(account_email, thread_key);
        CREATE INDEX IF NOT EXISTS idx_mail_messages_message_id
            ON mail_messages(account_email, message_id);
        CREATE INDEX IF NOT EXISTS idx_mail_messages_date
            ON mail_messages(account_email, date_utc);

        CREATE TABLE IF NOT EXISTS mail_attachments (
            id INTEGER PRIMARY KEY,
            message_db_id INTEGER NOT NULL REFERENCES mail_messages(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            content_type TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            storage_path TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS mail_outbox (
            id INTEGER PRIMARY KEY,
            account_email TEXT NOT NULL,
            reply_to_message_db_id INTEGER,
            to_addrs TEXT NOT NULL,
            subject TEXT NOT NULL,
            message_id TEXT NOT NULL,
            status TEXT NOT NULL,
            error TEXT,
            created_at TEXT NOT NULL,
            sent_at TEXT
        );
        """
    )
    return db


def decode_modutf7(value: str) -> str:
    """Decode IMAP modified UTF-7 mailbox names."""

    result: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char != "&":
            result.append(char)
            index += 1
            continue

        end = value.find("-", index)
        if end == -1:
            result.append(value[index:])
            break
        token = value[index + 1 : end]
        if token == "":
            result.append("&")
        else:
            padded = token.replace(",", "/")
            padded += "=" * ((4 - len(padded) % 4) % 4)
            try:
                result.append(base64.b64decode(padded).decode("utf-16-be"))
            except (UnicodeDecodeError, ValueError):
                result.append(value[index : end + 1])
        index = end + 1
    return "".join(result)


def encode_modutf7(value: str) -> str:
    """Encode a display mailbox name to IMAP modified UTF-7."""

    result: list[str] = []
    buffer: list[str] = []

    def flush_buffer() -> None:
        if not buffer:
            return
        raw = "".join(buffer).encode("utf-16-be")
        encoded = base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",")
        result.append(f"&{encoded}-")
        buffer.clear()

    for char in value:
        codepoint = ord(char)
        if char == "&":
            flush_buffer()
            result.append("&-")
        elif 0x20 <= codepoint <= 0x7E:
            flush_buffer()
            result.append(char)
        else:
            buffer.append(char)
    flush_buffer()
    return "".join(result)


def quote_mailbox(encoded_name: str) -> str:
    escaped = encoded_name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def unquote_imap_value(value: bytes) -> str:
    value = value.strip()
    if len(value) >= 2 and value.startswith(b'"') and value.endswith(b'"'):
        value = value[1:-1].replace(br"\\", b"\\").replace(br"\"", b'"')
    return value.decode("ascii", "replace")


def parse_list_response(line: bytes) -> MailFolder | None:
    match = re.match(rb"\((?P<attrs>.*?)\)\s+(?P<delimiter>NIL|\".*?\"|\S+)\s+(?P<name>.+)$", line)
    if not match:
        return None
    attrs_raw = match.group("attrs").decode("ascii", "replace")
    attributes = tuple(sorted(re.findall(r"\\[A-Za-z]+", attrs_raw)))
    encoded_name = unquote_imap_value(match.group("name"))
    return MailFolder(
        encoded_name=encoded_name,
        display_name=decode_modutf7(encoded_name),
        attributes=attributes,
    )


def imap_connect(config: MailConfig) -> imaplib.IMAP4_SSL:
    context = ssl.create_default_context()
    conn = imaplib.IMAP4_SSL(config.imap_host, config.imap_port, ssl_context=context)
    conn.login(config.email, config.app_password)
    return conn


def smtp_connect(config: MailConfig) -> smtplib.SMTP_SSL:
    context = ssl.create_default_context()
    smtp = smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, context=context, timeout=60)
    smtp.login(config.email, config.app_password)
    return smtp


def discover_folders(conn: imaplib.IMAP4_SSL) -> list[MailFolder]:
    status, rows = conn.list()
    if status != "OK":
        raise RuntimeError("Could not list IMAP folders")
    folders: list[MailFolder] = []
    for row in rows or []:
        if not isinstance(row, bytes):
            continue
        folder = parse_list_response(row)
        if folder:
            folders.append(folder)
    folders.sort(key=lambda item: (item.display_name.casefold() != "inbox", item.display_name.casefold()))
    return folders


def upsert_folders(db: sqlite3.Connection, config: MailConfig, folders: list[MailFolder]) -> None:
    now = utc_now()
    for folder in folders:
        db.execute(
            """
            INSERT INTO mail_folders (
                account_email, encoded_name, display_name, attributes_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(account_email, encoded_name) DO UPDATE SET
                display_name = excluded.display_name,
                attributes_json = excluded.attributes_json,
                updated_at = excluded.updated_at
            """,
            (
                config.email,
                folder.encoded_name,
                folder.display_name,
                json.dumps(folder.attributes, ensure_ascii=False),
                now,
            ),
        )
    db.commit()


def folder_is_sent(folder: MailFolder) -> bool:
    name = folder.display_name.casefold()
    return "\\Sent" in folder.attributes or "sent" in name or "отправ" in name


def resolve_folders(
    folders: list[MailFolder],
    requested: list[str] | None,
    all_folders: bool,
) -> list[MailFolder]:
    if all_folders:
        return folders
    if requested:
        resolved: list[MailFolder] = []
        by_display = {folder.display_name.casefold(): folder for folder in folders}
        by_encoded = {folder.encoded_name.casefold(): folder for folder in folders}
        for name in requested:
            key = name.casefold()
            folder = by_display.get(key) or by_encoded.get(key) or by_encoded.get(encode_modutf7(name).casefold())
            if not folder:
                raise SystemExit(f"Folder not found: {name}. Run folders command first.")
            resolved.append(folder)
        return dedupe_folders(resolved)

    defaults: list[MailFolder] = []
    for folder in folders:
        if folder.display_name.casefold() == "inbox" or folder_is_sent(folder):
            defaults.append(folder)
    return dedupe_folders(defaults)


def dedupe_folders(folders: list[MailFolder]) -> list[MailFolder]:
    seen: set[str] = set()
    result: list[MailFolder] = []
    for folder in folders:
        if folder.encoded_name in seen:
            continue
        seen.add(folder.encoded_name)
        result.append(folder)
    return result


def get_folder_state(db: sqlite3.Connection, config: MailConfig, folder: MailFolder) -> tuple[str, int]:
    row = db.execute(
        """
        SELECT uidvalidity, last_uid
        FROM mail_folders
        WHERE account_email = ? AND encoded_name = ?
        """,
        (config.email, folder.encoded_name),
    ).fetchone()
    if not row:
        return "", 0
    return row["uidvalidity"] or "", int(row["last_uid"] or 0)


def update_folder_state(
    db: sqlite3.Connection,
    config: MailConfig,
    folder: MailFolder,
    uidvalidity: str,
    last_uid: int,
) -> None:
    db.execute(
        """
        UPDATE mail_folders
        SET uidvalidity = ?, last_uid = MAX(last_uid, ?), updated_at = ?
        WHERE account_email = ? AND encoded_name = ?
        """,
        (uidvalidity, last_uid, utc_now(), config.email, folder.encoded_name),
    )
    db.commit()


def imap_uidvalidity(conn: imaplib.IMAP4_SSL) -> str:
    response = conn.response("UIDVALIDITY")[1]
    if response and response[0]:
        return response[0].decode("ascii", "replace")
    return "0"


def parse_fetch_meta(meta: bytes) -> tuple[list[str], str]:
    flags: list[str] = []
    flags_match = re.search(rb"FLAGS \((.*?)\)", meta)
    if flags_match:
        flags = [item.decode("ascii", "replace") for item in flags_match.group(1).split()]
    internaldate = ""
    date_match = re.search(rb'INTERNALDATE "([^"]+)"', meta)
    if date_match:
        internaldate = date_match.group(1).decode("ascii", "replace")
    return flags, internaldate


def decode_addresses(value: str) -> str:
    if not value:
        return ""
    addresses = email.utils.getaddresses([value])
    normalized = []
    for name, addr in addresses:
        label = f"{name} <{addr}>" if name and addr else addr or name
        if label:
            normalized.append(label)
    return ", ".join(normalized)


def header_value(msg: Message, name: str) -> str:
    value = msg.get(name, "")
    return str(value or "").strip()


def parse_message_date(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_subject(value: str) -> str:
    subject = re.sub(r"\s+", " ", value or "").strip()
    changed = True
    while changed:
        changed = False
        updated = re.sub(r"^\s*((re|fw|fwd|ответ|пересл)\s*:\s*)+", "", subject, flags=re.IGNORECASE)
        if updated != subject:
            changed = True
            subject = updated.strip()
    return subject or "(no subject)"


def message_ids_from_header(value: str) -> list[str]:
    if not value:
        return []
    ids = re.findall(r"<[^>]+>", value)
    if ids:
        return ids
    return [item.strip() for item in value.split() if item.strip()]


def compute_thread_key(message_id: str, in_reply_to: str, references_header: str, normalized_subject: str) -> str:
    references = message_ids_from_header(references_header)
    replies = message_ids_from_header(in_reply_to)
    if references:
        return references[0]
    if replies:
        return replies[0]
    if message_id:
        return message_id
    digest = hashlib.sha256(normalized_subject.casefold().encode("utf-8")).hexdigest()[:16]
    return f"subject:{digest}"


def content_text(part: Message) -> str:
    try:
        value = part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, "replace")
    return value if isinstance(value, str) else str(value)


def parse_email(raw_email: bytes, body_max_chars: int) -> ParsedMessage:
    msg = BytesParser(policy=policy.default).parsebytes(raw_email)
    text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[tuple[str, str, bytes]] = []

    for part in msg.walk():
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        disposition = (part.get_content_disposition() or "").casefold()
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename or disposition == "attachment":
            attachments.append((filename or "attachment", content_type, payload))
            continue
        if content_type == "text/plain":
            text_parts.append(content_text(part))
        elif content_type == "text/html":
            html_parts.append(content_text(part))

    subject = header_value(msg, "Subject")
    normalized_subject = normalize_subject(subject)
    message_id = header_value(msg, "Message-ID")
    in_reply_to = header_value(msg, "In-Reply-To")
    references_header = header_value(msg, "References")
    headers = {key: header_value(msg, key) for key in msg.keys()}
    return ParsedMessage(
        message_id=message_id,
        in_reply_to=in_reply_to,
        references_header=references_header,
        thread_key=compute_thread_key(message_id, in_reply_to, references_header, normalized_subject),
        subject=subject,
        normalized_subject=normalized_subject,
        from_addr=decode_addresses(header_value(msg, "From")),
        to_addrs=decode_addresses(header_value(msg, "To")),
        cc_addrs=decode_addresses(header_value(msg, "Cc")),
        date_utc=parse_message_date(header_value(msg, "Date")),
        body_text="\n".join(text_parts).strip()[:body_max_chars],
        body_html="\n".join(html_parts).strip()[:body_max_chars],
        raw_headers_json=json.dumps(headers, ensure_ascii=False, sort_keys=True),
        attachments=attachments,
    )


def upsert_message(
    db: sqlite3.Connection,
    config: MailConfig,
    folder: MailFolder,
    uidvalidity: str,
    uid: int,
    flags: list[str],
    internaldate: str,
    raw_email: bytes,
    parsed: ParsedMessage,
) -> int:
    now = utc_now()
    db.execute(
        """
        INSERT INTO mail_messages (
            account_email, folder_encoded, folder_name, uidvalidity, uid,
            message_id, thread_key, in_reply_to, references_header, subject,
            normalized_subject, from_addr, to_addrs, cc_addrs, date_utc,
            internaldate, flags_json, size_bytes, has_attachments, body_text,
            body_html, raw_headers_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(account_email, folder_encoded, uidvalidity, uid) DO UPDATE SET
            folder_name = excluded.folder_name,
            message_id = excluded.message_id,
            thread_key = excluded.thread_key,
            in_reply_to = excluded.in_reply_to,
            references_header = excluded.references_header,
            subject = excluded.subject,
            normalized_subject = excluded.normalized_subject,
            from_addr = excluded.from_addr,
            to_addrs = excluded.to_addrs,
            cc_addrs = excluded.cc_addrs,
            date_utc = excluded.date_utc,
            internaldate = excluded.internaldate,
            flags_json = excluded.flags_json,
            size_bytes = excluded.size_bytes,
            has_attachments = excluded.has_attachments,
            body_text = excluded.body_text,
            body_html = excluded.body_html,
            raw_headers_json = excluded.raw_headers_json,
            updated_at = excluded.updated_at
        """,
        (
            config.email,
            folder.encoded_name,
            folder.display_name,
            uidvalidity,
            uid,
            parsed.message_id,
            parsed.thread_key,
            parsed.in_reply_to,
            parsed.references_header,
            parsed.subject,
            parsed.normalized_subject,
            parsed.from_addr,
            parsed.to_addrs,
            parsed.cc_addrs,
            parsed.date_utc,
            internaldate,
            json.dumps(flags, ensure_ascii=False),
            len(raw_email),
            1 if parsed.attachments else 0,
            parsed.body_text,
            parsed.body_html,
            parsed.raw_headers_json,
            now,
            now,
        ),
    )
    row = db.execute(
        """
        SELECT id
        FROM mail_messages
        WHERE account_email = ? AND folder_encoded = ? AND uidvalidity = ? AND uid = ?
        """,
        (config.email, folder.encoded_name, uidvalidity, uid),
    ).fetchone()
    if not row:
        raise RuntimeError("Could not read message row after upsert")
    message_db_id = int(row["id"])
    db.execute("DELETE FROM mail_attachments WHERE message_db_id = ?", (message_db_id,))
    save_attachments(db, config, folder, message_db_id, parsed.attachments)
    upsert_thread(db, config, parsed)
    db.commit()
    return message_db_id


def save_attachments(
    db: sqlite3.Connection,
    config: MailConfig,
    folder: MailFolder,
    message_db_id: int,
    attachments: list[tuple[str, str, bytes]],
) -> None:
    if not attachments:
        return
    base_dir = (
        config.attachments_dir
        / safe_filename(config.email, "account")
        / safe_folder(folder.display_name)
        / str(message_db_id)
    )
    base_dir.mkdir(parents=True, exist_ok=True)
    for index, (filename, content_type, payload) in enumerate(attachments, start=1):
        safe_name = safe_filename(filename, f"attachment-{index}")
        target = base_dir / f"{index:02d}-{safe_name}"
        target.write_bytes(payload)
        db.execute(
            """
            INSERT INTO mail_attachments (
                message_db_id, filename, content_type, size_bytes, sha256, storage_path, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_db_id,
                filename,
                content_type,
                len(payload),
                hashlib.sha256(payload).hexdigest(),
                rel(target),
                utc_now(),
            ),
        )


def upsert_thread(db: sqlite3.Connection, config: MailConfig, parsed: ParsedMessage) -> None:
    now = utc_now()
    db.execute(
        """
        INSERT INTO mail_threads (
            account_email, thread_key, normalized_subject, first_message_at,
            last_message_at, message_count, updated_at
        )
        VALUES (?, ?, ?, ?, ?, 0, ?)
        ON CONFLICT(account_email, thread_key) DO UPDATE SET
            normalized_subject = excluded.normalized_subject,
            updated_at = excluded.updated_at
        """,
        (
            config.email,
            parsed.thread_key,
            parsed.normalized_subject,
            parsed.date_utc,
            parsed.date_utc,
            now,
        ),
    )
    db.execute(
        """
        UPDATE mail_threads
        SET
            first_message_at = (
                SELECT MIN(NULLIF(date_utc, ''))
                FROM mail_messages
                WHERE account_email = ? AND thread_key = ?
            ),
            last_message_at = (
                SELECT MAX(NULLIF(date_utc, ''))
                FROM mail_messages
                WHERE account_email = ? AND thread_key = ?
            ),
            message_count = (
                SELECT COUNT(*)
                FROM mail_messages
                WHERE account_email = ? AND thread_key = ?
            ),
            updated_at = ?
        WHERE account_email = ? AND thread_key = ?
        """,
        (
            config.email,
            parsed.thread_key,
            config.email,
            parsed.thread_key,
            config.email,
            parsed.thread_key,
            now,
            config.email,
            parsed.thread_key,
        ),
    )


def uid_search(conn: imaplib.IMAP4_SSL, last_uid: int, full: bool) -> list[int]:
    if full or last_uid <= 0:
        criteria = "ALL"
    else:
        criteria = f"UID {last_uid + 1}:*"
    status, rows = conn.uid("SEARCH", None, criteria)
    if status != "OK":
        raise RuntimeError(f"UID SEARCH failed for criteria: {criteria}")
    raw = rows[0] if rows else b""
    return [int(value) for value in raw.split() if value.isdigit()]


def fetch_raw_message(conn: imaplib.IMAP4_SSL, uid: int) -> tuple[list[str], str, bytes]:
    status, rows = conn.uid("FETCH", str(uid), "(UID FLAGS INTERNALDATE RFC822)")
    if status != "OK":
        raise RuntimeError(f"UID FETCH failed for {uid}")
    meta = b""
    raw_email = b""
    for row in rows or []:
        if isinstance(row, tuple):
            meta += row[0] or b""
            raw_email = row[1] or raw_email
        elif isinstance(row, bytes):
            meta += row
    if not raw_email:
        raise RuntimeError(f"UID FETCH returned empty message for {uid}")
    flags, internaldate = parse_fetch_meta(meta)
    return flags, internaldate, raw_email


def command_check(args: argparse.Namespace) -> int:
    configs = load_configs_from_args(args, require_password=True)
    for config in configs:
        prefix = account_prefix(configs, config)
        db = ensure_db(config.db_path)
        conn: imaplib.IMAP4_SSL | None = None
        try:
            conn = imap_connect(config)
            folders = discover_folders(conn)
            upsert_folders(db, config, folders)
            print(f"{prefix}IMAP OK: {config.imap_host}:{config.imap_port}, folders={len(folders)}")
        finally:
            if conn is not None:
                try:
                    conn.logout()
                except Exception:
                    pass
        smtp = smtp_connect(config)
        smtp.quit()
        print(f"{prefix}SMTP OK: {config.smtp_host}:{config.smtp_port}")
        print(f"{prefix}DB: {rel(config.db_path)}")
    return 0


def command_folders(args: argparse.Namespace) -> int:
    configs = load_configs_from_args(args, require_password=True)
    for config in configs:
        prefix = account_prefix(configs, config)
        db = ensure_db(config.db_path)
        conn = imap_connect(config)
        try:
            folders = discover_folders(conn)
            upsert_folders(db, config, folders)
        finally:
            conn.logout()
        for folder in folders:
            attrs = ", ".join(folder.attributes) if folder.attributes else "-"
            marker = " sent" if folder_is_sent(folder) else ""
            print(f"{prefix}{folder.display_name} [{folder.encoded_name}] attrs={attrs}{marker}")
    return 0


def command_sync(args: argparse.Namespace) -> int:
    configs = load_configs_from_args(args, require_password=True)
    total_messages = 0
    last_db_path = None
    for config in configs:
        prefix = account_prefix(configs, config)
        db = ensure_db(config.db_path)
        last_db_path = config.db_path
        conn = imap_connect(config)
        try:
            folders = discover_folders(conn)
            upsert_folders(db, config, folders)
            selected_folders = resolve_folders(folders, args.folder, args.all_folders)
            if not selected_folders:
                raise SystemExit("No folders selected. Run folders command and pass --folder explicitly.")
            for folder in selected_folders:
                status, _ = conn.select(quote_mailbox(folder.encoded_name), readonly=True)
                if status != "OK":
                    print(f"{prefix}SKIP {folder.display_name}: SELECT failed", file=sys.stderr)
                    continue
                uidvalidity = imap_uidvalidity(conn)
                stored_uidvalidity, last_uid = get_folder_state(db, config, folder)
                if stored_uidvalidity and stored_uidvalidity != uidvalidity:
                    last_uid = 0
                uids = uid_search(conn, last_uid, args.full)
                if args.limit and len(uids) > args.limit:
                    uids = uids[-args.limit :]
                print(f"{prefix}{folder.display_name}: {len(uids)} new/selected messages")
                max_uid = last_uid
                for uid in uids:
                    try:
                        flags, internaldate, raw_email = fetch_raw_message(conn, uid)
                        parsed = parse_email(raw_email, args.body_max_chars)
                        message_db_id = upsert_message(
                            db,
                            config,
                            folder,
                            uidvalidity,
                            uid,
                            flags,
                            internaldate,
                            raw_email,
                            parsed,
                        )
                        max_uid = max(max_uid, uid)
                        total_messages += 1
                        if args.verbose:
                            print(f"  {prefix}#{message_db_id} uid={uid} {parsed.subject[:100]}")
                    except Exception as exc:
                        print(f"  {prefix}ERROR uid={uid}: {exc}", file=sys.stderr)
                update_folder_state(db, config, folder, uidvalidity, max_uid)
                try:
                    conn.close()
                except Exception:
                    pass
        finally:
            conn.logout()
    print(f"Synced messages: {total_messages}")
    if last_db_path is not None:
        print(f"DB: {rel(last_db_path)}")
    return 0


def ensure_reply_subject(subject: str) -> str:
    if re.match(r"^\s*re\s*:", subject or "", flags=re.IGNORECASE):
        return subject
    return f"Re: {subject or '(no subject)'}"


def first_address(value: str) -> str:
    addresses = email.utils.getaddresses([value or ""])
    for _name, addr in addresses:
        if addr:
            return addr
    return ""


def original_reply_recipient(config: MailConfig, row: sqlite3.Row) -> str:
    from_addr = first_address(row["from_addr"] or "")
    if from_addr.casefold() == config.email.casefold():
        to_addr = first_address(row["to_addrs"] or "")
        if to_addr:
            return to_addr
    if from_addr:
        return from_addr
    raise SystemExit("Original message has no usable From address")


def build_reply_message(config: MailConfig, row: sqlite3.Row, text: str, html: str, to_addr: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = config.email
    msg["To"] = to_addr
    msg["Subject"] = ensure_reply_subject(row["subject"] or "")
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=config.email.split("@")[-1])
    original_message_id = row["message_id"] or ""
    if original_message_id:
        msg["In-Reply-To"] = original_message_id
        references = row["references_header"] or ""
        if original_message_id not in references:
            references = f"{references} {original_message_id}".strip()
        if references:
            msg["References"] = references
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def read_text_arg(value: str, file_value: str) -> str:
    if value:
        return value
    if file_value:
        return resolve_input_path(file_value).read_text(encoding="utf-8")
    raise SystemExit("Provide --text or --text-file")


def resolve_input_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def parse_address_args(values: list[str]) -> list[tuple[str, str]]:
    parsed: list[tuple[str, str]] = []
    for name, addr in email.utils.getaddresses(values):
        if not addr:
            continue
        formatted = email.utils.formataddr((name, addr)) if name else addr
        parsed.append((formatted, addr))
    return parsed


def address_header(addresses: list[tuple[str, str]]) -> str:
    return ", ".join(formatted for formatted, _addr in addresses)


def address_recipients(*groups: list[tuple[str, str]]) -> list[str]:
    recipients: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for _formatted, addr in group:
            key = addr.casefold()
            if key in seen:
                continue
            seen.add(key)
            recipients.append(addr)
    return recipients


def attach_files(message: EmailMessage, attachment_paths: list[str]) -> None:
    for value in attachment_paths:
        path = resolve_input_path(value)
        payload = path.read_bytes()
        content_type, _encoding = mimetypes.guess_type(str(path))
        if not content_type:
            content_type = "application/octet-stream"
        maintype, subtype = content_type.split("/", 1)
        message.add_attachment(payload, maintype=maintype, subtype=subtype, filename=path.name)


def find_sent_folder(conn: imaplib.IMAP4_SSL) -> MailFolder | None:
    folders = discover_folders(conn)
    for folder in folders:
        if folder_is_sent(folder):
            return folder
    return None


def append_sent_copy(config: MailConfig, message: EmailMessage) -> bool:
    conn = imap_connect(config)
    try:
        folder = find_sent_folder(conn)
        if not folder:
            return False
        status, _ = conn.append(
            quote_mailbox(folder.encoded_name),
            "\\Seen",
            imaplib.Time2Internaldate(time.time()),
            message.as_bytes(policy=policy.SMTP),
        )
        return status == "OK"
    finally:
        conn.logout()


def command_reply(args: argparse.Namespace) -> int:
    config = load_config_from_args(args, require_password=True)
    db = ensure_db(config.db_path)
    row = db.execute(
        "SELECT * FROM mail_messages WHERE id = ? AND account_email = ?",
        (args.message_db_id, config.email),
    ).fetchone()
    if not row:
        raise SystemExit(f"Message not found in DB: {args.message_db_id}")
    text = read_text_arg(args.text, args.text_file)
    html = resolve_input_path(args.html_file).read_text(encoding="utf-8") if args.html_file else ""
    to_addr = args.to or original_reply_recipient(config, row)
    message = build_reply_message(config, row, text, html, to_addr)
    attach_files(message, args.attachment or [])
    now = utc_now()

    if args.dry_run:
        print("Dry run: message was not sent")
        print(f"To: {to_addr}")
        print(f"Subject: {message['Subject']}")
        print(f"In-Reply-To: {message.get('In-Reply-To', '')}")
        if args.attachment:
            print(f"Attachments: {len(args.attachment)}")
        return 0

    outbox_id = db.execute(
        """
        INSERT INTO mail_outbox (
            account_email, reply_to_message_db_id, to_addrs, subject,
            message_id, status, created_at
        )
        VALUES (?, ?, ?, ?, ?, 'sending', ?)
        """,
        (config.email, args.message_db_id, to_addr, str(message["Subject"]), str(message["Message-ID"]), now),
    ).lastrowid
    db.commit()

    try:
        smtp = smtp_connect(config)
        try:
            smtp.send_message(message)
        finally:
            smtp.quit()
        sent_appended = True if args.no_append_sent else append_sent_copy(config, message)
        db.execute(
            """
            UPDATE mail_outbox
            SET status = ?, sent_at = ?, error = NULL
            WHERE id = ?
            """,
            ("sent" if sent_appended else "sent_without_imap_append", utc_now(), outbox_id),
        )
        db.commit()
        print(f"Sent reply to {to_addr}")
        if not sent_appended:
            print("Warning: SMTP send succeeded, but IMAP Sent append was not confirmed", file=sys.stderr)
        return 0
    except Exception as exc:
        db.execute(
            "UPDATE mail_outbox SET status = 'error', error = ? WHERE id = ?",
            (str(exc)[:1000], outbox_id),
        )
        db.commit()
        raise


def build_new_message(
    config: MailConfig,
    to_addrs: list[tuple[str, str]],
    cc_addrs: list[tuple[str, str]],
    subject: str,
    text: str,
    html: str,
    attachment_paths: list[str],
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = config.email
    msg["To"] = address_header(to_addrs)
    if cc_addrs:
        msg["Cc"] = address_header(cc_addrs)
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=config.email.split("@")[-1])
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    attach_files(msg, attachment_paths)
    return msg


def command_send(args: argparse.Namespace) -> int:
    config = load_config_from_args(args, require_password=True)
    db = ensure_db(config.db_path)
    to_addrs = parse_address_args(args.to or [])
    cc_addrs = parse_address_args(args.cc or [])
    bcc_addrs = parse_address_args(args.bcc or [])
    if not to_addrs:
        raise SystemExit("Provide at least one --to address")
    recipients = address_recipients(to_addrs, cc_addrs, bcc_addrs)
    text = read_text_arg(args.text, args.text_file)
    html = resolve_input_path(args.html_file).read_text(encoding="utf-8") if args.html_file else ""
    message = build_new_message(
        config,
        to_addrs,
        cc_addrs,
        args.subject,
        text,
        html,
        args.attachment or [],
    )

    if args.dry_run:
        print("Dry run: message was not sent")
        print(f"To: {message['To']}")
        if message.get("Cc"):
            print(f"Cc: {message['Cc']}")
        if bcc_addrs:
            print(f"Bcc recipients: {len(bcc_addrs)}")
        print(f"Subject: {message['Subject']}")
        if args.attachment:
            print(f"Attachments: {len(args.attachment)}")
        return 0

    now = utc_now()
    outbox_id = db.execute(
        """
        INSERT INTO mail_outbox (
            account_email, reply_to_message_db_id, to_addrs, subject,
            message_id, status, created_at
        )
        VALUES (?, NULL, ?, ?, ?, 'sending', ?)
        """,
        (
            config.email,
            ", ".join(recipients),
            str(message["Subject"]),
            str(message["Message-ID"]),
            now,
        ),
    ).lastrowid
    db.commit()

    try:
        smtp = smtp_connect(config)
        try:
            smtp.send_message(message, from_addr=config.email, to_addrs=recipients)
        finally:
            smtp.quit()
        sent_appended = True if args.no_append_sent else append_sent_copy(config, message)
        db.execute(
            """
            UPDATE mail_outbox
            SET status = ?, sent_at = ?, error = NULL
            WHERE id = ?
            """,
            ("sent" if sent_appended else "sent_without_imap_append", utc_now(), outbox_id),
        )
        db.commit()
        print(f"Sent message to {', '.join(recipients)}")
        if not sent_appended:
            print("Warning: SMTP send succeeded, but IMAP Sent append was not confirmed", file=sys.stderr)
        return 0
    except Exception as exc:
        db.execute(
            "UPDATE mail_outbox SET status = 'error', error = ? WHERE id = ?",
            (str(exc)[:1000], outbox_id),
        )
        db.commit()
        raise


def command_summary(args: argparse.Namespace) -> int:
    configs = load_configs_from_args(args, require_password=False)
    for config in configs:
        prefix = account_prefix(configs, config)
        db = ensure_db(config.db_path)
        rows = db.execute(
            """
            SELECT t.id, t.normalized_subject, t.message_count, t.last_message_at
            FROM mail_threads t
            WHERE t.account_email = ?
            ORDER BY COALESCE(t.last_message_at, '') DESC, t.id DESC
            LIMIT ?
            """,
            (config.email, args.limit),
        ).fetchall()
        if not rows:
            print(f"{prefix}No synced threads yet")
            continue
        for row in rows:
            print(
                f"{prefix}thread#{row['id']} messages={row['message_count']} "
                f"last={row['last_message_at'] or '-'} subject={row['normalized_subject']}"
            )
    return 0


def command_messages(args: argparse.Namespace) -> int:
    configs = load_configs_from_args(args, require_password=False)
    for config in configs:
        prefix = account_prefix(configs, config)
        db = ensure_db(config.db_path)
        rows = db.execute(
            """
            SELECT
                id, folder_name, date_utc, from_addr, to_addrs, subject, has_attachments
            FROM mail_messages
            WHERE account_email = ?
            ORDER BY COALESCE(date_utc, '') DESC, id DESC
            LIMIT ?
            """,
            (config.email, args.limit),
        ).fetchall()
        if not rows:
            print(f"{prefix}No synced messages yet")
            continue
        for row in rows:
            sender = row["from_addr"] or "-"
            recipient = row["to_addrs"] or "-"
            peer = sender
            if sender.casefold().find(config.email.casefold()) >= 0 and recipient:
                peer = recipient
            attachment_marker = " attachments" if row["has_attachments"] else ""
            print(
                f"{prefix}#{row['id']} [{row['folder_name']}] {row['date_utc'] or '-'} "
                f"{peer[:80]}{attachment_marker} | {row['subject'] or '(no subject)'}"
            )
    return 0


def command_show(args: argparse.Namespace) -> int:
    config = load_config_from_args(args, require_password=False)
    db = ensure_db(config.db_path)
    row = db.execute(
        """
        SELECT *
        FROM mail_messages
        WHERE id = ? AND account_email = ?
        """,
        (args.message_db_id, config.email),
    ).fetchone()
    if not row:
        raise SystemExit(f"Message not found in DB: {args.message_db_id}")

    print(f"ID: {row['id']}")
    print(f"Folder: {row['folder_name']}")
    print(f"Date: {row['date_utc'] or row['internaldate'] or '-'}")
    print(f"From: {row['from_addr'] or '-'}")
    print(f"To: {row['to_addrs'] or '-'}")
    if row["cc_addrs"]:
        print(f"Cc: {row['cc_addrs']}")
    print(f"Subject: {row['subject'] or '(no subject)'}")
    print(f"Message-ID: {row['message_id'] or '-'}")

    attachments = db.execute(
        """
        SELECT filename, content_type, size_bytes, storage_path
        FROM mail_attachments
        WHERE message_db_id = ?
        ORDER BY id
        """,
        (args.message_db_id,),
    ).fetchall()
    if attachments:
        print("Attachments:")
        for attachment in attachments:
            print(
                f"  - {attachment['filename']} "
                f"({attachment['content_type']}, {attachment['size_bytes']} bytes) "
                f"{attachment['storage_path']}"
            )

    body = row["body_html"] if args.html else row["body_text"]
    if not body and not args.html:
        body = row["body_html"]
    if not body:
        print("\n(no body stored)")
        return 0

    if args.max_chars and len(body) > args.max_chars:
        body = body[: args.max_chars] + "\n...[truncated]"
    print("\n" + body)
    return 0


def build_parser() -> argparse.ArgumentParser:
    account_parent = argparse.ArgumentParser(add_help=False)
    account_parent.add_argument(
        "--account",
        choices=sorted([*MAILRU_ACCOUNT_ENV, MAILRU_ACCOUNT_ALL]),
        default=argparse.SUPPRESS,
        help=(
            "Mailbox account from .env. "
            "personal uses MAILRU_EMAIL/MAILRU_APP_PASSWORD; "
            "workmail uses MAILRU_WORKMAIL/MAILRU_WORKMAIL_PASSWORD. "
            "all checks/syncs both read-only sources. "
            "Can also be set with MAILRU_ACCOUNT."
        ),
    )

    parser = argparse.ArgumentParser(description=__doc__, parents=[account_parent])
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", parents=[account_parent], help="Check IMAP and SMTP login")
    check.set_defaults(func=command_check)

    folders = subparsers.add_parser("folders", parents=[account_parent], help="List IMAP folders")
    folders.set_defaults(func=command_folders)

    sync = subparsers.add_parser("sync", parents=[account_parent], help="Sync INBOX and Sent by default")
    sync.add_argument("--folder", action="append", help="Folder display/encoded name; can be repeated")
    sync.add_argument("--all-folders", action="store_true", help="Sync all folders")
    sync.add_argument("--full", action="store_true", help="Ignore stored last UID and refetch selected messages")
    sync.add_argument("--limit", type=int, default=0, help="Limit selected messages per folder")
    sync.add_argument("--body-max-chars", type=int, default=200_000, help="Max body chars stored per text/html part set")
    sync.add_argument("--verbose", action="store_true", help="Print every synced message")
    sync.set_defaults(func=command_sync)

    reply = subparsers.add_parser("reply", parents=[account_parent], help="Reply to a synced message by DB id")
    reply.add_argument("message_db_id", type=int)
    reply.add_argument("--to", default="", help="Override recipient; default is original From")
    reply.add_argument("--text", default="", help="Plain text reply body")
    reply.add_argument("--text-file", default="", help="Read plain text reply body from file")
    reply.add_argument("--html-file", default="", help="Optional HTML alternative body from file")
    reply.add_argument("--attachment", action="append", default=[], help="Attach a file; can be repeated")
    reply.add_argument("--dry-run", action="store_true", help="Build but do not send")
    reply.add_argument("--no-append-sent", action="store_true", help="Do not append SMTP copy to Sent over IMAP")
    reply.set_defaults(func=command_reply)

    send = subparsers.add_parser("send", parents=[account_parent], help="Send a new email")
    send.add_argument("--to", action="append", required=True, help="Recipient address; can be repeated")
    send.add_argument("--cc", action="append", default=[], help="Cc address; can be repeated")
    send.add_argument("--bcc", action="append", default=[], help="Bcc address; can be repeated")
    send.add_argument("--subject", required=True)
    send.add_argument("--text", default="", help="Plain text body")
    send.add_argument("--text-file", default="", help="Read plain text body from file")
    send.add_argument("--html-file", default="", help="Optional HTML alternative body from file")
    send.add_argument("--attachment", action="append", default=[], help="Attach a file; can be repeated")
    send.add_argument("--dry-run", action="store_true", help="Build but do not send")
    send.add_argument("--no-append-sent", action="store_true", help="Do not append SMTP copy to Sent over IMAP")
    send.set_defaults(func=command_send)

    summary = subparsers.add_parser("summary", parents=[account_parent], help="Show latest synced threads from local DB")
    summary.add_argument("--limit", type=int, default=20)
    summary.set_defaults(func=command_summary)

    messages = subparsers.add_parser("messages", parents=[account_parent], help="List latest synced messages")
    messages.add_argument("--limit", type=int, default=20)
    messages.set_defaults(func=command_messages)

    show = subparsers.add_parser("show", parents=[account_parent], help="Show a synced message by DB id")
    show.add_argument("message_db_id", type=int)
    show.add_argument("--html", action="store_true", help="Show stored HTML body instead of plain text")
    show.add_argument("--max-chars", type=int, default=20_000)
    show.set_defaults(func=command_show)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
