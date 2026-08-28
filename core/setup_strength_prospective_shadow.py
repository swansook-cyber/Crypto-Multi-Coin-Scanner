# -*- coding: utf-8 -*-
"""Prospective setup-strength shadow tracker.

This module is analytics-only. It observes the existing setup_strength value
after production decisions have assigned a final signal status. It never blocks
signals, changes score/confidence, or routes Telegram/Cornix messages.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from core.signal_identity import canonical_signal_key, normalize_float, normalize_side, normalize_symbol


SHADOW_VERSION = "SETUP_STRENGTH_PROSPECTIVE_V1"
TARGET_LOW_SETUP_CLOSED = 100

FIELDNAMES = [
    "canonical_signal_key",
    "timestamp_utc",
    "symbol",
    "side",
    "setup_strength",
    "setup_shadow_class",
    "score",
    "confidence",
    "quality_tier",
    "market_session",
    "signal_status",
    "rejection_reason",
    "entry",
    "sl",
    "tp1",
    "tp2",
    "shadow_version",
    "prospective_start_timestamp_utc",
    "generated_at_utc",
    "sr_class",
    "market_exhaustion_class",
    "entry_timing_class",
    "btc_regime",
    "loss_cooldown_active",
    "daily_risk_state",
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def classify_setup_strength(value: Any) -> str:
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric):
        return "UNKNOWN"
    return "LOW_SETUP_SHADOW" if float(numeric) <= 79 else "NORMAL_SETUP_SHADOW"


def _text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    return "" if text.lower() in {"nan", "nat", "none", "null"} else text


def _number_text(value: Any) -> str:
    normalized = normalize_float(value)
    return normalized if normalized else _text(value)


def _setup_strength_from_signal(signal: Any) -> Any:
    value = getattr(signal, "setup_strength", "")
    if value is None:
        return ""
    return value


def classify_signal_setup_strength(signal: Any) -> str:
    return classify_setup_strength(_setup_strength_from_signal(signal))


def build_shadow_record(
    signal: Any,
    *,
    signal_status: str,
    rejection_reason: str = "",
    prospective_start_timestamp_utc: str = "",
    generated_at_utc: str | None = None,
    sr_class: str = "",
    market_exhaustion_class: str = "",
    entry_timing_class: str = "",
    loss_cooldown_active: str = "",
    daily_risk_state: str = "",
) -> dict[str, Any]:
    setup_strength = _setup_strength_from_signal(signal)
    timestamp = getattr(signal, "timestamp", "")
    if hasattr(timestamp, "isoformat"):
        timestamp = timestamp.isoformat()
    side = getattr(signal, "direction", getattr(signal, "side", ""))
    entry = getattr(signal, "entry", "")
    canonical_key = canonical_signal_key(
        symbol=getattr(signal, "symbol", ""),
        side=side,
        timestamp=timestamp,
        entry=entry,
        signal_id=getattr(signal, "signal_id", ""),
        candidate_id=getattr(signal, "candidate_id", ""),
    )
    return {
        "canonical_signal_key": canonical_key,
        "timestamp_utc": _text(timestamp),
        "symbol": normalize_symbol(getattr(signal, "symbol", "")),
        "side": normalize_side(side),
        "setup_strength": _text(setup_strength),
        "setup_shadow_class": classify_signal_setup_strength(signal),
        "score": _text(getattr(signal, "score", "")),
        "confidence": _text(getattr(signal, "confidence", "")),
        "quality_tier": _text(getattr(signal, "watchlist_tier", getattr(signal, "quality_tier", ""))),
        "market_session": _text(getattr(signal, "market_session", "")),
        "signal_status": _text(signal_status),
        "rejection_reason": _text(rejection_reason),
        "entry": _number_text(entry),
        "sl": _number_text(getattr(signal, "sl", getattr(signal, "stop_loss", ""))),
        "tp1": _number_text(getattr(signal, "tp1", "")),
        "tp2": _number_text(getattr(signal, "tp2", "")),
        "shadow_version": SHADOW_VERSION,
        "prospective_start_timestamp_utc": prospective_start_timestamp_utc,
        "generated_at_utc": generated_at_utc or utc_now_iso(),
        "sr_class": _text(sr_class),
        "market_exhaustion_class": _text(market_exhaustion_class),
        "entry_timing_class": _text(entry_timing_class),
        "btc_regime": _text(getattr(signal, "btc_regime", "")),
        "loss_cooldown_active": _text(loss_cooldown_active),
        "daily_risk_state": _text(daily_risk_state),
    }


class SetupStrengthProspectiveShadowLogger:
    """Append-only idempotent prospective shadow logger."""

    def __init__(self, path: Path, prospective_start_timestamp_utc: str | None = None) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.prospective_start_timestamp_utc = self._resolve_start(prospective_start_timestamp_utc)
        self._known_keys: set[str] | None = None
        self._ensure_header()

    def _resolve_start(self, explicit: str | None) -> str:
        if explicit:
            return explicit
        if self.path.exists() and self.path.stat().st_size > 0:
            try:
                with self.path.open("r", encoding="utf-8", newline="") as handle:
                    reader = csv.DictReader(handle)
                    for row in reader:
                        value = str(row.get("prospective_start_timestamp_utc", "")).strip()
                        if value:
                            return value
            except OSError:
                pass
        return utc_now_iso()

    def _ensure_header(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            with self.path.open("w", encoding="utf-8", newline="") as handle:
                csv.DictWriter(handle, fieldnames=FIELDNAMES).writeheader()
            return
        try:
            existing = pd.read_csv(self.path)
        except Exception:
            return
        missing = [field for field in FIELDNAMES if field not in existing.columns]
        if not missing:
            return
        for field in missing:
            existing[field] = ""
        existing.to_csv(self.path, index=False, columns=FIELDNAMES)

    def _load_keys(self) -> set[str]:
        if self._known_keys is not None:
            return self._known_keys
        keys: set[str] = set()
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8", newline="") as handle:
                    reader = csv.DictReader(handle)
                    for row in reader:
                        key = str(row.get("canonical_signal_key", "")).strip()
                        if key:
                            keys.add(key)
            except OSError:
                keys = set()
        self._known_keys = keys
        return keys

    def append(self, record: dict[str, Any]) -> bool:
        key = str(record.get("canonical_signal_key", "")).strip()
        if not key:
            return False
        keys = self._load_keys()
        if key in keys:
            return False
        with self.path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
            writer.writerow({field: record.get(field, "") for field in FIELDNAMES})
            handle.flush()
        keys.add(key)
        return True

    def log_signal(self, signal: Any, *, signal_status: str, rejection_reason: str = "", **context: Any) -> bool:
        record = build_shadow_record(
            signal,
            signal_status=signal_status,
            rejection_reason=rejection_reason,
            prospective_start_timestamp_utc=self.prospective_start_timestamp_utc,
            **context,
        )
        return self.append(record)


@dataclass(frozen=True)
class ProspectiveReport:
    text: str
    low_closed: int
    target: int


def _read_csv_safe(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(path, low_memory=False)
    except (FileNotFoundError, pd.errors.EmptyDataError):
        return pd.DataFrame()


def _canonical_from_frame(df: pd.DataFrame, timestamp_col: str = "timestamp") -> pd.Series:
    if df.empty:
        return pd.Series(dtype=str)
    side_source = df["side"] if "side" in df.columns else df.get("direction", pd.Series([""] * len(df)))
    return pd.Series(
        [
            canonical_signal_key(symbol=symbol, side=side, timestamp=timestamp, entry=entry)
            for symbol, side, timestamp, entry in zip(
                df.get("symbol", pd.Series([""] * len(df))),
                side_source,
                df.get(timestamp_col, pd.Series([""] * len(df))),
                df.get("entry", pd.Series([""] * len(df))),
            )
        ],
        index=df.index,
    )


def _result_r(row: pd.Series) -> float:
    numeric = pd.to_numeric(pd.Series([row.get("net_r_estimate", "")]), errors="coerce").iloc[0]
    if pd.notna(numeric):
        return float(numeric)
    result = str(row.get("result", "")).upper()
    hit = str(row.get("hit_target", "")).upper()
    if result == "LOSS":
        return -1.0
    if result == "WIN":
        if hit in {"TP2", "2"}:
            return 2.0
        if hit in {"TP3", "3"}:
            return 3.0
        return 1.0
    return 0.0


def _perf(df: pd.DataFrame) -> dict[str, Any]:
    sent = df[df.get("signal_status", pd.Series(dtype=str)).fillna("").astype(str).str.lower() == "sent"] if "signal_status" in df else pd.DataFrame()
    closed = sent[sent.get("result", pd.Series(dtype=str)).fillna("").astype(str).str.upper().isin(["WIN", "LOSS"])] if not sent.empty else pd.DataFrame()
    wins = int((closed.get("result", pd.Series(dtype=str)).fillna("").astype(str).str.upper() == "WIN").sum()) if not closed.empty else 0
    losses = int((closed.get("result", pd.Series(dtype=str)).fillna("").astype(str).str.upper() == "LOSS").sum()) if not closed.empty else 0
    net_r = float(closed.get("result_r", pd.Series(dtype=float)).sum()) if not closed.empty else 0.0
    avg_r = float(closed.get("result_r", pd.Series(dtype=float)).mean()) if not closed.empty else 0.0
    return {
        "observed": int(len(df)),
        "sent": int(len(sent)),
        "closed": int(len(closed)),
        "wins": wins,
        "losses": losses,
        "wr": (wins / len(closed) * 100.0) if len(closed) else None,
        "net_r": net_r,
        "avg_r": avg_r,
        "open": max(0, int(len(sent) - len(closed))),
    }


def build_prospective_report(shadow_path: Path, signals_path: Path) -> ProspectiveReport:
    shadow = _read_csv_safe(shadow_path)
    if shadow.empty:
        return ProspectiveReport("Setup Strength Prospective Shadow\nN/A: no prospective rows yet.", 0, TARGET_LOW_SETUP_CLOSED)
    if "canonical_signal_key" not in shadow.columns:
        shadow["canonical_signal_key"] = _canonical_from_frame(shadow, "timestamp_utc")
    generated = pd.to_datetime(shadow.get("generated_at_utc"), utc=True, errors="coerce")
    start = pd.to_datetime(shadow.get("prospective_start_timestamp_utc"), utc=True, errors="coerce")
    prospective = shadow[(generated.notna()) & (start.notna()) & (generated >= start)].copy()
    signals = _read_csv_safe(signals_path)
    if not signals.empty:
        signals["canonical_signal_key"] = _canonical_from_frame(signals, "timestamp")
        signals["result_r"] = signals.apply(_result_r, axis=1)
        for column in ["result", "closed_at"]:
            if column not in signals.columns:
                signals[column] = ""
        outcome_cols = ["canonical_signal_key", "result", "closed_at", "result_r"]
        prospective = prospective.merge(signals[outcome_cols].drop_duplicates("canonical_signal_key", keep="last"), on="canonical_signal_key", how="left")
    else:
        prospective["result"] = ""
        prospective["closed_at"] = ""
        prospective["result_r"] = 0.0

    low = prospective[prospective.get("setup_shadow_class", "").astype(str) == "LOW_SETUP_SHADOW"].copy()
    normal = prospective[prospective.get("setup_shadow_class", "").astype(str) == "NORMAL_SETUP_SHADOW"].copy()
    low_perf = _perf(low)
    normal_perf = _perf(normal)

    def line_perf(label: str, values: dict[str, Any]) -> list[str]:
        wr = "N/A" if values["wr"] is None else f"{values['wr']:.1f}%"
        return [
            f"{label}:",
            f"- observed N: {values['observed']}",
            f"- sent N: {values['sent']}",
            f"- closed N: {values['closed']}",
            f"- wins/losses: {values['wins']}/{values['losses']}",
            f"- WR: {wr}",
            f"- Net R: {values['net_r']:.2f}",
            f"- Avg R: {values['avg_r']:.3f}",
            f"- open/unresolved: {values['open']}",
        ]

    lines = [
        "Setup Strength Prospective Shadow V1",
        "=" * 37,
        f"Shadow version: {SHADOW_VERSION}",
        f"Progress: {low_perf['closed']} / {TARGET_LOW_SETUP_CLOSED} prospective LOW_SETUP sent/closed observations",
        "",
        *line_perf("PROSPECTIVE LOW SETUP", low_perf),
        "",
        *line_perf("NORMAL SETUP comparison", normal_perf),
        "",
        "LOW_SETUP by side:",
    ]
    for side, group in low.groupby(low.get("side", pd.Series(dtype=str)).fillna("Unknown").astype(str)):
        p = _perf(group)
        wr = "N/A" if p["wr"] is None else f"{p['wr']:.1f}%"
        lines.append(f"- {side}: closed {p['closed']} WR {wr} NetR {p['net_r']:.2f}")
    lines.append("")
    lines.append("LOW_SETUP by session:")
    for session, group in low.groupby(low.get("market_session", pd.Series(dtype=str)).fillna("Unknown").astype(str)):
        p = _perf(group)
        wr = "N/A" if p["wr"] is None else f"{p['wr']:.1f}%"
        lines.append(f"- {session}: closed {p['closed']} WR {wr} NetR {p['net_r']:.2f}")
    lines.append("")
    lines.append("LOW_SETUP by month:")
    if "timestamp_utc" in low.columns and not low.empty:
        low["month"] = pd.to_datetime(low["timestamp_utc"], utc=True, errors="coerce").dt.strftime("%Y-%m")
        for month, group in low.groupby(low["month"].fillna("Unknown").astype(str)):
            p = _perf(group)
            wr = "N/A" if p["wr"] is None else f"{p['wr']:.1f}%"
            lines.append(f"- {month}: closed {p['closed']} WR {wr} NetR {p['net_r']:.2f}")
    else:
        lines.append("- N/A")
    return ProspectiveReport("\n".join(lines), int(low_perf["closed"]), TARGET_LOW_SETUP_CLOSED)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report setup-strength prospective shadow tracking.")
    parser.add_argument("--shadow-path", type=Path, default=Path("logs/setup_strength_prospective_shadow.csv"))
    parser.add_argument("--signals-path", type=Path, default=Path("logs/signals.csv"))
    parser.add_argument("--report", action="store_true", help="Print the prospective shadow report.")
    args = parser.parse_args(argv)
    report = build_prospective_report(args.shadow_path, args.signals_path)
    print(report.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
