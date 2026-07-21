# -*- coding: utf-8 -*-
"""Archive development statistics before a manual live pilot season.

This utility is maintenance-only. It creates a runtime backup, copies runtime
statistics into an immutable season archive, then resets local statistics files
to fresh empty files. It never edits trading logic, configuration, Git metadata,
or backup archives.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import backup_runtime_data


BASE_DIR = Path(__file__).resolve().parent
ARCHIVE_ROOT = BASE_DIR / "archive"

ARCHIVE_DIRS = [
    "logs",
    "reports",
    "journal",
    "daily_reports",
    "performance_reports",
    "analytics",
]

ARCHIVE_FILES = [
    "signal_state.json",
]

PRESERVED_ITEMS = [
    ".env",
    ".git/",
    "backups/",
    "config.bat",
    "config.example.bat",
    ".env.example",
    "README.md",
    "PROJECT_CONTEXT.md",
    "PRODUCTION_V1.md",
]

FRESH_CSV_HEADERS = {
    "logs/manual_live_pilot.csv": [
        "timestamp_utc",
        "season",
        "symbol",
        "direction",
        "entry",
        "stop",
        "tp1",
        "tp2",
        "account_balance",
        "risk_percent",
        "max_loss_amount",
        "position_notional",
        "status",
        "result",
        "pnl_percent",
        "reason",
    ],
    "logs/signals.csv": [],
    "logs/signals_history.csv": [],
    "logs/external_signals.csv": [],
    "logs/daily_summary.csv": [],
    "logs/daily_performance.csv": [],
    "logs/symbol_performance.csv": [],
    "logs/source_performance.csv": [],
    "logs/position_management.csv": [],
    "logs/equity_curve.csv": ["timestamp", "balance", "daily_pnl", "cumulative_rr", "drawdown"],
    "logs/rejected_signals.csv": [],
    "logs/entry_timing_engine.csv": [],
}

RESET_GLOBS = [
    "logs/*.csv",
    "logs/*.json",
    "logs/*.txt",
    "reports/*.csv",
    "reports/*.html",
    "reports/*.txt",
    "journal/*.csv",
    "analytics/*.csv",
    "daily_reports/*.csv",
    "performance_reports/*.csv",
    "signal_state.json",
]


@dataclass
class ResetResult:
    season_name: str
    archive_path: Path
    backup_path: Path | None
    archived_files: list[str]
    reset_files: list[str]
    preserved_items: list[str]
    dry_run: bool = False


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def default_season_name(now: datetime | None = None) -> str:
    value = now or utc_now()
    return f"Production_S1_{value.strftime('%Y%m%d')}"


def safe_season_name(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in name.strip())
    if not cleaned:
        raise ValueError("season name cannot be blank")
    if cleaned in {".", ".."}:
        raise ValueError("season name is invalid")
    return cleaned


def rel(path: Path) -> str:
    return path.resolve().relative_to(BASE_DIR.resolve()).as_posix()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=BASE_DIR,
            text=True,
            capture_output=True,
            check=True,
        )
        return result.stdout.strip()
    except Exception:
        return "UNKNOWN"


def git_tracked_clean() -> bool:
    try:
        result = subprocess.run(
            ["git", "status", "--short", "--untracked-files=no"],
            cwd=BASE_DIR,
            text=True,
            capture_output=True,
            check=True,
        )
        return not result.stdout.strip()
    except Exception:
        return False


def scanner_version() -> str:
    return os.getenv("SCANNER_RELEASE", "V1.0")


def iter_archive_sources() -> list[Path]:
    paths: set[Path] = set()
    for directory in ARCHIVE_DIRS:
        root = BASE_DIR / directory
        if root.exists():
            for path in root.rglob("*"):
                if path.is_file():
                    paths.add(path)
    for item in ARCHIVE_FILES:
        path = BASE_DIR / item
        if path.is_file():
            paths.add(path)
    return sorted(paths)


def iter_reset_targets() -> list[Path]:
    paths: set[Path] = set()
    for pattern in RESET_GLOBS:
        for path in BASE_DIR.glob(pattern):
            if path.is_file() and not is_preserved(path):
                paths.add(path)
    for path in FRESH_CSV_HEADERS:
        paths.add(BASE_DIR / path)
    return sorted(paths)


def is_preserved(path: Path) -> bool:
    try:
        relative = rel(path)
    except Exception:
        return True
    if relative.startswith("backups/") or relative.startswith("archive/"):
        return True
    return relative in {".env", "config.bat"}


def existing_header(path: Path) -> list[str]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            return next(reader, [])
    except Exception:
        return []


def write_empty_csv(path: Path, header: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = header if header is not None else existing_header(path)
    with path.open("w", encoding="utf-8", newline="") as handle:
        if columns:
            csv.writer(handle).writerow(columns)


def reset_file(path: Path) -> None:
    relative = rel(path)
    if path.suffix.lower() == ".csv":
        configured = FRESH_CSV_HEADERS.get(relative)
        write_empty_csv(path, configured if configured else existing_header(path))
    elif path.suffix.lower() == ".json":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")


def copy_archive_files(archive_path: Path, sources: Iterable[Path]) -> list[dict[str, str | int]]:
    entries: list[dict[str, str | int]] = []
    for source in sources:
        relative = rel(source)
        destination = archive_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        entries.append(
            {
                "path": relative,
                "size": source.stat().st_size,
                "sha256": sha256_file(source),
            }
        )
    return entries


def write_manifest(
    archive_path: Path,
    season_name: str,
    backup_path: Path,
    entries: list[dict[str, str | int]],
) -> None:
    manifest = {
        "timestamp_utc": utc_now().isoformat(),
        "git_commit": git_commit(),
        "version": scanner_version(),
        "season_name": season_name,
        "backup_file": rel(backup_path) if backup_path.exists() else str(backup_path),
        "file_count": len(entries),
        "files": entries,
    }
    (archive_path / "archive_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def write_reset_report(result: ResetResult, manifest_entries: list[dict[str, str | int]]) -> None:
    lines = [
        f"# Production Reset Report: {result.season_name}",
        "",
        f"- Season: `{result.season_name}`",
        f"- Timestamp UTC: `{utc_now().isoformat()}`",
        f"- Commit: `{git_commit()}`",
        f"- Version: `{scanner_version()}`",
        f"- Backup filename: `{result.backup_path.name if result.backup_path else 'DRY_RUN'}`",
        "",
        "## Archived Files",
        "",
    ]
    lines.extend(f"- `{entry['path']}`" for entry in manifest_entries)
    lines.extend(
        [
            "",
            "## Reset Files",
            "",
        ]
    )
    lines.extend(f"- `{path}`" for path in result.reset_files)
    lines.extend(
        [
            "",
            "## Preserved Files",
            "",
        ]
    )
    lines.extend(f"- `{path}`" for path in result.preserved_items)
    lines.append("")
    (result.archive_path / "RESET_REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def create_runtime_backup() -> Path:
    try:
        return backup_runtime_data.create_backup()
    except Exception as exc:
        raise RuntimeError(f"runtime backup failed: {exc}") from exc


def confirm_reset(force: bool) -> None:
    if force:
        return
    print("Type RESET to archive and reset production statistics:")
    typed = input("> ").strip()
    if typed != "RESET":
        raise RuntimeError("confirmation failed; reset aborted")


def dry_run(season_name: str) -> ResetResult:
    archive_path = ARCHIVE_ROOT / season_name
    archived = [rel(path) for path in iter_archive_sources()]
    reset = [rel(path) for path in iter_reset_targets()]
    return ResetResult(season_name, archive_path, None, archived, reset, PRESERVED_ITEMS, dry_run=True)


def run_reset(season_name: str, force: bool = False) -> ResetResult:
    confirm_reset(force)
    backup_path = create_runtime_backup()
    archive_path = ARCHIVE_ROOT / season_name
    if archive_path.exists():
        raise RuntimeError(f"archive season already exists: {archive_path}")
    archive_path.mkdir(parents=True, exist_ok=False)

    sources = iter_archive_sources()
    manifest_entries = copy_archive_files(archive_path, sources)
    write_manifest(archive_path, season_name, backup_path, manifest_entries)

    reset_files: list[str] = []
    for target in iter_reset_targets():
        if is_preserved(target):
            continue
        reset_file(target)
        reset_files.append(rel(target))

    result = ResetResult(
        season_name=season_name,
        archive_path=archive_path,
        backup_path=backup_path,
        archived_files=[entry["path"] for entry in manifest_entries if isinstance(entry["path"], str)],
        reset_files=reset_files,
        preserved_items=PRESERVED_ITEMS,
    )
    write_reset_report(result, manifest_entries)
    return result


def format_summary(result: ResetResult) -> str:
    if result.dry_run:
        return "\n".join(
            [
                "Production Reset Dry Run",
                f"Season: {result.season_name}",
                f"Archive path: {result.archive_path}",
                "",
                f"Would archive: {len(result.archived_files)} files",
                f"Would reset: {len(result.reset_files)} files",
                "Would preserve:",
                *[f"- {item}" for item in result.preserved_items],
            ]
        )
    git_clean = "PASS" if git_tracked_clean() else "WARNING"
    return "\n".join(
        [
            "Production Reset Summary",
            "",
            f"Backup: {'PASS' if result.backup_path else 'FAIL'}",
            "Archive: PASS",
            "Statistics Reset: PASS",
            "Configuration Preserved: PASS",
            f"Git Clean: {git_clean}",
            f"Season: {result.season_name}",
            f"Backup file: {result.backup_path}",
            f"Archive path: {result.archive_path}",
            f"Archived files: {len(result.archived_files)}",
            f"Reset files: {len(result.reset_files)}",
        ]
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Archive and reset production statistics before a manual pilot season.")
    parser.add_argument("--archive", action="store_true", help="Archive runtime statistics and reset fresh production files.")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be archived/reset without changing files.")
    parser.add_argument("--force", action="store_true", help="Skip RESET confirmation prompt.")
    parser.add_argument("--season-name", default="", help='Season name, e.g. "Production_S1". Defaults to Production_S1_YYYYMMDD.')
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    season_name = safe_season_name(args.season_name or default_season_name())
    try:
        if args.dry_run:
            print(format_summary(dry_run(season_name)))
            return 0
        if not args.archive:
            print("Use --dry-run to preview or --archive to reset production statistics.", file=sys.stderr)
            return 2
        result = run_reset(season_name, force=args.force)
        print(format_summary(result))
        return 0
    except Exception as exc:
        print(f"Production reset aborted: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
