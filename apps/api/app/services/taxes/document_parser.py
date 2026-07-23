"""Разбор налоговых документов от налогового агента (бухгалтер на аутсорсе, askad02@mail.ru).

Агент присылает стандартные унифицированные формы — это ключ к автоматизации:

* **платёжные поручения** (форма 0401060, ``.docx``): ЕНП, УСН, допвзнос 1%. Дают сумму,
  КБК, получателя, назначение и срок. Тип платежа берётся из имени файла и заголовка —
  «банк не говорит, что внутри платежа», а агент говорит прямо;
* **ведомости** (форма Т-53, ``.xls``): фактические выплаты по сотрудникам за период;
* **ПД (налог)** (``.xls``, «0,2 %»): взнос на травматизм в СФР.

``.docx`` — это zip+xml, читается стандартной библиотекой без зависимостей. ``.xls`` (старый
бинарный BIFF) требует ``xlrd``.

ВАЖНО про доверие: сумма и КБК из платёжки достоверны. Тип платежа (``tax_kind``) выводится
из ИМЕНИ ФАЙЛА, которое агент пишет вручную — формат стабильный, но человеческий. Поэтому
разбор возвращает ``needs_review=True`` там, где уверенности нет, а не угадывает молча.
"""

from __future__ import annotations

import contextlib
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from io import BytesIO

# КБК → назначение. Единый КБК ЕНП покрывает УСН, НДФЛ, взносы «за себя» — их не различить
# по КБК, только по имени файла. КБК травматизма отдельный.
KBK_ENP = "18201061201010000510"
KBK_INJURY = "79710212000061000160"

# Классификация типа платежа по ключевым словам в имени файла / заголовке документа.
# Порядок важен: «1%» проверяется раньше «УСН», потому что оба могут встретиться.
_KIND_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"1\s*%|1%\s*св|допвзнос|1\s*процент", "contrib_extra_1pct"),
    (r"\bусн\b", "usn_advance"),
    (r"0[.,]2\s*%|травматизм|несчастн", "contrib_injury"),
    (r"\bенп\b", "enp_payroll"),  # смешанный НДФЛ+взносы — требует разноса по уведомлению
)


@dataclass(frozen=True)
class PaymentOrderDoc:
    """Разобранное платёжное поручение (форма 0401060)."""

    amount: Decimal | None
    kbk: str | None
    recipient: str | None  # 'fns' | 'sfr'
    tax_kind: str | None  # ключ TaxPayment.kind или 'enp_payroll' (смешанный) / None
    due_date: date | None
    purpose: str | None
    period_hint: str | None  # 'q1'|'h1'|'9m'|'year' | 'YYYY-MM' — из имени файла, если понятно
    needs_review: bool
    review_reasons: list[str] = field(default_factory=list)
    source_filename: str | None = None


@dataclass(frozen=True)
class PayrollRow:
    tab_number: str | None
    employee: str
    amount: Decimal


@dataclass(frozen=True)
class PayrollStatementDoc:
    """Разобранная платёжная ведомость (форма Т-53)."""

    doc_number: str | None
    payout_kind: str | None  # 'advance' | 'salary' — из имени файла
    period_start: date | None
    period_end: date | None
    rows: list[PayrollRow]
    total: Decimal
    needs_review: bool
    review_reasons: list[str] = field(default_factory=list)
    source_filename: str | None = None


# ── docx ─────────────────────────────────────────────────────────────────────


def _docx_text(data: bytes) -> str:
    """Текст docx по абзацам. Стандартная библиотека, без зависимостей."""
    with zipfile.ZipFile(BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab[^>]*/>", " ", xml)
    return re.sub(r"<[^>]+>", "", xml)


def _parse_amount(text: str) -> Decimal | None:
    """Сумма из формата «105628-00» (рубли-копейки). Берём в блоке «Сумма»/итога.

    Ноль (0-00) — валидная сумма (нулевая платёжка-заглушка), поэтому отличаем «не нашли»
    (None) от «ноль» (Decimal 0).
    """
    m = re.search(r"\b(\d{1,9})-(\d{2})\b", text)
    if not m:
        return None
    return Decimal(m.group(1)) + Decimal(m.group(2)) / 100


def _parse_kbk(text: str) -> str | None:
    for m in re.finditer(r"\b(\d{20})\b", text):
        code = m.group(1)
        if code.startswith(("182", "797")):
            return code
    return None


def _parse_due_date(text: str, filename: str, default_year: int) -> date | None:
    """Срок «до 28.07» / «до 25.09» из имени файла или заголовка. Год — из полной даты
    в тексте, иначе из ``default_year`` (год письма)."""
    for source in (filename, text):
        m = re.search(r"до\s+(\d{1,2})[.\s](\d{1,2})(?:[.\s](\d{2,4}))?", source, re.I)
        if m:
            day, month = int(m.group(1)), int(m.group(2))
            year = _normalize_year(m.group(3), default_year)
            try:
                return date(year, month, day)
            except ValueError:
                continue
    return None


def _normalize_year(raw: str | None, default_year: int) -> int:
    if not raw:
        return default_year
    y = int(raw)
    return 2000 + y if y < 100 else y


def _classify_kind(filename: str, header: str) -> str | None:
    haystack = f"{filename} {header}".lower()
    for pattern, kind in _KIND_PATTERNS:
        if re.search(pattern, haystack):
            return kind
    return None


def _period_hint(filename: str, header: str) -> str | None:
    text = f"{filename} {header}".lower()
    if re.search(r"1\s*кв|1\s*квартал", text):
        return "q1"
    if re.search(r"2\s*кв|полугод|2\s*квартал", text):
        return "h1"
    if re.search(r"3\s*кв|9\s*мес", text):
        return "9m"
    if re.search(r"год|4\s*кв", text):
        return "year"
    m = re.search(r"\b(0?[1-9]|1[0-2])[.\-](\d{4})\b", text)
    if m:
        return f"{m.group(2)}-{int(m.group(1)):02d}"
    return None


def parse_payment_order(
    data: bytes, *, filename: str = "", default_year: int | None = None
) -> PaymentOrderDoc:
    """Разобрать платёжное поручение (docx, форма 0401060)."""
    text = _docx_text(data)
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    header = lines[0] if lines else ""
    year = default_year or _guess_year(text) or date.today().year

    amount = _parse_amount(text)
    kbk = _parse_kbk(text)
    recipient = _recipient_from(text, kbk)
    tax_kind = _classify_kind(filename, header)
    due = _parse_due_date(text, filename, year)
    purpose = _purpose_from(lines)
    period = _period_hint(filename, header)

    reasons: list[str] = []
    if amount is None:
        reasons.append("не распознана сумма")
    if kbk is None:
        reasons.append("не распознан КБК")
    if tax_kind is None:
        reasons.append("не распознан вид платежа по имени файла/заголовку")
    if tax_kind == "enp_payroll":
        reasons.append("ЕНП: НДФЛ и взносы смешаны — нужен разнос по уведомлению")
    if due is None:
        reasons.append("не распознан срок уплаты")

    return PaymentOrderDoc(
        amount=amount,
        kbk=kbk,
        recipient=recipient,
        tax_kind=tax_kind,
        due_date=due,
        purpose=purpose,
        period_hint=period,
        needs_review=bool(reasons),
        review_reasons=reasons,
        source_filename=filename or None,
    )


def _recipient_from(text: str, kbk: str | None) -> str | None:
    if kbk == KBK_INJURY or "ОСФР" in text or "СФР" in text.upper():
        return "sfr"
    if kbk == KBK_ENP or "Казначейство" in text or "ФНС" in text:
        return "fns"
    return None


def _purpose_from(lines: list[str]) -> str | None:
    for i, line in enumerate(lines):
        if line.startswith("Назначение платежа") and i > 0:
            return lines[i - 1]
    for line in lines:
        if line in ("ЕНП.", "ЕНП"):
            return "Единый налоговый платеж"
    return None


def _guess_year(text: str) -> int | None:
    m = re.search(r"\b(20\d{2})\b", text)
    return int(m.group(1)) if m else None


# ── xls (ведомость Т-53) ─────────────────────────────────────────────────────

# Строка сотрудника в Т-53: «ФАМИЛИЯ И.О.» + сумма. Фамилия в ведомостях идёт ЗАГЛАВНЫМИ,
# инициалы — обязательно с точками (это отличает сотрудника от подписи руководителя,
# где инициалы без точек).
_NAME_RE = re.compile(
    r"^[А-ЯЁ][А-ЯЁа-яё]+(?:-[А-ЯЁ][А-ЯЁа-яё]+)?\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.?$"
)


def parse_payroll_statement(data: bytes, *, filename: str = "") -> PayrollStatementDoc:
    """Разобрать платёжную ведомость (xls, форма Т-53)."""
    import xlrd  # локальный импорт: тяжёлый только для .xls-веток

    wb = xlrd.open_workbook(file_contents=data)
    grid: list[list[str]] = []
    for sheet in wb.sheets():
        for r in range(sheet.nrows):
            grid.append([str(sheet.cell_value(r, c)).strip() for c in range(sheet.ncols)])

    doc_number = _find_doc_number(grid)
    period_start, period_end = _find_period(grid)
    rows = _find_employee_rows(grid)
    total = sum((row.amount for row in rows), Decimal("0"))
    payout_kind = _payout_kind(filename)

    reasons: list[str] = []
    if not rows:
        reasons.append("не найдено ни одной строки сотрудника")
    if period_start is None:
        reasons.append("не распознан расчётный период")
    if payout_kind is None:
        reasons.append("не распознан тип выплаты (аванс/зарплата) по имени файла")

    return PayrollStatementDoc(
        doc_number=doc_number,
        payout_kind=payout_kind,
        period_start=period_start,
        period_end=period_end,
        rows=rows,
        total=total,
        needs_review=bool(reasons),
        review_reasons=reasons,
        source_filename=filename or None,
    )


def _payout_kind(filename: str) -> str | None:
    low = filename.lower()
    if "аванс" in low:
        return "advance"
    if "зп" in low or "зарплат" in low:
        return "salary"
    return None


def _find_doc_number(grid: list[list[str]]) -> str | None:
    """Номер ведомости формата «20-13». Первое совпадение — это и есть номер документа."""
    for row in grid:
        for cell in row:
            if re.fullmatch(r"\d{1,3}-\d{1,3}", cell):
                return cell
    return None


def _find_period(grid: list[list[str]]) -> tuple[date | None, date | None]:
    dates: list[date] = []
    for row in grid:
        for cell in row:
            m = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", cell)
            if m:
                with contextlib.suppress(ValueError):
                    dates.append(date(int(m.group(3)), int(m.group(2)), int(m.group(1))))
    if not dates:
        return None, None
    # Расчётный период — минимальная и максимальная даты в документе (1-е и последнее число).
    first_of_month = [d for d in dates if d.day == 1]
    start = min(first_of_month) if first_of_month else min(dates)
    end = max(dates)
    return start, end


def _find_employee_rows(grid: list[list[str]]) -> list[PayrollRow]:
    rows: list[PayrollRow] = []
    seen: set[tuple[str, str]] = set()
    for row in grid:
        name = next((c for c in row if _NAME_RE.match(c)), None)
        if not name:
            continue
        amount = _row_amount(row)
        if amount is None:
            continue
        tab = _row_tab_number(row)
        key = (name, str(amount))
        if key in seen:
            continue
        seen.add(key)
        rows.append(PayrollRow(tab_number=tab, employee=name, amount=amount))
    return rows


def _row_amount(row: list[str]) -> Decimal | None:
    """Денежная сумма в строке: формат 20986.00 (xls хранит числа как float-строки)."""
    for cell in row:
        m = re.fullmatch(r"(\d{3,7})\.(\d{2})", cell)
        if m:
            return Decimal(m.group(1)) + Decimal(m.group(2)) / 100
    return None


def _row_tab_number(row: list[str]) -> str | None:
    for cell in row:
        m = re.fullmatch(r"(\d{2,4})(?:\.0)?", cell)
        if m and 100 <= int(m.group(1)) <= 9999:
            return m.group(1)
    return None
