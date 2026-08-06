"""У каждого вердикта сходимости обязана быть подпись по-русски.

Карточка «Сходимость денежного слоя» — единственное место отчёта, где названа сумма, ушедшая
из прибыли: перевод расхода в другой месяц, период до начала учёта, деньги чужого слоя. Фронт
печатает ``VERDICT_LABEL[verdict] ?? verdict``, поэтому забытый ключ выходит к владельцу сырым
идентификатором. Так и случилось с двумя вердиктами, добавленными 06.08.2026: на экране стояло
«excluded_before_accounting_start — 30 402,00».

Это не косметика. Подпись занижения, написанная на языке, которого читатель отчёта не знает,
равносильна отсутствию подписи — а занижение в этом модуле обязано быть названным.

Тест читает TSX, а не запускает фронт: цель — поймать рассинхрон при добавлении вердикта, и
для этого достаточно текста. Дешёвая проверка, которая срабатывает ровно тогда, когда нужно.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services.pnl.types import Verdict

PNL_PAGE = (
    Path(__file__).resolve().parents[3] / "web" / "src" / "routes" / "reports" / "pnl" / "index.tsx"
)


def _labelled_verdicts() -> set[str]:
    # В контейнере API смонтирован только backend — фронта там нет вовсе. Тест страхует от
    # рассинхрона при добавлении вердикта и должен идти там, где виден весь репозиторий.
    if not PNL_PAGE.exists():
        pytest.skip("страница ОПиУ недоступна: прогон без фронтенда")
    source = PNL_PAGE.read_text(encoding="utf-8")
    block = source.split("const VERDICT_LABEL: Record<string, string> = {", 1)[1].split("};", 1)[0]
    return set(re.findall(r"^\s{2}([a-z_]+):", block, re.M))


def test_every_verdict_has_a_russian_label() -> None:
    labelled = _labelled_verdicts()
    missing = {verdict.value for verdict in Verdict} - labelled
    assert not missing, (
        "вердикты без подписи на экране — владелец увидит сырой ключ: " + ", ".join(sorted(missing))
    )


def test_no_stale_labels_left_behind() -> None:
    """Обратная сторона: метка без вердикта означает, что кто-то переименовал ключ."""
    known = {verdict.value for verdict in Verdict}
    stale = _labelled_verdicts() - known
    assert not stale, "подписи без вердикта (переименован или удалён): " + ", ".join(sorted(stale))
