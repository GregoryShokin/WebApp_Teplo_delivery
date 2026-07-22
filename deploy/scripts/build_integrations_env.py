from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path


SIMPLE_VALUE_RE = re.compile(r"^[A-Za-z0-9_@%+=:,./-]+$")
# Rendering follows the template line by line, so a key missing from the template is
# silently dropped from the deployed file. That is how IIKO_CLOUD_* disappeared on
# 2026-07-21 and every Cloud call started failing with KeyError once the containers
# were recreated. These keys must carry a value or the build stops before anything
# reaches production.
REQUIRED_KEYS = (
    "IIKO_SERVER_BASE_URL",
    "IIKO_SERVER_LOGIN",
    "IIKO_SERVER_PASSWORD",
    "IIKO_CLOUD_APP_ID",
    "IIKO_CLOUD_API_LOGIN",
    "IIKO_CLOUD_CLIENT_SECRET",
    "TBANK_API_BASE_URL",
    "TBANK_PAYMENT_BASE_URL",
    "TBANK_API_ACCESS_TOKEN",
)
# Prefixes owned by THIS file (mail, SBIS and the rest live in .env.prod). A key with
# one of these set locally but missing from the template never reaches production —
# worth a warning even when it is not required.
INTEGRATION_PREFIXES = ("IIKO_", "TBANK_", "SBER_")
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
RUSSIAN_TRUSTED_ROOT_CA = (
    Path(__file__).resolve().parents[1] / "certs" / "russian_trusted_root_ca.pem"
)


def strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def parse_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = strip_quotes(value)
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
        destination = sber_dir / remote_name
        shutil.copy2(source, destination)
        if env_name == "SBER_API_CA_BUNDLE_PATH":
            # Sber Fintech uses SberCA, whereas SSO OAuth uses the Russian trusted
            # root certificate. Both are required for a fully verified token refresh.
            with (
                destination.open("ab") as bundle,
                RUSSIAN_TRUSTED_ROOT_CA.open("rb") as root,
            ):
                bundle.write(b"\n")
                shutil.copyfileobj(root, bundle)
        values[env_name] = f"/run/secrets/teplo/sber/{remote_name}"


def template_defaults(template: Path) -> dict[str, str]:
    """Values written straight into the template — base URLs, timeouts."""

    defaults: dict[str, str] = {}
    for raw_line in template.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        defaults[key.strip()] = strip_quotes(value)
    return defaults


def effective_values(template: Path, values: dict[str, str]) -> dict[str, str]:
    """What the deployed file will actually carry: the local .env wins, the template
    default fills the gap. Without the fallback a key the local .env never had (base
    URLs, timeouts) would be deployed EMPTY and override nothing but itself."""

    defaults = template_defaults(template)
    return {
        key: (values.get(key, "").strip() or default)
        for key, default in defaults.items()
    }


def missing_required(effective: dict[str, str]) -> list[str]:
    return [key for key in REQUIRED_KEYS if not effective.get(key, "").strip()]


def render_env(template: Path, values: dict[str, str]) -> str:
    effective = effective_values(template, values)
    output: list[str] = []
    for raw_line in template.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in raw_line:
            output.append(raw_line)
            continue
        key = raw_line.split("=", 1)[0].strip()
        output.append(f"{key}={quote_env_value(effective.get(key, ''))}")
    return "\n".join(output) + "\n"


def dropped_integration_keys(values: dict[str, str], known: set[str]) -> list[str]:
    return sorted(
        key
        for key, value in values.items()
        if value.strip()
        and key not in known
        and key.startswith(INTEGRATION_PREFIXES)
    )


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

    effective = effective_values(args.template, values)
    for key in dropped_integration_keys(values, set(effective)):
        print(
            f"warning: {key} is set in {args.source} but absent from {args.template.name}"
            " — it will NOT reach production. Add it to the template.",
            file=sys.stderr,
        )
    missing = missing_required(effective)
    if missing:
        raise SystemExit(
            "refusing to build .env.integrations: no value for "
            + ", ".join(missing)
            + f"\nfill them in {args.source} (the deployed file would start the "
            "containers without working credentials)"
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_env(args.template, values), encoding="utf-8")


if __name__ == "__main__":
    main()
