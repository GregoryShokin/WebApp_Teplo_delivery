"""Стенд: имитация письма со счётом на «Страницу на оплату».

На проде это делает почтовый робот (IMAP → process_attachment). Здесь тот же самый
process_attachment, только вложение подсовываем руками: роутов создания intake в API
нет, счёт попадает в систему исключительно из почты.

PDF собираем с картой ToUnicode — иначе pypdf прочитает кириллицу как латиницу и
распознаватель не увидит ни суммы, ни ИНН.

Запуск:
  docker exec -w /app/apps/api teplo-api-b python /app/apps/api/scripts/stand-mail.py [номер] [сумма]
"""

import asyncio
import sys
from datetime import UTC, datetime

sys.path.insert(0, "/app/apps/api")

from app.core.config import get_settings  # noqa: E402
from app.db.session import AsyncSessionLocal  # noqa: E402
from app.services.email_invoice_ingest import process_attachment  # noqa: E402
from app.services.mail.imap_client import FetchedAttachment  # noqa: E402


def _tounicode(used: set[int]) -> bytes:
    """CMap байт cp1251 → Unicode, чтобы pypdf извлёк русский текст."""
    pairs = []
    for code in sorted(used):
        ch = bytes([code]).decode("cp1251", errors="replace")
        pairs.append(f"<{code:02X}> <{ord(ch):04X}>")
    chunks = [pairs[i : i + 100] for i in range(0, len(pairs), 100)]
    body = "\n".join(
        f"{len(chunk)} beginbfchar\n" + "\n".join(chunk) + "\nendbfchar" for chunk in chunks
    )
    return (
        "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        "/CMapName /StandCMap def\n/CMapType 2 def\n"
        "1 begincodespacerange\n<00> <FF>\nendcodespacerange\n"
        f"{body}\n"
        "endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend"
    ).encode("ascii")


def _pdf(lines: list[str]) -> bytes:
    text_ops = ["BT /F1 12 Tf 50 780 Td 16 TL"]
    used: set[int] = set()
    for line in lines:
        raw = line.encode("cp1251", errors="replace")
        used.update(raw)
        escaped = (
            raw.replace(b"\\", rb"\\").replace(b"(", rb"\(").replace(b")", rb"\)").decode("latin-1")
        )
        text_ops.append(f"({escaped}) Tj T*")
    text_ops.append("ET")
    stream = "\n".join(text_ops).encode("latin-1")
    cmap = _tounicode(used)

    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /ToUnicode 6 0 R >>",
        b"<< /Length " + str(len(cmap)).encode() + b" >>\nstream\n" + cmap + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


async def main() -> None:
    number = sys.argv[1] if len(sys.argv) > 1 else "С-2026-001"
    amount = sys.argv[2] if len(sys.argv) > 2 else "44503.00"
    subject = f"Счет на оплату № {number}"
    lines = [
        f"Счет на оплату № {number} от 15.07.2026",
        "",
        "Поставщик: ООО ОвощБаза",
        "ИНН 7736207543  КПП 773601001",
        "Банк получателя: АО ТБанк",
        "БИК 044525974",
        # Контрольный разряд р/с обязан сходиться с БИК: «Отправить в банк» подтверждает
        # реквизиты, а payee_account_error отклоняет битый счёт (банк отдал бы 422).
        "Р/с 40702810900000010345",
        "К/с 30101810145250000974",
        "",
        "Наименование: Поставка овощей",
        f"Итого к оплате: {amount} руб.",
        "Без НДС",
    ]
    settings = get_settings()
    now = datetime.now(UTC)
    att = FetchedAttachment(
        mailbox="buh@teplo.local",
        message_uid=f"stand-{now.timestamp()}",
        message_id=f"<stand-{now.timestamp()}@teplo.local>",
        from_addr="postavshik@example.com",
        subject=subject,
        received_at=now,
        filename=f"schet-{number}.pdf",
        mime="application/pdf",
        content=_pdf(lines),
    )
    async with AsyncSessionLocal() as session:
        status = await process_attachment(session, att, settings=settings)
        await session.commit()
    print(f"письмо принято, статус разбора: {status}")


if __name__ == "__main__":
    asyncio.run(main())
