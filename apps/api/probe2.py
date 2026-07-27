import collections, email, imaplib, hashlib
from app.core.config import get_settings
from app.services.mail.imap_client import configured_accounts, _attachments_from_message, _is_tax_document_part

s = get_settings()
seen = collections.Counter()
for account in configured_accounts(s):
    imap = imaplib.IMAP4_SSL(s.mailru_imap_host, s.mailru_imap_port)
    imap.login(account.email, account.password)
    imap.select("INBOX", readonly=True)
    typ, data = imap.uid("SEARCH", None, 'FROM', 'askad02@mail.ru', 'SINCE', '15-Dec-2025', 'BEFORE', '01-Jan-2026')
    uids = data[0].split() if data and data[0] else []
    print(f"[{account.label}] писем: {len(uids)}; первые uid: {[u.decode() for u in uids[:5]]}")
    for raw in uids:
        t, md = imap.uid("FETCH", raw.decode(), "(BODY.PEEK[])")
        if t != "OK" or not md or not isinstance(md[0], tuple):
            continue
        msg = email.message_from_bytes(md[0][1])
        for a in _attachments_from_message(account, raw.decode(), msg, _is_tax_document_part):
            seen[a.filename] += 1
    imap.logout()
print("всего вложений:", sum(seen.values()))
for name, n in sorted(seen.items()):
    print(f"   {n} × {name}")
