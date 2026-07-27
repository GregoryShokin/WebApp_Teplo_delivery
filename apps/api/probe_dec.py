import email, imaplib, hashlib
from app.core.config import get_settings
from app.services.mail.imap_client import configured_accounts, _attachments_from_message, _is_tax_document_part, _decode

s = get_settings()
for account in configured_accounts(s):
    imap = imaplib.IMAP4_SSL(s.mailru_imap_host, s.mailru_imap_port)
    imap.login(account.email, account.password)
    imap.select("INBOX", readonly=True)
    typ, data = imap.uid("SEARCH", None, 'FROM', 'askad02@mail.ru', 'SINCE', '15-Dec-2025', 'BEFORE', '01-Jan-2026')
    uids = data[0].split() if data and data[0] else []
    print(f"[{account.label}] typ={typ} писем={len(uids)}")
    for raw in uids:
        uid = raw.decode()
        t, md = imap.uid("FETCH", uid, "(BODY.PEEK[])")
        msg = email.message_from_bytes(md[0][1])
        atts = _attachments_from_message(account, uid, msg, _is_tax_document_part)
        print("   ", _decode(msg.get("Date")), "|", (_decode(msg.get("Subject")) or "")[:40], "|", [a.filename for a in atts])
    imap.logout()
