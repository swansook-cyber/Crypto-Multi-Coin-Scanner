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
import time
from dataclasses import dataclass
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
    score_threshold: int
    min_confidence: int
    min_rr: float
    cooldown_minutes: int
    confidence_override_delta: int
    loss_cooldown_minutes: int
    max_signals_per_scan: int
    max_signals_per_direction_per_candle: int
    max_major_correlated_signals: int
    use_btc_regime_filter: bool
    btc_sideway_penalty: int
    btc_low_vol_skip: bool
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

    @classmethod
    def from_env(cls) -> "ScannerConfig":
        default_watchlist = (
            "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,"
            "LTCUSDT,ZECUSDT,HYPEUSDT,LABUSDT"
        )
        watchlist = [
            SymbolFormatter.to_binance_symbol(item)
            for item in os.getenv("WATCHLIST", default_watchlist).split(",")
            if item.strip()
        ]
        return cls(
            watchlist=watchlist[:10],
            score_threshold=env_int("SCORE_THRESHOLD", 70),
            min_confidence=env_int("MIN_CONFIDENCE", 75),
            min_rr=env_float("MIN_RR", 1.8),
            cooldown_minutes=env_int("COOLDOWN_MINUTES", 240),
            confidence_override_delta=env_int("CONFIDENCE_OVERRIDE_DELTA", 12),
            loss_cooldown_minutes=env_int("LOSS_COOLDOWN_MINUTES", 180),
            max_signals_per_scan=env_int("MAX_SIGNALS_PER_SCAN", 3),
            max_signals_per_direction_per_candle=env_int("MAX_SIGNALS_PER_DIRECTION_PER_CANDLE", 2),
            max_major_correlated_signals=env_int("MAX_MAJOR_CORRELATED_SIGNALS", 1),
            use_btc_regime_filter=env_bool("USE_BTC_REGIME_FILTER", True),
            btc_sideway_penalty=env_int("BTC_SIDEWAY_PENALTY", 10),
            btc_low_vol_skip=env_bool("BTC_LOW_VOL_SKIP", True),
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
        )


@dataclass
class MarketRegime:
    name: str
    details: str


@dataclass
class TradeSignal:
    timestamp: datetime
    symbol: str
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
    volume_spike: bool
    volume_ratio: float
    atr_pct: float
    mfi: float
    mfi_confirmed: bool
    reason: str
    liquidation_context: str = ""
    ai_commentary: str = ""
    risk_amount_usdt: float = 0.0
    position_size_coin: float = 0.0
    position_value_usdt: float = 0.0
    chart_path: Path | None = None


@dataclass
class BtcMarketContext:
    regime: str
    atr_pct: float


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

    def score(self, symbol: str, df_1h: pd.DataFrame, df_15m: pd.DataFrame, fear_greed: int | None = None) -> TradeSignal | None:
        latest_1h = df_1h.iloc[-1]
        latest_15m = df_15m.iloc[-1]
        previous_20 = df_1h.iloc[-21:-1]
        support, resistance = self.support_resistance.calculate(df_1h)
        regime = self.regime_detector.detect(df_1h)

        price = float(latest_1h["close"])
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

        if long_score < 1 and short_score < 1:
            return None

        direction = "LONG" if long_score >= short_score else "SHORT"
        score = max(long_score, short_score)
        mfi_confirmed = long_mfi_confirmed if direction == "LONG" else short_mfi_confirmed
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
        if self.config.use_mfi_filter and not mfi_confirmed:
            confidence = max(1, confidence - 4)
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
            f"volume ratio {volume_ratio:.2f}; ATR {atr_pct:.2f}%; {mfi_reason}; RR {rr:.2f}"
        )

        return TradeSignal(
            timestamp=datetime.now(timezone.utc),
            symbol=symbol,
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
            volume_spike=volume_spike,
            volume_ratio=volume_ratio,
            atr_pct=atr_pct,
            mfi=mfi,
            mfi_confirmed=mfi_confirmed,
            liquidation_context=liquidation_context,
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

    def summarize(self, signal: TradeSignal) -> str:
        if not self.client or self.disabled_for_run:
            return ""
        prompt = f"""
Summarize this Binance Futures rule-based signal in Thai in one short sentence.
Do not change direction, entry, TP, SL, RR, or confidence. Do not add financial guarantees.

Symbol: {signal.symbol}
Direction: {signal.direction}
Score: {signal.score}
Confidence: {signal.confidence}
Regime: {signal.regime}
Volume spike: {signal.volume_spike}
Reason: {signal.reason}
"""
        try:
            self.calls_this_run += 1
            response = self.client.models.generate_content(model=self.config.gemini_model, contents=prompt)
            return (response.text or "").strip().replace("\n", " ")[:240]
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
        "risk_reward", "confidence", "market_regime", "volume_spike", "score", "mfi", "mfi_confirmed", "ai_summary",
        "signal_status", "skip_reason",
        "result", "hit_target", "closed_at", "max_profit_pct", "max_drawdown_pct",
        "outcome_alert_sent", "outcome_alert_at",
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
            "signal_status": "",
            "skip_reason": "",
            "outcome_alert_sent": 0,
            "outcome_alert_at": "",
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
                    "market_regime": signal.regime,
                    "volume_spike": "YES" if signal.volume_spike else "NO",
                    "score": signal.score,
                    "mfi": f"{signal.mfi:.2f}",
                    "mfi_confirmed": "YES" if signal.mfi_confirmed else "NO",
                    "ai_summary": signal.ai_commentary,
                    "signal_status": signal_status,
                    "skip_reason": skip_reason,
                    "result": result,
                    "hit_target": "",
                    "closed_at": "",
                    "max_profit_pct": "",
                    "max_drawdown_pct": "",
                    "outcome_alert_sent": 0,
                    "outcome_alert_at": "",
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
            "top_symbols": ", ".join(f"{symbol} ({count})" for symbol, count in top_symbols.items()),
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
        ax.set_title(f"{signal.tradingview_symbol} {signal.direction} | RR {signal.rr:.2f} | Confidence {signal.confidence}%")
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

    def build_message(self, signal: TradeSignal) -> str:
        signal_emoji = "🚀" if signal.direction == "LONG" else "🔻"
        volume_text = "YES" if signal.volume_spike else "NO"
        commentary = f"\n\n🧠 AI Summary:\n{signal.ai_commentary}" if signal.ai_commentary else ""
        return (
            f"{signal_emoji} {signal.direction} SIGNAL\n"
            f"🪙 {SymbolFormatter.to_display_symbol(signal.symbol)}\n\n"
            f"💰 Entry: {format_price(signal.entry)}\n"
            f"🛑 SL: {format_price(signal.sl)}\n\n"
            f"🎯 TP1: {format_price(signal.tp1)}\n"
            f"🎯 TP2: {format_price(signal.tp2)}\n\n"
            f"📈 RR: 1:{signal.rr:.2f}\n"
            f"🔥 Confidence: {signal.confidence}%\n"
            f"⭐ Score: {signal.score}\n\n"
            f"📊 Market: {signal.regime}\n"
            f"📦 Volume Spike: {volume_text}\n"
            f"📈 MFI: {signal.mfi:.1f}\n"
            f"🧱 Support: {format_price(signal.support)}\n"
            f"🧱 Resistance: {format_price(signal.resistance)}\n\n"
            f"⚖️ Risk: {signal.risk_amount_usdt:.2f} USDT ({self.config.risk_per_trade_pct:.2f}%)\n"
            f"📐 Size: {signal.position_size_coin:.6f} {signal.symbol.replace('USDT', '')}\n\n"
            f"🧠 Reason:\n{signal.reason}"
            f"{commentary}\n\n"
            f"Exchange: Binance Futures\n"
            f"Leverage: Cross {self.config.max_leverage}x\n"
            f"TradingView: {signal.tradingview_symbol}"
        )

    def build_daily_summary_message(self, summary: dict[str, Any]) -> str:
        return (
            f"📅 Daily Signal Summary UTC\n"
            f"Date: {summary.get('day')}\n\n"
            f"📌 Total signals: {summary.get('total', 0)}\n"
            f"✅ Sent signals: {summary.get('sent_total', 0)}\n"
            f"🚀 Long: {summary.get('long_count', 0)}\n"
            f"🔻 Short: {summary.get('short_count', 0)}\n"
            f"🔥 Avg confidence: {summary.get('avg_confidence', 0):.1f}%\n"
            f"📈 Avg RR: {summary.get('avg_rr', 0):.2f}\n"
            f"📈 Avg MFI: {summary.get('avg_mfi', 0):.1f}\n"
            f"🔥 MFI-confirmed signals: {summary.get('mfi_confirmed_count', 0)}\n"
            f"🧊 Skipped loss cooldown: {summary.get('skipped_loss_cooldown', 0)}\n"
            f"🔗 Skipped correlation: {summary.get('skipped_correlation', 0)}\n"
            f"₿ Skipped BTC regime: {summary.get('skipped_btc_regime', 0)}\n"
            f"🏅 Skipped not top: {summary.get('skipped_not_top_candidate', 0)}\n"
            f"🏆 Top symbols: {summary.get('top_symbols') or '-'}"
        )

    def send_signal(self, signal: TradeSignal) -> bool:
        message = self.build_message(signal)
        if self.config.dry_run:
            LOGGER.info("DRY_RUN Telegram signal for %s:\n%s", signal.symbol, message)
            if signal.chart_path:
                LOGGER.info("DRY_RUN chart path: %s", signal.chart_path)
            return True
        if not self.config.send_telegram:
            LOGGER.info("SEND_TELEGRAM=0, skipped Telegram for %s", signal.symbol)
            return True
        if not self.config.telegram_bot_token or not self.config.telegram_chat_id:
            LOGGER.warning("Telegram credentials are missing; skipped %s", signal.symbol)
            return False

        if signal.chart_path and signal.chart_path.exists():
            return self._send_photo(signal.chart_path, message)
        return self._send_message(message)

    def send_daily_summary(self, summary: dict[str, Any]) -> bool:
        message = self.build_daily_summary_message(summary)
        if self.config.dry_run:
            LOGGER.info("DRY_RUN daily summary:\n%s", message)
            return True
        if not self.config.send_telegram or not self.config.send_daily_summary:
            return True
        return self._send_message(message)

    def _send_message(self, message: str) -> bool:
        url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage"
        payload = {"chat_id": self.config.telegram_chat_id, "text": message}
        response = self.session.post(url, data=payload, timeout=20)
        if response.status_code != 200:
            LOGGER.error("Telegram message failed: %s", response.text)
            return False
        return True

    def _send_photo(self, chart_path: Path, caption: str) -> bool:
        url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendPhoto"
        with chart_path.open("rb") as image:
            files = {"photo": image}
            data = {"chat_id": self.config.telegram_chat_id, "caption": caption[:1024]}
            response = self.session.post(url, data=data, files=files, timeout=30)
        if response.status_code != 200:
            LOGGER.error("Telegram photo failed: %s", response.text)
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
        if elapsed >= self.config.cooldown_minutes * 60:
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
        LOGGER.info("Watchlist: %s", ", ".join(self.config.watchlist))
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
            except Exception as exc:
                LOGGER.exception("Scan failed for %s: %s", symbol, exc)
            time.sleep(self.config.request_delay_seconds)

        self.process_candidates(candidates)

    def scan_symbol(self, symbol: str) -> TradeSignal | None:
        df_1h = self.indicators.add_indicators(self.data_client.fetch_closed_klines(symbol, "1h", 200))
        df_15m = self.indicators.add_indicators(self.data_client.fetch_closed_klines(symbol, "15m", 200))
        signal = self.scorer.score(symbol, df_1h, df_15m, self.fear_greed_value)
        if not signal:
            LOGGER.info("%s WAIT: no valid setup", symbol)
            return None

        signal = self.risk_manager.apply(signal)
        signal.ai_commentary = ""
        return signal

    def process_candidates(self, candidates: list[TradeSignal]) -> None:
        if not candidates:
            return
        btc_context = self.get_btc_context()
        eligible: list[TradeSignal] = []
        for signal in candidates:
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
            if self.apply_btc_regime_filter(signal, btc_context):
                continue
            eligible.append(signal)

        selected = self.select_top_candidates(eligible)
        for signal in selected:
            if self.is_in_cooldown(signal):
                self.journal.log_signal(signal, "skipped_not_top_candidate", "send_cooldown_active")
                LOGGER.info("%s skipped: cooldown active", signal.symbol)
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
            regime = MarketRegimeDetector().detect(df_btc)
            return BtcMarketContext(regime=regime.name, atr_pct=float(df_btc.iloc[-1]["atr_pct"]))
        except Exception as exc:
            LOGGER.warning("BTC regime filter unavailable: %s", exc)
            return None

    def apply_btc_regime_filter(self, signal: TradeSignal, btc_context: BtcMarketContext | None) -> bool:
        if not btc_context or signal.symbol == "BTCUSDT":
            return False
        if btc_context.regime == "Sideway":
            signal.confidence = max(1, signal.confidence - self.config.btc_sideway_penalty)
            signal.reason += f"; BTC sideway penalty -{self.config.btc_sideway_penalty}"
            if signal.confidence < self.config.min_confidence:
                self.journal.log_signal(signal, "skipped_btc_regime", "btc_sideway_confidence_below_minimum")
                LOGGER.info("%s skipped: BTC sideway regime filter", signal.symbol)
                return True
        if self.config.btc_low_vol_skip and btc_context.atr_pct < self.config.min_atr_pct:
            self.journal.log_signal(signal, "skipped_btc_regime", "btc_low_volatility")
            LOGGER.info("%s skipped: BTC low volatility filter", signal.symbol)
            return True
        return False

    def select_top_candidates(self, candidates: list[TradeSignal]) -> list[TradeSignal]:
        major_symbols = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "LTCUSDT"}
        ranked = sorted(
            candidates,
            key=lambda item: (item.score, item.confidence, int(item.volume_spike), item.rr),
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
    if len(config.watchlist) != 10:
        LOGGER.warning("WATCHLIST has %s symbols; expected 10", len(config.watchlist))
    AgentRunner(config).run_forever()


if __name__ == "__main__":
    main()
