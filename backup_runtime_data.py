# -*- coding: utf-8 -*-
"""Create a deployment-safe runtime data backup.

Secrets are intentionally excluded: .env, tokens, private keys, passwords, and
real config files that may contain credentials.
"""

from __future__ import annotations

import argparse
import fnmatch
import zipfile
from datetime import datetime, timezone
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
BACKUP_DIR = BASE_DIR / "backups"

INCLUDE_PATTERNS = [
    "logs/*.csv",
    "reports/*.html",
    "reports/*.csv",
    "signal_state.json",
    "watchdog/state.json",
    "watchdog/services.json",
    "logs/*lock*",
    "logs/*state*",
    ".env.example",
    "config.example.bat",
    "requirements.txt",
]

EXCLUDE_PATTERNS = [
    ".env",
    "*.env",
    "config.bat",
    "*token*",
    "*secret*",
    "*password*",
    "*.pem",
    "*.key",
    "id_rsa*",
    "backups/*",
]


def is_excluded(path: Path) -> bool:
    rel = path.relative_to(BASE_DIR).as_posix()
    lower = rel.lower()
    return any(fnmatch.fnmatch(lower, pattern.lower()) for pattern in EXCLUDE_PATTERNS)


def iter_backup_files() -> list[Path]:
    files: set[Path] = set()
    for pattern in INCLUDE_PATTERNS:
        for path in BASE_DIR.glob(pattern):
            if path.is_file() and not is_excluded(path):
                files.add(path)
    return sorted(files)


def create_backup(destination: Path | None = None) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if destination is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = BACKUP_DIR / f"runtime_{timestamp}.zip"
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        manifest_lines = [
            "Crypto Multi-Coin Scanner runtime backup",
            f"created_utc={datetime.now(timezone.utc).isoformat()}",
            "secrets_excluded=true",
            "",
        ]
        for path in iter_backup_files():
            archive.write(path, path.relative_to(BASE_DIR).as_posix())
            manifest_lines.append(path.relative_to(BASE_DIR).as_posix())
        archive.writestr("BACKUP_MANIFEST.txt", "\n".join(manifest_lines) + "\n")
    return destination


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Back up runtime CSV/report/state data without secrets.")
    parser.add_argument("--output", type=Path, help="Optional zip destination.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    backup = create_backup(args.output)
    print(f"Runtime backup created: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
