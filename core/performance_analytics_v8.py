# -*- coding: utf-8 -*-
"""Root Cause Analytics V8.

Report-only analytics for cross-factor performance. This module does not
modify scanner logic, routing, outcome review, Cornix, or position watcher
behavior.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from core.performance_analytics_v3 import _closed_trades, _hit_level, normalize_for_v3, score_range_label


PERFORMANCE_COLUMNS = [
    "Closed Trades",
    "Wins",
    "Losses",
    "Win Rate",
    "Net R",
    "TP1 Rate",
    "TP2 Rate",
]

CLUSTER_COLUMNS = [
    "Cluster Name",
    "Closed Trades",
    "Wins",
    "Losses",
    "Win Rate",
    "Net R",
    "Loss Count",
    "Loss Share %",
]

WIN_CLUSTER_COLUMNS = [
    "Cluster Name",
    "Closed Trades",
    "Wins",
    "Losses",
    "Win Rate",
    "Net R",
    "Win Count",
    "Win Share %",
]


def _closed_with_buckets(df: pd.DataFrame) -> pd.DataFrame:
    data = normalize_for_v3(df)
    closed = _closed_trades(data)
    if closed.empty:
        return closed
    closed = closed.copy()
    closed["score_bucket"] = closed["score"].map(score_range_label).fillna("Unbucketed")
    closed["tier"] = closed["tier"].fillna("-").replace("", "-").astype(str).str.upper()
    closed["session"] = closed["session"].fillna("OffHours").replace("", "OffHours").astype(str)
    closed["side"] = closed["side"].fillna("-").replace("", "-").astype(str).str.upper()
    closed["symbol"] = closed["symbol"].fillna("-").replace("", "-").astype(str).str.upper()
    return closed


def _performance_row(group: pd.DataFrame) -> dict[str, Any]:
    wins = int((group["result"] == "WIN").sum())
    losses = int((group["result"] == "LOSS").sum())
    trades = int(len(group))
    hit_levels = group["hit_target"].map(_hit_level) if "hit_target" in group else pd.Series(dtype=int)
    return {
        "Closed Trades": trades,
        "Wins": wins,
        "Losses": losses,
        "Win Rate": round(wins / trades * 100.0, 1) if trades else 0.0,
        "Net R": round(float(pd.to_numeric(group.get("estimated_r", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()), 2),
        "TP1 Rate": round(float(hit_levels.ge(1).mean() * 100.0), 1) if not hit_levels.empty else 0.0,
        "TP2 Rate": round(float(hit_levels.ge(2).mean() * 100.0), 1) if not hit_levels.empty else 0.0,
    }


def cross_factor_table(
    df: pd.DataFrame,
    factors: list[str],
    labels: list[str],
    min_symbol_trades: int = 0,
) -> pd.DataFrame:
    columns = labels + PERFORMANCE_COLUMNS
    closed = _closed_with_buckets(df)
    if closed.empty or any(factor not in closed.columns for factor in factors):
        return pd.DataFrame(columns=columns)

    rows: list[dict[str, Any]] = []
    grouped = closed.groupby([closed[factor].fillna("-").replace("", "-").astype(str) for factor in factors], dropna=False)
    for keys, group in grouped:
        if not isinstance(keys, tuple):
            keys = (keys,)
        if min_symbol_trades and len(group) < min_symbol_trades:
            continue
        row = {label: str(key) for label, key in zip(labels, keys)}
        row.update(_performance_row(group))
        rows.append(row)
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows, columns=columns).sort_values(["Net R", "Win Rate", "Closed Trades"], ascending=[False, False, False])


def _cluster_name(labels: list[str], keys: tuple[Any, ...]) -> str:
    parts = [f"{label}={key}" for label, key in zip(labels, keys)]
    return " | ".join(parts)


def cluster_table(
    df: pd.DataFrame,
    factors: list[str],
    labels: list[str],
    *,
    kind: str,
) -> pd.DataFrame:
    if kind not in {"loss", "win"}:
        raise ValueError("kind must be loss or win")
    columns = CLUSTER_COLUMNS if kind == "loss" else WIN_CLUSTER_COLUMNS
    closed = _closed_with_buckets(df)
    if closed.empty or any(factor not in closed.columns for factor in factors):
        return pd.DataFrame(columns=columns)

    total_losses = int((closed["result"] == "LOSS").sum())
    total_wins = int((closed["result"] == "WIN").sum())
    rows: list[dict[str, Any]] = []
    grouped = closed.groupby([closed[factor].fillna("-").replace("", "-").astype(str) for factor in factors], dropna=False)
    for keys, group in grouped:
        if not isinstance(keys, tuple):
            keys = (keys,)
        trades = int(len(group))
        wins = int((group["result"] == "WIN").sum())
        losses = int((group["result"] == "LOSS").sum())
        if trades < 5:
            continue
        if kind == "loss" and losses < 3:
            continue
        if kind == "win" and wins < 3:
            continue
        base = _performance_row(group)
        if kind == "loss":
            rows.append(
                {
                    "Cluster Name": _cluster_name(labels, keys),
                    **base,
                    "Loss Count": losses,
                    "Loss Share %": round(losses / total_losses * 100.0, 1) if total_losses else 0.0,
                }
            )
        else:
            rows.append(
                {
                    "Cluster Name": _cluster_name(labels, keys),
                    **base,
                    "Win Count": wins,
                    "Win Share %": round(wins / total_wins * 100.0, 1) if total_wins else 0.0,
                }
            )
    if not rows:
        return pd.DataFrame(columns=columns)
    table = pd.DataFrame(rows, columns=columns)
    if kind == "loss":
        return table.sort_values(["Net R", "Loss Count"], ascending=[True, False])
    return table.sort_values(["Net R", "Win Count"], ascending=[False, False])


def build_loss_clusters(df: pd.DataFrame) -> pd.DataFrame:
    specs = [
        (["session", "side"], ["Session", "Direction"]),
        (["session", "tier"], ["Session", "Tier"]),
        (["score_bucket", "session"], ["Score Bucket", "Session"]),
        (["score_bucket", "side"], ["Score Bucket", "Direction"]),
        (["symbol", "side"], ["Symbol", "Direction"]),
        (["symbol", "session"], ["Symbol", "Session"]),
        (["tier", "side"], ["Tier", "Direction"]),
        (["tier", "session", "side"], ["Tier", "Session", "Direction"]),
        (["score_bucket", "session", "side"], ["Score Bucket", "Session", "Direction"]),
    ]
    tables = [cluster_table(df, factors, labels, kind="loss") for factors, labels in specs]
    non_empty = [table for table in tables if not table.empty]
    combined = pd.concat(non_empty, ignore_index=True) if non_empty else pd.DataFrame()
    if combined.empty:
        return pd.DataFrame(columns=CLUSTER_COLUMNS)
    return combined.sort_values(["Net R", "Loss Count"], ascending=[True, False]).reset_index(drop=True)


def build_win_clusters(df: pd.DataFrame) -> pd.DataFrame:
    specs = [
        (["session", "side"], ["Session", "Direction"]),
        (["session", "tier"], ["Session", "Tier"]),
        (["score_bucket", "session"], ["Score Bucket", "Session"]),
        (["score_bucket", "side"], ["Score Bucket", "Direction"]),
        (["symbol", "side"], ["Symbol", "Direction"]),
        (["symbol", "session"], ["Symbol", "Session"]),
        (["tier", "side"], ["Tier", "Direction"]),
        (["tier", "session", "side"], ["Tier", "Session", "Direction"]),
        (["score_bucket", "session", "side"], ["Score Bucket", "Session", "Direction"]),
    ]
    tables = [cluster_table(df, factors, labels, kind="win") for factors, labels in specs]
    non_empty = [table for table in tables if not table.empty]
    combined = pd.concat(non_empty, ignore_index=True) if non_empty else pd.DataFrame()
    if combined.empty:
        return pd.DataFrame(columns=WIN_CLUSTER_COLUMNS)
    return combined.sort_values(["Net R", "Win Count"], ascending=[False, False]).reset_index(drop=True)


def root_cause_recommendations(loss_clusters: pd.DataFrame, win_clusters: pd.DataFrame) -> pd.DataFrame:
    columns = ["Recommendation", "Cluster Name", "Reason"]
    rows: list[dict[str, str]] = []
    if not loss_clusters.empty:
        for _, row in loss_clusters.head(5).iterrows():
            rows.append(
                {
                    "Recommendation": "REVIEW / REPORT-ONLY CANDIDATE",
                    "Cluster Name": str(row["Cluster Name"]),
                    "Reason": (
                        f"High-loss cluster: {int(row['Loss Count'])} losses, "
                        f"{float(row['Win Rate']):.1f}% WR, {float(row['Net R']):.2f}R."
                    ),
                }
            )
    if not win_clusters.empty:
        for _, row in win_clusters.head(5).iterrows():
            rows.append(
                {
                    "Recommendation": "KEEP / MONITOR PRIORITY",
                    "Cluster Name": str(row["Cluster Name"]),
                    "Reason": (
                        f"High-win cluster: {int(row['Win Count'])} wins, "
                        f"{float(row['Win Rate']):.1f}% WR, {float(row['Net R']):.2f}R."
                    ),
                }
            )
    if loss_clusters.empty and win_clusters.empty:
        rows.append(
            {
                "Recommendation": "COLLECT MORE DATA",
                "Cluster Name": "N/A",
                "Reason": "No root-cause clusters met minimum sample thresholds yet.",
            }
        )
    return pd.DataFrame(rows, columns=columns)


def build_root_cause_analytics(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    loss_clusters = build_loss_clusters(df)
    win_clusters = build_win_clusters(df)
    return {
        "root_score_session": cross_factor_table(df, ["score_bucket", "session"], ["Score Bucket", "Session"]),
        "root_score_direction": cross_factor_table(df, ["score_bucket", "side"], ["Score Bucket", "Direction"]),
        "root_tier_session": cross_factor_table(df, ["tier", "session"], ["Tier", "Session"]),
        "root_symbol_session": cross_factor_table(df, ["symbol", "session"], ["Symbol", "Session"], min_symbol_trades=5),
        "root_symbol_direction": cross_factor_table(df, ["symbol", "side"], ["Symbol", "Direction"], min_symbol_trades=5),
        "root_loss_clusters": loss_clusters,
        "root_win_clusters": win_clusters,
        "root_cause_recommendations": root_cause_recommendations(loss_clusters, win_clusters),
    }
