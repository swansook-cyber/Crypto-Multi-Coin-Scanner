# -*- coding: utf-8 -*-
"""Complete Performance Analytics V1 helpers.

This module is intentionally read-only against scanner decisions. It consumes
journals/history/external logs and emits report-ready summaries for monitoring.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from core.performance_analytics_v2 import build_performance_v2
from core.performance_analytics_v3 import build_performance_v3, format_table as format_v3_table


NA = "N/A"
SMALL_SAMPLE_CLOSED_TRADES = 30


DAILY_PERFORMANCE_COLUMNS = [
    "date",
    "total_sent_signals",
    "closed_signals",
    "open_signals",
    "wins",
    "losses",
    "win_rate",
    "tp1_hits",
    "tp2_hits",
    "tp3_hits",
    "sl_hits",
    "avg_profit_pct",
    "avg_drawdown_pct",
    "avg_max_profit_pct",
    "avg_time_to_tp",
    "avg_time_to_sl",
    "best_symbol",
    "worst_symbol",
    "long_win_rate",
    "short_win_rate",
    "scanner_win_rate",
    "external_win_rate",
    "hold_count",
    "opposite_signal_count",
    "exit_recommendation_count",
    "stale_position_count",
    "tier_c_report_count",
    "tier_c_report_wins",
    "tier_c_report_losses",
    "tier_c_report_win_rate",
    "weak_symbol_report_count",
    "weak_symbol_report_wins",
    "weak_symbol_report_losses",
    "weak_symbol_report_win_rate",
    "session_risk_report_count",
    "session_risk_report_wins",
    "session_risk_report_losses",
    "session_risk_report_win_rate",
    "tp1_alerts_watcher",
    "tp1_alerts_outcome_review",
    "breakeven_recommendations",
    "open_tp1_be_recommended",
]


def load_csv_safely(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except (pd.errors.EmptyDataError, FileNotFoundError, OSError):
        return pd.DataFrame()


def _ensure(df: pd.DataFrame, defaults: dict[str, Any]) -> pd.DataFrame:
    data = df.copy()
    for column, default in defaults.items():
        if column not in data.columns:
            data[column] = default
    return data


def _first_existing(data: pd.DataFrame, columns: list[str], default: Any = "") -> pd.Series:
    result = pd.Series([""] * len(data), index=data.index)
    for column in columns:
        if column in data.columns:
            values = data[column]
            result = result.where(result.fillna("").astype(str).str.strip() != "", values)
    return result.where(result.fillna("").astype(str).str.strip() != "", default)


def _num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _date_series(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce")


def normalize_scanner_data(df: pd.DataFrame, source: str = "scanner") -> pd.DataFrame:
    defaults = {
        "timestamp": "",
        "closed_at": "",
        "symbol": "",
        "side": "",
        "direction": "",
        "watchlist_tier": "",
        "tier": "",
        "market_session": "",
        "session": "",
        "entry": "",
        "stop_loss": "",
        "sl": "",
        "tp1": "",
        "tp2": "",
        "tp3": "",
        "risk_reward": "",
        "rr": "",
        "real_rr": "",
        "setup_strength": "",
        "confidence": "",
        "score": "",
        "raw_score": "",
        "market_regime": "",
        "btc_regime": "",
        "htf_alignment": "",
        "volume_spike": "",
        "mfi": "",
        "atr": "",
        "body_percent": "",
        "body_ratio": "",
        "atr_expansion": "",
        "atr_expansion_ratio": "",
        "result": "OPEN",
        "hit_target": "",
        "outcome": "",
        "pnl_percent": "",
        "holding_minutes": "",
        "max_profit_pct": "",
        "max_drawdown_pct": "",
        "signal_status": "sent",
        "skip_reason": "",
        "tp1_alert_source": "",
        "breakeven_recommended": 0,
        "position_management_stage": "",
    }
    data = _ensure(df, defaults)
    normalized = pd.DataFrame(index=data.index)
    normalized["timestamp"] = _date_series(data["timestamp"])
    normalized["closed_at"] = _date_series(data["closed_at"])
    normalized["symbol"] = data["symbol"].fillna("").astype(str).str.upper().str.replace("BINANCE:", "", regex=False).str.replace(".P", "", regex=False)
    normalized["side"] = _first_existing(data, ["side", "direction"]).fillna("").astype(str).str.upper()
    normalized["tier"] = _first_existing(data, ["tier", "watchlist_tier"], "B").fillna("B").replace("", "B").astype(str).str.upper()
    normalized.loc[~normalized["tier"].isin(["A", "B", "C", "EXTERNAL"]), "tier"] = "B"
    normalized["session"] = _first_existing(data, ["session", "market_session"], "Other").fillna("Other").replace("", "Other").astype(str)
    normalized["entry"] = _num(data["entry"])
    normalized["sl"] = _num(_first_existing(data, ["sl", "stop_loss"]))
    normalized["tp1"] = _num(data["tp1"])
    normalized["tp2"] = _num(data["tp2"])
    normalized["tp3"] = _num(data["tp3"])
    normalized["rr"] = _num(_first_existing(data, ["rr", "risk_reward"]))
    normalized["real_rr"] = _num(data["real_rr"])
    normalized["setup_strength"] = _num(_first_existing(data, ["setup_strength", "confidence"]))
    normalized["score"] = _num(_first_existing(data, ["score", "raw_score"]))
    normalized["market_regime"] = data["market_regime"].fillna(NA).replace("", NA).astype(str)
    normalized["btc_regime"] = data["btc_regime"].fillna(NA).replace("", NA).astype(str).str.lower()
    normalized["htf_alignment"] = data["htf_alignment"].fillna(NA).replace("", NA).astype(str)
    normalized["volume_spike"] = data["volume_spike"].fillna(NA).replace("", NA).astype(str)
    normalized["mfi"] = _num(data["mfi"])
    normalized["atr"] = _num(data["atr"])
    body_percent = _num(data["body_percent"])
    body_ratio = _num(data["body_ratio"])
    normalized["body_percent"] = body_percent.where(body_percent.notna(), body_ratio * 100)
    normalized["atr_expansion"] = _num(_first_existing(data, ["atr_expansion", "atr_expansion_ratio"]))
    normalized["result"] = data["result"].fillna("OPEN").replace("", "OPEN").astype(str).str.upper()
    normalized.loc[normalized["result"].isin(["NAN", "NONE", "NULL"]), "result"] = "OPEN"
    normalized["hit_target"] = data["hit_target"].fillna("").astype(str).str.upper()
    normalized["outcome"] = data["outcome"].fillna("").astype(str).str.upper()
    normalized.loc[normalized["outcome"].eq("") & normalized["result"].eq("WIN"), "outcome"] = "WIN_" + normalized["hit_target"].replace("", "TP1")
    normalized.loc[normalized["outcome"].eq("") & normalized["result"].eq("LOSS"), "outcome"] = "LOSS"
    normalized.loc[normalized["outcome"].eq("") & normalized["result"].eq("OPEN"), "outcome"] = "OPEN"
    normalized["pnl_percent"] = _num(data["pnl_percent"])
    normalized["holding_minutes"] = _num(data["holding_minutes"])
    missing_holding = normalized["holding_minutes"].isna() & normalized["timestamp"].notna() & normalized["closed_at"].notna()
    normalized.loc[missing_holding, "holding_minutes"] = (
        normalized.loc[missing_holding, "closed_at"] - normalized.loc[missing_holding, "timestamp"]
    ).dt.total_seconds().div(60)
    normalized["max_profit_pct"] = _num(data["max_profit_pct"])
    normalized["max_drawdown_pct"] = _num(data["max_drawdown_pct"])
    raw_status = data["signal_status"].fillna("sent").replace("", "sent").astype(str).str.lower()
    normalized["signal_status"] = raw_status
    inferred_skipped = normalized["result"].eq("SKIPPED") & raw_status.eq("sent")
    normalized.loc[inferred_skipped, "signal_status"] = "skipped"
    normalized["skip_reason"] = data["skip_reason"].fillna("").astype(str)
    normalized["tp1_alert_source"] = data["tp1_alert_source"].fillna("").astype(str).str.lower()
    normalized["breakeven_recommended"] = data["breakeven_recommended"].fillna(0).astype(str).str.lower().isin(["1", "true", "yes"])
    normalized["position_management_stage"] = data["position_management_stage"].fillna("").astype(str)
    normalized["source"] = source
    return normalized


def normalize_external_data(df: pd.DataFrame) -> pd.DataFrame:
    defaults = {
        "timestamp_utc": "",
        "timestamp": "",
        "source": "external",
        "source_type": "external",
        "symbol": "",
        "side": "",
        "direction": "",
        "entry_low": "",
        "entry_high": "",
        "entry": "",
        "stop_loss": "",
        "sl": "",
        "tp1": "",
        "tp2": "",
        "tp3": "",
        "recommendation": "",
        "status": "",
        "analysis_score": "",
        "confidence": "",
        "setup_strength": "",
        "rr": "",
        "mfi": "",
        "atr_pct": "",
        "market_regime": "",
        "btc_regime": "",
        "htf_alignment": "",
        "volume_spike": "",
        "reject_reason": "",
        "approved_reason": "",
        "result": "OPEN",
        "hit_target": "",
        "closed_at": "",
        "max_profit_pct": "",
        "max_drawdown_pct": "",
        "holding_minutes": "",
        "net_r_estimate": "",
        "sent_to_signals": "NO",
        "sent_to_cornix": "NO",
        "parse_status": "",
    }
    data = _ensure(df, defaults)
    normalized = pd.DataFrame(index=data.index)
    timestamp_source = data["timestamp"].where(data["timestamp"].fillna("").astype(str).str.strip().ne(""), data["timestamp_utc"])
    normalized["timestamp"] = _date_series(timestamp_source)
    normalized["closed_at"] = _date_series(data["closed_at"])
    normalized["symbol"] = data["symbol"].fillna("").astype(str).str.upper()
    side_source = data["direction"].where(data["direction"].fillna("").astype(str).str.strip().ne(""), data["side"])
    normalized["side"] = side_source.fillna("").astype(str).str.upper()
    normalized["tier"] = "External"
    normalized["session"] = "External"
    entry_low = _num(data["entry_low"])
    entry_high = _num(data["entry_high"])
    explicit_entry = _num(data["entry"])
    normalized["entry"] = explicit_entry.fillna(pd.concat([entry_low, entry_high], axis=1).mean(axis=1))
    explicit_sl = _num(data["sl"])
    normalized["sl"] = explicit_sl.fillna(_num(data["stop_loss"]))
    normalized["tp1"] = _num(data["tp1"])
    normalized["tp2"] = _num(data["tp2"])
    normalized["tp3"] = _num(data["tp3"])
    normalized["rr"] = _num(data["rr"])
    normalized["real_rr"] = _num(data["net_r_estimate"])
    normalized["setup_strength"] = _num(data["setup_strength"]).fillna(_num(data["confidence"])).fillna(_num(data["analysis_score"]))
    normalized["score"] = _num(data["analysis_score"])
    normalized["market_regime"] = data["market_regime"].fillna(NA).replace("", NA).astype(str)
    normalized["btc_regime"] = data["btc_regime"].fillna(NA).replace("", NA).astype(str).str.lower()
    normalized["htf_alignment"] = data["htf_alignment"].fillna(NA).replace("", NA).astype(str)
    normalized["volume_spike"] = data["volume_spike"].fillna(NA).replace("", NA).astype(str)
    normalized["mfi"] = _num(data["mfi"])
    normalized["atr"] = _num(data["atr_pct"])
    normalized["body_percent"] = pd.NA
    normalized["atr_expansion"] = pd.NA
    normalized["result"] = data["result"].fillna("OPEN").replace("", "OPEN").astype(str).str.upper()
    normalized["hit_target"] = data["hit_target"].fillna("").astype(str).str.upper()
    normalized["outcome"] = data["recommendation"].fillna("").astype(str).str.upper()
    normalized["pnl_percent"] = pd.NA
    normalized["holding_minutes"] = _num(data["holding_minutes"])
    normalized["max_profit_pct"] = _num(data["max_profit_pct"])
    normalized["max_drawdown_pct"] = _num(data["max_drawdown_pct"])
    status_source = data["status"].where(data["status"].fillna("").astype(str).str.strip().ne(""), data["recommendation"])
    normalized["signal_status"] = status_source.fillna("").astype(str).str.upper().map(lambda value: "sent" if value == "APPROVED" else "rejected")
    reject_reason = data["reject_reason"].fillna("").astype(str)
    normalized["skip_reason"] = reject_reason.where(reject_reason.str.strip().ne(""), data["recommendation"].fillna("").astype(str))
    normalized["source"] = "external"
    normalized["sent_to_signals"] = data["sent_to_signals"].fillna("NO").astype(str).str.upper()
    normalized["sent_to_cornix"] = data["sent_to_cornix"].fillna("NO").astype(str).str.upper()
    normalized["parse_status"] = data["parse_status"].fillna("").astype(str).str.upper()
    normalized["recommendation"] = data["recommendation"].fillna("").astype(str).str.upper()
    normalized["status"] = status_source.fillna("").astype(str).str.upper()
    normalized["reject_reason"] = data["reject_reason"].fillna("").astype(str)
    normalized["approved_reason"] = data["approved_reason"].fillna("").astype(str)
    return normalized


def combine_scanner_sources(journal: pd.DataFrame, history: pd.DataFrame) -> pd.DataFrame:
    if not journal.empty:
        return normalize_scanner_data(journal, "scanner")
    frames = []
    if not history.empty:
        frames.append(normalize_scanner_data(history, "scanner"))
    if not frames:
        return normalize_scanner_data(pd.DataFrame(), "scanner")
    combined = pd.concat(frames, ignore_index=True)
    if combined.empty:
        return combined
    key_columns = ["timestamp", "symbol", "side", "entry"]
    combined["_key"] = combined[key_columns].apply(lambda row: "|".join(str(value) for value in row), axis=1)
    combined = combined.drop_duplicates("_key", keep="last").drop(columns="_key")
    return combined


def latest_report_date(scanner_df: pd.DataFrame, date: str | None = None) -> str:
    if date:
        return date
    return "ALL"


def filter_report_day(df: pd.DataFrame, date: str) -> pd.DataFrame:
    if df.empty or "timestamp" not in df:
        return df.copy()
    if not date or str(date).upper() == "ALL":
        return df.copy()
    return df[df["timestamp"].dt.strftime("%Y-%m-%d") == date].copy()


def sent_signals(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    return df[df["signal_status"].fillna("sent").astype(str).str.lower() == "sent"].copy()


def closed_trades(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    return df[df["result"].isin(["WIN", "LOSS"])].copy()


def winning_trades(df: pd.DataFrame) -> pd.DataFrame:
    closed = closed_trades(df)
    return closed[closed["result"] == "WIN"].copy()


def losing_trades(df: pd.DataFrame) -> pd.DataFrame:
    closed = closed_trades(df)
    return closed[closed["result"] == "LOSS"].copy()


def hit_level(value: Any) -> int:
    text = str(value).strip().upper()
    if not text or text in {"NAN", "NONE", "NULL"}:
        return 0
    if text.startswith("TP"):
        text = text.replace("TP", "", 1)
    numeric = pd.to_numeric(pd.Series([text]), errors="coerce").iloc[0]
    if pd.isna(numeric):
        return 0
    return max(0, int(float(numeric)))


def target_hits(df: pd.DataFrame, level: int) -> int:
    if df.empty or "hit_target" not in df.columns:
        return 0
    levels = df["hit_target"].map(hit_level)
    return int((levels >= level).sum())


def estimated_r(row: pd.Series) -> float:
    if pd.notna(row.get("real_rr")):
        return float(row.get("real_rr"))
    if str(row.get("result", "")).upper() == "LOSS" or str(row.get("outcome", "")).upper() == "LOSS":
        return -1.0
    rr = pd.to_numeric(pd.Series([row.get("rr")]), errors="coerce").iloc[0]
    rr = 0.0 if pd.isna(rr) else float(rr)
    level = hit_level(row.get("hit_target", ""))
    outcome = str(row.get("outcome", "")).upper()
    if level >= 3 or outcome == "WIN_TP3":
        return rr if rr > 0 else 3.0
    if level >= 2 or outcome == "WIN_TP2":
        return rr if rr > 0 else 2.0
    if str(row.get("result", "")).upper() == "WIN" or outcome == "WIN_TP1":
        return min(rr, 1.2) if rr > 0 else 1.0
    return 0.0


def calculate_pnl(row: pd.Series) -> float | None:
    value = pd.to_numeric(pd.Series([row.get("pnl_percent")]), errors="coerce").iloc[0]
    if not pd.isna(value):
        return float(value)
    entry = pd.to_numeric(pd.Series([row.get("entry")]), errors="coerce").iloc[0]
    if pd.isna(entry) or float(entry) <= 0:
        return None
    side = str(row.get("side", "")).upper()
    result = str(row.get("result", "")).upper()
    level = hit_level(row.get("hit_target", ""))
    if result == "LOSS":
        sl = pd.to_numeric(pd.Series([row.get("sl")]), errors="coerce").iloc[0]
        if pd.isna(sl):
            return None
        raw = (sl - entry) / entry * 100 if side == "LONG" else (entry - sl) / entry * 100
        return -abs(float(raw))
    if result == "WIN":
        column = "tp3" if level >= 3 else "tp2" if level >= 2 else "tp1"
        tp = pd.to_numeric(pd.Series([row.get(column)]), errors="coerce").iloc[0]
        if pd.isna(tp):
            return None
        raw = (tp - entry) / entry * 100 if side == "LONG" else (entry - tp) / entry * 100
        return abs(float(raw))
    return None


def safe_mean(series: pd.Series) -> float | None:
    numeric = pd.to_numeric(series, errors="coerce").dropna()
    if numeric.empty:
        return None
    return float(numeric.mean())


def safe_percent(numerator: int | float, denominator: int | float) -> float | None:
    if not denominator:
        return None
    return float(numerator) / float(denominator) * 100


def format_value(value: Any, suffix: str = "", precision: int = 1) -> str:
    if value is None:
        return NA
    if isinstance(value, str):
        return value if value else NA
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric):
        return NA
    return f"{float(numeric):.{precision}f}{suffix}"


def format_minutes(value: Any) -> str:
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric):
        return NA
    total = max(0, int(round(float(numeric))))
    hours = total // 60
    minutes = total % 60
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def classification_symbols(table: pd.DataFrame, classification: str) -> str:
    if table.empty or "Classification" not in table.columns or "Symbol" not in table.columns:
        return NA
    symbols = table.loc[table["Classification"].astype(str).eq(classification), "Symbol"].astype(str).tolist()
    return ", ".join(symbols) if symbols else NA


def win_rate_by_label(df: pd.DataFrame, column: str, best: bool) -> str:
    table = performance_by(df, column)
    if table.empty:
        return NA
    table = table[pd.to_numeric(table["closed_signals"], errors="coerce").fillna(0) > 0]
    if table.empty:
        return NA
    sorted_table = table.sort_values(["win_rate_sort", "closed_signals"], ascending=[not best, False])
    row = sorted_table.iloc[0]
    label = row.get(column, NA)
    rate = row.get("win_rate", NA)
    closed = row.get("closed_signals", 0)
    return f"{label} ({rate}, {int(closed)})"


def direction_win_rate(df: pd.DataFrame, side: str) -> float | None:
    side_df = closed_trades(df[df["side"] == side.upper()])
    if side_df.empty:
        return None
    return safe_percent(len(winning_trades(side_df)), len(side_df))


def performance_by(df: pd.DataFrame, column: str) -> pd.DataFrame:
    columns = [
        column,
        "total_signals",
        "closed_signals",
        "open_signals",
        "wins",
        "losses",
        "win_rate",
        "tp1_hits",
        "tp2_hits",
        "tp3_hits",
        "sl_hits",
        "avg_rr",
        "net_r",
        "avg_profit_pct",
        "avg_drawdown_pct",
        "avg_max_profit_pct",
        "avg_time_to_tp",
        "avg_time_to_sl",
        "win_rate_sort",
    ]
    if df.empty or column not in df.columns:
        return pd.DataFrame(columns=columns)
    rows = []
    for key, group in df.groupby(df[column].fillna(NA).replace("", NA).astype(str), dropna=False):
        sent = sent_signals(group)
        closed = closed_trades(sent)
        wins = winning_trades(sent)
        losses = losing_trades(sent)
        pnl_values = closed.apply(calculate_pnl, axis=1) if not closed.empty else pd.Series(dtype=float)
        net_r = closed.apply(estimated_r, axis=1).sum() if not closed.empty else 0.0
        win_rate = safe_percent(len(wins), len(closed))
        rows.append(
            {
                column: key,
                "total_signals": int(len(sent)),
                "closed_signals": int(len(closed)),
                "open_signals": int((sent["result"] == "OPEN").sum()) if not sent.empty else 0,
                "wins": int(len(wins)),
                "losses": int(len(losses)),
                "win_rate": format_value(win_rate, "%"),
                "tp1_hits": target_hits(wins, 1),
                "tp2_hits": target_hits(wins, 2),
                "tp3_hits": target_hits(wins, 3),
                "sl_hits": int(len(losses)),
                "avg_rr": format_value(safe_mean(sent["rr"]) if not sent.empty else None, precision=2),
                "net_r": format_value(net_r, "R", precision=2),
                "avg_profit_pct": format_value(safe_mean(pd.Series([v for v in pnl_values if v is not None and v > 0])), "%", 2),
                "avg_drawdown_pct": format_value(safe_mean(group["max_drawdown_pct"]), "%", 2),
                "avg_max_profit_pct": format_value(safe_mean(group["max_profit_pct"]), "%", 2),
                "avg_time_to_tp": format_minutes(safe_mean(wins["holding_minutes"]) if not wins.empty else None),
                "avg_time_to_sl": format_minutes(safe_mean(losses["holding_minutes"]) if not losses.empty else None),
                "win_rate_sort": -1.0 if win_rate is None else win_rate,
            }
        )
    result = pd.DataFrame(rows, columns=columns)
    if not result.empty:
        result = result.sort_values(["win_rate_sort", "closed_signals"], ascending=[False, False])
    return result


def position_management_counts(journal: pd.DataFrame, date: str | None = None) -> dict[str, int]:
    data = normalize_scanner_data(journal, "scanner") if not journal.empty else normalize_scanner_data(pd.DataFrame(), "scanner")
    if date:
        data = filter_report_day(data, date)
    skipped = data[data["signal_status"].astype(str).str.lower().str.contains("position", na=False)]
    reasons = skipped["skip_reason"].fillna("").astype(str).str.lower()
    return {
        "hold_count": int(reasons.str.contains("same_symbol_same_direction|hold").sum()),
        "opposite_signal_count": int(reasons.str.contains("same_symbol_opposite_direction|opposite").sum()),
        "exit_recommendation_count": int(reasons.str.contains("exit").sum()),
        "stale_position_count": int(reasons.str.contains("position_review|stale|over_6h").sum()),
    }


def external_counts(external_df: pd.DataFrame, date: str | None = None) -> dict[str, int]:
    data = normalize_external_data(external_df) if not external_df.empty else normalize_external_data(pd.DataFrame())
    if date:
        data = filter_report_day(data, date)
    if data.empty:
        return {
            "external_total": 0,
            "external_approved": 0,
            "external_rejected": 0,
            "external_sent_to_signals": 0,
            "external_sent_to_cornix": 0,
            "external_approval_rate": None,
            "external_top_reject_reasons": NA,
            "external_top_approved_symbols": NA,
            "external_top_rejected_symbols": NA,
            "external_wins": 0,
            "external_losses": 0,
            "external_open": 0,
            "external_win_rate": None,
            "external_net_r_estimate": 0.0,
        }
    approved = data["recommendation"].eq("APPROVED")
    rejected = ~approved
    approved_data = data[approved].copy()
    external_wins = int((approved_data["result"] == "WIN").sum())
    external_losses = int((approved_data["result"] == "LOSS").sum())
    external_open = int((approved_data["result"] == "OPEN").sum())
    external_closed = external_wins + external_losses
    net_r_values = pd.to_numeric(approved_data["real_rr"], errors="coerce").fillna(0)
    top_approved_symbols = (
        data.loc[approved, "symbol"].replace("", NA).value_counts().head(5)
        if approved.any()
        else pd.Series(dtype=int)
    )
    top_rejected_symbols = (
        data.loc[rejected, "symbol"].replace("", NA).value_counts().head(5)
        if rejected.any()
        else pd.Series(dtype=int)
    )
    reject_reasons = data.loc[rejected, "skip_reason"].fillna("").astype(str)
    blank_reasons = reject_reasons.str.strip().eq("")
    reject_reasons.loc[blank_reasons] = data.loc[rejected, "recommendation"].fillna("").astype(str).loc[blank_reasons]
    top_reject_reasons = reject_reasons.replace("", NA).value_counts().head(5)
    return {
        "external_total": int(len(data)),
        "external_approved": int(approved.sum()),
        "external_rejected": int(rejected.sum()),
        "external_sent_to_signals": int(data["sent_to_signals"].eq("YES").sum()),
        "external_sent_to_cornix": int(data["sent_to_cornix"].eq("YES").sum()),
        "external_approval_rate": safe_percent(int(approved.sum()), int(len(data))),
        "external_top_reject_reasons": ", ".join(f"{key}: {count}" for key, count in top_reject_reasons.items()) if not top_reject_reasons.empty else NA,
        "external_top_approved_symbols": ", ".join(f"{key}: {count}" for key, count in top_approved_symbols.items()) if not top_approved_symbols.empty else NA,
        "external_top_rejected_symbols": ", ".join(f"{key}: {count}" for key, count in top_rejected_symbols.items()) if not top_rejected_symbols.empty else NA,
        "external_wins": external_wins,
        "external_losses": external_losses,
        "external_open": external_open,
        "external_win_rate": safe_percent(external_wins, external_closed),
        "external_net_r_estimate": float(net_r_values.sum()),
    }


def build_complete_report(
    journal: pd.DataFrame,
    history: pd.DataFrame,
    external: pd.DataFrame,
    date: str | None = None,
) -> tuple[dict[str, Any], dict[str, pd.DataFrame]]:
    scanner_all = combine_scanner_sources(journal, history)
    report_date = latest_report_date(scanner_all, date)
    scanner_day = filter_report_day(scanner_all, report_date)
    sent_day = sent_signals(scanner_day)
    tier_c_report = scanner_day[
        scanner_day["signal_status"].fillna("").astype(str).str.lower().eq("tier_c_report_only")
    ].copy() if not scanner_day.empty else scanner_day
    tier_c_report_closed = closed_trades(tier_c_report)
    tier_c_report_wins = winning_trades(tier_c_report)
    tier_c_report_losses = losing_trades(tier_c_report)
    weak_symbol_report = scanner_day[
        scanner_day["signal_status"].fillna("").astype(str).str.lower().eq("weak_symbol_report_only")
    ].copy() if not scanner_day.empty else scanner_day
    weak_symbol_report_closed = closed_trades(weak_symbol_report)
    weak_symbol_report_wins = winning_trades(weak_symbol_report)
    weak_symbol_report_losses = losing_trades(weak_symbol_report)
    session_risk_report = scanner_day[
        scanner_day["signal_status"].fillna("").astype(str).str.lower().eq("session_risk_report_only")
    ].copy() if not scanner_day.empty else scanner_day
    session_risk_report_closed = closed_trades(session_risk_report)
    session_risk_report_wins = winning_trades(session_risk_report)
    session_risk_report_losses = losing_trades(session_risk_report)
    closed = closed_trades(sent_day)
    wins = winning_trades(sent_day)
    losses = losing_trades(sent_day)
    open_signals = sent_day[sent_day["result"] == "OPEN"].copy() if not sent_day.empty else sent_day
    tp1_sources = scanner_day["tp1_alert_source"].fillna("").astype(str).str.lower() if "tp1_alert_source" in scanner_day else pd.Series(dtype=str)
    stages = scanner_day["position_management_stage"].fillna("").astype(str).str.upper() if "position_management_stage" in scanner_day else pd.Series(dtype=str)
    breakeven = scanner_day["breakeven_recommended"] if "breakeven_recommended" in scanner_day else pd.Series(dtype=bool)
    pnl_values = closed.apply(calculate_pnl, axis=1) if not closed.empty else pd.Series(dtype=float)
    net_r = closed.apply(estimated_r, axis=1).sum() if not closed.empty else 0.0
    pos_counts = position_management_counts(journal, report_date)
    ext_counts = external_counts(external, report_date)

    report: dict[str, Any] = {
        "date": report_date,
        "total_sent_signals": int(len(sent_day)),
        "closed_signals": int(len(closed)),
        "open_signals": int(len(open_signals)),
        "wins": int(len(wins)),
        "losses": int(len(losses)),
        "win_rate": safe_percent(len(wins), len(closed)),
        "tp1_hits": target_hits(wins, 1),
        "tp2_hits": target_hits(wins, 2),
        "tp3_hits": target_hits(wins, 3),
        "sl_hits": int(len(losses)),
        "net_r_estimate": net_r,
        "avg_profit_pct": safe_mean(pd.Series([v for v in pnl_values if v is not None and v > 0])),
        "avg_loss_pct": safe_mean(pd.Series([v for v in pnl_values if v is not None and v < 0])),
        "avg_drawdown_pct": safe_mean(closed["max_drawdown_pct"]) if not closed.empty else None,
        "avg_max_profit_pct": safe_mean(closed["max_profit_pct"]) if not closed.empty else None,
        "avg_time_to_tp": safe_mean(wins["holding_minutes"]) if not wins.empty else None,
        "avg_time_to_sl": safe_mean(losses["holding_minutes"]) if not losses.empty else None,
        "best_symbol": win_rate_by_label(sent_day, "symbol", best=True),
        "worst_symbol": win_rate_by_label(sent_day, "symbol", best=False),
        "best_tier": win_rate_by_label(sent_day, "tier", best=True),
        "worst_tier": win_rate_by_label(sent_day, "tier", best=False),
        "best_session": win_rate_by_label(sent_day, "session", best=True),
        "worst_session": win_rate_by_label(sent_day, "session", best=False),
        "long_win_rate": direction_win_rate(sent_day, "LONG"),
        "short_win_rate": direction_win_rate(sent_day, "SHORT"),
        "scanner_win_rate": safe_percent(len(wins), len(closed)),
        "external_win_rate": None,
        "small_sample_warning": len(closed) < SMALL_SAMPLE_CLOSED_TRADES,
        "tier_c_report_count": int(len(tier_c_report)),
        "tier_c_report_wins": int(len(tier_c_report_wins)),
        "tier_c_report_losses": int(len(tier_c_report_losses)),
        "tier_c_report_win_rate": safe_percent(len(tier_c_report_wins), len(tier_c_report_closed)),
        "weak_symbol_report_count": int(len(weak_symbol_report)),
        "weak_symbol_report_wins": int(len(weak_symbol_report_wins)),
        "weak_symbol_report_losses": int(len(weak_symbol_report_losses)),
        "weak_symbol_report_win_rate": safe_percent(len(weak_symbol_report_wins), len(weak_symbol_report_closed)),
        "session_risk_report_count": int(len(session_risk_report)),
        "session_risk_report_wins": int(len(session_risk_report_wins)),
        "session_risk_report_losses": int(len(session_risk_report_losses)),
        "session_risk_report_win_rate": safe_percent(len(session_risk_report_wins), len(session_risk_report_closed)),
        "tp1_alerts_watcher": int(tp1_sources.eq("watcher").sum()) if not tp1_sources.empty else 0,
        "tp1_alerts_outcome_review": int(tp1_sources.eq("outcome_review").sum()) if not tp1_sources.empty else 0,
        "breakeven_recommendations": int(breakeven.sum()) if not breakeven.empty else 0,
        "open_tp1_be_recommended": int(((scanner_day["result"] == "OPEN") & stages.eq("TP1_REACHED_BE_RECOMMENDED")).sum()) if not scanner_day.empty and not stages.empty else 0,
        **pos_counts,
        **ext_counts,
    }

    source_perf = performance_by(sent_day, "source")
    v2 = build_performance_v2(sent_day)
    v3 = build_performance_v3(scanner_day)
    external_summary = pd.DataFrame(
        [
            {
                "source": "external",
                "total_signals": ext_counts["external_total"],
                "approved": ext_counts["external_approved"],
                "rejected": ext_counts["external_rejected"],
                "sent_to_signals": ext_counts["external_sent_to_signals"],
                "sent_to_cornix": ext_counts["external_sent_to_cornix"],
                "win_rate": NA,
            }
        ]
    )
    if source_perf.empty:
        source_perf = external_summary
    else:
        source_perf = pd.concat([source_perf.drop(columns=["win_rate_sort"], errors="ignore"), external_summary], ignore_index=True, sort=False)

    report["performance_warnings"] = "\n\n".join(v2["warnings"]) if v2["warnings"] else NA
    report["performance_v3_symbol"] = format_v3_table(v3["symbol_performance_v3"])
    report["performance_v3_session"] = format_v3_table(v3["session_performance_v3"])
    report["performance_v3_tier"] = format_v3_table(v3["tier_performance_v3"])
    report["performance_v3_direction"] = format_v3_table(v3["direction_performance_v3"])
    report["performance_v3_hour"] = format_v3_table(v3["hour_performance_v3"])
    report["score_performance_v3"] = format_v3_table(v3["score_performance_v3"], limit=10)
    report["score_tier_audit"] = format_v3_table(v3["score_tier_audit"], limit=12)
    report["score_session_audit"] = format_v3_table(v3["score_session_audit"], limit=12)
    report["score_direction_audit"] = format_v3_table(v3["score_direction_audit"], limit=12)
    report["score_symbol_audit"] = format_v3_table(v3["score_symbol_audit"], limit=12)
    report["score_efficiency_audit"] = format_v3_table(v3["score_efficiency_audit"], limit=10)
    report["production_universe_ranking"] = format_v3_table(v3["production_universe_ranking"], limit=20)
    report["production_universe_tier_s"] = classification_symbols(v3["production_universe_ranking"], "Tier S")
    report["production_universe_tier_a"] = classification_symbols(v3["production_universe_ranking"], "Tier A")
    report["production_universe_watch"] = classification_symbols(v3["production_universe_ranking"], "Watch")
    report["production_universe_report_only"] = classification_symbols(v3["production_universe_ranking"], "Report Only")
    report["shadow_filter_backtest"] = format_v3_table(v3["shadow_filter_backtest"], limit=12)
    report["recommended_actions"] = format_v3_table(v3["recommended_actions"], limit=12)

    tables = {
        "symbol_performance": performance_by(sent_day, "symbol").drop(columns=["win_rate_sort"], errors="ignore"),
        "tier_performance": performance_by(sent_day, "tier").drop(columns=["win_rate_sort"], errors="ignore"),
        "session_performance": performance_by(sent_day, "session").drop(columns=["win_rate_sort"], errors="ignore"),
        "btc_regime_performance": performance_by(sent_day, "btc_regime").drop(columns=["win_rate_sort"], errors="ignore"),
        "market_regime_performance": performance_by(sent_day, "market_regime").drop(columns=["win_rate_sort"], errors="ignore"),
        "direction_performance": performance_by(sent_day, "side").drop(columns=["win_rate_sort"], errors="ignore"),
        "source_performance": source_perf,
        "position_management": pd.DataFrame([{"date": report_date, **pos_counts}]),
        "symbol_performance_v2": v2["symbol_performance_v2"],
        "top_symbols": v2["top_symbols"],
        "bottom_symbols": v2["bottom_symbols"],
        "session_performance_v2": v2["session_performance_v2"],
        "direction_performance_v2": v2["direction_performance_v2"],
        "tier_performance_v2": v2["tier_performance_v2"],
        "performance_warnings": pd.DataFrame({"warning": v2["warnings"]}),
        "symbol_performance_v3": v3["symbol_performance_v3"],
        "session_performance_v3": v3["session_performance_v3"],
        "tier_performance_v3": v3["tier_performance_v3"],
        "direction_performance_v3": v3["direction_performance_v3"],
        "hour_performance_v3": v3["hour_performance_v3"],
        "score_performance_v3": v3["score_performance_v3"],
        "score_tier_audit": v3["score_tier_audit"],
        "score_session_audit": v3["score_session_audit"],
        "score_direction_audit": v3["score_direction_audit"],
        "score_symbol_audit": v3["score_symbol_audit"],
        "score_efficiency_audit": v3["score_efficiency_audit"],
        "production_universe_ranking": v3["production_universe_ranking"],
        "shadow_filter_backtest": v3["shadow_filter_backtest"],
        "recommended_actions": v3["recommended_actions"],
    }
    return report, tables


def export_v1_outputs(report: dict[str, Any], tables: dict[str, pd.DataFrame], logs_dir: Path) -> dict[str, Path]:
    logs_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "daily_performance": logs_dir / "daily_performance.csv",
        "symbol_performance": logs_dir / "symbol_performance.csv",
        "source_performance": logs_dir / "source_performance.csv",
        "position_management": logs_dir / "position_management.csv",
        "symbol_performance_v2": logs_dir / "symbol_performance_v2.csv",
        "session_performance_v2": logs_dir / "session_performance_v2.csv",
        "direction_performance_v2": logs_dir / "direction_performance_v2.csv",
        "tier_performance_v2": logs_dir / "tier_performance_v2.csv",
        "performance_warnings": logs_dir / "performance_warnings.csv",
        "symbol_performance_v3": logs_dir / "symbol_performance_v3.csv",
        "session_performance_v3": logs_dir / "session_performance_v3.csv",
        "tier_performance_v3": logs_dir / "tier_performance_v3.csv",
        "direction_performance_v3": logs_dir / "direction_performance_v3.csv",
        "hour_performance_v3": logs_dir / "hour_performance_v3.csv",
        "score_performance_v3": logs_dir / "score_performance_v3.csv",
        "score_tier_audit": logs_dir / "score_tier_audit.csv",
        "score_session_audit": logs_dir / "score_session_audit.csv",
        "score_direction_audit": logs_dir / "score_direction_audit.csv",
        "score_symbol_audit": logs_dir / "score_symbol_audit.csv",
        "score_efficiency_audit": logs_dir / "score_efficiency_audit.csv",
        "production_universe_ranking": logs_dir / "production_universe_ranking.csv",
        "shadow_filter_backtest": logs_dir / "shadow_filter_backtest.csv",
        "recommended_actions": logs_dir / "recommended_actions.csv",
    }

    daily_row = pd.DataFrame([{column: report.get(column, NA) for column in DAILY_PERFORMANCE_COLUMNS}])
    if paths["daily_performance"].exists():
        existing = load_csv_safely(paths["daily_performance"])
        daily = pd.concat([existing, daily_row], ignore_index=True)
        if "date" in daily.columns:
            daily = daily.drop_duplicates("date", keep="last")
    else:
        daily = daily_row
    daily.to_csv(paths["daily_performance"], index=False)

    tables.get("symbol_performance", pd.DataFrame()).to_csv(paths["symbol_performance"], index=False)
    tables.get("source_performance", pd.DataFrame()).to_csv(paths["source_performance"], index=False)
    tables.get("symbol_performance_v2", pd.DataFrame()).to_csv(paths["symbol_performance_v2"], index=False)
    tables.get("session_performance_v2", pd.DataFrame()).to_csv(paths["session_performance_v2"], index=False)
    tables.get("direction_performance_v2", pd.DataFrame()).to_csv(paths["direction_performance_v2"], index=False)
    tables.get("tier_performance_v2", pd.DataFrame()).to_csv(paths["tier_performance_v2"], index=False)
    tables.get("performance_warnings", pd.DataFrame()).to_csv(paths["performance_warnings"], index=False)
    tables.get("symbol_performance_v3", pd.DataFrame()).to_csv(paths["symbol_performance_v3"], index=False)
    tables.get("session_performance_v3", pd.DataFrame()).to_csv(paths["session_performance_v3"], index=False)
    tables.get("tier_performance_v3", pd.DataFrame()).to_csv(paths["tier_performance_v3"], index=False)
    tables.get("direction_performance_v3", pd.DataFrame()).to_csv(paths["direction_performance_v3"], index=False)
    tables.get("hour_performance_v3", pd.DataFrame()).to_csv(paths["hour_performance_v3"], index=False)
    tables.get("score_performance_v3", pd.DataFrame()).to_csv(paths["score_performance_v3"], index=False)
    tables.get("score_tier_audit", pd.DataFrame()).to_csv(paths["score_tier_audit"], index=False)
    tables.get("score_session_audit", pd.DataFrame()).to_csv(paths["score_session_audit"], index=False)
    tables.get("score_direction_audit", pd.DataFrame()).to_csv(paths["score_direction_audit"], index=False)
    tables.get("score_symbol_audit", pd.DataFrame()).to_csv(paths["score_symbol_audit"], index=False)
    tables.get("score_efficiency_audit", pd.DataFrame()).to_csv(paths["score_efficiency_audit"], index=False)
    tables.get("production_universe_ranking", pd.DataFrame()).to_csv(paths["production_universe_ranking"], index=False)
    tables.get("shadow_filter_backtest", pd.DataFrame()).to_csv(paths["shadow_filter_backtest"], index=False)
    tables.get("recommended_actions", pd.DataFrame()).to_csv(paths["recommended_actions"], index=False)
    position_row = tables.get("position_management", pd.DataFrame())
    if paths["position_management"].exists():
        existing_position = load_csv_safely(paths["position_management"])
        position = pd.concat([existing_position, position_row], ignore_index=True)
        if "date" in position.columns:
            position = position.drop_duplicates("date", keep="last")
    else:
        position = position_row
    position.to_csv(paths["position_management"], index=False)
    return paths
