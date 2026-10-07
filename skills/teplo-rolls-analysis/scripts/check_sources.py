#!/usr/bin/env python3
"""Read-only source freshness check. No database, settings, credentials or network."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def check(repo: Path, manifest: dict) -> dict:
    repo = repo.resolve()
    changed, missing, matched = [], [], []
    for item in manifest["files"]:
        relative = item["path"]
        path = (repo / relative).resolve()
        if not path.is_relative_to(repo):
            raise ValueError(f"Path escapes repository: {relative}")
        if not path.is_file():
            missing.append(relative)
        elif hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            changed.append(relative)
        else:
            matched.append(relative)
    try:
        run = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                             check=True, capture_output=True, text=True, timeout=10)
        current_commit = run.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        current_commit = None
    return {
        "status": "requires_source_review" if changed or missing else "audited_sources_match",
        "repo": str(repo), "audited_commit": manifest["audited_commit"],
        "current_commit": current_commit, "audit_date": manifest["audit_date"],
        "matched_count": len(matched), "changed": changed, "missing": missing,
        "live_settings_checked": False,
        "notice": "Hashes check listed files only, not live recipes, sales, external skill files, unlisted modules or data completeness.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=Path(__file__).resolve().parents[1]
                        / "references" / "source-manifest.json")
    args = parser.parse_args()
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        result = check(args.repo, manifest)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "check_failed", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["changed"] or result["missing"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
