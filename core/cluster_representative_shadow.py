# -*- coding: utf-8 -*-
"""Cluster representative selection shadow analytics.

This module is report-only. It reads rejected candidate outcomes and production
sent signals, compares deterministic representative-selection strategies, and
never changes scanner logic, routing, scoring, TP/SL/RR, or Cornix behavior.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from core.signal_identity import canonical_signal_key, normalize_float, normalize_side, normalize_symbol, normalize_timestamp


FIELDNAMES = [
    "cluster_key",
    "cluster_hour_utc",
    "side",
    "cluster_size",
    "candidate_symbols",
    "strategy",
    "representative_canonical_key",
    "representative_symbol",
    "representative_score",
    "representative_confidence",
    "representative_tier",
    "hypothetical_outcome",
    "hypothetical_r",
    "production_sent_count",
    "production_sent_present",
    "production_sent_symbols",
    "production_comparison_symbol",
    "production_comparison_score",
    "production_comparison_confidence",
    "production_comparison_tier",
    "production_comparison_outcome",
    "production_comparison_r",
    "shadow_vs_production_r_delta",
    "source_population",
    "generated_at_utc",
]

STRATEGIES = [
    "BEST_SCORE",
    "BEST_CONFIDENCE",
    "SCORE_PLUS_CONFIDENCE",
    "TIER_SCORE_CONF",
    "QUALITY_COMPOSITE_V1",
]


@dataclass(frozen=True)
class ClusterSummary:
    clusters_evaluated: int
    mean_cluster_size: float
    max_cluster_size: int
    written: int
    dry_run: bool


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not pd.notna(number) or number in {float("inf"), float("-inf")}:
        return default
    return number


def utc_hour(value: Any) -> str:
    normalized = normalize_timestamp(value, precision="second")
    if not normalized:
        return ""
    ts = pd.to_datetime(normalized, utc=True, errors="coerce")
    if pd.isna(ts):
        return ""
    return ts.floor("h").strftime("%Y-%m-%dT%H:00:00Z")


def tier_priority(value: Any) -> int:
    return {"A": 3, "B": 2, "C": 1}.get(str(value or "").strip().upper(), 0)


def tier_quality_points(value: Any) -> float:
    return {"A": 100.0, "B": 70.0, "C": 40.0}.get(str(value or "").strip().upper(), 50.0)


def score_bucket(value: Any) -> float:
    return max(min(safe_float(value), 100.0), 0.0)


def quality_composite_v1(row: pd.Series) -> float:
    """Transparent analytics-only ranking score.

    Formula:
    45% stored score + 35% stored confidence/setup strength + 20% tier quality.
    No outcome or future market data is used.
    """
    return (
        0.45 * score_bucket(row.get("score"))
        + 0.35 * score_bucket(row.get("confidence"))
        + 0.20 * tier_quality_points(row.get("tier"))
    )


def cluster_key(cluster_hour_utc: str, side: str) -> str:
    raw = f"cluster:v1:{cluster_hour_utc}|{normalize_side(side)}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{raw}|{digest}"


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text and text.lower() not in {"nan", "none", "null", "nat"}:
            return text
    return ""


def hit_level(value: Any) -> int:
    text = str(value or "").strip().upper()
    if text.startswith("TP"):
        text = text.replace("TP", "", 1)
    try:
        return int(float(text))
    except ValueError:
        return 0


def production_r(row: pd.Series) -> float:
    explicit = safe_float(row.get("net_r_estimate"), default=float("nan"))
    if pd.notna(explicit):
        return explicit
    result = str(row.get("result", "")).strip().upper()
    if result == "LOSS":
        return -1.0
    if result == "WIN":
        if hit_level(row.get("hit_target")) >= 2:
            rr = safe_float(_first_text(row.get("risk_reward"), row.get("rr")), 2.0)
            return rr if rr > 0 else 2.0
        return 1.0
    return 0.0


def production_outcome(row: pd.Series) -> str:
    result = str(row.get("result", "")).strip().upper()
    hit = str(row.get("hit_target", "")).strip().upper()
    if result == "WIN" and hit:
        return f"WIN_{hit}"
    if result:
        return result
    return "UNKNOWN"


def read_csv_safe(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(path)
    except (FileNotFoundError, pd.errors.EmptyDataError):
        return pd.DataFrame()


def column(data: pd.DataFrame, name: str, default: Any = "") -> pd.Series:
    if name in data.columns:
        return data[name]
    return pd.Series([default] * len(data), index=data.index)


def normalize_rejected_outcomes(path: Path) -> pd.DataFrame:
    df = read_csv_safe(path)
    if df.empty:
        return pd.DataFrame()
    data = df.copy()
    for field in {"score", "confidence", "tier", "side", "symbol", "timestamp_utc", "entry", "canonical_signal_key"}:
        if field not in data.columns:
            data[field] = ""
    data["rejection_reason"] = column(data, "rejection_reason").fillna("").astype(str).str.upper()
    data = data[data["rejection_reason"].eq("CORRELATION")].copy()
    if data.empty:
        return data
    data["side"] = data.get("side", "").map(normalize_side)
    data["symbol"] = data.get("symbol", "").map(normalize_symbol)
    data["cluster_hour_utc"] = column(data, "timestamp_utc", "").where(column(data, "timestamp_utc", "").astype(str).str.strip().ne(""), column(data, "timestamp", "")).map(utc_hour)
    data["canonical_signal_key"] = data.apply(
        lambda row: _first_text(
            row.get("canonical_signal_key"),
            canonical_signal_key(
                symbol=row.get("symbol"),
                side=row.get("side"),
                timestamp=row.get("timestamp_utc"),
                entry=row.get("entry"),
            ),
        ),
        axis=1,
    )
    data["score_num"] = column(data, "score").map(safe_float)
    data["confidence_num"] = column(data, "confidence").map(safe_float)
    data["tier_priority"] = column(data, "tier").map(tier_priority)
    data["quality_composite_v1"] = data.apply(quality_composite_v1, axis=1)
    data["tie_key"] = data.apply(
        lambda row: f"{row.get('symbol','')}|{normalize_float(row.get('entry'))}|{row.get('canonical_signal_key','')}",
        axis=1,
    )
    data = data[
        data["cluster_hour_utc"].astype(str).ne("")
        & data["side"].isin(["LONG", "SHORT"])
        & data["canonical_signal_key"].astype(str).ne("")
    ].copy()
    return data


def normalize_production_sent(path: Path) -> pd.DataFrame:
    df = read_csv_safe(path)
    if df.empty:
        return pd.DataFrame()
    data = df.copy()
    status = column(data, "signal_status", "sent").fillna("sent").replace("", "sent").astype(str).str.lower()
    data = data[status.eq("sent")].copy()
    if data.empty:
        return data
    data["side"] = column(data, "side", "").where(column(data, "side", "").astype(str).str.strip().ne(""), column(data, "direction", "")).map(normalize_side)
    data["symbol"] = column(data, "symbol", "").map(normalize_symbol)
    data["cluster_hour_utc"] = column(data, "timestamp", "").where(column(data, "timestamp", "").astype(str).str.strip().ne(""), column(data, "timestamp_utc", "")).map(utc_hour)
    data["canonical_signal_key"] = data.apply(
        lambda row: canonical_signal_key(
            symbol=row.get("symbol"),
            side=row.get("side"),
            timestamp=_first_text(row.get("timestamp"), row.get("timestamp_utc")),
            entry=row.get("entry"),
        ),
        axis=1,
    )
    data["score_num"] = column(data, "score", "").where(column(data, "score", "").astype(str).str.strip().ne(""), column(data, "raw_score", "")).map(safe_float)
    data["confidence_num"] = column(data, "confidence", "").where(column(data, "confidence", "").astype(str).str.strip().ne(""), column(data, "setup_strength", "")).map(safe_float)
    data["tier"] = column(data, "watchlist_tier", "").where(column(data, "watchlist_tier", "").astype(str).str.strip().ne(""), column(data, "tier", ""))
    data["tier_priority"] = column(data, "tier", "").map(tier_priority)
    data["production_r"] = data.apply(production_r, axis=1)
    data["production_outcome"] = data.apply(production_outcome, axis=1)
    data["tie_key"] = data.apply(
        lambda row: f"{row.get('symbol','')}|{normalize_float(row.get('entry'))}|{row.get('canonical_signal_key','')}",
        axis=1,
    )
    data = data[data["cluster_hour_utc"].astype(str).ne("") & data["side"].isin(["LONG", "SHORT"])].copy()
    return data


def select_representative(cluster: pd.DataFrame, strategy: str) -> pd.Series:
    data = cluster.copy()
    if strategy == "BEST_SCORE":
        sort_cols = ["score_num", "confidence_num", "tier_priority", "tie_key"]
        ascending = [False, False, False, True]
    elif strategy == "BEST_CONFIDENCE":
        sort_cols = ["confidence_num", "score_num", "tier_priority", "tie_key"]
        ascending = [False, False, False, True]
    elif strategy == "SCORE_PLUS_CONFIDENCE":
        data["score_plus_confidence"] = data["score_num"] + data["confidence_num"]
        sort_cols = ["score_plus_confidence", "score_num", "confidence_num", "tier_priority", "tie_key"]
        ascending = [False, False, False, False, True]
    elif strategy == "TIER_SCORE_CONF":
        sort_cols = ["tier_priority", "score_num", "confidence_num", "tie_key"]
        ascending = [False, False, False, True]
    elif strategy == "QUALITY_COMPOSITE_V1":
        sort_cols = ["quality_composite_v1", "score_num", "confidence_num", "tier_priority", "tie_key"]
        ascending = [False, False, False, False, True]
    else:
        raise ValueError(f"Unknown strategy: {strategy}")
    return data.sort_values(sort_cols, ascending=ascending, kind="mergesort").iloc[0]


def select_production_comparison(production_group: pd.DataFrame) -> pd.Series | None:
    if production_group.empty:
        return None
    data = production_group.copy()
    return data.sort_values(["score_num", "confidence_num", "tier_priority", "tie_key"], ascending=[False, False, False, True], kind="mergesort").iloc[0]


def build_shadow_rows(rejected: pd.DataFrame, production: pd.DataFrame | None = None) -> pd.DataFrame:
    if rejected.empty:
        return pd.DataFrame(columns=FIELDNAMES)
    production = production if production is not None else pd.DataFrame()
    rows: list[dict[str, Any]] = []
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    grouped = rejected.groupby(["cluster_hour_utc", "side"], sort=True)
    for (hour, side), cluster in grouped:
        cluster = cluster.sort_values("tie_key", kind="mergesort").copy()
        key = cluster_key(str(hour), str(side))
        prod_group = pd.DataFrame()
        if not production.empty:
            prod_group = production[(production["cluster_hour_utc"].eq(hour)) & (production["side"].eq(side))].copy()
        prod_symbols = ",".join(sorted(prod_group["symbol"].dropna().astype(str).unique())) if not prod_group.empty else ""
        prod_rep = select_production_comparison(prod_group)
        prod_count = int(len(prod_group))
        for strategy in STRATEGIES:
            rep = select_representative(cluster, strategy)
            shadow_r = safe_float(rep.get("hypothetical_r"))
            prod_r = production_r(prod_rep) if prod_rep is not None else 0.0
            rows.append(
                {
                    "cluster_key": key,
                    "cluster_hour_utc": hour,
                    "side": side,
                    "cluster_size": int(len(cluster)),
                    "candidate_symbols": ",".join(sorted(cluster["symbol"].dropna().astype(str).unique())),
                    "strategy": strategy,
                    "representative_canonical_key": rep.get("canonical_signal_key", ""),
                    "representative_symbol": rep.get("symbol", ""),
                    "representative_score": rep.get("score", ""),
                    "representative_confidence": rep.get("confidence", ""),
                    "representative_tier": rep.get("tier", ""),
                    "hypothetical_outcome": rep.get("hypothetical_outcome", ""),
                    "hypothetical_r": f"{shadow_r:.4f}",
                    "production_sent_count": prod_count,
                    "production_sent_present": "YES" if prod_count > 0 else "NO",
                    "production_sent_symbols": prod_symbols,
                    "production_comparison_symbol": "" if prod_rep is None else prod_rep.get("symbol", ""),
                    "production_comparison_score": "" if prod_rep is None else prod_rep.get("score", prod_rep.get("raw_score", "")),
                    "production_comparison_confidence": "" if prod_rep is None else prod_rep.get("confidence", prod_rep.get("setup_strength", "")),
                    "production_comparison_tier": "" if prod_rep is None else prod_rep.get("tier", ""),
                    "production_comparison_outcome": "" if prod_rep is None else prod_rep.get("production_outcome", ""),
                    "production_comparison_r": "" if prod_rep is None else f"{prod_r:.4f}",
                    "shadow_vs_production_r_delta": "" if prod_rep is None else f"{(shadow_r - prod_r):.4f}",
                    "source_population": "correlation_rejected",
                    "generated_at_utc": generated_at,
                }
            )
    return pd.DataFrame(rows, columns=FIELDNAMES)


class ClusterRepresentativeShadowLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            with self.path.open("w", newline="", encoding="utf-8") as handle:
                csv.DictWriter(handle, fieldnames=FIELDNAMES).writeheader()
            return
        try:
            existing = pd.read_csv(self.path)
        except pd.errors.EmptyDataError:
            existing = pd.DataFrame(columns=FIELDNAMES)
        changed = False
        for column in FIELDNAMES:
            if column not in existing.columns:
                existing[column] = ""
                changed = True
        if changed or list(existing.columns) != FIELDNAMES:
            existing[FIELDNAMES].to_csv(self.path, index=False)

    def existing_keys(self) -> set[str]:
        try:
            df = pd.read_csv(self.path, usecols=["cluster_key", "strategy"])
        except (FileNotFoundError, pd.errors.EmptyDataError, ValueError):
            return set()
        return {
            f"{row.get('cluster_key')}|{row.get('strategy')}"
            for _, row in df.iterrows()
            if str(row.get("cluster_key", "")).strip() and str(row.get("strategy", "")).strip()
        }

    def append(self, rows: pd.DataFrame) -> int:
        if rows.empty:
            return 0
        existing = self.existing_keys()
        keep_records = []
        for _, row in rows.iterrows():
            key = f"{row.get('cluster_key')}|{row.get('strategy')}"
            if key in existing:
                continue
            existing.add(key)
            keep_records.append(row.to_dict())
        if not keep_records:
            return 0
        with self.path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore")
            for record in keep_records:
                writer.writerow(record)
        return len(keep_records)


def strategy_summary(rows: pd.DataFrame) -> pd.DataFrame:
    columns = ["Strategy", "N", "Resolved", "WR", "Hypothetical NetR"]
    if rows.empty:
        return pd.DataFrame(columns=columns)
    output = []
    for strategy, group in rows.groupby("strategy", sort=True):
        outcomes = group["hypothetical_outcome"].fillna("").astype(str).str.upper()
        resolved = group[outcomes.isin(["WIN_TP1", "WIN_TP2", "LOSS"])]
        wins = int(resolved["hypothetical_outcome"].astype(str).str.upper().str.startswith("WIN").sum()) if not resolved.empty else 0
        wr = wins / len(resolved) * 100 if len(resolved) else 0.0
        net_r = pd.to_numeric(resolved.get("hypothetical_r", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()
        output.append({"Strategy": strategy, "N": len(group), "Resolved": len(resolved), "WR": round(wr, 2), "Hypothetical NetR": round(float(net_r), 4)})
    return pd.DataFrame(output, columns=columns)


def monthly_summary(rows: pd.DataFrame) -> pd.DataFrame:
    columns = ["Month", "Strategy", "Resolved", "WR", "Hypothetical NetR"]
    if rows.empty:
        return pd.DataFrame(columns=columns)
    data = rows.copy()
    data["month"] = pd.to_datetime(data["cluster_hour_utc"], utc=True, errors="coerce").dt.strftime("%Y-%m")
    output = []
    for (month, strategy), group in data.dropna(subset=["month"]).groupby(["month", "strategy"], sort=True):
        outcomes = group["hypothetical_outcome"].fillna("").astype(str).str.upper()
        resolved = group[outcomes.isin(["WIN_TP1", "WIN_TP2", "LOSS"])]
        wins = int(resolved["hypothetical_outcome"].astype(str).str.upper().str.startswith("WIN").sum()) if not resolved.empty else 0
        wr = wins / len(resolved) * 100 if len(resolved) else 0.0
        net_r = pd.to_numeric(resolved.get("hypothetical_r", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()
        output.append({"Month": month, "Strategy": strategy, "Resolved": len(resolved), "WR": round(wr, 2), "Hypothetical NetR": round(float(net_r), 4)})
    return pd.DataFrame(output, columns=columns)


def production_linkage_summary(rows: pd.DataFrame) -> dict[str, Any]:
    if rows.empty:
        return {"clusters": 0, "with_sent": 0, "comparable": 0, "delta": 0.0, "multi_sent": 0}
    one_per_cluster = rows.drop_duplicates("cluster_key")
    with_sent = one_per_cluster[pd.to_numeric(one_per_cluster["production_sent_count"], errors="coerce").fillna(0).gt(0)]
    comparable = rows[rows["shadow_vs_production_r_delta"].astype(str).str.strip().ne("")]
    delta = pd.to_numeric(comparable["shadow_vs_production_r_delta"], errors="coerce").fillna(0).sum()
    multi = int(pd.to_numeric(one_per_cluster["production_sent_count"], errors="coerce").fillna(0).gt(1).sum())
    return {
        "clusters": int(len(one_per_cluster)),
        "with_sent": int(len(with_sent)),
        "comparable": int(len(comparable)),
        "delta": round(float(delta), 4),
        "multi_sent": multi,
    }


def format_summary(rows: pd.DataFrame, summary: ClusterSummary) -> str:
    strategy = strategy_summary(rows)
    monthly = monthly_summary(rows)
    linkage = production_linkage_summary(rows)
    lines = [
        "Cluster Representative Selection Shadow V1",
        f"Clusters evaluated: {summary.clusters_evaluated}",
        f"Mean cluster size: {summary.mean_cluster_size:.2f}",
        f"Max cluster size: {summary.max_cluster_size}",
        f"Rows written: {summary.written}",
        f"Dry run: {summary.dry_run}",
        "",
        "Result by strategy:",
        strategy.to_string(index=False) if not strategy.empty else "N/A",
        "",
        "Production sent linkage:",
        f"Clusters with sent candidate: {linkage['with_sent']} / {linkage['clusters']}",
        f"Comparable strategy rows: {linkage['comparable']}",
        f"Multiple sent clusters: {linkage['multi_sent']}",
        f"Aggregate shadow-production R delta: {linkage['delta']:.4f}",
        "",
        "Monthly stability:",
        monthly.to_string(index=False) if not monthly.empty else "N/A",
    ]
    return "\n".join(lines)


def run_shadow(
    *,
    rejected_outcome_path: Path,
    signals_path: Path,
    output_path: Path,
    limit: int | None = None,
    dry_run: bool = False,
) -> tuple[pd.DataFrame, ClusterSummary]:
    rejected = normalize_rejected_outcomes(rejected_outcome_path)
    if limit is not None and not rejected.empty:
        rejected = rejected.sort_values(["cluster_hour_utc", "side", "tie_key"], kind="mergesort").head(max(limit, 0)).copy()
    production = normalize_production_sent(signals_path)
    rows = build_shadow_rows(rejected, production)
    cluster_sizes = rejected.groupby(["cluster_hour_utc", "side"]).size() if not rejected.empty else pd.Series(dtype=int)
    written = 0
    if not dry_run:
        written = ClusterRepresentativeShadowLogger(output_path).append(rows)
    summary = ClusterSummary(
        clusters_evaluated=int(len(cluster_sizes)),
        mean_cluster_size=float(cluster_sizes.mean()) if len(cluster_sizes) else 0.0,
        max_cluster_size=int(cluster_sizes.max()) if len(cluster_sizes) else 0,
        written=written,
        dry_run=dry_run,
    )
    return rows, summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze correlation-rejected candidate cluster representative strategies.")
    parser.add_argument("--base-dir", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--rejected-outcomes", default="")
    parser.add_argument("--signals", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if env_bool("CLUSTER_REPRESENTATIVE_LIVE_ENABLED", False):
        print("CLUSTER_REPRESENTATIVE_LIVE_ENABLED is ignored in V1; analytics-only mode remains active.")

    base_dir = Path(args.base_dir)
    rejected_outcomes = Path(args.rejected_outcomes) if args.rejected_outcomes else base_dir / "logs" / "rejected_outcome_shadow.csv"
    signals = Path(args.signals) if args.signals else base_dir / "logs" / "signals.csv"
    output = Path(args.output) if args.output else base_dir / "logs" / "cluster_representative_shadow.csv"
    rows, summary = run_shadow(
        rejected_outcome_path=rejected_outcomes,
        signals_path=signals,
        output_path=output,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    print(format_summary(rows, summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
