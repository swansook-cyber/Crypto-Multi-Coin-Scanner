# -*- coding: utf-8 -*-
"""
Crypto Multi-Coin Scanner for Binance Futures.

This is a Telegram signal assistant only. It does not place orders. The rule
engine owns the signal decision; Gemini is optional commentary after a setup is
already scored.
"""

from __future__ import annotations

import csv
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from requests import Timeout
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.btc_regime_filter import detect_btc_regime
from core.loss_cooldown import LossCooldownTracker
from core.wave_structure_analyzer import calculate_wave_score
from position_manager import evaluate_new_signal

try:
    from google import genai
except Exception:  # pragma: no cover - optional dependency path
    genai = None


BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
CHART_DIR = BASE_DIR / "charts"
STATE_FILE = BASE_DIR / "signal_state.json"
LOG_FILE = LOG_DIR / "cornix_agent.log"
SIGNAL_JOURNAL = LOG_DIR / "signals.csv"
SIGNAL_VERSION = "internal-lab-v2"

LOG_DIR.mkdir(exist_ok=True)
CHART_DIR.mkdir(exist_ok=True)
load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def format_price(value: float) -> str:
    if value >= 1000:
        return f"{value:.2f}"
    if value >= 10:
        return f"{value:.3f}"
    if value >= 1:
        return f"{value:.4f}"
    return f"{value:.6f}"


def score_bucket(value: float | int) -> str:
    score = float(value)
    if score >= 90:
        return "A+"
    if score >= 80:
        return "A"
    if score >= 70:
        return "B"
    return "C"


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("cornix_agent")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


LOGGER = setup_logging()


@dataclass
class ScannerConfig:
    watchlist: list[str]
    watchlist_tiers: dict[str, str]
    score_threshold: int
    min_confidence: int
    min_rr: float
    cooldown_minutes: int
    confidence_override_delta: int
    loss_cooldown_minutes: int
    loss_streak_cooldown_losses: int
    loss_streak_cooldown_hours: int
    max_signals_per_scan: int
    max_signals_per_direction_per_candle: int
    max_major_correlated_signals: int
    use_btc_regime_filter: bool
    btc_sideway_penalty: int
    btc_low_vol_skip: bool
    use_candle_body_filter: bool
    min_body_ratio: float
    use_wick_filter: bool
    max_opposite_wick_ratio: float
    use_atr_expansion_filter: bool
    min_atr_expansion_ratio: float
    use_losing_streak_protection: bool
    max_symbol_loss_streak: int
    symbol_pause_after_loss_minutes: int
    use_daily_risk_guard: bool
    max_daily_losses: int
    max_daily_signals: int
    use_session_filter: bool
    active_sessions: list[str]
    allow_asia_session: bool
    session_penalty_asia: int
    use_4h_regime_filter: bool
    htf_timeframe: str
    trend_timeframe: str
    entry_timeframe: str
    htf_conflict_penalty: int
    run_once: bool
    dry_run: bool
    ai_commentary: bool
    ai_min_confidence: int
    ai_max_calls_per_run: int
    send_telegram: bool
    send_daily_summary: bool
    close_delay_seconds: int
    request_delay_seconds: float
    risk_per_trade_pct: float
    account_balance_usdt: float
    max_leverage: int
    min_volume_ratio: float
    min_atr_pct: float
    volume_spike_multiplier: float
    use_mfi_filter: bool
    mfi_period: int
    mfi_bullish_threshold: float
    mfi_bearish_threshold: float
    mfi_score_bonus: int
    use_liquidation_context: bool
    liquidation_confidence_penalty: int
    use_fear_greed: bool
    fear_greed_greed_threshold: int
    fear_greed_fear_threshold: int
    fear_greed_score_adjustment: int
    gemini_api_key: str
    gemini_model: str
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_signals_chat_id: str
    telegram_cornix_chat_id: str
    telegram_reports_chat_id: str
    telegram_external_inbox_chat_id: str

    @classmethod
    def from_env(cls) -> "ScannerConfig":
        default_tier_a = "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT"
        default_tier_b = "HYPEUSDT,SUIUSDT,DOGEUSDT,LINKUSDT,AVAXUSDT,ADAUSDT,DOTUSDT,NEARUSDT,OPUSDT,ARBUSDT,APTUSDT,INJUSDT,FILUSDT,LTCUSDT,ZECUSDT"
        default_tier_c = "PEPEUSDT,WIFUSDT,FLOKIUSDT,BONKUSDT,SEIUSDT,ORDIUSDT,ATOMUSDT,AAVEUSDT,UNIUSDT,RUNEUSDT"
        default_watchlist = "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,LTCUSDT,ZECUSDT,HYPEUSDT,LABUSDT"

        def parse_symbols(value: str) -> list[str]:
            symbols: list[str] = []
            for item in value.split(","):
                if not item.strip():
                    continue
                symbol = SymbolFormatter.to_binance_symbol(item)
                if symbol and symbol not in symbols:
                    symbols.append(symbol)
            return symbols

        tier_values = {
            "A": os.getenv("WATCHLIST_TIER_A", "").strip(),
            "B": os.getenv("WATCHLIST_TIER_B", "").strip(),
            "C": os.getenv("WATCHLIST_TIER_C", "").strip(),
        }
        if any(tier_values.values()):
            tier_lists = {
                "A": parse_symbols(tier_values["A"] or default_tier_a),
                "B": parse_symbols(tier_values["B"] or default_tier_b),
                "C": parse_symbols(tier_values["C"] or default_tier_c),
            }
            watchlist = []
            watchlist_tiers = {}
            for tier in ("A", "B", "C"):
                for symbol in tier_lists[tier]:
                    if symbol not in watchlist_tiers:
                        watchlist.append(symbol)
                        watchlist_tiers[symbol] = tier
        else:
            legacy_symbols = os.getenv("SYMBOLS", os.getenv("WATCHLIST", default_watchlist))
            watchlist = parse_symbols(legacy_symbols)
            watchlist_tiers = {symbol: "B" for symbol in watchlist}

        return cls(
            watchlist=watchlist,
            watchlist_tiers=watchlist_tiers,
            score_threshold=env_int("SCORE_THRESHOLD", 70),
            min_confidence=env_int("MIN_CONFIDENCE", 75),
            min_rr=env_float("MIN_RR", 1.8),
            cooldown_minutes=env_int("COOLDOWN_MINUTES", 240),
            confidence_override_delta=env_int("CONFIDENCE_OVERRIDE_DELTA", 12),
            loss_cooldown_minutes=env_int("LOSS_COOLDOWN_MINUTES", 180),
            loss_streak_cooldown_losses=env_int("LOSS_STREAK_COOLDOWN_LOSSES", 3),
            loss_streak_cooldown_hours=env_int("LOSS_STREAK_COOLDOWN_HOURS", 12),
            max_signals_per_scan=env_int("MAX_SIGNALS_PER_SCAN", 3),
            max_signals_per_direction_per_candle=env_int("MAX_SIGNALS_PER_DIRECTION_PER_CANDLE", 2),
            max_major_correlated_signals=env_int("MAX_MAJOR_CORRELATED_SIGNALS", 1),
            use_btc_regime_filter=env_bool("USE_BTC_REGIME_FILTER", True),
            btc_sideway_penalty=env_int("BTC_SIDEWAY_PENALTY", 10),
            btc_low_vol_skip=env_bool("BTC_LOW_VOL_SKIP", True),
            use_candle_body_filter=env_bool("USE_CANDLE_BODY_FILTER", True),
            min_body_ratio=env_float("MIN_BODY_RATIO", 0.45),
            use_wick_filter=env_bool("USE_WICK_FILTER", True),
            max_opposite_wick_ratio=env_float("MAX_OPPOSITE_WICK_RATIO", 0.45),
            use_atr_expansion_filter=env_bool("USE_ATR_EXPANSION_FILTER", True),
            min_atr_expansion_ratio=env_float("MIN_ATR_EXPANSION_RATIO", 1.05),
            use_losing_streak_protection=env_bool("USE_LOSING_STREAK_PROTECTION", True),
            max_symbol_loss_streak=env_int("MAX_SYMBOL_LOSS_STREAK", 2),
            symbol_pause_after_loss_minutes=env_int("SYMBOL_PAUSE_AFTER_LOSS_MINUTES", 360),
            use_daily_risk_guard=env_bool("USE_DAILY_RISK_GUARD", True),
            max_daily_losses=env_int("MAX_DAILY_LOSSES", 5),
            max_daily_signals=env_int("MAX_DAILY_SIGNALS", 12),
            use_session_filter=env_bool("USE_SESSION_FILTER", True),
            active_sessions=[item.strip() for item in os.getenv("ACTIVE_SESSIONS", "London,NewYork").split(",") if item.strip()],
            allow_asia_session=env_bool("ALLOW_ASIA_SESSION", True),
            session_penalty_asia=env_int("SESSION_PENALTY_ASIA", 3),
            use_4h_regime_filter=env_bool("USE_4H_REGIME_FILTER", True),
            htf_timeframe=os.getenv("HTF_TIMEFRAME", "4h").strip(),
            trend_timeframe=os.getenv("TREND_TIMEFRAME", "1h").strip(),
            entry_timeframe=os.getenv("ENTRY_TIMEFRAME", "15m").strip(),
            htf_conflict_penalty=env_int("HTF_CONFLICT_PENALTY", 8),
            run_once=env_bool("RUN_ONCE", False),
            dry_run=env_bool("DRY_RUN", False),
            ai_commentary=env_bool("AI_COMMENTARY", env_bool("USE_AI_COMMENTARY", True)),
            ai_min_confidence=env_int("AI_MIN_CONFIDENCE", 88),
            ai_max_calls_per_run=env_int("AI_MAX_CALLS_PER_RUN", 1),
            send_telegram=env_bool("SEND_TELEGRAM", True),
            send_daily_summary=env_bool("SEND_DAILY_SUMMARY", True),
            close_delay_seconds=env_int("CLOSE_DELAY_SECONDS", 20),
            request_delay_seconds=env_float("REQUEST_DELAY_SECONDS", 0.5),
            risk_per_trade_pct=env_float("RISK_PER_TRADE_PCT", 1.0),
            account_balance_usdt=env_float("ACCOUNT_BALANCE_USDT", 1000.0),
            max_leverage=env_int("MAX_LEVERAGE", 10),
            min_volume_ratio=env_float("MIN_VOLUME_RATIO", 0.80),
            min_atr_pct=env_float("MIN_ATR_PCT", 0.35),
            volume_spike_multiplier=env_float("VOLUME_SPIKE_MULTIPLIER", 1.20),
            use_mfi_filter=env_bool("USE_MFI_FILTER", True),
            mfi_period=env_int("MFI_PERIOD", 14),
            mfi_bullish_threshold=env_float("MFI_BULLISH_THRESHOLD", 55),
            mfi_bearish_threshold=env_float("MFI_BEARISH_THRESHOLD", 45),
            mfi_score_bonus=env_int("MFI_SCORE_BONUS", 8),
            use_liquidation_context=env_bool("USE_LIQUIDATION_CONTEXT", False),
            liquidation_confidence_penalty=env_int("LIQUIDATION_CONFIDENCE_PENALTY", 5),
            use_fear_greed=env_bool("USE_FEAR_GREED", False),
            fear_greed_greed_threshold=env_int("FEAR_GREED_GREED_THRESHOLD", 75),
            fear_greed_fear_threshold=env_int("FEAR_GREED_FEAR_THRESHOLD", 25),
            fear_greed_score_adjustment=env_int("FEAR_GREED_SCORE_ADJUSTMENT", 8),
            gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip(),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            telegram_signals_chat_id=os.getenv("TELEGRAM_SIGNALS_CHAT_ID", "").strip(),
            telegram_cornix_chat_id=os.getenv("TELEGRAM_CORNIX_CHAT_ID", "").strip(),
            telegram_reports_chat_id=os.getenv("TELEGRAM_REPORTS_CHAT_ID", "").strip(),
            telegram_external_inbox_chat_id=os.getenv("TELEGRAM_EXTERNAL_INBOX_CHAT_ID", "").strip(),
        )


@dataclass
class MarketRegime:
    name: str
    details: str


@dataclass
class TradeSignal:
    timestamp: datetime
    symbol: str
    watchlist_tier: str
    tradingview_symbol: str
    direction: str
    entry: float
    tp1: float
    tp2: float
    sl: float
    rr: float
    confidence: int
    score: int
    support: float
    resistance: float
    regime: str
    regime_details: str
    market_session: str
    htf_regime: str
    htf_alignment: str
    volume_spike: bool
    volume_ratio: float
    atr_pct: float
    mfi: float
    mfi_confirmed: bool
    body_ratio: float
    opposite_wick_ratio: float
    atr_expansion_ratio: float
    quality_flags: str
    reason: str
    liquidation_context: str = ""
    ai_commentary: str = ""
    risk_amount_usdt: float = 0.0
    position_size_coin: float = 0.0
    position_value_usdt: float = 0.0
    chart_path: Path | None = None
    wave_score: int = 0
    wave_structure: str = "unclear"
    wave_phase: str = "unknown"
    wave_notes: list[str] = field(default_factory=list)
    btc_regime: str = "unclear"
    risk_mode: str = "normal"
    btc_regime_notes: str = ""


@dataclass
class BtcMarketContext:
    regime: str
    allow_long: bool = True
    allow_short: bool = True
    risk_multiplier: float = 1.0
    notes: list[str] = field(default_factory=list)


class SymbolFormatter:
    @staticmethod
    def to_binance_symbol(symbol: str) -> str:
        cleaned = symbol.strip().upper()
        if "=" in cleaned:
            cleaned = cleaned.split("=", 1)[1]
        cleaned = cleaned.replace("BINANCE:", "").replace(".P", "")
        cleaned = cleaned.replace("/", "").replace("-", "")
        return cleaned

    @staticmethod
    def to_tradingview_symbol(symbol: str) -> str:
        return f"BINANCE:{SymbolFormatter.to_binance_symbol(symbol)}.P"

    @staticmethod
    def to_display_symbol(symbol: str) -> str:
        return f"{SymbolFormatter.to_binance_symbol(symbol)}.P"


def build_retry_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class MarketDataClient:
    BASE_URL = "https://fapi.binance.com/fapi/v1/klines"
    FEAR_GREED_URL = "https://api.alternative.me/fng/"

    def __init__(self, request_delay_seconds: float, session: requests.Session | None = None) -> None:
        self.request_delay_seconds = request_delay_seconds
        self.session = session or build_retry_session()
        self._last_request_ts = 0.0

    def _rate_limit_pause(self) -> None:
        elapsed = time.time() - self._last_request_ts
        if elapsed < self.request_delay_seconds:
            time.sleep(self.request_delay_seconds - elapsed)
        self._last_request_ts = time.time()

    def fetch_klines(self, symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
        self._rate_limit_pause()
        params = {"symbol": SymbolFormatter.to_binance_symbol(symbol), "interval": interval, "limit": limit}
        response = self.session.get(self.BASE_URL, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list) or len(data) < 60:
            raise ValueError(f"Not enough candle data for {symbol} {interval}")

        df = pd.DataFrame(
            data,
            columns=[
                "open_time", "open", "high", "low", "close", "volume",
                "close_time", "quote_asset_volume", "num_trades",
                "taker_buy_base_volume", "taker_buy_quote_volume", "ignore",
            ],
        )
        numeric_columns = [
            "open", "high", "low", "close", "volume",
            "quote_asset_volume", "taker_buy_base_volume", "taker_buy_quote_volume",
        ]
        for column in numeric_columns:
            df[column] = df[column].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
        return df

    def fetch_closed_klines(self, symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
        df = self.fetch_klines(symbol, interval, limit + 1)
        now = pd.Timestamp.now(tz="UTC")
        closed = df[df["close_time"] <= now]
        if len(closed) < limit:
            closed = df.iloc[:-1]
        return closed.tail(limit).reset_index(drop=True)

    def fetch_fear_greed(self) -> int | None:
        self._rate_limit_pause()
        response = self.session.get(self.FEAR_GREED_URL, params={"limit": 1, "format": "json"}, timeout=10)
        response.raise_for_status()
        data = response.json()
        try:
            return int(data["data"][0]["value"])
        except (KeyError, IndexError, TypeError, ValueError):
            return None


class IndicatorEngine:
    def __init__(self, mfi_period: int = 14) -> None:
        self.mfi_period = mfi_period

    @staticmethod
    def rsi(close: pd.Series, length: int = 14) -> pd.Series:
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
        relative_strength = avg_gain / avg_loss
        return 100 - (100 / (1 + relative_strength))

    @staticmethod
    def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
        high_low = df["high"] - df["low"]
        high_close = (df["high"] - df["close"].shift()).abs()
        low_close = (df["low"] - df["close"].shift()).abs()
        true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        return true_range.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()

    @staticmethod
    def mfi(df: pd.DataFrame, length: int = 14) -> pd.Series:
        typical_price = (df["high"] + df["low"] + df["close"]) / 3
        money_flow = typical_price * df["volume"]
        positive_flow = money_flow.where(typical_price > typical_price.shift(), 0.0)
        negative_flow = money_flow.where(typical_price < typical_price.shift(), 0.0)
        positive_sum = positive_flow.rolling(length, min_periods=length).sum()
        negative_sum = negative_flow.rolling(length, min_periods=length).sum()
        money_ratio = positive_sum / negative_sum.replace(0, pd.NA)
        return 100 - (100 / (1 + money_ratio))

    def add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        enriched = df.copy()
        enriched["rsi14"] = self.rsi(enriched["close"])
        enriched["ema9"] = enriched["close"].ewm(span=9, adjust=False).mean()
        enriched["ema20"] = enriched["close"].ewm(span=20, adjust=False).mean()
        enriched["ema21"] = enriched["close"].ewm(span=21, adjust=False).mean()
        enriched["ema50"] = enriched["close"].ewm(span=50, adjust=False).mean()
        enriched["atr14"] = self.atr(enriched)
        enriched["mfi"] = self.mfi(enriched, self.mfi_period)
        enriched["volume_sma20"] = enriched["volume"].rolling(20).mean()
        enriched["atr_pct"] = enriched["atr14"] / enriched["close"] * 100
        return enriched


class SupportResistanceEngine:
    def calculate(self, df: pd.DataFrame, lookback: int = 30) -> tuple[float, float]:
        recent = df.tail(lookback)
        return float(recent["low"].min()), float(recent["high"].max())


class MarketRegimeDetector:
    def detect(self, df_1h: pd.DataFrame) -> MarketRegime:
        latest = df_1h.iloc[-1]
        ema_gap_pct = abs(latest["ema20"] - latest["ema50"]) / latest["close"] * 100
        atr_pct = latest["atr_pct"]
        ema20_slope = (df_1h["ema20"].iloc[-1] - df_1h["ema20"].iloc[-8]) / latest["close"] * 100

        if atr_pct >= 2.8:
            return MarketRegime("High Volatility", f"ATR {atr_pct:.2f}%")
        if ema_gap_pct >= 0.35 and abs(ema20_slope) >= 0.15:
            return MarketRegime("Trending", f"EMA gap {ema_gap_pct:.2f}%, slope {ema20_slope:.2f}%")
        return MarketRegime("Sideway", f"EMA gap {ema_gap_pct:.2f}%, ATR {atr_pct:.2f}%")


class MarketSessionDetector:
    @staticmethod
    def detect(timestamp: datetime | pd.Timestamp | None = None) -> str:
        ts = pd.Timestamp(timestamp if timestamp is not None else datetime.now(timezone.utc))
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        ts = ts.tz_convert("UTC")
        hour = ts.hour + ts.minute / 60
        sessions = []
        if 0 <= hour < 8:
            sessions.append("Asia")
        if 8 <= hour < 16:
            sessions.append("London")
        if 13 <= hour < 21:
            sessions.append("NewYork")
        return "+".join(sessions) if sessions else "OffHours"


def classify_trend(df: pd.DataFrame) -> str:
    latest = df.iloc[-1]
    if latest["close"] > latest["ema20"] > latest["ema50"]:
        return "Bullish"
    if latest["close"] < latest["ema20"] < latest["ema50"]:
        return "Bearish"
    return "Sideway"


def alignment_for_direction(direction: str, htf_regime: str) -> str:
    if direction == "LONG" and htf_regime == "Bullish":
        return "Aligned"
    if direction == "SHORT" and htf_regime == "Bearish":
        return "Aligned"
    if htf_regime == "Sideway":
        return "Neutral"
    return "Conflict"


class LiquidationContextFilter:
    """Lightweight placeholder for future liquidation context sources."""

    def __init__(self, config: ScannerConfig) -> None:
        self.config = config

    def adjust(self, symbol: str, direction: str, confidence: int, price: float) -> tuple[int, str]:
        if not self.config.use_liquidation_context:
            return confidence, ""
        try:
            # Production-safe placeholder: no websocket, no heavy polling, no realtime orderflow.
            # Future integrations can return context such as "short cluster above price".
            context = self.fetch_context(symbol, price)
        except Exception as exc:
            LOGGER.info("Liquidation context unavailable for %s: %s", symbol, exc)
            return confidence, ""
        if not context:
            return confidence, ""
        adjusted = confidence
        if direction == "SHORT" and context == "short_cluster_above":
            adjusted = max(1, confidence - self.config.liquidation_confidence_penalty)
        elif direction == "LONG" and context == "long_cluster_below":
            adjusted = max(1, confidence - self.config.liquidation_confidence_penalty)
        if adjusted != confidence:
            LOGGER.info("Liquidation context adjusted confidence")
        return adjusted, context

    def fetch_context(self, symbol: str, price: float) -> str:
        return ""


class SignalScorer:
    def __init__(self, config: ScannerConfig, support_resistance: SupportResistanceEngine, regime_detector: MarketRegimeDetector) -> None:
        self.config = config
        self.support_resistance = support_resistance
        self.regime_detector = regime_detector
        self.liquidation_context = LiquidationContextFilter(config)

    def score(self, symbol: str, df_1h: pd.DataFrame, df_15m: pd.DataFrame, fear_greed: int | None = None, df_htf: pd.DataFrame | None = None) -> TradeSignal | None:
        watchlist_tier = self.config.watchlist_tiers.get(symbol, "B")
        latest_1h = df_1h.iloc[-1]
        latest_15m = df_15m.iloc[-1]
        previous_20 = df_1h.iloc[-21:-1]
        support, resistance = self.support_resistance.calculate(df_1h)
        regime = self.regime_detector.detect(df_1h)
        market_session = MarketSessionDetector.detect(latest_1h.get("close_time", datetime.now(timezone.utc)))
        htf_regime = classify_trend(df_htf) if df_htf is not None and not df_htf.empty else "Unknown"

        price = float(latest_1h["close"])
        candle_open = float(latest_1h["open"])
        candle_high = float(latest_1h["high"])
        candle_low = float(latest_1h["low"])
        atr = float(latest_1h["atr14"])
        atr_pct = float(latest_1h["atr_pct"])
        mfi = float(latest_1h["mfi"]) if not pd.isna(latest_1h["mfi"]) else 50.0
        volume_sma = float(latest_1h["volume_sma20"])
        volume_ratio = float(latest_1h["volume"] / volume_sma) if volume_sma > 0 else 0.0
        if math.isnan(atr) or atr <= 0:
            return None

        if self._is_no_trade(regime.name, volume_ratio, atr_pct):
            LOGGER.info(
                "%s NO TRADE: regime=%s volume_ratio=%.2f atr_pct=%.2f",
                symbol,
                regime.name,
                volume_ratio,
                atr_pct,
            )
            return None

        trend_long = latest_1h["close"] > latest_1h["ema20"] > latest_1h["ema50"]
        trend_short = latest_1h["close"] < latest_1h["ema20"] < latest_1h["ema50"]
        entry_long = latest_15m["close"] > latest_15m["ema9"] > latest_15m["ema21"] and latest_15m["rsi14"] >= 52
        entry_short = latest_15m["close"] < latest_15m["ema9"] < latest_15m["ema21"] and latest_15m["rsi14"] <= 48
        volume_spike = volume_ratio >= self.config.volume_spike_multiplier
        breakout_long = latest_1h["close"] > previous_20["high"].max()
        breakout_short = latest_1h["close"] < previous_20["low"].min()

        long_score = self._direction_score(trend_long, entry_long, volume_spike, breakout_long, regime.name, float(latest_1h["rsi14"]), "LONG")
        short_score = self._direction_score(trend_short, entry_short, volume_spike, breakout_short, regime.name, float(latest_1h["rsi14"]), "SHORT")

        if fear_greed is not None:
            if fear_greed >= self.config.fear_greed_greed_threshold:
                long_score -= self.config.fear_greed_score_adjustment
            if fear_greed <= self.config.fear_greed_fear_threshold:
                short_score -= self.config.fear_greed_score_adjustment

        long_score = max(0, long_score)
        short_score = max(0, short_score)

        candle_range = max(candle_high - candle_low, 0.0)
        candle_body = abs(price - candle_open)
        body_ratio = candle_body / candle_range if candle_range > 0 else 0.0
        upper_wick_ratio = (candle_high - max(candle_open, price)) / candle_range if candle_range > 0 else 0.0
        lower_wick_ratio = (min(candle_open, price) - candle_low) / candle_range if candle_range > 0 else 0.0
        atr_mean = float(df_1h["atr14"].iloc[-21:-1].mean())
        atr_expansion_ratio = atr / atr_mean if atr_mean > 0 and not math.isnan(atr_mean) else 1.0
        long_quality_flags: list[str] = []
        short_quality_flags: list[str] = []

        if self.config.use_candle_body_filter:
            if not (price > candle_open and body_ratio >= self.config.min_body_ratio):
                long_score = max(0, long_score - 12)
                long_quality_flags.append("weak_body")
            else:
                long_quality_flags.append("strong_body")
            if not (price < candle_open and body_ratio >= self.config.min_body_ratio):
                short_score = max(0, short_score - 12)
                short_quality_flags.append("weak_body")
            else:
                short_quality_flags.append("strong_body")

        if self.config.use_wick_filter:
            if upper_wick_ratio > self.config.max_opposite_wick_ratio:
                long_score = max(0, long_score - 8)
                long_quality_flags.append("upper_wick_risk")
            if lower_wick_ratio > self.config.max_opposite_wick_ratio:
                short_score = max(0, short_score - 8)
                short_quality_flags.append("lower_wick_risk")

        if self.config.use_atr_expansion_filter:
            if atr_expansion_ratio < self.config.min_atr_expansion_ratio:
                long_score = max(0, long_score - 8)
                short_score = max(0, short_score - 8)
                long_quality_flags.append("low_atr_expansion")
                short_quality_flags.append("low_atr_expansion")

        long_mfi_confirmed = self.config.use_mfi_filter and mfi >= self.config.mfi_bullish_threshold
        short_mfi_confirmed = self.config.use_mfi_filter and mfi <= self.config.mfi_bearish_threshold
        if long_mfi_confirmed:
            long_score += self.config.mfi_score_bonus
            LOGGER.info("MFI bullish confirmation")
        elif self.config.use_mfi_filter and mfi <= self.config.mfi_bearish_threshold:
            long_score = max(0, long_score - self.config.mfi_score_bonus)

        if short_mfi_confirmed:
            short_score += self.config.mfi_score_bonus
            LOGGER.info("MFI bearish confirmation")
        elif self.config.use_mfi_filter and mfi >= self.config.mfi_bullish_threshold:
            short_score = max(0, short_score - self.config.mfi_score_bonus)

        try:
            wave_context = calculate_wave_score(
                df_1h,
                {
                    "ema20": float(df_1h["ema20"].iloc[-1]),
                    "ema50": float(df_1h["ema50"].iloc[-1]),
                    "atr14": atr,
                    "mfi14": mfi,
                },
            )
        except Exception as exc:
            LOGGER.warning("Wave analyzer failed for %s: %s", symbol, exc)
            wave_context = {
                "wave_score": 0,
                "structure": "unclear",
                "possible_phase": "unknown",
                "notes": ["wave analysis unavailable"],
            }

        if long_score < 1 and short_score < 1:
            return None

        direction = "LONG" if long_score >= short_score else "SHORT"
        score = max(long_score, short_score)
        htf_alignment = alignment_for_direction(direction, htf_regime) if htf_regime != "Unknown" else "Unknown"
        mfi_confirmed = long_mfi_confirmed if direction == "LONG" else short_mfi_confirmed
        opposite_wick_ratio = upper_wick_ratio if direction == "LONG" else lower_wick_ratio
        quality_flags = long_quality_flags if direction == "LONG" else short_quality_flags
        if direction == "LONG":
            entry = price
            sl = entry - atr * 1.0
            tp1 = entry + atr * 1.2
            tp2 = entry + atr * 2.0
        else:
            entry = price
            sl = entry + atr * 1.0
            tp1 = entry - atr * 1.2
            tp2 = entry - atr * 2.0

        risk = abs(entry - sl)
        reward = abs(tp2 - entry)
        rr = reward / risk if risk > 0 else 0
        confidence = min(95, max(1, score + (8 if rr >= self.config.min_rr else 0)))
        wave_score = int(wave_context.get("wave_score", 0))
        wave_structure = str(wave_context.get("structure", "unclear"))
        wave_phase = str(wave_context.get("possible_phase", "unknown"))
        wave_notes = [str(item) for item in wave_context.get("notes", [])][:3]
        wave_aligned = (
            (direction == "LONG" and wave_structure == "bullish")
            or (direction == "SHORT" and wave_structure == "bearish")
        )
        wave_conflict = (
            (direction == "LONG" and wave_structure == "bearish")
            or (direction == "SHORT" and wave_structure == "bullish")
        )
        if wave_aligned:
            wave_bonus = min(8, max(1, wave_score // 12))
            score += wave_bonus
            confidence = min(95, confidence + wave_bonus)
            quality_flags.append("wave_aligned")
        elif wave_conflict:
            score = max(0, score - 5)
            confidence = max(1, confidence - 5)
            quality_flags.append("wave_conflict")
        elif wave_structure == "range":
            confidence = max(1, confidence - 2)
            quality_flags.append("wave_range")
        if watchlist_tier == "A":
            confidence = min(95, confidence + 3)
        elif watchlist_tier == "C":
            confidence = max(1, confidence - 3)
            if not volume_spike and not mfi_confirmed:
                score = max(0, score - 20)
                confidence = max(1, confidence - 8)
                quality_flags.append("tier_c_needs_volume_or_mfi")
            if score < 80:
                quality_flags.append("tier_c_score_below_80")
        if self.config.use_mfi_filter and not mfi_confirmed:
            confidence = max(1, confidence - 4)
        if self.config.use_session_filter:
            active_sessions = {item.strip().lower() for item in self.config.active_sessions}
            session_parts = {item.strip().lower() for item in market_session.split("+")}
            session_allowed = bool(session_parts & active_sessions)
            if market_session == "Asia" and self.config.allow_asia_session:
                confidence = max(1, confidence - self.config.session_penalty_asia)
                quality_flags.append("asia_session_penalty")
            elif not session_allowed:
                confidence = max(1, confidence - self.config.session_penalty_asia)
                quality_flags.append("inactive_session_penalty")
        if self.config.use_4h_regime_filter:
            if htf_alignment == "Aligned":
                confidence = min(95, confidence + 3)
                score += 3
            elif htf_alignment == "Conflict":
                confidence = max(1, confidence - self.config.htf_conflict_penalty)
                score = max(0, score - self.config.htf_conflict_penalty)
                quality_flags.append("htf_conflict")
        if self.config.use_candle_body_filter and "weak_body" in quality_flags:
            confidence = max(1, confidence - 6)
        if self.config.use_wick_filter and ("upper_wick_risk" in quality_flags or "lower_wick_risk" in quality_flags):
            confidence = max(1, confidence - 5)
        if self.config.use_atr_expansion_filter and "low_atr_expansion" in quality_flags:
            confidence = max(1, confidence - 5)
        confidence, liquidation_context = self.liquidation_context.adjust(symbol, direction, confidence, price)
        mfi_reason = (
            "MFI bullish confirmation"
            if direction == "LONG" and mfi_confirmed
            else "MFI bearish confirmation"
            if direction == "SHORT" and mfi_confirmed
            else f"MFI neutral/against {mfi:.1f}"
        )
        reason = (
            f"EMA {'bullish' if direction == 'LONG' else 'bearish'} trend on 1H; "
            f"15m confirmation {'confirmed' if (entry_long if direction == 'LONG' else entry_short) else 'weak'}; "
            f"volume ratio {volume_ratio:.2f}; ATR {atr_pct:.2f}%; "
            f"body {body_ratio:.2f}; ATR expansion {atr_expansion_ratio:.2f}x; "
            f"session {market_session}; 4H {htf_regime}/{htf_alignment}; {mfi_reason}; RR {rr:.2f}"
        )

        return TradeSignal(
            timestamp=datetime.now(timezone.utc),
            symbol=symbol,
            watchlist_tier=watchlist_tier,
            tradingview_symbol=SymbolFormatter.to_tradingview_symbol(symbol),
            direction=direction,
            entry=entry,
            tp1=tp1,
            tp2=tp2,
            sl=sl,
            rr=rr,
            confidence=confidence,
            score=score,
            support=support,
            resistance=resistance,
            regime=regime.name,
            regime_details=regime.details,
            market_session=market_session,
            htf_regime=htf_regime,
            htf_alignment=htf_alignment,
            volume_spike=volume_spike,
            volume_ratio=volume_ratio,
            atr_pct=atr_pct,
            mfi=mfi,
            mfi_confirmed=mfi_confirmed,
            body_ratio=body_ratio,
            opposite_wick_ratio=opposite_wick_ratio,
            atr_expansion_ratio=atr_expansion_ratio,
            quality_flags=", ".join(quality_flags),
            liquidation_context=liquidation_context,
            wave_score=wave_score,
            wave_structure=wave_structure,
            wave_phase=wave_phase,
            wave_notes=wave_notes,
            reason=reason,
        )

    def _is_no_trade(self, regime: str, volume_ratio: float, atr_pct: float) -> bool:
        return (
            regime == "Sideway"
            or volume_ratio < self.config.min_volume_ratio
            or atr_pct < self.config.min_atr_pct
        )

    @staticmethod
    def _direction_score(trend: bool, entry: bool, volume_spike: bool, breakout: bool, regime: str, rsi: float, direction: str) -> int:
        score = 0
        if trend:
            score += 30
        if entry:
            score += 25
        if volume_spike:
            score += 15
        if breakout:
            score += 15
        if regime == "Trending":
            score += 10
        elif regime == "High Volatility":
            score -= 10
        elif regime == "Sideway":
            score -= 15
        if direction == "LONG" and 50 <= rsi <= 70:
            score += 10
        if direction == "SHORT" and 30 <= rsi <= 50:
            score += 10
        return max(0, score)


class RiskManager:
    def __init__(self, config: ScannerConfig) -> None:
        self.config = config

    def apply(self, signal: TradeSignal) -> TradeSignal:
        risk_amount = self.config.account_balance_usdt * self.config.risk_per_trade_pct / 100
        per_coin_risk = abs(signal.entry - signal.sl)
        size_coin = risk_amount / per_coin_risk if per_coin_risk > 0 else 0.0
        signal.risk_amount_usdt = risk_amount
        signal.position_size_coin = size_coin
        signal.position_value_usdt = size_coin * signal.entry
        return signal


class AICommentaryEngine:
    def __init__(self, config: ScannerConfig) -> None:
        self.config = config
        self.client = None
        self.calls_this_run = 0
        self.disabled_for_run = False
        self.unavailable_logged = False
        if config.ai_commentary and config.gemini_api_key and genai:
            self.client = genai.Client(api_key=config.gemini_api_key)

    def reset_run_budget(self) -> None:
        self.calls_this_run = 0
        self.disabled_for_run = False
        self.unavailable_logged = False

    def can_summarize(self, signal: TradeSignal, config: ScannerConfig) -> bool:
        if not self.client:
            if config.ai_commentary and not self.unavailable_logged:
                LOGGER.info("AI skipped: Gemini unavailable")
                self.unavailable_logged = True
            return False
        if self.disabled_for_run:
            return False
        if signal.confidence < config.ai_min_confidence:
            LOGGER.info("AI skipped: confidence below AI_MIN_CONFIDENCE")
            return False
        if signal.rr < config.min_rr:
            return False
        if self.calls_this_run >= config.ai_max_calls_per_run:
            LOGGER.info("AI skipped: max calls reached")
            return False
        return True

    def build_prompt(self, signal: TradeSignal) -> str:
        reason_context = re.sub(r";?\s*RR\s+\d+(?:\.\d+)?", "", signal.reason, flags=re.IGNORECASE).strip(" ;")
        mfi_context = (
            "bullish MFI confirmation"
            if signal.direction == "LONG" and signal.mfi_confirmed
            else "bearish MFI confirmation"
            if signal.direction == "SHORT" and signal.mfi_confirmed
            else "MFI is neutral or against the setup"
        )
        volatility_context = (
            "high volatility"
            if signal.regime == "High Volatility"
            else "quiet volatility"
            if signal.atr_pct < self.config.min_atr_pct * 1.2
            else "normal volatility"
        )
        btc_context = "BTC alignment is not available"
        if "BTC sideway penalty" in signal.reason:
            btc_context = "BTC is sideways, so follow-through needs confirmation"
        elif signal.symbol == "BTCUSDT":
            btc_context = "this is the BTC market context"

        return f"""
Write trader-style market commentary for this rule-based Binance Futures signal.
Use English. Keep it to 1-2 short sentences under 240 characters.
Mention momentum, trend strength, MFI condition, volatility context, and BTC alignment when useful.
Use cautious professional wording such as "sell pressure", "momentum supports", or "continuation is possible under system conditions".
Avoid overconfident wording such as "clearly", "guaranteed", "certain", "แนวโน้มขาลงชัดเจน", or "ชัดเจน".
Do not repeat exact RR, exact confidence, entry, TP, SL, score, or numeric indicator values.
Do not override the trade logic and do not promise profit.

Symbol: {signal.symbol}
Direction: {signal.direction}
Trend/regime: {signal.regime}
Volume spike: {"yes" if signal.volume_spike else "no"}
MFI context: {mfi_context}
Volatility context: {volatility_context}
BTC context: {btc_context}
Rule reason without exact RR/confidence values: {reason_context}
"""

    @staticmethod
    def clean_commentary(text: str) -> str:
        cleaned = " ".join((text or "").strip().split())
        if not cleaned:
            return ""
        banned = re.compile(r"^(confidence|conf|RR|risk[- ]?reward|entry|TP1|TP2|SL|stop loss|score)\b", re.IGNORECASE)
        sentences = re.split(r"(?<=[.!?])\s+", cleaned.strip())
        kept = [sentence for sentence in sentences if sentence and not banned.match(sentence.strip())]
        cleaned = " ".join(kept[:2]).strip()
        cleaned = re.sub(r"\bclearly\b", "appears to", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\bguaranteed\b|\bcertain\b", "possible", cleaned, flags=re.IGNORECASE)
        cleaned = cleaned.replace("แนวโน้มขาลงชัดเจน", "มีแรงกดดันฝั่งขาย").replace("ชัดเจน", "ตามเงื่อนไขของระบบ")
        return cleaned[:240].rstrip(" ,;:")

    def summarize(self, signal: TradeSignal) -> str:
        if not self.client or self.disabled_for_run:
            return ""
        prompt = self.build_prompt(signal)
        try:
            self.calls_this_run += 1
            response = self.client.models.generate_content(model=self.config.gemini_model, contents=prompt)
            return self.clean_commentary(response.text or "")
        except Timeout:
            self.disabled_for_run = True
            LOGGER.info("AI skipped: Gemini unavailable")
            return ""
        except Exception as exc:
            message = str(exc)
            if (
                "403" in message
                or "429" in message
                or "timeout" in message.lower()
                or "quota" in message.lower()
            ):
                self.disabled_for_run = True
                LOGGER.info("AI skipped: Gemini unavailable")
                return ""
            LOGGER.info("AI skipped: Gemini unavailable")
            return ""


class TradeJournalLogger:
    FIELDNAMES = [
        "timestamp", "symbol", "side", "entry", "stop_loss", "tp1", "tp2",
        "risk_reward", "confidence", "setup_strength", "market_regime", "volume_spike", "score", "raw_score", "score_bucket", "watchlist_tier", "mfi", "mfi_confirmed", "ai_summary",
        "body_ratio", "opposite_wick_ratio", "atr_expansion_ratio", "quality_flags",
        "wave_score", "wave_structure", "wave_phase", "wave_notes",
        "btc_regime", "risk_mode", "btc_regime_notes",
        "market_session", "htf_regime", "htf_alignment", "htf_conflict", "signal_version",
        "signal_status", "skip_reason",
        "result", "hit_target", "closed_at", "max_profit_pct", "max_drawdown_pct",
        "outcome_alert_sent", "outcome_alert_at", "outcome_id",
    ]

    def __init__(self, path: Path = SIGNAL_JOURNAL) -> None:
        self.path = path
        self.path.parent.mkdir(exist_ok=True)
        if not self.path.exists():
            with self.path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=self.FIELDNAMES)
                writer.writeheader()
        else:
            self._migrate_schema()

    def _migrate_schema(self) -> None:
        try:
            df = pd.read_csv(self.path)
        except pd.errors.EmptyDataError:
            df = pd.DataFrame(columns=self.FIELDNAMES)
        changed = False
        defaults = {
            "result": "OPEN",
            "hit_target": "",
            "closed_at": "",
            "max_profit_pct": "",
            "max_drawdown_pct": "",
            "mfi": "",
            "mfi_confirmed": "",
            "watchlist_tier": "B",
            "setup_strength": "",
            "raw_score": "",
            "score_bucket": "",
            "body_ratio": "",
            "opposite_wick_ratio": "",
            "atr_expansion_ratio": "",
            "quality_flags": "",
            "wave_score": "",
            "wave_structure": "",
            "wave_phase": "",
            "wave_notes": "",
            "btc_regime": "",
            "risk_mode": "",
            "btc_regime_notes": "",
            "market_session": "",
            "htf_regime": "",
            "htf_alignment": "",
            "htf_conflict": "",
            "signal_version": SIGNAL_VERSION,
            "signal_status": "",
            "skip_reason": "",
            "outcome_alert_sent": 0,
            "outcome_alert_at": "",
            "outcome_id": "",
        }
        for column in self.FIELDNAMES:
            if column not in df.columns:
                df[column] = defaults.get(column, "")
                changed = True
        if changed:
            df = df[self.FIELDNAMES]
            df.to_csv(self.path, index=False)

    def log_signal(self, signal: TradeSignal, signal_status: str = "sent", skip_reason: str = "") -> None:
        result = "OPEN" if signal_status == "sent" else "SKIPPED"
        with self.path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.FIELDNAMES)
            writer.writerow(
                {
                    "timestamp": signal.timestamp.isoformat(),
                    "symbol": signal.symbol,
                    "side": signal.direction,
                    "entry": format_price(signal.entry),
                    "stop_loss": format_price(signal.sl),
                    "tp1": format_price(signal.tp1),
                    "tp2": format_price(signal.tp2),
                    "risk_reward": f"{signal.rr:.2f}",
                    "confidence": signal.confidence,
                    "setup_strength": signal.confidence,
                    "market_regime": signal.regime,
                    "volume_spike": "YES" if signal.volume_spike else "NO",
                    "score": signal.score,
                    "raw_score": signal.score,
                    "score_bucket": score_bucket(signal.confidence),
                    "watchlist_tier": signal.watchlist_tier,
                    "mfi": f"{signal.mfi:.2f}",
                    "mfi_confirmed": "YES" if signal.mfi_confirmed else "NO",
                    "ai_summary": signal.ai_commentary,
                    "body_ratio": f"{signal.body_ratio:.4f}",
                    "opposite_wick_ratio": f"{signal.opposite_wick_ratio:.4f}",
                    "atr_expansion_ratio": f"{signal.atr_expansion_ratio:.4f}",
                    "quality_flags": signal.quality_flags,
                    "wave_score": signal.wave_score,
                    "wave_structure": signal.wave_structure,
                    "wave_phase": signal.wave_phase,
                    "wave_notes": "; ".join(signal.wave_notes),
                    "btc_regime": signal.btc_regime,
                    "risk_mode": signal.risk_mode,
                    "btc_regime_notes": signal.btc_regime_notes,
                    "market_session": signal.market_session,
                    "htf_regime": signal.htf_regime,
                    "htf_alignment": signal.htf_alignment,
                    "htf_conflict": "YES" if signal.htf_alignment == "Conflict" else "NO",
                    "signal_version": SIGNAL_VERSION,
                    "signal_status": signal_status,
                    "skip_reason": skip_reason,
                    "result": result,
                    "hit_target": "",
                    "closed_at": "",
                    "max_profit_pct": "",
                    "max_drawdown_pct": "",
                    "outcome_alert_sent": 0,
                    "outcome_alert_at": "",
                    "outcome_id": "",
                }
            )

    def is_recent_loss(self, signal: TradeSignal, minutes: int) -> bool:
        if not self.path.exists():
            return False
        try:
            df = pd.read_csv(self.path)
        except (pd.errors.EmptyDataError, FileNotFoundError):
            return False
        required = {"symbol", "side", "result", "hit_target", "closed_at"}
        if df.empty or not required.issubset(df.columns):
            return False
        closed_at = pd.to_datetime(df["closed_at"], utc=True, errors="coerce")
        mask = (
            (df["symbol"].astype(str).str.upper() == signal.symbol.upper())
            & (df["side"].astype(str).str.upper() == signal.direction.upper())
            & (df["result"].astype(str).str.upper() == "LOSS")
            & (df["hit_target"].astype(str).str.upper() == "SL")
            & closed_at.notna()
        )
        if not mask.any():
            return False
        latest_loss = closed_at[mask].max()
        return pd.Timestamp.now(tz="UTC") - latest_loss < pd.Timedelta(minutes=minutes)

    def symbol_loss_streak_active(self, signal: TradeSignal, max_losses: int, pause_minutes: int) -> bool:
        if not self.path.exists():
            return False
        try:
            df = pd.read_csv(self.path)
        except (pd.errors.EmptyDataError, FileNotFoundError):
            return False
        required = {"symbol", "result", "closed_at"}
        if df.empty or not required.issubset(df.columns):
            return False
        symbol_df = df[df["symbol"].astype(str).str.upper() == signal.symbol.upper()].copy()
        symbol_df["closed_at"] = pd.to_datetime(symbol_df["closed_at"], utc=True, errors="coerce")
        symbol_df = symbol_df[symbol_df["result"].astype(str).str.upper().isin(["WIN", "LOSS"]) & symbol_df["closed_at"].notna()]
        if symbol_df.empty:
            return False
        symbol_df = symbol_df.sort_values("closed_at", ascending=False)
        streak = 0
        latest_loss_at = None
        for _, row in symbol_df.iterrows():
            if str(row["result"]).upper() != "LOSS":
                break
            streak += 1
            latest_loss_at = row["closed_at"] if latest_loss_at is None else latest_loss_at
        if streak < max_losses or latest_loss_at is None:
            return False
        return pd.Timestamp.now(tz="UTC") - latest_loss_at < pd.Timedelta(minutes=pause_minutes)

    def daily_risk_guard_active(self, max_losses: int, max_signals: int) -> tuple[bool, str]:
        if not self.path.exists():
            return False, ""
        try:
            df = pd.read_csv(self.path)
        except (pd.errors.EmptyDataError, FileNotFoundError):
            return False, ""
        if df.empty or "timestamp" not in df.columns:
            return False, ""
        today = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
        timestamps = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        day_df = df[timestamps.dt.strftime("%Y-%m-%d") == today]
        if day_df.empty:
            return False, ""
        sent_df = day_df[day_df.get("signal_status", "sent").fillna("sent") == "sent"] if "signal_status" in day_df else day_df
        losses = 0
        if "result" in df.columns and "closed_at" in df.columns:
            closed_at = pd.to_datetime(df["closed_at"], utc=True, errors="coerce")
            losses = int(((df["result"].astype(str).str.upper() == "LOSS") & (closed_at.dt.strftime("%Y-%m-%d") == today)).sum())
        if losses >= max_losses:
            return True, "max_daily_losses"
        if len(sent_df) >= max_signals:
            return True, "max_daily_signals"
        return False, ""

    def summarize_day(self, day: str) -> dict[str, Any]:
        if not self.path.exists():
            return {"day": day, "total": 0}
        df = pd.read_csv(self.path)
        if df.empty:
            return {"day": day, "total": 0}
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        day_df = df[df["timestamp"].dt.strftime("%Y-%m-%d") == day]
        if day_df.empty:
            return {"day": day, "total": 0}
        sent_df = day_df[day_df.get("signal_status", "sent").fillna("sent") == "sent"] if "signal_status" in day_df else day_df
        top_symbols = sent_df["symbol"].value_counts().head(3)
        statuses = day_df.get("signal_status", pd.Series([], dtype=str)).fillna("")
        results = day_df.get("result", pd.Series([], dtype=str)).fillna("").astype(str).str.upper()
        closed_at = pd.to_datetime(day_df.get("closed_at", pd.Series([], dtype=str)), utc=True, errors="coerce")
        daily_losses = int(((results == "LOSS") & (closed_at.dt.strftime("%Y-%m-%d") == day)).sum())
        tier_series = sent_df.get("watchlist_tier", pd.Series([], dtype=str)).fillna("B").astype(str).str.upper()
        session_counts = sent_df.get("market_session", pd.Series([], dtype=str)).fillna("-").astype(str).value_counts().head(5)
        tier_win_rates = {}
        if "watchlist_tier" in sent_df and "result" in sent_df:
            closed_sent = sent_df[sent_df["result"].astype(str).str.upper().isin(["WIN", "LOSS"])]
            for tier, group in closed_sent.groupby(closed_sent["watchlist_tier"].fillna("B").astype(str).str.upper()):
                tier_win_rates[tier] = float((group["result"].astype(str).str.upper() == "WIN").mean() * 100)
        best_tier = max(tier_win_rates, key=tier_win_rates.get) if tier_win_rates else "-"
        cooldown_status = LossCooldownTracker(self.path).status()
        return {
            "day": day,
            "total": int(len(day_df)),
            "sent_total": int(len(sent_df)),
            "long_count": int((sent_df["side"] == "LONG").sum()),
            "short_count": int((sent_df["side"] == "SHORT").sum()),
            "avg_confidence": float(pd.to_numeric(sent_df["confidence"], errors="coerce").mean()) if not sent_df.empty else 0.0,
            "avg_rr": float(pd.to_numeric(sent_df["risk_reward"], errors="coerce").mean()) if not sent_df.empty else 0.0,
            "avg_mfi": float(pd.to_numeric(sent_df.get("mfi"), errors="coerce").mean()) if "mfi" in sent_df and not sent_df.empty else 0.0,
            "mfi_confirmed_count": int((sent_df.get("mfi_confirmed", "") == "YES").sum()) if "mfi_confirmed" in sent_df else 0,
            "skipped_loss_cooldown": int((statuses == "skipped_loss_cooldown").sum()),
            "skipped_correlation": int((statuses == "skipped_correlation").sum()),
            "skipped_btc_regime": int((statuses == "skipped_btc_regime").sum()),
            "skipped_not_top_candidate": int((statuses == "skipped_not_top_candidate").sum()),
            "skipped_quality_filter": int((statuses == "logged_quality_filter").sum()),
            "skipped_daily_risk_guard": int((statuses == "skipped_daily_risk_guard").sum()),
            "skipped_losing_streak": int((statuses == "skipped_losing_streak").sum()),
            "daily_losses": daily_losses,
            "tier_a_sent": int((tier_series == "A").sum()),
            "tier_b_sent": int((tier_series == "B").sum()),
            "tier_c_sent": int((tier_series == "C").sum()),
            "best_tier_by_win_rate": best_tier,
            "sent_by_session": ", ".join(f"{session} ({count})" for session, count in session_counts.items()),
            "top_symbols": ", ".join(f"{symbol} ({count})" for symbol, count in top_symbols.items()),
            "loss_cooldown_status": "ACTIVE" if cooldown_status.active else "OFF",
            "loss_cooldown_streak": cooldown_status.loss_streak,
            "loss_cooldown_until": cooldown_status.pause_until.isoformat() if cooldown_status.pause_until is not None else "-",
        }


class ChartExporter:
    def export(self, symbol: str, df_1h: pd.DataFrame, signal: TradeSignal) -> Path:
        chart_df = df_1h.tail(80).copy()
        fig, ax = plt.subplots(figsize=(12, 7))
        ax.plot(chart_df["close_time"], chart_df["close"], label="Price", color="#111827", linewidth=1.5)
        ax.plot(chart_df["close_time"], chart_df["ema20"], label="EMA20", color="#2563eb", linewidth=1.0)
        ax.plot(chart_df["close_time"], chart_df["ema50"], label="EMA50", color="#f97316", linewidth=1.0)
        ax.axhline(signal.support, color="#16a34a", linestyle="--", linewidth=1, label="Support")
        ax.axhline(signal.resistance, color="#dc2626", linestyle="--", linewidth=1, label="Resistance")
        ax.axhline(signal.entry, color="#7c3aed", linewidth=1.2, label="Entry")
        ax.axhline(signal.tp1, color="#22c55e", linestyle="-.", linewidth=1, label="TP1")
        ax.axhline(signal.tp2, color="#15803d", linestyle="-.", linewidth=1, label="TP2")
        ax.axhline(signal.sl, color="#ef4444", linestyle="-.", linewidth=1, label="SL")
        ax.set_title(f"{signal.tradingview_symbol} {signal.direction} | RR {signal.rr:.2f} | Setup Strength {signal.confidence}%")
        ax.grid(True, alpha=0.25)
        ax.legend(loc="best")
        fig.autofmt_xdate()
        output = CHART_DIR / f"{symbol}_{signal.direction}_{signal.timestamp.strftime('%Y%m%d_%H%M%S')}.png"
        fig.tight_layout()
        fig.savefig(output, dpi=140)
        plt.close(fig)
        signal.chart_path = output
        return output


class TelegramNotifier:
    def __init__(self, config: ScannerConfig) -> None:
        self.config = config
        self.session = build_retry_session()

    def _channel_chat_id(self, channel: str) -> str:
        mapping = {
            "signals": self.config.telegram_signals_chat_id,
            "cornix": self.config.telegram_cornix_chat_id,
            "reports": self.config.telegram_reports_chat_id,
        }
        return mapping.get(channel, "").strip()

    def build_message(self, signal: TradeSignal) -> str:
        signal_emoji = "🚀" if signal.direction == "LONG" else "🔻"
        volume_text = "YES" if signal.volume_spike else "NO"
        commentary = f"\n\n🧠 AI Summary:\n{signal.ai_commentary}" if signal.ai_commentary else ""
        htf_alignment_yes = "YES" if signal.htf_alignment == "Aligned" else "NO"
        htf_conflict_yes = "YES" if signal.htf_alignment == "Conflict" else "NO"
        wave_notes = "\n".join(f"- {note}" for note in signal.wave_notes[:3]) or "- none"
        return (
            f"{signal_emoji} {signal.direction} SIGNAL\n"
            f"🪙 {SymbolFormatter.to_display_symbol(signal.symbol)}\n\n"
            f"💰 Entry: {format_price(signal.entry)}\n"
            f"🛑 SL: {format_price(signal.sl)}\n\n"
            f"🎯 TP1: {format_price(signal.tp1)}\n"
            f"🎯 TP2: {format_price(signal.tp2)}\n\n"
            f"📈 RR: 1:{signal.rr:.2f}\n"
            f"🔥 Setup Strength: {signal.confidence}%\n"
            f"⭐ Score: {signal.score}\n\n"
            f"📊 Market: {signal.regime}\n"
            f"🏷 Tier: {signal.watchlist_tier}\n"
            f"🕒 Session: {signal.market_session or 'Other'}\n\n"
            f"Market Regime:\n"
            f"BTC: {signal.btc_regime}\n"
            f"Risk Mode: {signal.risk_mode}\n\n"
            f"🧭 HTF:\n"
            f"4H Trend: {signal.htf_regime}\n"
            f"Alignment: {htf_alignment_yes}\n"
            f"Conflict: {htf_conflict_yes}\n"
            f"Regime Score: {score_bucket(signal.confidence)}\n\n"
            f"Wave Structure:\n"
            f"- Structure: {signal.wave_structure}\n"
            f"- Wave Score: {signal.wave_score}/100\n"
            f"- Possible Phase: {signal.wave_phase}\n"
            f"- Notes:\n{wave_notes}\n\n"
            f"📦 Volume Spike: {volume_text}\n"
            f"📈 MFI: {signal.mfi:.1f}\n"
            f"🕯 Body: {signal.body_ratio * 100:.0f}%\n"
            f"🌊 ATR Expansion: {signal.atr_expansion_ratio:.2f}x\n"
            f"🧱 Support: {format_price(signal.support)}\n"
            f"🧱 Resistance: {format_price(signal.resistance)}\n\n"
            f"⚖️ Risk: {signal.risk_amount_usdt:.2f} USDT ({self.config.risk_per_trade_pct:.2f}%)\n"
            f"📐 Size: {signal.position_size_coin:.6f} {signal.symbol.replace('USDT', '')}\n\n"
            f"🧠 Reason:\n{signal.reason}"
            f"{commentary}\n\n"
            f"For educational analysis only. Not financial advice.\n\n"
            f"Exchange: Binance Futures\n"
            f"Leverage: Cross {self.config.max_leverage}x\n"
            f"TradingView: {signal.tradingview_symbol}"
        )

    def build_cornix_message(self, signal: TradeSignal) -> str:
        entry_low = min(signal.entry, signal.entry * 0.995)
        entry_high = max(signal.entry, signal.entry * 1.005)
        return (
            "🧪 DRY RUN - CORNIX FORMAT TEST\n"
            "DO NOT AUTO TRADE\n\n"
            f"{signal.direction} {SymbolFormatter.to_binance_symbol(signal.symbol)}\n\n"
            "Entry:\n"
            f"{format_price(entry_low)}-{format_price(entry_high)}\n\n"
            "Targets:\n"
            f"{format_price(signal.tp1)}\n"
            f"{format_price(signal.tp2)}\n\n"
            "Stop:\n"
            f"{format_price(signal.sl)}\n\n"
            "Leverage:\n"
            f"{self.config.max_leverage}x"
        )

    def build_daily_summary_message(self, summary: dict[str, Any]) -> str:
        return (
            f"📅 Daily Signal Summary UTC\n"
            f"Date: {summary.get('day')}\n\n"
            f"📌 Total signals: {summary.get('total', 0)}\n"
            f"✅ Sent signals: {summary.get('sent_total', 0)}\n"
            f"🚀 Long: {summary.get('long_count', 0)}\n"
            f"🔻 Short: {summary.get('short_count', 0)}\n"
            f"🔥 Avg setup strength: {summary.get('avg_confidence', 0):.1f}%\n"
            f"📈 Avg RR: {summary.get('avg_rr', 0):.2f}\n"
            f"📈 Avg MFI: {summary.get('avg_mfi', 0):.1f}\n"
            f"🕒 Sent by session: {summary.get('sent_by_session') or '-'}\n"
            f"🏷 Tier A sent: {summary.get('tier_a_sent', 0)}\n"
            f"🏷 Tier B sent: {summary.get('tier_b_sent', 0)}\n"
            f"🏷 Tier C sent: {summary.get('tier_c_sent', 0)}\n"
            f"🏆 Best tier win rate: {summary.get('best_tier_by_win_rate', '-')}\n"
            f"🔥 MFI-confirmed signals: {summary.get('mfi_confirmed_count', 0)}\n"
            f"🛑 Daily losses: {summary.get('daily_losses', 0)}\n"
            f"🧪 Skipped quality filter: {summary.get('skipped_quality_filter', 0)}\n"
            f"🧯 Skipped daily risk guard: {summary.get('skipped_daily_risk_guard', 0)}\n"
            f"🥶 Skipped losing streak: {summary.get('skipped_losing_streak', 0)}\n"
            f"🧊 Skipped loss cooldown: {summary.get('skipped_loss_cooldown', 0)}\n"
            f"🧊 Loss cooldown: {summary.get('loss_cooldown_status', 'OFF')} "
            f"(streak {summary.get('loss_cooldown_streak', 0)}, until {summary.get('loss_cooldown_until', '-')})\n"
            f"🔗 Skipped correlation: {summary.get('skipped_correlation', 0)}\n"
            f"₿ Skipped BTC regime: {summary.get('skipped_btc_regime', 0)}\n"
            f"🏅 Skipped not top: {summary.get('skipped_not_top_candidate', 0)}\n"
            f"🏆 Top symbols: {summary.get('top_symbols') or '-'}"
        )

    def send_signal(self, signal: TradeSignal) -> bool:
        message = self.build_message(signal)
        if self.config.dry_run:
            LOGGER.info("DRY_RUN Telegram signal for %s:\n%s", signal.symbol, message)
            LOGGER.info("DRY_RUN Cornix message for %s:\n%s", signal.symbol, self.build_cornix_message(signal))
            if signal.chart_path:
                LOGGER.info("DRY_RUN chart path: %s", signal.chart_path)
            return True
        if not self.config.send_telegram:
            LOGGER.info("SEND_TELEGRAM=0, skipped Telegram for %s", signal.symbol)
            return True
        if not self.config.telegram_bot_token:
            LOGGER.warning("Telegram credentials are missing; skipped %s", signal.symbol)
            return False

        delivered = False
        signals_chat_id = self._channel_chat_id("signals")
        if not signals_chat_id:
            LOGGER.warning("Telegram signals chat id missing; skipped full signal for %s", signal.symbol)
        elif signal.chart_path and signal.chart_path.exists():
            delivered = self._send_photo(signal.chart_path, message, signals_chat_id, "signals") or delivered
        else:
            delivered = self._send_message(message, signals_chat_id, "signals") or delivered

        cornix_chat_id = self._channel_chat_id("cornix")
        if not cornix_chat_id:
            LOGGER.info("Telegram Cornix chat id missing; skipped Cornix dry-run for %s", signal.symbol)
        else:
            delivered = self._send_message(self.build_cornix_message(signal), cornix_chat_id, "cornix") or delivered
        return delivered

    def send_daily_summary(self, summary: dict[str, Any]) -> bool:
        message = self.build_daily_summary_message(summary)
        if self.config.dry_run:
            LOGGER.info("DRY_RUN daily summary:\n%s", message)
            return True
        if not self.config.send_telegram or not self.config.send_daily_summary:
            return True
        return self._send_message(message, self._channel_chat_id("reports"), "reports")

    def send_position_message(self, message: str) -> bool:
        if self.config.dry_run:
            LOGGER.info("DRY_RUN position management message:\n%s", message)
            return True
        if not self.config.send_telegram:
            LOGGER.info("SEND_TELEGRAM=0, skipped position management message")
            return True
        if not self.config.telegram_bot_token:
            LOGGER.warning("Telegram credentials are missing; skipped position management message")
            return False
        return self._send_message(message, self._channel_chat_id("reports"), "reports")

    def _send_message(self, message: str, chat_id: str, channel_name: str = "telegram") -> bool:
        if not self.config.telegram_bot_token:
            LOGGER.warning("Telegram %s bot token missing; skipped message", channel_name)
            return False
        if not chat_id:
            LOGGER.warning("Telegram %s chat id missing; skipped message", channel_name)
            return False
        url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message}
        try:
            response = self.session.post(url, data=payload, timeout=20)
        except requests.RequestException as exc:
            LOGGER.error("Telegram %s message failed: %s", channel_name, exc)
            return False
        if response.status_code != 200:
            LOGGER.error("Telegram %s message failed: %s", channel_name, response.text)
            return False
        return True

    def _send_photo(self, chart_path: Path, caption: str, chat_id: str, channel_name: str = "telegram") -> bool:
        if not self.config.telegram_bot_token:
            LOGGER.warning("Telegram %s bot token missing; skipped photo", channel_name)
            return False
        if not chat_id:
            LOGGER.warning("Telegram %s chat id missing; skipped photo", channel_name)
            return False
        url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendPhoto"
        try:
            with chart_path.open("rb") as image:
                files = {"photo": image}
                data = {"chat_id": chat_id, "caption": caption[:1024]}
                response = self.session.post(url, data=data, files=files, timeout=30)
        except (OSError, requests.RequestException) as exc:
            LOGGER.error("Telegram %s photo failed: %s", channel_name, exc)
            return False
        if response.status_code != 200:
            LOGGER.error("Telegram %s photo failed: %s", channel_name, response.text)
            return False
        return True


class AgentRunner:
    def __init__(self, config: ScannerConfig) -> None:
        self.config = config
        self.data_client = MarketDataClient(config.request_delay_seconds)
        self.indicators = IndicatorEngine(config.mfi_period)
        self.scorer = SignalScorer(config, SupportResistanceEngine(), MarketRegimeDetector())
        self.risk_manager = RiskManager(config)
        self.ai_commentary = AICommentaryEngine(config)
        self.journal = TradeJournalLogger()
        self.loss_cooldown = LossCooldownTracker(
            self.journal.path,
            config.loss_streak_cooldown_losses,
            config.loss_streak_cooldown_hours,
        )
        self.chart_exporter = ChartExporter()
        self.notifier = TelegramNotifier(config)
        self.state = self._load_state()
        self.fear_greed_value: int | None = None

    def _load_state(self) -> dict[str, Any]:
        if not STATE_FILE.exists():
            return {}
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            LOGGER.warning("State file is invalid; starting fresh")
            return {}

    def _save_state(self) -> None:
        STATE_FILE.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    def is_in_cooldown(self, signal: TradeSignal) -> bool:
        data = self.state.get("cooldowns", {}).get(signal.symbol)
        if not data:
            return False
        elapsed = time.time() - float(data.get("sent_at", 0))
        cooldown_minutes = self.config.cooldown_minutes * (1.5 if signal.watchlist_tier == "C" else 1.0)
        if elapsed >= cooldown_minutes * 60:
            return False
        last_confidence = int(data.get("confidence", 0))
        if signal.confidence >= last_confidence + self.config.confidence_override_delta:
            LOGGER.info(
                "%s cooldown override: confidence %s >= previous %s + %s",
                signal.symbol,
                signal.confidence,
                last_confidence,
                self.config.confidence_override_delta,
            )
            return False
        return True

    def mark_sent(self, signal: TradeSignal) -> None:
        self.state.setdefault("cooldowns", {})[signal.symbol] = {
            "sent_at": time.time(),
            "direction": signal.direction,
            "confidence": signal.confidence,
        }
        self._save_state()

    def seconds_until_next_1h_close(self) -> int:
        now = datetime.now(timezone.utc)
        seconds_into_hour = now.minute * 60 + now.second
        wait_seconds = 3600 - seconds_into_hour + self.config.close_delay_seconds
        return max(self.config.close_delay_seconds, wait_seconds)

    def maybe_send_daily_summary(self) -> None:
        if not self.config.send_daily_summary:
            return
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        last_summary_day = self.state.get("last_summary_day")
        if last_summary_day is None:
            self.state["last_summary_day"] = today
            self._save_state()
            return
        if last_summary_day == today:
            return
        summary = self.journal.summarize_day(last_summary_day)
        self.notifier.send_daily_summary(summary)
        self.state["last_summary_day"] = today
        self._save_state()

    def run_forever(self) -> None:
        LOGGER.info("Watchlist: %s", ", ".join(f"{symbol}({self.config.watchlist_tiers.get(symbol, 'B')})" for symbol in self.config.watchlist))
        LOGGER.info(
            "Threshold=%s min_confidence=%s min_rr=%.2f cooldown=%sm dry_run=%s ai_commentary=%s",
            self.config.score_threshold,
            self.config.min_confidence,
            self.config.min_rr,
            self.config.cooldown_minutes,
            self.config.dry_run,
            bool(self.ai_commentary.client),
        )
        if self.config.run_once:
            self.scan_once()
            return

        while True:
            wait_seconds = self.seconds_until_next_1h_close()
            LOGGER.info("Waiting %s seconds for next closed 1H candle", wait_seconds)
            time.sleep(wait_seconds)
            self.scan_once()

    def scan_once(self) -> None:
        self.maybe_send_daily_summary()
        self.ai_commentary.reset_run_budget()
        if self.config.use_fear_greed:
            try:
                self.fear_greed_value = self.data_client.fetch_fear_greed()
                LOGGER.info("Fear & Greed value: %s", self.fear_greed_value)
            except Exception as exc:
                LOGGER.warning("Fear & Greed fetch failed: %s", exc)
                self.fear_greed_value = None

        LOGGER.info("Scanning latest closed 1H candles")
        candidates: list[TradeSignal] = []
        for symbol in self.config.watchlist:
            try:
                signal = self.scan_symbol(symbol)
                if signal:
                    candidates.append(signal)
            except requests.HTTPError as exc:
                LOGGER.warning("Scan skipped for %s: %s", symbol, exc)
            except Exception as exc:
                LOGGER.exception("Scan failed for %s: %s", symbol, exc)
            time.sleep(self.config.request_delay_seconds)

        self.process_candidates(candidates)

    def scan_symbol(self, symbol: str) -> TradeSignal | None:
        df_1h = self.indicators.add_indicators(self.data_client.fetch_closed_klines(symbol, self.config.trend_timeframe, 200))
        df_15m = self.indicators.add_indicators(self.data_client.fetch_closed_klines(symbol, self.config.entry_timeframe, 200))
        df_htf = None
        if self.config.use_4h_regime_filter:
            df_htf = self.indicators.add_indicators(self.data_client.fetch_closed_klines(symbol, self.config.htf_timeframe, 200))
        signal = self.scorer.score(symbol, df_1h, df_15m, self.fear_greed_value, df_htf)
        if not signal:
            LOGGER.info("%s WAIT: no valid setup", symbol)
            return None

        signal = self.risk_manager.apply(signal)
        signal.ai_commentary = ""
        return signal

    def process_candidates(self, candidates: list[TradeSignal]) -> None:
        if not candidates:
            return
        if self.config.use_daily_risk_guard:
            risk_guard_active, risk_guard_reason = self.journal.daily_risk_guard_active(
                self.config.max_daily_losses,
                self.config.max_daily_signals,
            )
            if risk_guard_active:
                for signal in candidates:
                    self.journal.log_signal(signal, "skipped_daily_risk_guard", risk_guard_reason)
                LOGGER.info("Daily risk guard active: %s", risk_guard_reason)
                return
        cooldown_status = self.loss_cooldown.status()
        if cooldown_status.active:
            pause_until = cooldown_status.pause_until.isoformat() if cooldown_status.pause_until is not None else ""
            reason = f"global_loss_cooldown_until_{pause_until}"
            for signal in candidates:
                signal.risk_mode = "cooldown"
                self.journal.log_signal(signal, "skipped_loss_cooldown", reason)
            LOGGER.info("Loss cooldown active: %s", "; ".join(cooldown_status.notes or []))
            return
        btc_context = self.get_btc_context()
        eligible: list[TradeSignal] = []
        for signal in candidates:
            if signal.watchlist_tier == "C" and signal.score < 80:
                self.journal.log_signal(signal, "logged_quality_filter", "tier_c_score_below_80")
                LOGGER.info("%s logged only: Tier C score %s below 80", signal.symbol, signal.score)
                continue
            if signal.watchlist_tier == "C" and not signal.volume_spike and not signal.mfi_confirmed:
                self.journal.log_signal(signal, "logged_quality_filter", "tier_c_needs_volume_or_mfi")
                LOGGER.info("%s logged only: Tier C requires volume spike or MFI confirmation", signal.symbol)
                continue
            if signal.score < self.config.score_threshold:
                self.journal.log_signal(signal, "logged_quality_filter", "score_below_threshold")
                LOGGER.info("%s logged only: score %s below threshold %s", signal.symbol, signal.score, self.config.score_threshold)
                continue
            if signal.confidence < self.config.min_confidence:
                self.journal.log_signal(signal, "logged_quality_filter", "confidence_below_minimum")
                LOGGER.info("%s logged only: confidence %s below minimum %s", signal.symbol, signal.confidence, self.config.min_confidence)
                continue
            if signal.rr < self.config.min_rr:
                self.journal.log_signal(signal, "logged_quality_filter", "rr_below_minimum")
                LOGGER.info("%s logged only: RR %.2f below minimum %.2f", signal.symbol, signal.rr, self.config.min_rr)
                continue
            if self.journal.is_recent_loss(signal, self.config.loss_cooldown_minutes):
                self.journal.log_signal(signal, "skipped_loss_cooldown", "recent_sl_same_symbol_direction")
                LOGGER.info("%s skipped: loss cooldown active for %s", signal.symbol, signal.direction)
                continue
            if self.config.use_losing_streak_protection and self.journal.symbol_loss_streak_active(
                signal,
                self.config.max_symbol_loss_streak,
                self.config.symbol_pause_after_loss_minutes,
            ):
                self.journal.log_signal(signal, "skipped_losing_streak", "symbol_loss_streak_pause")
                LOGGER.info("%s skipped: symbol losing streak pause", signal.symbol)
                continue
            if self.apply_btc_regime_filter(signal, btc_context):
                continue
            eligible.append(signal)

        selected = self.select_top_candidates(eligible)
        for signal in selected:
            if self.is_in_cooldown(signal):
                self.journal.log_signal(signal, "skipped_not_top_candidate", "send_cooldown_active")
                LOGGER.info("%s skipped: cooldown active", signal.symbol)
                continue
            position_advice = evaluate_new_signal(signal, self.journal.path)
            if not position_advice.should_send_signal:
                self.journal.log_signal(signal, "skipped_position_management", position_advice.reason)
                if position_advice.message:
                    self.notifier.send_position_message(position_advice.message)
                LOGGER.info("%s skipped: position manager %s", signal.symbol, position_advice.action)
                continue
            if self.ai_commentary.can_summarize(signal, self.config):
                signal.ai_commentary = self.ai_commentary.summarize(signal)
            self.journal.log_signal(signal, "sent", "")
            try:
                self.chart_exporter.export(signal.symbol, self.indicators.add_indicators(self.data_client.fetch_closed_klines(signal.symbol, "1h", 120)), signal)
            except Exception as exc:
                LOGGER.warning("Chart export failed for %s: %s", signal.symbol, exc)
            if self.notifier.send_signal(signal) and not self.config.dry_run:
                self.mark_sent(signal)

    def get_btc_context(self) -> BtcMarketContext | None:
        if not self.config.use_btc_regime_filter:
            return None
        try:
            df_btc = self.indicators.add_indicators(self.data_client.fetch_closed_klines("BTCUSDT", "1h", 120))
            context = detect_btc_regime(df_btc)
            return BtcMarketContext(
                regime=str(context.get("regime", "unclear")),
                allow_long=bool(context.get("allow_long", True)),
                allow_short=bool(context.get("allow_short", True)),
                risk_multiplier=float(context.get("risk_multiplier", 1.0)),
                notes=[str(item) for item in context.get("notes", [])],
            )
        except Exception as exc:
            LOGGER.warning("BTC regime filter unavailable: %s", exc)
            return BtcMarketContext(regime="unclear", notes=["BTC regime unavailable"])

    def apply_btc_regime_filter(self, signal: TradeSignal, btc_context: BtcMarketContext | None) -> bool:
        if not btc_context:
            return False
        signal.btc_regime = btc_context.regime
        signal.btc_regime_notes = "; ".join(btc_context.notes)
        signal.risk_mode = "normal" if btc_context.risk_multiplier >= 1.0 else "reduced"
        if btc_context.risk_multiplier < 1.0:
            signal.risk_amount_usdt *= btc_context.risk_multiplier
            signal.position_size_coin *= btc_context.risk_multiplier
            signal.position_value_usdt *= btc_context.risk_multiplier
        if signal.symbol == "BTCUSDT" or btc_context.regime == "unclear":
            return False

        opposite_direction = (
            (btc_context.regime == "bullish" and signal.direction == "SHORT")
            or (btc_context.regime == "bearish" and signal.direction == "LONG")
        )
        if opposite_direction:
            penalty = max(4, self.config.btc_sideway_penalty // 2)
            signal.confidence = max(1, signal.confidence - penalty)
            signal.score = max(0, signal.score - penalty)
            signal.risk_mode = "reduced"
            signal.reason += f"; BTC {btc_context.regime} opposite-direction penalty -{penalty}"
            if signal.confidence < self.config.min_confidence or signal.score < self.config.score_threshold:
                self.journal.log_signal(signal, "skipped_btc_regime", f"btc_{btc_context.regime}_opposite_direction")
                LOGGER.info("%s skipped: BTC %s opposite-direction filter", signal.symbol, btc_context.regime)
                return True

        if btc_context.regime == "sideways":
            signal.confidence = max(1, signal.confidence - self.config.btc_sideway_penalty)
            signal.reason += f"; BTC sideways penalty -{self.config.btc_sideway_penalty}"
            if signal.confidence < self.config.min_confidence:
                self.journal.log_signal(signal, "skipped_btc_regime", "btc_sideways_confidence_below_minimum")
                LOGGER.info("%s skipped: BTC sideways regime filter", signal.symbol)
                return True

        if btc_context.regime == "high_volatility":
            signal.confidence = max(1, signal.confidence - self.config.btc_sideway_penalty)
            signal.score = max(0, signal.score - self.config.btc_sideway_penalty)
            signal.reason += f"; BTC high-volatility risk reduction -{self.config.btc_sideway_penalty}"
            weak_signal = signal.confidence < self.config.min_confidence or signal.score < self.config.score_threshold
            if weak_signal:
                self.journal.log_signal(signal, "skipped_btc_regime", "btc_high_volatility_weak_signal")
                LOGGER.info("%s skipped: BTC high-volatility weak signal", signal.symbol)
                return True
        return False

    def select_top_candidates(self, candidates: list[TradeSignal]) -> list[TradeSignal]:
        major_symbols = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "LTCUSDT"}
        ranked = sorted(
            candidates,
            key=lambda item: (
                item.score,
                item.confidence,
                {"A": 3, "B": 2, "C": 1}.get(item.watchlist_tier, 2),
                int(item.volume_spike),
                item.rr,
            ),
            reverse=True,
        )
        selected: list[TradeSignal] = []
        direction_counts: dict[str, int] = {}
        major_direction_counts: dict[str, int] = {}
        for signal in ranked:
            direction_count = direction_counts.get(signal.direction, 0)
            major_count = major_direction_counts.get(signal.direction, 0)
            if direction_count >= self.config.max_signals_per_direction_per_candle:
                self.journal.log_signal(signal, "skipped_correlation", "max_signals_per_direction_per_candle")
                LOGGER.info("%s skipped: direction cap/correlation filter", signal.symbol)
                continue
            if signal.symbol in major_symbols and major_count >= self.config.max_major_correlated_signals:
                self.journal.log_signal(signal, "skipped_correlation", "max_major_correlated_signals")
                LOGGER.info("%s skipped: major correlation filter", signal.symbol)
                continue
            if len(selected) >= self.config.max_signals_per_scan:
                self.journal.log_signal(signal, "skipped_not_top_candidate", "not_in_top_candidates")
                LOGGER.info("%s skipped: not a top candidate", signal.symbol)
                continue
            selected.append(signal)
            direction_counts[signal.direction] = direction_count + 1
            if signal.symbol in major_symbols:
                major_direction_counts[signal.direction] = major_count + 1
        return selected


def main() -> None:
    config = ScannerConfig.from_env()
    if len(config.watchlist) != 30:
        LOGGER.warning("WATCHLIST has %s symbols; tier mode expects about 30", len(config.watchlist))
    AgentRunner(config).run_forever()


if __name__ == "__main__":
    main()
