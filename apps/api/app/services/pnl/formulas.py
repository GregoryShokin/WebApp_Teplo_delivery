"""Вычисление расчётных строк каскада по декларативным формулам справочника.

ПОЧЕМУ НЕ EVAL. Формула приходит из базы, а база правится миграцией и (в будущем) экраном
настройки. ``eval`` над таким входом — исполнение произвольного кода из данных. Здесь четыре
операции над кодами строк, разбираемые вручную; всё, что не разобралось, роняет расчёт с
внятной ошибкой, а не считает молча неверно.

СОГЛАШЕНИЕ О ЗНАКАХ. Подытог расходного блока хранит ПОЛОЖИТЕЛЬНУЮ величину расхода, каскад
вычитает её явным ``sub``. ``{"op": "sum", "block": X}`` складывает величины активных строк
блока как есть, не применяя ``sign_role``: тогда компенсирующая величина (излишек ревизии,
возврат клиенту) сама уменьшает сумму блока. Строки ``memo`` в сумму не входят — «Выручка без
учёта скидок» справочная, сложить её с выручкой со скидками значило бы задвоить продажи.

РАСПРОСТРАНЕНИЕ НЕИЗВЕСТНОСТИ — главное правило файла. Если хоть одно слагаемое неизвестно,
подытог получает статус ``incomplete`` и список недостающих кодов. Сумма известных при этом
считается и показывается, но выдавать её за итог нельзя: в июле 2026 без выручки и фудкоста
EBITDA обязана читаться как «неполно», а не как уверенный убыток в четыре миллиона.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.services.pnl.types import KNOWN_STATUSES, LineStatus, LineValue

#: Строки этих типов участвуют в сумме блока. ``memo`` намеренно исключён.
SUMMABLE_KINDS = frozenset({"source"})


class FormulaError(ValueError):
    """Формула не разобралась. Ошибка данных справочника, а не входных чисел."""


def _block_members(
    block: str, lines: dict[str, LineValue], catalog: list[dict[str, Any]]
) -> list[str]:
    """Коды активных строк-источников блока в порядке справочника."""
    return [
        row["code"]
        for row in catalog
        if row["block"] == block
        and row["kind"] in SUMMABLE_KINDS
        and row["status"] == "active"
        and row["code"] in lines
    ]


def evaluate(
    formula: dict[str, Any],
    lines: dict[str, LineValue],
    catalog: list[dict[str, Any]],
) -> tuple[Decimal | None, list[str]]:
    """Вычислить формулу. Возвращает (значение, коды неизвестных слагаемых).

    Значение не бывает ``None`` при непустом наборе известных слагаемых: сумма известных
    считается всегда, а неизвестность передаётся вторым элементом. ``None`` возвращается
    только когда известных слагаемых нет вовсе — тогда у подытога нет даже приблизительной
    цифры, и показывать «≈ 0» было бы враньём.
    """
    op = formula.get("op")
    if op == "sum":
        return _sum(formula, lines, catalog)
    if op == "sub":
        return _sub(formula, lines, catalog)
    if op == "ratio":
        return _ratio(formula, lines)
    raise FormulaError(f"неизвестная операция формулы: {op!r}")


def _operand(
    item: Any,
    lines: dict[str, LineValue],
    catalog: list[dict[str, Any]],
) -> tuple[Decimal | None, list[str]]:
    """Операнд — либо код строки, либо вложенная формула."""
    if isinstance(item, dict):
        return evaluate(item, lines, catalog)
    if not isinstance(item, str):
        raise FormulaError(f"операнд должен быть кодом строки или формулой, получено {item!r}")
    line = lines.get(item)
    if line is None:
        raise FormulaError(f"формула ссылается на несуществующую строку {item!r}")
    if line.status not in KNOWN_STATUSES:
        # Строка «не используется» — это не пробел в данных, а осознанное решение владельца.
        # Она не делает подытог неполным и просто не участвует в сумме.
        if line.status is LineStatus.NOT_USED:
            return Decimal("0.00"), []
        if line.status is LineStatus.INCOMPLETE:
            # Неполный подытог тянет свою неполноту вверх вместе со списком причин.
            return line.amount, list(line.missing_lines)
        return None, [item]
    return line.amount, []


def _sum(
    formula: dict[str, Any],
    lines: dict[str, LineValue],
    catalog: list[dict[str, Any]],
) -> tuple[Decimal | None, list[str]]:
    if "block" in formula:
        items: list[Any] = _block_members(formula["block"], lines, catalog)
    else:
        items = list(formula.get("args", []))
    total = Decimal("0.00")
    missing: list[str] = []
    known = False
    for item in items:
        value, item_missing = _operand(item, lines, catalog)
        missing.extend(item_missing)
        if value is not None:
            total += value
            known = True
    return (total if known else None), missing


def _sub(
    formula: dict[str, Any],
    lines: dict[str, LineValue],
    catalog: list[dict[str, Any]],
) -> tuple[Decimal | None, list[str]]:
    args = formula.get("args", [])
    if len(args) != 2:
        raise FormulaError(f"sub ожидает ровно два операнда, получено {len(args)}")
    left, left_missing = _operand(args[0], lines, catalog)
    right, right_missing = _operand(args[1], lines, catalog)
    missing = left_missing + right_missing
    if left is None and right is None:
        return None, missing
    return (left or Decimal("0.00")) - (right or Decimal("0.00")), missing


def _ratio(
    formula: dict[str, Any],
    lines: dict[str, LineValue],
) -> tuple[Decimal | None, list[str]]:
    """Рентабельность. Неполнота не прячет показатель, а переносится на него.

    Раньше неполный числитель обнулял процент целиком — из опасения, что доля от неполной
    базы выглядит как факт. Опасение верное, вывод оказался слишком резким: 03.08.2026 одна
    неприехавшая УПД по контекстной рекламе стёрла из июльского отчёта ВСЕ рентабельности,
    включая ту единственную, ради которой отчёт и открывают. Пустая ячейка при этом сообщает
    меньше, чем «≈ 23,6 %» рядом с «≈» на самой EBITDA.

    Поэтому неполный ЧИСЛИТЕЛЬ показатель не отменяет: он считается от известных слагаемых,
    а неполнота передаётся статусом и списком недостающих строк — тем же механизмом, что и у
    сумм.

    А вот неполный ЗНАМЕНАТЕЛЬ отменяет по-прежнему, и разница здесь не косметическая.
    Числитель неполон на величину недостающего документа — процент смещается на проценты.
    Знаменатель у нас всегда выручка: пока она не подтянулась из iiko, доля считается от
    остатка вроде −1 428 ₽ и выдаёт «≈ 100 %» на пустом месяце. Такую цифру нельзя показать
    даже с оговоркой.
    """
    num = lines.get(formula["num"])
    den = lines.get(formula["den"])
    if num is None or den is None:
        raise FormulaError("ratio ссылается на несуществующую строку")
    if den.status is LineStatus.INCOMPLETE:
        return None, list(den.missing_lines)
    missing_from_operands = list(num.missing_lines) + list(den.missing_lines)
    if num.amount is None or den.amount is None or den.amount == 0:
        missing = [c for c, v in ((formula["num"], num), (formula["den"], den)) if v.amount is None]
        return None, missing + missing_from_operands
    return (num.amount / den.amount).quantize(Decimal("0.0001")), missing_from_operands


def _dependencies(formula: dict[str, Any], catalog: list[dict[str, Any]]) -> set[str]:
    """Коды строк, от которых зависит формула, включая раскрытые ссылки на блок."""
    deps: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, str):
            deps.add(node)
            return
        if not isinstance(node, dict):
            return
        if "block" in node:
            deps.update(
                row["code"]
                for row in catalog
                if row["block"] == node["block"] and row["kind"] in SUMMABLE_KINDS
            )
        for arg in node.get("args", []):
            walk(arg)
        for key in ("num", "den"):
            if key in node:
                deps.add(node[key])

    walk(formula)
    return deps


def computation_order(catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Расчётные строки в порядке ЗАВИСИМОСТЕЙ, а не отображения.

    Порядок в отчёте и порядок вычисления — разные вещи, и это не мелочь: «Производственные
    расходы» и «Косвенные расходы» показываются заголовками НАД своими слагаемыми, но
    сложить их можно только после. Считая в порядке ``sort_order``, мы получали бы пустой
    подытог при полностью известных слагаемых — молча и правдоподобно.
    """
    computed = [row for row in catalog if row["kind"] in {"subtotal", "ratio"}]
    by_code = {row["code"]: row for row in computed}
    ordered: list[dict[str, Any]] = []
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(code: str) -> None:
        if code in visited or code not in by_code:
            return
        if code in visiting:
            raise FormulaError(f"циклическая зависимость формул на строке {code!r}")
        visiting.add(code)
        row = by_code[code]
        for dependency in _dependencies(row.get("formula") or {}, catalog):
            visit(dependency)
        visiting.discard(code)
        visited.add(code)
        ordered.append(row)

    for row in sorted(computed, key=lambda item: item["sort_order"]):
        visit(row["code"])
    return ordered


def apply_formulas(
    lines: dict[str, LineValue],
    catalog: list[dict[str, Any]],
) -> None:
    """Досчитать подытоги и рентабельности на месте, в порядке зависимостей."""
    for row in computation_order(catalog):
        line = lines.get(row["code"])
        if line is None or not row.get("formula"):
            continue
        if row["status"] != "active":
            line.status = LineStatus.NOT_USED
            line.amount = None
            continue
        value, missing = evaluate(row["formula"], lines, catalog)
        line.amount = value
        line.missing_lines = sorted(set(missing))
        if missing:
            line.status = LineStatus.INCOMPLETE
        elif value is None:
            line.status = LineStatus.NO_DATA
        else:
            line.status = LineStatus.OK
