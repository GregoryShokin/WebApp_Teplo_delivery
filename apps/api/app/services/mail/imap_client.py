"""Компактное чтение PDF-вложений из почтовых ящиков mail.ru (стандартный imaplib).

Зачем своё, а не существующий ``integrations/mailru/scripts/mailru_mailbox.py``: тот скрипт —
CLI, синхронный, пишет в отдельную ``mail.sqlite3`` под путём ``data/private/mail``, который в
контейнеры api/scheduler НЕ примонтирован (примонтированы только ``integrations`` и
``research/private``). Здесь нужен лишь срез — найти новые PDF-счета — поэтому держим
минимальный read-only IMAP-клиент внутри бэкенда, а распознанное складываем в Postgres.

Класс синхронный (imaplib блокирующий); вызывать из джобы через ``asyncio.to_thread``.
Читаем строго на чтение (``BODY.PEEK[]`` + ``select(readonly=True)``) — флаги \\Seen не трогаем.
"""

from __future__ import annotations

import contextlib
import email
import hashlib
import imaplib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime

from app.core.config import Settings

logger = logging.getLogger(__name__)

# Английские сокращения месяцев для IMAP SEARCH SINCE (формат dd-Mon-yyyy не зависит от локали).
_IMAP_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)
# Предохранитель от патологического первого прохода: берём только N самых свежих писем окна.
_MAX_MESSAGES_PER_RUN = 400


@dataclass(frozen=True)
class MailAccount:
    """Учётка ящика для опроса. ``label`` уходит в intake.mailbox."""

    label: str
    email: str
    password: str


@dataclass(frozen=True)
class FetchedAttachment:
    mailbox: str
    message_uid: str
    message_id: str | None
    from_addr: str | None
    subject: str | None
    received_at: datetime | None
    filename: str | None
    mime: str
    content: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


def configured_accounts(settings: Settings) -> list[MailAccount]:
    """Собрать ящики, у которых заданы логин+пароль. Пустые — молча пропускаем."""
    accounts: list[MailAccount] = []
    if settings.mailru_email and settings.mailru_app_password:
        accounts.append(
            MailAccount("personal", settings.mailru_email, settings.mailru_app_password)
        )
    if settings.mailru_workmail and settings.mailru_workmail_password:
        accounts.append(
            MailAccount("corporate", settings.mailru_workmail, settings.mailru_workmail_password)
        )
    return accounts


def _decode(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(make_header(decode_header(value))).strip() or None
    except Exception:  # noqa: BLE001 - битый заголовок не должен ронять разбор письма
        return value.strip() or None


def _since_token(lookback_days: int) -> str:
    day = datetime.now(UTC).date() - timedelta(days=max(0, lookback_days))
    return f"{day.day:02d}-{_IMAP_MONTHS[day.month - 1]}-{day.year}"


def _received_at(message: Message) -> datetime | None:
    raw = message.get("Date")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None


def _is_pdf_part(part: Message) -> bool:
    filename = part.get_filename()
    content_type = (part.get_content_type() or "").casefold()
    if content_type == "application/pdf":
        return True
    # Иногда PDF приходит как octet-stream — опираемся на расширение имени файла.
    decoded_name = (_decode(filename) or "").casefold()
    return decoded_name.endswith(".pdf")


# Тип предиката, отбирающего вложения по MIME/имени. Позволяет одному fetch-коду тянуть
# и PDF-счета, и налоговые docx/xls, не дублируя IMAP-логику.
AttachmentPredicate = Callable[[Message], bool]

# Расширения офисных документов налогового агента.
_TAX_DOC_EXTENSIONS = (".docx", ".xls", ".xlsx", ".doc")
_TAX_DOC_MIMES = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "application/msword",
)


def _is_tax_document_part(part: Message) -> bool:
    """docx/xls/doc/xlsx — по MIME или расширению имени файла."""
    content_type = (part.get_content_type() or "").casefold()
    if content_type in _TAX_DOC_MIMES:
        return True
    decoded_name = (_decode(part.get_filename()) or "").casefold()
    return decoded_name.endswith(_TAX_DOC_EXTENSIONS)


def _attachments_from_message(
    account: MailAccount, uid: str, message: Message, accept: AttachmentPredicate
) -> list[FetchedAttachment]:
    message_id = _decode(message.get("Message-ID"))
    from_addr = _decode(message.get("From"))
    subject = _decode(message.get("Subject"))
    received_at = _received_at(message)
    found: list[FetchedAttachment] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        if not accept(part):
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:  # noqa: BLE001
            payload = None
        if not payload:
            continue
        found.append(
            FetchedAttachment(
                mailbox=account.label,
                message_uid=uid,
                message_id=message_id,
                from_addr=from_addr,
                subject=subject,
                received_at=received_at,
                filename=_decode(part.get_filename()),
                mime=part.get_content_type() or "application/octet-stream",
                content=payload,
            )
        )
    return found


def fetch_attachments(
    account: MailAccount,
    *,
    host: str,
    port: int,
    lookback_days: int,
    accept: AttachmentPredicate,
) -> list[FetchedAttachment]:
    """Скачать вложения писем из INBOX за окно ``lookback_days``, отобранные ``accept``.

    Бросает наружу только фатальные ошибки логина/соединения; ошибки разбора отдельных писем
    изолированы (логируются, проход продолжается). Дедуп по содержимому — выше по стеку.
    """
    attachments: list[FetchedAttachment] = []
    imap = imaplib.IMAP4_SSL(host, port)
    try:
        imap.login(account.email, account.password)
        imap.select("INBOX", readonly=True)
        typ, data = imap.uid("SEARCH", None, "SINCE", _since_token(lookback_days))
        if typ != "OK" or not data or not data[0]:
            return attachments
        uids = data[0].split()
        if len(uids) > _MAX_MESSAGES_PER_RUN:
            logger.info(
                "mail %s: окно вернуло %s писем, беру последние %s",
                account.label, len(uids), _MAX_MESSAGES_PER_RUN,
            )
            uids = uids[-_MAX_MESSAGES_PER_RUN:]
        for raw_uid in uids:
            uid = raw_uid.decode("ascii", "ignore")
            try:
                typ, msg_data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
                if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                    continue
                message = email.message_from_bytes(msg_data[0][1])
                attachments.extend(_attachments_from_message(account, uid, message, accept))
            except Exception:  # noqa: BLE001 - одно письмо не должно валить весь проход
                logger.warning("mail %s: ошибка разбора письма uid=%s", account.label, uid,
                               exc_info=True)
                continue
    finally:
        with contextlib.suppress(Exception):
            imap.logout()
    return attachments


def fetch_pdf_attachments(
    account: MailAccount, *, host: str, port: int, lookback_days: int
) -> list[FetchedAttachment]:
    """PDF-вложения из INBOX. Тонкая обёртка над ``fetch_attachments`` — контур счетов."""
    return fetch_attachments(
        account, host=host, port=port, lookback_days=lookback_days, accept=_is_pdf_part
    )


def fetch_tax_document_attachments(
    account: MailAccount, *, host: str, port: int, lookback_days: int
) -> list[FetchedAttachment]:
    """docx/xls-вложения из INBOX — налоговые документы от бухгалтера."""
    return fetch_attachments(
        account,
        host=host,
        port=port,
        lookback_days=lookback_days,
        accept=_is_tax_document_part,
    )
