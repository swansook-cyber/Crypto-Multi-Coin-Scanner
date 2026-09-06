"""Explicit reporting populations and immutable daily snapshot evidence.

This module is deliberately reporting-only.  It never imports scanner routing
code, mutates source data, or exposes environment values other than the
non-secret routing fields supplied by the caller.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

import pandas as pd


SNAPSHOT_VERSION = "REPORTING_TRUTH_V1"
LIVE_SENT_PERFORMANCE = "LIVE_SENT_PERFORMANCE"
RESEARCH_ALL_STATUS_PERFORMANCE = "RESEARCH_ALL_STATUS_PERFORMANCE"
LIVE_ROUTING_UNIVERSE = "LIVE_ROUTING_UNIVERSE"
PERFORMANCE_QUALIFIED_RESEARCH_SYMBOLS = "PERFORMANCE_QUALIFIED_RESEARCH_SYMBOLS"
PERFORMANCE_V1 = "performance_v1"
ANALYTICS_V3 = "analytics_v3"


class SnapshotConflictError(RuntimeError):
    """Raised only when a different immutable manifest already exists."""


@dataclass(frozen=True)
class SnapshotWriteResult:
    path: Path
    status: str
    conflict_path: Path | None = None


def terminal_rows(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "result" not in df.columns:
        return df.copy()
    return df[df["result"].fillna("").astype(str).str.upper().isin(["WIN", "LOSS"])].copy()


def live_sent_rows(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    status = df.get("signal_status", pd.Series("sent", index=df.index))
    return df[status.fillna("sent").astype(str).str.lower().eq("sent")].copy()


def research_all_status_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Return normalized scanner rows without retrospective status exclusion."""
    return df.copy()


def status_counts(df: pd.DataFrame) -> dict[str, int]:
    if df.empty:
        return {}
    status = df.get("signal_status", pd.Series("sent", index=df.index))
    counts = status.fillna("sent").replace("", "sent").astype(str).str.lower().value_counts()
    return {str(key): int(value) for key, value in sorted(counts.items())}


def canonical_signal_keys(df: pd.DataFrame) -> list[str]:
    """Stable, non-secret identifiers for the sent/closed population."""
    if df.empty:
        return []
    data = df.copy()
    parts: list[pd.Series] = []
    for column in ("timestamp", "closed_at", "symbol", "side", "entry", "result", "hit_target", "signal_status"):
        if column in data.columns:
            value = data[column]
        else:
            value = pd.Series("", index=data.index)
        if column in {"timestamp", "closed_at"}:
            parsed = pd.to_datetime(value, utc=True, errors="coerce")
            value = parsed.dt.strftime("%Y-%m-%dT%H:%M:%SZ").fillna("")
        elif column == "entry":
            value = pd.to_numeric(value, errors="coerce").map(lambda item: "" if pd.isna(item) else format(float(item), ".12g"))
        else:
            value = value.fillna("").astype(str).str.strip().str.upper()
        parts.append(value.astype(str))
    return sorted("|".join(row) for row in zip(*parts))


def canonical_evidence_rows(df: pd.DataFrame) -> list[dict[str, str]]:
    """Stable report-date rows containing every field used by accounting views.

    This is deliberately independent of CSV byte layout and source-file mtime.
    It includes all statuses because research analytics and report-only findings
    are part of the date-scoped reporting evidence.
    """
    if df.empty:
        return []
    fields = (
        "timestamp", "closed_at", "symbol", "side", "entry", "sl", "stop_loss",
        "tp1", "tp2", "tp3", "rr", "risk_reward", "real_rr", "net_r_estimate",
        "result", "outcome", "hit_target", "signal_status",
    )
    rows: list[dict[str, str]] = []
    for _, row in df.iterrows():
        item: dict[str, str] = {}
        for field in fields:
            value = row.get(field, "")
            if field in {"timestamp", "closed_at"}:
                parsed = pd.to_datetime(value, utc=True, errors="coerce")
                item[field] = "" if pd.isna(parsed) else parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
            elif field in {"entry", "sl", "stop_loss", "tp1", "tp2", "tp3", "rr", "risk_reward", "real_rr", "net_r_estimate"}:
                numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
                item[field] = "" if pd.isna(numeric) else format(float(numeric), ".12g")
            else:
                item[field] = "" if pd.isna(value) else str(value).strip().upper()
        rows.append(item)
    return sorted(rows, key=lambda item: stable_sha256(item))


def stable_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def git_commit(base_dir: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(base_dir), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value or None


def routing_snapshot_from_config(config: Any) -> dict[str, Any]:
    """Project a ScannerConfig to non-secret routing truth only."""
    tiers = getattr(config, "watchlist_tiers", {}) or {}
    ordered_tiers = {str(symbol): str(tier) for symbol, tier in sorted(tiers.items())}
    return {
        "population": LIVE_ROUTING_UNIVERSE,
        "source": "ScannerConfig.from_env",
        "symbols": list(getattr(config, "watchlist", []) or []),
        "tiers": ordered_tiers,
        "report_only_controls": {
            "enable_tier_c_report_only": bool(getattr(config, "enable_tier_c_report_only", False)),
            "weak_symbol_report_only_symbols": list(getattr(config, "weak_symbol_report_only_symbols", []) or []),
            "session_report_only_sessions": list(getattr(config, "session_report_only_sessions", []) or []),
            "london_long_report_only": bool(getattr(config, "london_long_report_only", False)),
        },
    }


def routing_summary(routing: Mapping[str, Any] | None) -> str:
    if not routing:
        return "Unavailable: ScannerConfig routing snapshot was not supplied."
    symbols = routing.get("symbols", [])
    tiers = routing.get("tiers", {})
    tier_counts: dict[str, int] = {}
    for tier in tiers.values() if isinstance(tiers, Mapping) else []:
        tier_counts[str(tier)] = tier_counts.get(str(tier), 0) + 1
    counts = ", ".join(f"Tier {tier}: {count}" for tier, count in sorted(tier_counts.items())) or "no tier metadata"
    return f"Configured symbols ({len(symbols)}): {', '.join(str(item) for item in symbols) or 'N/A'}; {counts}."


def build_snapshot_manifest(
    *,
    report_date_utc: str,
    source_path: Path,
    source_rows: pd.DataFrame,
    routing: Mapping[str, Any] | None,
    output_hashes: Mapping[str, str] | None = None,
    base_dir: Path | None = None,
    source_file_row_count: int | None = None,
    generated_at_utc: str | None = None,
) -> dict[str, Any]:
    sent = live_sent_rows(source_rows)
    closed_sent = terminal_rows(sent)
    keys = canonical_signal_keys(closed_sent)
    evidence_rows = canonical_evidence_rows(source_rows)
    evidence_identity = {
        "snapshot_version": SNAPSHOT_VERSION,
        "report_date_utc": report_date_utc,
        "population_definitions": {
            "production": {"name": LIVE_SENT_PERFORMANCE, "filter": "signal_status == sent"},
            "research": {"name": RESEARCH_ALL_STATUS_PERFORMANCE, "filter": "all normalized statuses"},
        },
        "formula_versions": {
            "overall_r_formula_version": PERFORMANCE_V1,
            "research_r_formula_version": ANALYTICS_V3,
        },
        "relevant_population": {
            "row_count": int(len(source_rows)),
            "canonical_rows_sha256": stable_sha256(evidence_rows),
            "canonical_closed_sent_population_sha256": stable_sha256(keys),
        },
    }
    manifest: dict[str, Any] = {
        "snapshot_version": SNAPSHOT_VERSION,
        "report_date_utc": report_date_utc,
        "generated_at_utc": generated_at_utc or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "evidence_identity": evidence_identity,
        "source": {
            "signals_csv_path": str(source_path),
            "size_bytes": source_path.stat().st_size if source_path.exists() else None,
            "sha256": file_sha256(source_path),
            "row_count": int(source_file_row_count if source_file_row_count is not None else len(source_rows)),
        },
        "production_population": {
            "name": LIVE_SENT_PERFORMANCE,
            "filter": "signal_status == sent",
            "sent_rows": int(len(sent)),
            "closed_sent_rows": int(len(closed_sent)),
            "wins": int((closed_sent.get("result", pd.Series(dtype=str)).astype(str).str.upper() == "WIN").sum()),
            "losses": int((closed_sent.get("result", pd.Series(dtype=str)).astype(str).str.upper() == "LOSS").sum()),
            "open": int((sent.get("result", pd.Series(dtype=str)).fillna("OPEN").astype(str).str.upper() == "OPEN").sum()),
            "canonical_closed_signal_keys": keys,
            "canonical_closed_population_sha256": stable_sha256(keys),
        },
        "formulas": {
            "overall_r_formula_version": PERFORMANCE_V1,
            "research_r_formula_version": ANALYTICS_V3,
        },
        "routing_provenance": {
            "historical_routing_status": "UNKNOWN; current-generation configuration is not historical routing evidence.",
            "current_generation_snapshot": dict(routing or {"population": LIVE_ROUTING_UNIVERSE, "source": "unavailable"}),
        },
        "research_population": {
            "name": RESEARCH_ALL_STATUS_PERFORMANCE,
            "row_count": int(len(source_rows)),
            "statuses_included": status_counts(source_rows),
        },
        "output_hashes": dict(output_hashes or {}),
        "git_commit": git_commit(base_dir) if base_dir else None,
    }
    # Only accounting/population evidence controls immutable equality. Source
    # file bytes, report rendering, Git revision, generation time, and current
    # routing configuration remain useful provenance but cannot create a false
    # evidence conflict by themselves.
    manifest["content_sha256"] = stable_sha256(evidence_identity)
    return manifest


def write_snapshot_manifest(manifest: Mapping[str, Any], snapshots_dir: Path) -> SnapshotWriteResult:
    """Create one immutable manifest, retain identical evidence, and preserve conflicts."""
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    date = str(manifest.get("report_date_utc", "ALL"))
    path = snapshots_dir / f"{date}.json"
    serialized = json.dumps(dict(manifest), indent=2, sort_keys=True) + "\n"
    if not path.exists():
        path.write_text(serialized, encoding="utf-8")
        return SnapshotWriteResult(path=path, status="created")
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotConflictError(f"Existing snapshot cannot be verified: {path}: {exc}") from exc
    if existing.get("content_sha256") == manifest.get("content_sha256"):
        return SnapshotWriteResult(path=path, status="identical")
    conflict_path = snapshots_dir / f"{date}.conflict-{str(manifest.get('content_sha256', 'unknown'))[:12]}.json"
    if not conflict_path.exists():
        conflict_manifest = dict(manifest)
        conflict_manifest["conflict"] = {
            "reason": "evidence_identity_sha256 differs from preserved immutable daily manifest",
            "preserved_manifest": path.name,
            "preserved_evidence_sha256": existing.get("content_sha256"),
            "new_evidence_sha256": manifest.get("content_sha256"),
        }
        conflict_path.write_text(json.dumps(conflict_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return SnapshotWriteResult(path=path, status="conflict", conflict_path=conflict_path)
