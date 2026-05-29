# -*- coding: utf-8 -*-
"""Adaptive analytics recommendations.

This module writes research recommendations and temporary filter state. It does
not execute trades and does not force scanner behavior unless a caller chooses
to consume the generated state.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .performance_stats import performance_by, rejection_counts


def build_adaptive_state(history: pd.DataFrame, rejected: pd.DataFrame | None = None) -> dict[str, Any]:
    state: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "temporary_blacklist": {},
        "score_adjustments": {},
        "threshold_adjustments": {},
        "risk_guards": {
            "max_consecutive_daily_losses": 3,
            "pause_after_losses_hours": 6,
            "max_daily_drawdown_percent": 3.0,
            "volatility_protection": "skip_extreme_atr_spikes",
        },
        "recommendations": [],
        "top_rejection_reasons": [],
    }
    symbol_perf = performance_by(history, "symbol")
    until = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    if not symbol_perf.empty:
        weak = symbol_perf[(symbol_perf["trades"] >= 10) & (symbol_perf["win_rate"] < 35)]
        for _, row in weak.iterrows():
            symbol = str(row["symbol"]).upper()
            state["temporary_blacklist"][symbol] = {
                "until": until,
                "reason": f"winrate {row['win_rate']:.1f}% over {int(row['trades'])} trades",
            }
            state["recommendations"].append(f"Blacklist {symbol} for 7 days: {state['temporary_blacklist'][symbol]['reason']}.")

    htf_perf = performance_by(history, "htf_alignment")
    if not htf_perf.empty and "htf_alignment" in htf_perf.columns:
        aligned = htf_perf[htf_perf["htf_alignment"].astype(str).str.upper().isin(["YES", "ALIGNED"])]
        not_aligned = htf_perf[~htf_perf["htf_alignment"].astype(str).str.upper().isin(["YES", "ALIGNED", "-", ""])]
        aligned_rate = float(aligned["win_rate"].mean()) if not aligned.empty else 0.0
        no_rate = float(not_aligned["win_rate"].mean()) if not not_aligned.empty else 0.0
        if not not_aligned.empty and no_rate < aligned_rate:
            state["score_adjustments"]["htf_alignment_no"] = -5
            state["recommendations"].append(
                f"Reduce score by 5 for HTF alignment NO/Conflict: {no_rate:.1f}% vs aligned {aligned_rate:.1f}%."
            )

    session_perf = performance_by(history, "session")
    if not session_perf.empty:
        weak_sessions = session_perf[(session_perf["trades"] >= 30) & (session_perf["win_rate"] < 40)]
        for _, row in weak_sessions.iterrows():
            session = str(row["session"])
            state["threshold_adjustments"][session] = {
                "min_score_delta": 5,
                "reason": f"session winrate {row['win_rate']:.1f}% over {int(row['trades'])} trades",
            }
            state["recommendations"].append(f"Increase quality threshold for {session} by 5 points.")

    if rejected is not None and not rejected.empty:
        counts = rejection_counts(rejected).head(7)
        state["top_rejection_reasons"] = counts.to_dict("records")

    if not state["recommendations"]:
        state["recommendations"].append("No adaptive filter changes suggested yet; collect more closed outcomes.")
    return state


def write_adaptive_state(state: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
