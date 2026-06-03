# -*- coding: utf-8 -*-
"""Track real signal outcomes from Binance Futures candles."""

from __future__ import annotations

import os
import time
import argparse
import sys
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.analytics_engine import update_validation_artifacts
from core.outcome_tracker import HISTORY_COLUMNS, sync_history_files


BASE_DIR = Path(__file__).resolve().parent
JOURNAL = BASE_DIR / "logs" / "signals.csv"
HISTORY = BASE_DIR / "logs" / "signals_history.csv"
REJECTED = BASE_DIR / "logs" / "rejected_signals.csv"
BINANCE_FUTURES_KLINES = "https://fapi.binance.com/fapi/v1/klines"
ERROR_RETRY_SECONDS = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOGGER = logging.getLogger("outcome_checker")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUTCOME_COLUMNS = {
    "watchlist_tier": "B",
    "market_session": "",
    "htf_regime": "",
    "htf_alignment": "",
    "setup_strength": "",
    "raw_score": "",
    "score_bucket": "",
    "htf_conflict": "",
    "signal_version": "",
    "result": "OPEN",
    "hit_target": "",
    "closed_at": "",
    "max_profit_pct": "",
    "max_drawdown_pct": "",
    "outcome_alert_sent": 0,
    "outcome_alert_at": "",
    "outcome_id": "",
    "tp1_alert_sent": 0,
    "tp2_alert_sent": 0,
    "sl_alert_sent": 0,
    "outcome_alert_sent_at": "",
}

PROCESSED_OUTCOMES: set[str] = set()

@dataclass
class Outcome:
    result: str
    hit_target: str
    closed_at: str
    max_profit_pct: float
    max_drawdown_pct: float


@dataclass
class ReviewStats:
    open_trades: int = 0
    tp_hits: int = 0
    sl_hits: int = 0
    skipped_alerts: int = 0
    sent_alerts: int = 0
    errors: int = 0


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def telegram_channel_ids() -> dict[str, str]:
    return {
        "reports": os.getenv("TELEGRAM_REPORTS_CHAT_ID", "").strip(),
        "signals": os.getenv("TELEGRAM_SIGNALS_CHAT_ID", "").strip(),
        "cornix": os.getenv("TELEGRAM_CORNIX_CHAT_ID", "").strip(),
    }


def log_telegram_startup_routes() -> None:
    channels = telegram_channel_ids()
    LOGGER.info("REPORTS_CHAT_ID=%s", channels["reports"] or "-")
    LOGGER.info("SIGNALS_CHAT_ID=%s", channels["signals"] or "-")
    LOGGER.info("CORNIX_CHAT_ID=%s", channels["cornix"] or "-")


def build_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def display_symbol(symbol: str) -> str:
    cleaned = str(symbol).strip().upper().replace("BINANCE:", "").replace(".P", "")
    return f"{cleaned}.P"


def clean_symbol(symbol: str) -> str:
    return str(symbol).strip().upper().replace("BINANCE:", "").replace(".P", "")


def format_price(value: Any) -> str:
    price = float(value)
    if price >= 1000:
        return f"{price:.2f}"
    if price >= 10:
        return f"{price:.3f}"
    if price >= 1:
        return f"{price:.4f}"
    return f"{price:.6f}"


def format_period(start_iso: Any, end_iso: Any) -> str:
    start = pd.to_datetime(start_iso, utc=True, errors="coerce")
    end = pd.to_datetime(end_iso, utc=True, errors="coerce")
    if pd.isna(start) or pd.isna(end):
        return "-"
    total_minutes = max(0, int((end - start).total_seconds() // 60))
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if hours and minutes:
        return f"{hours} hr {minutes} min"
    if hours:
        return f"{hours} hr"
    return f"{minutes} min"


def format_hold_time(start_iso: Any, end_iso: Any) -> str:
    start = pd.to_datetime(start_iso, utc=True, errors="coerce")
    end = pd.to_datetime(end_iso, utc=True, errors="coerce")
    if pd.isna(start) or pd.isna(end):
        return "-"
    total_minutes = max(0, int((end - start).total_seconds() // 60))
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if hours and minutes:
        return f"{hours}h {minutes:02d}m"
    if hours:
        return f"{hours}h 00m"
    return f"{minutes}m"


def format_setup_strength(value: Any) -> str:
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric):
        return "-"
    return f"{numeric:.0f}%"


def format_score(value: Any) -> str:
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric):
        return "-"
    return f"{numeric:.0f}"


def ensure_columns(df: pd.DataFrame) -> pd.DataFrame:
    for column, default in OUTCOME_COLUMNS.items():
        if column not in df.columns:
            df[column] = default
        df[column] = df[column].astype("object")
    return df


def reload_journal() -> pd.DataFrame | None:
    if not JOURNAL.exists():
        LOGGER.info("No journal found at logs/signals.csv")
        return None
    try:
        df = pd.read_csv(JOURNAL)
    except pd.errors.EmptyDataError:
        LOGGER.info("Journal is empty.")
        return None
    if df.empty:
        LOGGER.info("Journal is empty.")
        return None
    df = ensure_columns(df)
    persisted_ids = {
        str(value).strip()
        for value in df["outcome_id"].dropna()
        if has_outcome_id(value)
    }
    PROCESSED_OUTCOMES.update(persisted_ids)
    LOGGER.info("Reloaded journal: %s rows, %s persisted outcome ids", len(df), len(persisted_ids))
    return df


def persist_journal(df: pd.DataFrame) -> None:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("w", newline="", encoding="utf-8") as handle:
        df.to_csv(handle, index=False)
        handle.flush()
        os.fsync(handle.fileno())
    LOGGER.info("CSV persisted: %s", JOURNAL)


def safe_float(value: Any, default: float = 0.0) -> float:
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric):
        return default
    return float(numeric)


def signal_history_key(df: pd.DataFrame) -> pd.Series:
    parts = []
    for column in ["timestamp", "symbol", "side", "entry"]:
        if column not in df.columns:
            parts.append(pd.Series([""] * len(df), index=df.index))
        else:
            parts.append(df[column].fillna("").astype(str).str.strip())
    return parts[0] + "|" + parts[1].str.upper() + "|" + parts[2].str.upper() + "|" + parts[3]


def realized_pnl_percent(row: pd.Series) -> float:
    result = str(row.get("result", "OPEN")).upper()
    hit_target = str(row.get("hit_target", "")).upper()
    side = str(row.get("side", "")).upper()
    entry = safe_float(row.get("entry"))
    if entry <= 0:
        return 0.0
    if result == "LOSS":
        stop_loss = safe_float(row.get("stop_loss", row.get("sl")))
        pnl = (stop_loss - entry) / entry * 100 if side == "LONG" else (entry - stop_loss) / entry * 100
        return -abs(float(pnl))
    if result == "WIN":
        target_column = "tp2" if hit_target == "TP2" else "tp1"
        target = safe_float(row.get(target_column))
        pnl = (target - entry) / entry * 100 if side == "LONG" else (entry - target) / entry * 100
        return abs(float(pnl))
    return 0.0


def holding_minutes(row: pd.Series) -> float:
    start = pd.to_datetime(row.get("timestamp"), utc=True, errors="coerce")
    end = pd.to_datetime(row.get("closed_at"), utc=True, errors="coerce")
    if pd.isna(start) or pd.isna(end):
        return 0.0
    return max(0.0, float((end - start).total_seconds() / 60))


def journal_to_history(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=HISTORY_COLUMNS)
    rows = []
    for _, row in df.iterrows():
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
            "setup_strength": row.get("setup_strength", row.get("confidence", "")),
            "score": row.get("raw_score", row.get("score", "")),
            "market_regime": row.get("market_regime", ""),
            "htf_alignment": row.get("htf_alignment", ""),
            "volume_spike": row.get("volume_spike", ""),
            "mfi": row.get("mfi", ""),
            "atr": row.get("atr", row.get("atr_pct", "")),
            "result": str(row.get("result", "OPEN") or "OPEN").upper(),
            "pnl_percent": f"{realized_pnl_percent(row):.4f}",
            "holding_minutes": f"{holding_minutes(row):.1f}",
        })
    history = pd.DataFrame(rows, columns=HISTORY_COLUMNS)
    return history


def sync_signal_history(df: pd.DataFrame) -> None:
    if HISTORY.parent != BASE_DIR / "logs":
        history, rejected = sync_history_files(df, HISTORY, HISTORY.with_name("rejected_signals_smoke.csv"))
        LOGGER.info("Validation artifacts synced: history=%s rejected=%s equity=0", len(history), len(rejected))
        return
    artifacts = update_validation_artifacts(df, BASE_DIR / "logs")
    LOGGER.info(
        "Validation artifacts synced: history=%s rejected=%s equity=%s",
        len(artifacts["history"]),
        len(artifacts["rejected"]),
        len(artifacts["equity"]),
    )


def alert_already_sent(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def has_outcome_id(value: Any) -> bool:
    if value is None or pd.isna(value):
        return False
    return bool(str(value).strip())


def build_outcome_id(row: pd.Series) -> str:
    symbol = str(row.get("symbol", "")).strip().upper()
    side = str(row.get("side", "")).strip().upper()
    entry = str(row.get("entry", "")).strip()
    result = str(row.get("result", "")).strip().upper()
    closed_at = str(row.get("closed_at", "")).strip()
    return f"{symbol}|{side}|{entry}|{result}|{closed_at}"


def outcome_alert_column(hit_target: Any) -> str:
    target = str(hit_target).strip().upper()
    if target == "TP2":
        return "tp2_alert_sent"
    if target == "TP1":
        return "tp1_alert_sent"
    return "sl_alert_sent"


def target_alert_already_sent(row: pd.Series) -> bool:
    if alert_already_sent(row.get("outcome_alert_sent", 0)):
        return True
    return alert_already_sent(row.get(outcome_alert_column(row.get("hit_target", "SL")), 0))


def build_outcome_alert(row: pd.Series) -> str:
    result = str(row["result"]).upper()
    hit_target = str(row.get("hit_target", "")).upper()
    hold_time = format_hold_time(row.get("timestamp"), row.get("closed_at"))
    setup_strength = row.get("setup_strength", row.get("confidence", ""))
    score = row.get("raw_score", row.get("score", ""))
    market = str(row.get("market_regime", "-") or "-")
    session_name = str(row.get("market_session", "-") or "-")
    disclaimer = "⚠️ Research tracking only. Past performance does not guarantee future results."

    if result == "WIN":
        r_value = "+2R" if hit_target == "TP2" else "+1R"
        return (
            f"✅ {hit_target or 'TP'} HIT\n"
            f"🪙 {clean_symbol(row['symbol'])}\n"
            f"📈 Result: {r_value}\n"
            f"⏱ Hold Time: {hold_time}\n"
            f"🔥 Setup Strength: {format_setup_strength(setup_strength)}\n"
            f"⭐ Score: {format_score(score)}\n"
            f"📊 Market: {market}\n"
            f"🧭 Session: {session_name}\n\n"
            f"{disclaimer}"
        )

    return (
        "❌ SL HIT\n"
        f"🪙 {clean_symbol(row['symbol'])}\n"
        "📉 Result: -1R\n"
        "⚠️ Risk managed correctly\n"
        f"⏱ Hold Time: {hold_time}\n"
        f"🔥 Setup Strength: {format_setup_strength(setup_strength)}\n"
        f"⭐ Score: {format_score(score)}\n"
        f"📊 Market: {market}\n"
        f"🧭 Session: {session_name}\n\n"
        f"{disclaimer}"
    )


def send_report_message(
    session: requests.Session,
    message: str,
    label: str,
    symbol: str = "-",
    result: str = "-",
    require_outcome_enabled: bool = True,
) -> bool:
    if not env_bool("SEND_TELEGRAM", True) or (require_outcome_enabled and not env_bool("SEND_OUTCOME_ALERTS", True)):
        LOGGER.info("Outcome alert skipped: SEND_TELEGRAM or SEND_OUTCOME_ALERTS is off")
        return False
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    reports_chat_id = telegram_channel_ids()["reports"]
    LOGGER.info(
        "OUTCOME ALERT ROUTE chat_id=%s symbol=%s result=%s",
        reports_chat_id or "-",
        symbol or "-",
        result or "-",
    )
    if not token or not reports_chat_id:
        LOGGER.warning("Outcome alert skipped: Telegram token/reports chat id missing")
        return False
    try:
        response = session.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": reports_chat_id, "text": message},
            timeout=20,
        )
    except requests.RequestException as exc:
        LOGGER.error("%s Telegram send failed: %s", label, exc)
        return False
    if response.status_code != 200:
        LOGGER.error("%s Telegram send failed: status=%s body=%s", label, response.status_code, response.text)
        return False
    LOGGER.info("%s Telegram send success: status=%s", label, response.status_code)
    return True


def send_telegram_alert(session: requests.Session, message: str, symbol: str = "-", result: str = "-") -> bool:
    return send_report_message(session, message, "Outcome alert", symbol, result)


def send_test_report(session: requests.Session) -> bool:
    reports_chat_id = telegram_channel_ids()["reports"]
    print(f"Test report destination chat id: {reports_chat_id or '-'}")
    message = (
        "🧪 Crypto Scanner Reports Channel Test\n"
        "Destination: TELEGRAM_REPORTS_CHAT_ID only\n"
        "No trade signal. No outcome update."
    )
    return send_report_message(session, message, "Test report", "TEST", "REPORT", require_outcome_enabled=False)


def fetch_klines(session: requests.Session, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    params = {
        "symbol": symbol,
        "interval": "15m",
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": 1000,
    }
    response = session.get(BINANCE_FUTURES_KLINES, params=params, timeout=20)
    response.raise_for_status()
    data = response.json()
    if not data:
        return pd.DataFrame()

    df = pd.DataFrame(
        data,
        columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_asset_volume", "num_trades",
            "taker_buy_base_volume", "taker_buy_quote_volume", "ignore",
        ],
    )
    for column in ["open", "high", "low", "close"]:
        df[column] = df[column].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    return df


def calculate_extremes(side: str, entry: float, candles: pd.DataFrame) -> tuple[float, float]:
    if candles.empty or entry <= 0:
        return 0.0, 0.0
    if side == "LONG":
        max_profit = (candles["high"].max() - entry) / entry * 100
        max_drawdown = (candles["low"].min() - entry) / entry * 100
    else:
        max_profit = (entry - candles["low"].min()) / entry * 100
        max_drawdown = (entry - candles["high"].max()) / entry * 100
    return float(max_profit), float(max_drawdown)


def evaluate_outcome(row: pd.Series, candles: pd.DataFrame) -> Outcome:
    side = str(row["side"]).upper()
    entry = float(row["entry"])
    stop_loss = float(row["stop_loss"])
    tp1 = float(row["tp1"])
    tp2 = float(row["tp2"])
    max_profit_pct, max_drawdown_pct = calculate_extremes(side, entry, candles)

    if candles.empty:
        return Outcome("OPEN", "", "", max_profit_pct, max_drawdown_pct)

    for _, candle in candles.iterrows():
        high = float(candle["high"])
        low = float(candle["low"])
        closed_at = candle["close_time"].isoformat()

        if side == "LONG":
            sl_hit = low <= stop_loss
            tp2_hit = high >= tp2
            tp1_hit = high >= tp1
        else:
            sl_hit = high >= stop_loss
            tp2_hit = low <= tp2
            tp1_hit = low <= tp1

        # Conservative mode: if SL and TP happen in the same candle, SL wins.
        if sl_hit:
            return Outcome("LOSS", "SL", closed_at, max_profit_pct, max_drawdown_pct)
        if tp2_hit:
            return Outcome("WIN", "TP2", closed_at, max_profit_pct, max_drawdown_pct)
        if tp1_hit:
            return Outcome("WIN", "TP1", closed_at, max_profit_pct, max_drawdown_pct)

    return Outcome("OPEN", "", "", max_profit_pct, max_drawdown_pct)


def review_signal(session: requests.Session, row: pd.Series, lookahead_hours: int) -> Outcome:
    timestamp = pd.to_datetime(row["timestamp"], utc=True, errors="coerce")
    if pd.isna(timestamp):
        return Outcome("OPEN", "", "", 0.0, 0.0)

    start_ms = int(timestamp.timestamp() * 1000)
    end_ts = min(timestamp + pd.Timedelta(hours=lookahead_hours), pd.Timestamp.now(tz="UTC"))
    end_ms = int(end_ts.timestamp() * 1000)
    candles = fetch_klines(session, str(row["symbol"]).upper(), start_ms, end_ms)
    return evaluate_outcome(row, candles)


def print_summary(df: pd.DataFrame) -> None:
    total = len(df)
    wins = int((df["result"] == "WIN").sum())
    losses = int((df["result"] == "LOSS").sum())
    open_trades = int((df["result"] == "OPEN").sum())
    closed = wins + losses
    win_rate = wins / closed * 100 if closed else 0.0
    rr = pd.to_numeric(df["risk_reward"], errors="coerce")

    symbol_win_rates: dict[str, float] = {}
    for symbol, group in df[df["result"].isin(["WIN", "LOSS"])].groupby("symbol"):
        symbol_win_rates[symbol] = float((group["result"] == "WIN").mean() * 100)
    best_symbol = max(symbol_win_rates, key=symbol_win_rates.get) if symbol_win_rates else "-"
    worst_symbol = min(symbol_win_rates, key=symbol_win_rates.get) if symbol_win_rates else "-"
    tier_win_rates: dict[str, float] = {}
    if "watchlist_tier" in df.columns:
        closed_by_tier = df[df["result"].isin(["WIN", "LOSS"])].copy()
        for tier, group in closed_by_tier.groupby(closed_by_tier["watchlist_tier"].fillna("B").astype(str).str.upper()):
            tier_win_rates[tier] = float((group["result"] == "WIN").mean() * 100)
    best_tier = max(tier_win_rates, key=tier_win_rates.get) if tier_win_rates else "-"

    print("Crypto Multi-Coin Scanner Outcome Review")
    print("----------------------------------------")
    print(f"Total signals: {total}")
    print(f"Wins: {wins}")
    print(f"Losses: {losses}")
    print(f"Open trades: {open_trades}")
    print(f"Win rate: {win_rate:.1f}%")
    print(f"Avg RR: {rr.mean():.2f}")
    print(f"Best symbol: {best_symbol}")
    print(f"Worst symbol: {worst_symbol}")
    print(f"Best tier: {best_tier}")
    if tier_win_rates:
        tier_text = ", ".join(f"{tier}: {rate:.1f}%" for tier, rate in sorted(tier_win_rates.items()))
        print(f"Tier win rates: {tier_text}")
    print()
    print("Summary table")
    print("-------------")
    for _, row in df.tail(50).iterrows():
        target = row["hit_target"] if isinstance(row["hit_target"], str) and row["hit_target"] else ""
        print(f"{row['symbol']} {row['side']} {row['result']} {target}".rstrip())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Review signal outcomes from Binance Futures candles.")
    parser.add_argument("--notify", action="store_true", help="Send Telegram alerts for newly closed outcomes.")
    parser.add_argument("--dry-run", action="store_true", help="Review outcomes without sending Telegram alerts.")
    parser.add_argument("--resend-unsent", action="store_true", help="Resend closed WIN/LOSS alerts where outcome_alert_sent is not 1.")
    parser.add_argument("--force-resend-outcome-alerts", action="store_true", help="Resend all closed WIN/LOSS outcome alerts, including already sent rows.")
    parser.add_argument("--test-report", action="store_true", help="Send a diagnostic message to TELEGRAM_REPORTS_CHAT_ID only.")
    return parser.parse_args()


def should_skip_outcome_alert(row: pd.Series, outcome_id: str, resend_unsent: bool, force_resend: bool) -> bool:
    if force_resend:
        return False
    if alert_already_sent(row.get("outcome_alert_sent", 0)):
        return True
    if resend_unsent:
        return False
    if target_alert_already_sent(row):
        return True
    if has_outcome_id(row.get("outcome_id", "")):
        return True
    return outcome_id in PROCESSED_OUTCOMES


def run_review_cycle(
    notify: bool,
    session: requests.Session,
    lookahead_hours: int,
    print_report: bool = True,
    resend_unsent: bool = False,
    force_resend: bool = False,
) -> ReviewStats:
    stats = ReviewStats()
    df = reload_journal()
    if df is None:
        return stats

    open_mask = df["result"].astype(str).str.upper() == "OPEN"
    stats.open_trades = int(open_mask.sum())

    for index, row in df.iterrows():
        previous_result = str(row.get("result", "OPEN")).upper()
        if previous_result == "OPEN":
            if target_alert_already_sent(row) or has_outcome_id(row.get("outcome_id", "")):
                stats.skipped_alerts += 1
                LOGGER.info("Outcome duplicate skipped before review: %s", row.get("outcome_id", ""))
                continue
            try:
                outcome = review_signal(session, row, lookahead_hours)
            except (requests.RequestException, ValueError, KeyError) as exc:
                stats.errors += 1
                LOGGER.error("Review skipped for %s: %s", row.get("symbol", "UNKNOWN"), exc)
                continue

            df.at[index, "result"] = outcome.result
            df.at[index, "hit_target"] = outcome.hit_target
            df.at[index, "closed_at"] = outcome.closed_at
            df.at[index, "max_profit_pct"] = f"{outcome.max_profit_pct:.2f}"
            df.at[index, "max_drawdown_pct"] = f"{outcome.max_drawdown_pct:.2f}"
            if outcome.result == "WIN":
                stats.tp_hits += 1
            elif outcome.result == "LOSS":
                stats.sl_hits += 1
        elif previous_result not in {"WIN", "LOSS"}:
            continue

        updated_row = df.loc[index]
        updated_result = str(updated_row.get("result", "")).upper()
        if updated_result not in {"WIN", "LOSS"}:
            time.sleep(0.2)
            continue

        outcome_id = build_outcome_id(updated_row)
        if should_skip_outcome_alert(updated_row, outcome_id, resend_unsent, force_resend):
            stats.skipped_alerts += 1
            LOGGER.info("Outcome duplicate skipped: %s", updated_row.get("outcome_id", outcome_id))
            time.sleep(0.2)
            continue
        if not notify:
            stats.skipped_alerts += 1
            time.sleep(0.2)
            continue

        LOGGER.info(
            "Outcome alert detected: outcome_id=%s symbol=%s result=%s hit_target=%s resend_unsent=%s force_resend=%s",
            outcome_id,
            updated_row.get("symbol", ""),
            updated_result,
            updated_row.get("hit_target", ""),
            resend_unsent,
            force_resend,
        )
        sent = send_telegram_alert(
            session,
            build_outcome_alert(updated_row),
            str(updated_row.get("symbol", "")),
            updated_result,
        )
        if sent:
            stats.sent_alerts += 1
            PROCESSED_OUTCOMES.add(outcome_id)
            df.at[index, "outcome_alert_sent"] = 1
            df.at[index, "outcome_alert_at"] = datetime.now(timezone.utc).isoformat()
            df.at[index, outcome_alert_column(updated_row.get("hit_target", "SL"))] = 1
            df.at[index, "outcome_alert_sent_at"] = df.at[index, "outcome_alert_at"]
            df.at[index, "outcome_id"] = outcome_id
            persist_journal(df)
        else:
            stats.skipped_alerts += 1
            LOGGER.error("Outcome alert not marked sent because Telegram delivery failed: %s", outcome_id)
        time.sleep(0.2)

    persist_journal(df)
    sync_signal_history(df)
    if print_report:
        print_summary(df)
    return stats


def run_loop(notify: bool, lookahead_hours: int, interval_seconds: int) -> int:
    LOGGER.info("Outcome checker started")
    while True:
        session = build_session()
        try:
            stats = run_review_cycle(notify=notify, session=session, lookahead_hours=lookahead_hours, print_report=False)
            LOGGER.info(
                "Open trades: %s | TP hits: %s | SL hits: %s | skipped alerts: %s | sent alerts: %s | errors: %s",
                stats.open_trades,
                stats.tp_hits,
                stats.sl_hits,
                stats.skipped_alerts,
                stats.sent_alerts,
                stats.errors,
            )
            LOGGER.info("Next outcome check in %s seconds", interval_seconds)
            time.sleep(interval_seconds)
        except Exception as exc:
            LOGGER.exception("Outcome checker loop error: %s", exc)
            LOGGER.info("Retrying outcome checker in %s seconds", ERROR_RETRY_SECONDS)
            time.sleep(ERROR_RETRY_SECONDS)


def main() -> int:
    args = parse_args()
    load_dotenv(BASE_DIR / ".env")
    log_telegram_startup_routes()
    lookahead_hours = env_int("REVIEW_LOOKAHEAD_HOURS", 24)
    interval_seconds = env_int(
        "OUTCOME_LOOP_INTERVAL_SECONDS",
        env_int("OUTCOME_CHECK_INTERVAL_MINUTES", 15) * 60,
    )
    loop_mode = env_bool("OUTCOME_LOOP_MODE", False)

    session = build_session()
    if args.test_report:
        return 0 if send_test_report(session) else 1

    if loop_mode:
        return run_loop(notify=True, lookahead_hours=lookahead_hours, interval_seconds=interval_seconds)

    notify = (args.notify or args.resend_unsent or args.force_resend_outcome_alerts) and not args.dry_run
    run_review_cycle(
        notify=notify,
        session=session,
        lookahead_hours=lookahead_hours,
        print_report=True,
        resend_unsent=args.resend_unsent,
        force_resend=args.force_resend_outcome_alerts,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
