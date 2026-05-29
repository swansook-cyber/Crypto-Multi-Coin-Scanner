# -*- coding: utf-8 -*-
"""High-level analytics orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from .adaptive_filters import build_adaptive_state, write_adaptive_state
from .equity_tracker import sync_equity_curve
from .outcome_tracker import sync_history_files
from .performance_stats import performance_by, rejection_counts, summary


def update_validation_artifacts(
    journal: pd.DataFrame,
    logs_dir: Path,
    starting_balance: float = 1000.0,
    risk_per_r: float = 10.0,
) -> dict[str, Any]:
    logs_dir.mkdir(parents=True, exist_ok=True)
    history, rejected = sync_history_files(
        journal,
        logs_dir / "signals_history.csv",
        logs_dir / "rejected_signals.csv",
    )
    equity = sync_equity_curve(
        history,
        logs_dir / "equity_curve.csv",
        starting_balance=starting_balance,
        risk_per_r=risk_per_r,
    )
    adaptive_state = build_adaptive_state(history, rejected)
    write_adaptive_state(adaptive_state, logs_dir / "adaptive_filters.json")
    write_performance_report(history, rejected, equity, adaptive_state, logs_dir / "performance_report.txt")
    return {
        "history": history,
        "rejected": rejected,
        "equity": equity,
        "adaptive_state": adaptive_state,
    }


def write_performance_report(
    history: pd.DataFrame,
    rejected: pd.DataFrame,
    equity: pd.DataFrame,
    adaptive_state: dict[str, Any],
    path: Path,
) -> None:
    stats = summary(history)
    tables = {
        "Tier Performance": performance_by(history, "tier"),
        "Symbol Performance": performance_by(history, "symbol"),
        "Session Performance": performance_by(history, "session"),
        "Side Performance": performance_by(history, "side"),
        "HTF Alignment Performance": performance_by(history, "htf_alignment"),
        "Market Regime Performance": performance_by(history, "market_regime"),
        "AI Commentary Performance": performance_by(history, "ai_commentary_used"),
    }
    lines = [
        "Crypto Multi-Coin Scanner Quant Research Report",
        "===============================================",
        "",
        f"Total signals: {stats['total_signals']}",
        f"Closed trades: {stats['closed_trades']}",
        f"Win rate: {stats['win_rate']:.1f}%",
        f"Net RR: {stats['net_rr']:.2f}",
        f"Avg RR: {stats['avg_rr']:.2f}",
        f"Avg holding minutes: {stats['avg_holding_minutes']:.1f}",
        f"Max drawdown: {stats['max_drawdown']:.2f}R",
        f"Best symbol: {stats['best_symbol']}",
        f"Worst symbol: {stats['worst_symbol']}",
        f"Best session: {stats['best_session']}",
        f"Top tier: {stats['top_tier']}",
        f"Current streak: {stats['current_streak']}",
        f"Equity status: {stats['equity_status']}",
    ]
    if not equity.empty:
        latest = equity.iloc[-1]
        lines.extend([
            "",
            "Equity Curve",
            "------------",
            f"Balance: {float(latest['balance']):.2f}",
            f"Cumulative RR: {float(latest['cumulative_rr']):.2f}",
            f"Drawdown: {float(latest['drawdown']):.2f}",
        ])
    for name, table in tables.items():
        lines.extend(["", name, "-" * len(name)])
        lines.append("No data" if table.empty else table.head(20).to_string(index=False))
    lines.extend(["", "Top Rejection Reasons", "---------------------"])
    rejection_table = rejection_counts(rejected).head(7)
    lines.append("No data" if rejection_table.empty else rejection_table.to_string(index=False))
    lines.extend(["", "Adaptive Filtering", "------------------"])
    for item in adaptive_state.get("recommendations", []):
        lines.append(f"- {item}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
