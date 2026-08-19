# -*- coding: utf-8 -*-
"""Read-only shadow-to-outcome linkage coverage report.

This tool inspects existing CSV files and prints coverage metrics. It does not
modify logs, send Telegram messages, or change scanner behavior.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from core.signal_identity import canonical_signal_key, normalize_float, normalize_side, normalize_symbol, normalize_timestamp


@dataclass
class LinkageCoverage:
    shadow: str
    total_rows: int
    unique_shadow_signals: int
    matched_sent: int
    matched_closed: int
    matched_open: int
    rejected_candidate: int
    unmatched: int
    ambiguous: int
    duplicate_shadow_rows: int
    coverage_pct_closed: float


def _read_csv(path: Path) -> pd.DataFrame:
    try:
        if path.exists():
            return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()
    return pd.DataFrame()


def _first(df: pd.DataFrame, names: list[str], default: str = "") -> pd.Series:
    out = pd.Series([""] * len(df), index=df.index)
    for name in names:
        if name in df.columns:
            values = df[name].where(~df[name].isna(), "")
            mask = out.fillna("").astype(str).str.strip().eq("")
            out = out.where(~mask, values)
    if default:
        out = out.fillna("").replace("", default)
    out = out.where(~out.isna(), "")
    return out


def _hit_level(value: Any) -> int:
    text = str(value or "").strip().upper()
    if text.startswith("TP"):
        text = text[2:]
    try:
        return max(0, int(float(text)))
    except (TypeError, ValueError):
        return 0


def _r_value(row: pd.Series) -> float:
    if row.get("result") == "LOSS":
        return -1.0
    if row.get("result") != "WIN":
        return 0.0
    rr = pd.to_numeric(pd.Series([row.get("rr")]), errors="coerce").iloc[0]
    rr = 0.0 if pd.isna(rr) else float(rr)
    target = _hit_level(row.get("hit_target"))
    if target >= 3:
        return rr if rr > 0 else 3.0
    if target >= 2:
        return rr if rr > 0 else 2.0
    return min(rr, 1.2) if rr > 0 else 1.0


def normalize_signals(df: pd.DataFrame, source_path: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    out = pd.DataFrame(index=df.index)
    out["source_path"] = source_path
    out["timestamp"] = pd.to_datetime(_first(df, ["timestamp"]).map(normalize_timestamp), utc=True, errors="coerce")
    out["symbol"] = _first(df, ["symbol", "normalized_symbol"]).map(normalize_symbol)
    out["side"] = _first(df, ["side", "direction", "normalized_direction"]).map(normalize_side)
    out["entry"] = pd.to_numeric(_first(df, ["entry", "entry_low"]), errors="coerce")
    out["entry_key"] = [normalize_float(value) for value in out["entry"]]
    out["rr"] = pd.to_numeric(_first(df, ["risk_reward", "rr"]), errors="coerce")
    out["result"] = _first(df, ["result"], "OPEN").astype(str).str.upper()
    out.loc[out["result"].isin(["", "NAN", "NONE", "NULL"]), "result"] = "OPEN"
    out["hit_target"] = _first(df, ["hit_target", "outcome"]).astype(str).str.upper()
    out["signal_status"] = _first(df, ["signal_status"], "sent").astype(str).str.lower()
    out.loc[out["result"].eq("SKIPPED") & out["signal_status"].eq("sent"), "signal_status"] = "skipped"
    out["canonical_signal_key"] = [
        canonical_signal_key(symbol=symbol, side=side, timestamp=timestamp, entry=entry)
        for symbol, side, timestamp, entry in zip(out["symbol"], out["side"], out["timestamp"], out["entry"])
    ]
    out["is_sent"] = out["signal_status"].eq("sent")
    out["is_closed"] = out["is_sent"] & out["result"].isin(["WIN", "LOSS"])
    out["is_open"] = out["is_sent"] & out["result"].eq("OPEN")
    out["r"] = out.apply(_r_value, axis=1)
    out["tp_level"] = out["hit_target"].map(_hit_level)
    return out


def normalize_shadow(df: pd.DataFrame, source_path: str, shadow_name: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    out = pd.DataFrame(index=df.index)
    out["shadow"] = shadow_name
    out["source_path"] = source_path
    out["existing_canonical_signal_key"] = _first(df, ["canonical_signal_key"])
    out["symbol"] = _first(df, ["symbol", "normalized_symbol"]).map(normalize_symbol)
    out["side"] = _first(df, ["side", "direction", "normalized_direction"]).map(normalize_side)
    out["timestamp"] = pd.to_datetime(
        _first(df, ["signal_timestamp", "final_signal_timestamp", "timestamp", "timestamp_utc"]).map(normalize_timestamp),
        utc=True,
        errors="coerce",
    )
    out["entry"] = pd.to_numeric(_first(df, ["entry", "entry_low"]), errors="coerce")
    out["entry_key"] = [normalize_float(value) for value in out["entry"]]
    derived = [
        canonical_signal_key(symbol=symbol, side=side, timestamp=timestamp, entry=entry)
        for symbol, side, timestamp, entry in zip(out["symbol"], out["side"], out["timestamp"], out["entry"])
    ]
    out["canonical_signal_key"] = out["existing_canonical_signal_key"].where(
        out["existing_canonical_signal_key"].astype(str).str.strip().ne(""),
        pd.Series(derived, index=out.index),
    )
    out["signal_status"] = _first(df, ["signal_status"]).astype(str).str.lower()
    out["class"] = _first(df, ["recommendation", "sr_gate_decision", "exhaustion_class"], "UNKNOWN").astype(str).str.upper()
    out["missing_reason"] = ""
    out.loc[out["canonical_signal_key"].astype(str).str.strip().eq(""), "missing_reason"] = "missing_identity_fields"
    out.loc[out["entry"].isna(), "missing_reason"] = "missing_entry"
    out.loc[out["timestamp"].isna(), "missing_reason"] = "missing_timestamp"
    return out


def load_signal_population(base: Path, include_archive: bool = True) -> pd.DataFrame:
    paths = [base / "logs" / "signals.csv", base / "logs" / "signals_history.csv"]
    if include_archive and (base / "archive").exists():
        paths.extend(sorted((base / "archive").rglob("logs/signals.csv")))
        paths.extend(sorted((base / "archive").rglob("logs/signals_history.csv")))
    frames = []
    for path in paths:
        if path.exists():
            normalized = normalize_signals(_read_csv(path), str(path.relative_to(base)))
            if not normalized.empty:
                frames.append(normalized)
    if not frames:
        return pd.DataFrame()
    data = pd.concat(frames, ignore_index=True)
    source_paths = data["source_path"].astype(str).str.replace("\\", "/", regex=False)
    priority = source_paths.map(
        lambda value: 3
        if value == "logs/signals.csv"
        else 2
        if value == "logs/signals_history.csv"
        else 1
        if value.endswith("signals.csv")
        else 0
    )
    data["_priority"] = priority
    return data.sort_values(["canonical_signal_key", "_priority"]).drop_duplicates("canonical_signal_key", keep="last").drop(columns=["_priority"])


def classify_shadow_rows(shadow: pd.DataFrame, signals: pd.DataFrame) -> pd.DataFrame:
    if shadow.empty:
        return shadow.copy()
    if "canonical_signal_key" not in shadow.columns:
        shadow = normalize_shadow(shadow, "runtime_dataframe", "unknown")
    signal_cols = ["canonical_signal_key", "is_sent", "is_closed", "is_open", "result", "hit_target", "r", "tp_level", "signal_status"]
    merged = shadow.merge(signals[signal_cols], on="canonical_signal_key", how="left", suffixes=("", "_signal"))
    merged["link_status"] = "UNMATCHED"
    merged.loc[merged["is_sent"].fillna(False), "link_status"] = "SENT_MATCH"
    merged.loc[merged["is_closed"].fillna(False), "link_status"] = "CLOSED_SIGNAL"
    merged.loc[merged["is_open"].fillna(False), "link_status"] = "OPEN_SIGNAL"
    rejected_statuses = {
        "logged_quality_filter",
        "skipped",
        "skipped_btc_regime",
        "skipped_correlation",
        "skipped_daily_risk_guard",
        "skipped_loss_cooldown",
        "skipped_not_top_candidate",
    }
    signal_status = (
        merged["signal_status_signal"]
        if "signal_status_signal" in merged.columns
        else merged.get("signal_status", pd.Series("", index=merged.index))
    ).fillna("").astype(str).str.lower()
    shadow_status = merged.get("signal_status", pd.Series("", index=merged.index)).fillna("").astype(str).str.lower()
    result_status = merged.get("result", pd.Series("", index=merged.index)).fillna("").astype(str).str.upper()
    rejected_mask = merged["link_status"].eq("UNMATCHED") & (
        signal_status.isin(rejected_statuses)
        | shadow_status.isin(rejected_statuses)
        | result_status.eq("SKIPPED")
    )
    merged.loc[rejected_mask, "link_status"] = "REJECTED_CANDIDATE"
    if not signals.empty and not merged.empty:
        unmatched_index = merged.index[merged["link_status"].eq("UNMATCHED")]
        indexed_signals = {
            key: group
            for key, group in signals.dropna(subset=["timestamp"]).groupby(["symbol", "side", "entry_key"], dropna=False)
        }
        for idx in unmatched_index:
            row = merged.loc[idx]
            if str(row.get("missing_reason", "")).strip():
                continue
            group = indexed_signals.get((row.get("symbol"), row.get("side"), row.get("entry_key")))
            if group is None or group.empty or pd.isna(row.get("timestamp")):
                continue
            deltas = (group["timestamp"] - row["timestamp"]).abs().dt.total_seconds()
            candidates = group[deltas <= 1.0]
            if len(candidates) != 1:
                continue
            match = candidates.iloc[0]
            merged.at[idx, "canonical_signal_key"] = match.get("canonical_signal_key", row.get("canonical_signal_key"))
            for field in ["is_sent", "is_closed", "is_open", "result", "hit_target", "r", "tp_level", "signal_status"]:
                merged.at[idx, field] = match.get(field)
            if bool(match.get("is_closed")):
                merged.at[idx, "link_status"] = "CLOSED_SIGNAL"
            elif bool(match.get("is_open")):
                merged.at[idx, "link_status"] = "OPEN_SIGNAL"
            elif bool(match.get("is_sent")):
                merged.at[idx, "link_status"] = "SENT_MATCH"
            else:
                merged.at[idx, "link_status"] = "REJECTED_CANDIDATE"
    return merged


def coverage_for(shadow_name: str, shadow: pd.DataFrame, classified: pd.DataFrame, closed_count: int) -> LinkageCoverage:
    duplicate_rows = int(shadow["canonical_signal_key"].duplicated(keep=False).sum()) if not shadow.empty and "canonical_signal_key" in shadow else 0
    unique_signals = int(shadow["canonical_signal_key"].replace("", pd.NA).dropna().nunique()) if not shadow.empty else 0
    ambiguous = int(classified["canonical_signal_key"].duplicated(keep=False).sum()) if not classified.empty else 0
    unique_classified = classified.drop_duplicates("canonical_signal_key", keep="last") if not classified.empty else classified
    matched_closed = int(unique_classified["link_status"].eq("CLOSED_SIGNAL").sum()) if not unique_classified is None and not unique_classified.empty else 0
    return LinkageCoverage(
        shadow=shadow_name,
        total_rows=int(len(shadow)),
        unique_shadow_signals=unique_signals,
        matched_sent=int(unique_classified["link_status"].isin(["SENT_MATCH", "OPEN_SIGNAL", "CLOSED_SIGNAL"]).sum()) if not unique_classified is None and not unique_classified.empty else 0,
        matched_closed=matched_closed,
        matched_open=int(unique_classified["link_status"].eq("OPEN_SIGNAL").sum()) if not unique_classified is None and not unique_classified.empty else 0,
        rejected_candidate=int(unique_classified["link_status"].eq("REJECTED_CANDIDATE").sum()) if not unique_classified is None and not unique_classified.empty else 0,
        unmatched=int(unique_classified["link_status"].eq("UNMATCHED").sum()) if not unique_classified is None and not unique_classified.empty else 0,
        ambiguous=ambiguous,
        duplicate_shadow_rows=duplicate_rows,
        coverage_pct_closed=round(matched_closed / closed_count * 100, 1) if closed_count else 0.0,
    )


def build_report(base: Path) -> dict[str, Any]:
    start = time.perf_counter()
    signals = load_signal_population(base)
    closed_count = int(signals["is_closed"].sum()) if not signals.empty else 0
    shadows = {
        "entry_timing": base / "logs" / "entry_timing_engine.csv",
        "sr_trade_weight": base / "logs" / "sr_trade_weight_shadow.csv",
        "market_exhaustion": base / "logs" / "market_exhaustion_shadow.csv",
    }
    coverage: list[dict[str, Any]] = []
    causes: dict[str, dict[str, int]] = {}
    for name, path in shadows.items():
        shadow = normalize_shadow(_read_csv(path), str(path.relative_to(base)), name)
        classified = classify_shadow_rows(shadow, signals)
        coverage.append(asdict(coverage_for(name, shadow, classified, closed_count)))
        if not classified.empty:
            unmatched = classified[classified["link_status"].eq("UNMATCHED")]
            reason_counts = unmatched["missing_reason"].replace("", "true_no_match").value_counts().to_dict()
            causes[name] = {str(key): int(value) for key, value in reason_counts.items()}
        else:
            causes[name] = {}
    elapsed = time.perf_counter() - start
    return {
        "base": str(base),
        "closed_signals": closed_count,
        "signal_rows": int(len(signals)),
        "coverage": coverage,
        "unmatched_causes": causes,
        "benchmark": {"seconds": round(elapsed, 4), "rows_per_second": round((len(signals) + sum(item["total_rows"] for item in coverage)) / elapsed, 1) if elapsed > 0 else None},
    }


def format_report(report: dict[str, Any]) -> str:
    lines = [
        "Shadow Linkage Coverage V1",
        "==========================",
        "",
        f"Base: {report['base']}",
        f"Signal rows indexed: {report['signal_rows']}",
        f"Closed sent signals: {report['closed_signals']}",
        f"Benchmark: {report['benchmark']['seconds']}s ({report['benchmark']['rows_per_second']} rows/s)",
        "",
        "Coverage",
        "--------",
    ]
    for item in report["coverage"]:
        lines.extend(
            [
                f"{item['shadow']}:",
                f"  rows={item['total_rows']} unique={item['unique_shadow_signals']}",
                f"  matched_sent={item['matched_sent']} matched_closed={item['matched_closed']} open={item['matched_open']}",
                f"  rejected_candidate={item['rejected_candidate']} unmatched={item['unmatched']} ambiguous={item['ambiguous']}",
                f"  closed_coverage={item['coverage_pct_closed']}%",
            ]
        )
    lines.append("")
    lines.append("Unmatched Causes")
    lines.append("----------------")
    for name, causes in report["unmatched_causes"].items():
        if not causes:
            lines.append(f"{name}: none")
            continue
        joined = ", ".join(f"{key}={value}" for key, value in sorted(causes.items()))
        lines.append(f"{name}: {joined}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only shadow linkage coverage report.")
    parser.add_argument("--base", type=Path, default=Path("."), help="Project root containing logs/.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text.")
    args = parser.parse_args()
    report = build_report(args.base.resolve())
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
