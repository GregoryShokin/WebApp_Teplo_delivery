"""Прицельный добор писем Натальи за декабрь 2025 (окно ingest их не достаёт)."""
import asyncio, email, imaplib, os
from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.services.mail.imap_client import MailAccount, _attachments_from_message, _is_tax_document_part
from app.services.taxes.document_ingest import ingest_tax_documents


def fetch(account, *, host, port, lookback_days):
    out = []
    imap = imaplib.IMAP4_SSL(host, port)
    try:
        imap.login(account.email, account.password)
        imap.select("INBOX", readonly=True)
        typ, data = imap.uid("SEARCH", None, 'FROM', 'askad02@mail.ru', 'SINCE', '15-Dec-2025')
        for raw_uid in (data[0].split() if data and data[0] else []):
            uid = raw_uid.decode()
            typ, msg_data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
            if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            out.extend(_attachments_from_message(account, uid, msg, _is_tax_document_part))
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return out


async def main():
    settings = get_settings()
    async with AsyncSessionLocal() as s:
        res = await ingest_tax_documents(s, settings=settings, fetch=fetch)
        await s.commit()
        print(res)

asyncio.run(main())
