from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path


SIMPLE_VALUE_RE = re.compile(r"^[A-Za-z0-9_@%+=:,./-]+$")
SBER_CERT_DEFAULTS = {
    "SBER_API_TLS_CERT_PATH": (
        "client.crt",
        "integrations/sber/credentials/sber_client_cert.pem",
    ),
    "SBER_API_TLS_KEY_PATH": (
        "client.key",
        "integrations/sber/credentials/sber_client_key.pem",
    ),
    "SBER_API_CA_BUNDLE_PATH": ("ca.pem", ""),
}


def parse_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        result[key] = value
    return result


def quote_env_value(value: str) -> str:
    if not value:
        return ""
    if SIMPLE_VALUE_RE.fullmatch(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def resolve_source_path(value: str, repo_dir: Path) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = repo_dir / path
    return path if path.exists() else None


def copy_sber_secret_files(
    values: dict[str, str], repo_dir: Path, secrets_dir: Path
) -> None:
    sber_dir = secrets_dir / "sber"
    for env_name, (remote_name, fallback_relative) in SBER_CERT_DEFAULTS.items():
        source = resolve_source_path(values.get(env_name, ""), repo_dir)
        if source is None and fallback_relative:
            source = resolve_source_path(fallback_relative, repo_dir)
        if source is None:
            continue
        sber_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, sber_dir / remote_name)
        values[env_name] = f"/run/secrets/teplo/sber/{remote_name}"


def render_env(template: Path, values: dict[str, str]) -> str:
    output: list[str] = []
    for raw_line in template.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in raw_line:
            output.append(raw_line)
            continue
        key = raw_line.split("=", 1)[0].strip()
        output.append(f"{key}={quote_env_value(values.get(key, ''))}")
    return "\n".join(output) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build deploy/.env.integrations from local .env."
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--template", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--secrets-dir", required=True, type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    repo_dir = args.template.resolve().parents[1]
    values = parse_env(args.source)
    copy_sber_secret_files(values, repo_dir, args.secrets_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_env(args.template, values), encoding="utf-8")


if __name__ == "__main__":
    main()
