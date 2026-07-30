"""Три списка обязательных прод-секретов обязаны совпадать.

Набор ключей для `.env.integrations` продублирован в трёх местах, и все три нужны:

* ``REQUIRED_KEYS`` в ``deploy/scripts/build_integrations_env.py`` — рендер отказывается
  собирать файл, если значение пустое;
* ``deploy/env.integrations.example`` — шаблон, по которому рендер идёт ПОСТРОЧНО: ключ,
  которого в шаблоне нет, молча не попадает в задеплоенный файл;
* ``deploy/check-prod-secrets.sh`` — предполётная проверка на сервере.

Расхождение между ними уже стоило прода: 21.07.2026 ``IIKO_CLOUD_*`` выпали из шаблона,
и после пересоздания контейнеров каждый облачный вызов iiko падал с ``KeyError``.
Синхронизировать три списка руками — ровно тот случай, где нужен замок, а не внимательность.

Документация намеренно НЕ проверяется: её ``check-prod-secrets.sh`` перечисляет сам при
запуске, поэтому доки ссылаются на скрипт вместо копирования списка.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEPLOY = REPO_ROOT / "deploy"
BUILDER = DEPLOY / "scripts" / "build_integrations_env.py"
TEMPLATE = DEPLOY / "env.integrations.example"
CHECK_SCRIPT = DEPLOY / "check-prod-secrets.sh"
CONFIG = REPO_ROOT / "apps" / "api" / "app" / "core" / "config.py"
PROD_TEMPLATE = DEPLOY / "env.prod.example"


def _required_keys() -> tuple[str, ...]:
    """``REQUIRED_KEYS`` из билдера — читаем AST, чтобы не импортировать скрипт целиком."""
    tree = ast.parse(BUILDER.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(t, ast.Name) and t.id == "REQUIRED_KEYS" for t in node.targets
        ):
            return tuple(ast.literal_eval(node.value))
    raise AssertionError(f"REQUIRED_KEYS не найден в {BUILDER}")


def _template_keys() -> set[str]:
    keys = set()
    for line in TEMPLATE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        keys.add(line.split("=", 1)[0].strip())
    return keys


def _script_required_keys() -> set[str]:
    """Ключи, которые ``check-prod-secrets.sh`` требует безусловно.

    Условные блоки (Sber включается только при ``BANK_SYNC_PROVIDERS=sber``) сюда не идут:
    берём строки до первого ``if``-блока провайдеров, как их видит свежий стенд.
    """
    text = CHECK_SCRIPT.read_text(encoding="utf-8")
    sber_block = text.find("bank_sync_providers=")
    head = text[:sber_block] if sber_block != -1 else text
    return set(re.findall(r"^\s*require_integrations_value\s+(\w+)", head, re.M))


def test_template_carries_every_required_key() -> None:
    """Ключ, забытый в шаблоне, не доедет до прода — рендер идёт построчно по шаблону."""
    missing = sorted(set(_required_keys()) - _template_keys())
    assert not missing, (
        "в env.integrations.example нет обязательных ключей: "
        f"{missing}. Рендер собирает файл ПО ШАБЛОНУ, поэтому такой ключ молча "
        "выпадет из .env.integrations на проде (инцидент 21.07.2026 с IIKO_CLOUD_*)."
    )


def test_preflight_check_requires_every_required_key() -> None:
    """То, что билдер считает обязательным, проверка на сервере обязана ловить до старта."""
    missing = sorted(set(_required_keys()) - _script_required_keys())
    assert not missing, (
        "check-prod-secrets.sh не требует обязательных ключей: "
        f"{missing}. Стенд поднимется с пустым значением, а сломается позже и молча."
    )


def _config_production_requirements() -> set[str]:
    """Переменные, без которых ``Settings`` отказывается собраться в production.

    Берём имена из сообщений валидатора (``errors.append("NAME must ...")``) — они и есть
    контракт: каждое такое сообщение означает «стенд не поднимется».
    """
    block = CONFIG.read_text(encoding="utf-8")
    start = block.find("def validate_production_settings")
    assert start != -1, "validate_production_settings не найден — тест устарел"
    end = block.find("@property", start)
    body = block[start : end if end != -1 else len(block)]
    return set(re.findall(r'errors\.append\(\s*\n?\s*"([A-Z0-9_]+) must', body))


def test_preflight_check_covers_settings_production_requirements() -> None:
    """Что роняет старт в проде, предполётная проверка обязана поймать заранее.

    ``get_settings()`` вызывается на импорте ``app/db/session.py``, поэтому пропущенная
    переменная валит не отдельный запрос, а подъём api и scheduler целиком. Так и было с
    ``TBANK_WEBHOOK_TOKEN``: код требовал его с самого начала, скрипт молчал, и стенд,
    собранный строго по докам, проходил проверку с «passed» и не стартовал.
    """
    script = CHECK_SCRIPT.read_text(encoding="utf-8")
    # Переменная может жить и в .env.prod, и в .env.integrations — скрипт проверяет их
    # разными функциями. Для контракта важно одно: проверяется ли она вообще.
    checked = set(
        re.findall(
            r"^\s*require_(?:value|value_either|equal|integrations_value)\s+(\w+)",
            script,
            re.M,
        )
    )
    missing = sorted(_config_production_requirements() - checked)
    assert not missing, (
        "check-prod-secrets.sh не проверяет то, без чего Settings не соберётся в "
        f"production: {missing}. Стенд пройдёт предполётную проверку и не поднимется."
    )


def test_templates_carry_settings_requirements() -> None:
    """И в шаблонах эти переменные должны быть — иначе их некуда вписать при развёртывании."""

    def _keys(path: Path) -> set[str]:
        keys = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in line:
                continue
            keys.add(line.split("=", 1)[0].strip())
        return keys

    available = _keys(PROD_TEMPLATE) | _keys(TEMPLATE)
    missing = sorted(_config_production_requirements() - available)
    assert not missing, (
        "ни в env.prod.example, ни в env.integrations.example нет обязательных для "
        f"прода ключей: {missing}"
    )


def test_preflight_check_has_no_extra_required_keys() -> None:
    """И наоборот: скрипт не должен требовать того, чего билдер обязательным не считает.

    Иначе развёртывание строго по шаблону не проходит предполётную проверку — человек
    видит отказ на значении, которое взять неоткуда.
    """
    extra = sorted(_script_required_keys() - set(_required_keys()))
    assert not extra, (
        "check-prod-secrets.sh требует ключи вне REQUIRED_KEYS: "
        f"{extra}. Либо добавь их в REQUIRED_KEYS билдера, либо сделай проверку "
        "предупреждением (optional_integrations_value)."
    )
