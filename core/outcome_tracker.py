# -*- coding: utf-8 -*-
"""Outcome/history database helpers for the validation layer."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pandas as pd


HISTORY_COLUMNS = [
    "timestamp",
    "symbol",
    "side",
    "tier",
    "session",
    "entry",
    "sl",
    "tp1",
    "tp2",
    "rr",
    "real_rr",
    "setup_strength",
    "score",
    "market_regime",
    "htf_alignment",
    "volume_spike",
    "mfi",
    "atr",
    "body_percent",
    "atr_expansion",
    "btc_regime",
    "result",
    "pnl_percent",
    "holding_minutes",
    "outcome",
    "ai_commentary_used",
]

REJECTED_COLUMNS = [
    "timestamp",
    "symbol",
    "side",
    "tier",
    "session",
    "score",
    "setup_strength",
    "market_regime",
    "htf_alignment",
    "reason",
    "signal_status",
]


def safe_float(value: Any, default: float = 0.0) -> float:
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric):
        return default
    return float(numeric)


def clean_symbol(symbol: Any) -> str:
    return str(symbol).strip().upper().replace("BINANCE:", "").replace(".P", "")


def normalize_bool(value: Any) -> str:
    text = str(value).strip().upper()
    if text in {"1", "TRUE", "YES", "Y"}:
        return "YES"
    if text in {"0", "FALSE", "NO", "N"}:
        return "NO"
    return text or "NO"


def history_key(df: pd.DataFrame) -> pd.Series:
    parts = []
    for column in ["timestamp", "symbol", "side", "entry"]:
        if column not in df.columns:
            parts.append(pd.Series([""] * len(df), index=df.index))
        else:
            parts.append(df[column].fillna("").astype(str).str.strip())
    return parts[0] + "|" + parts[1].str.upper() + "|" + parts[2].str.upper() + "|" + parts[3]


def rejection_key(df: pd.DataFrame) -> pd.Series:
    parts = []
    for column in ["timestamp", "symbol", "side", "reason", "signal_status"]:
        if column not in df.columns:
            parts.append(pd.Series([""] * len(df), index=df.index))
        else:
            parts.append(df[column].fillna("").astype(str).str.strip())
    return parts[0] + "|" + parts[1].str.upper() + "|" + parts[2].str.upper() + "|" + parts[3] + "|" + parts[4]


def outcome_classification(row: pd.Series) -> str:
    result = str(row.get("result", "OPEN")).strip().upper()
    hit_target = str(row.get("hit_target", "")).strip().upper()
    if result == "WIN" and hit_target == "TP2":
        return "WIN_TP2"
    if result == "WIN":
        return "WIN_TP1"
    if result == "LOSS":
        return "LOSS"
    if result == "BREAKEVEN":
        return "BREAKEVEN"
    if result == "EXPIRED":
        return "EXPIRED"
    return "OPEN"


def pnl_percent(row: pd.Series) -> float:
    result = str(row.get("result", "OPEN")).strip().upper()
    hit_target = str(row.get("hit_target", "")).strip().upper()
    side = str(row.get("side", "")).strip().upper()
    entry = safe_float(row.get("entry"))
    if entry <= 0:
        return 0.0
    if result == "LOSS":
        stop_loss = safe_float(row.get("stop_loss", row.get("sl")))
        value = (stop_loss - entry) / entry * 100 if side == "LONG" else (entry - stop_loss) / entry * 100
        return -abs(value)
    if result == "WIN":
        target = safe_float(row.get("tp2" if hit_target == "TP2" else "tp1"))
        value = (target - entry) / entry * 100 if side == "LONG" else (entry - target) / entry * 100
        return abs(value)
    return 0.0


def risk_percent(row: pd.Series) -> float:
    entry = safe_float(row.get("entry"))
    stop_loss = safe_float(row.get("stop_loss", row.get("sl")))
    if entry <= 0 or stop_loss <= 0:
        return 0.0
    return abs(entry - stop_loss) / entry * 100


def real_rr(row: pd.Series) -> float:
    risk = risk_percent(row)
    if risk <= 0:
        return 0.0
    return pnl_percent(row) / risk


def holding_minutes(row: pd.Series) -> float:
    start = pd.to_datetime(row.get("timestamp"), utc=True, errors="coerce")
    end = pd.to_datetime(row.get("closed_at"), utc=True, errors="coerce")
    if pd.isna(start) or pd.isna(end):
        return 0.0
    return max(0.0, float((end - start).total_seconds() / 60))


def ai_commentary_used(row: pd.Series) -> str:
    return "YES" if str(row.get("ai_summary", "")).strip() else "NO"


def journal_to_history(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if df.empty:
        return pd.DataFrame(columns=HISTORY_COLUMNS)
    if "signal_status" in df.columns:
        status = df["signal_status"].fillna("sent").astype(str).str.lower()
    else:
        status = pd.Series(["sent"] * len(df), index=df.index)
    sent = df[status == "sent"].copy()
    for _, row in sent.iterrows():
        rows.append({
            "timestamp": row.get("timestamp", ""),
            "symbol": clean_symbol(row.get("symbol", "")),
            "side": str(row.get("side", "")).upper(),
            "tier": str(row.get("watchlist_tier", row.get("tier", "B")) or "B").upper(),
            "session": row.get("market_session", row.get("session", "")),
            "entry": row.get("entry", ""),
            "sl": row.get("stop_loss", row.get("sl", "")),
            "tp1": row.get("tp1", ""),
            "tp2": row.get("tp2", ""),
            "rr": row.get("risk_reward", row.get("rr", "")),
            "real_rr": f"{real_rr(row):.4f}",
            "setup_strength": row.get("setup_strength", row.get("confidence", "")),
            "score": row.get("raw_score", row.get("score", "")),
            "market_regime": row.get("market_regime", ""),
            "htf_alignment": row.get("htf_alignment", ""),
            "volume_spike": normalize_bool(row.get("volume_spike", "")),
            "mfi": row.get("mfi", ""),
            "atr": row.get("atr", row.get("atr_pct", "")),
            "body_percent": safe_float(row.get("body_ratio", 0.0)) * 100,
            "atr_expansion": row.get("atr_expansion_ratio", ""),
            "btc_regime": row.get("btc_regime", ""),
            "result": str(row.get("result", "OPEN") or "OPEN").upper(),
            "pnl_percent": f"{pnl_percent(row):.4f}",
            "holding_minutes": f"{holding_minutes(row):.1f}",
            "outcome": outcome_classification(row),
            "ai_commentary_used": ai_commentary_used(row),
        })
    return pd.DataFrame(rows, columns=HISTORY_COLUMNS)


def journal_to_rejections(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "signal_status" not in df.columns:
        return pd.DataFrame(columns=REJECTED_COLUMNS)
    rejected = df[df["signal_status"].fillna("").astype(str).str.lower() != "sent"].copy()
    rows = []
    for _, row in rejected.iterrows():
        rows.append({
            "timestamp": row.get("timestamp", ""),
            "symbol": clean_symbol(row.get("symbol", "")),
            "side": str(row.get("side", "")).upper(),
            "tier": str(row.get("watchlist_tier", row.get("tier", "B")) or "B").upper(),
            "session": row.get("market_session", row.get("session", "")),
            "score": row.get("raw_score", row.get("score", "")),
            "setup_strength": row.get("setup_strength", row.get("confidence", "")),
            "market_regime": row.get("market_regime", ""),
            "htf_alignment": row.get("htf_alignment", ""),
            "reason": row.get("skip_reason", row.get("quality_flags", "")),
            "signal_status": row.get("signal_status", ""),
        })
    return pd.DataFrame(rows, columns=REJECTED_COLUMNS)


def upsert_csv(path: Path, current: pd.DataFrame, columns: list[str], key_func) -> pd.DataFrame:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            existing = pd.read_csv(path)
        except pd.errors.EmptyDataError:
            existing = pd.DataFrame(columns=columns)
    else:
        existing = pd.DataFrame(columns=columns)
    for column in columns:
        if column not in existing.columns:
            existing[column] = ""
        if column not in current.columns:
            current[column] = ""
    combined = pd.concat([existing[columns], current[columns]], ignore_index=True)
    if not combined.empty:
        combined["_key"] = key_func(combined)
        combined = combined.drop_duplicates("_key", keep="last").drop(columns=["_key"])
    with path.open("w", newline="", encoding="utf-8") as handle:
        combined.to_csv(handle, index=False)
        handle.flush()
        os.fsync(handle.fileno())
    return combined


def sync_history_files(journal: pd.DataFrame, history_path: Path, rejected_path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    history = upsert_csv(history_path, journal_to_history(journal), HISTORY_COLUMNS, history_key)
    rejected = upsert_csv(rejected_path, journal_to_rejections(journal), REJECTED_COLUMNS, rejection_key)
    return history, rejected
