# -*- coding: utf-8 -*-
"""
Crypto Multi-Coin Scanner for Binance Futures.

The rule engine owns signal decisions. Gemini/OpenAI-style AI commentary is
optional and only summarizes the reason after the score already passes.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

try:
    from google import genai
except Exception:  # pragma: no cover - optional dependency path
    genai = None


BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "signal_state.json"
LOG_FILE = BASE_DIR / "cornix_agent.log"


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

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

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
    cooldown_minutes: int
    run_once: bool
    dry_run: bool
    use_ai_commentary: bool
    send_telegram: bool
    close_delay_seconds: int
    risk_per_trade_pct: float
    account_balance_usdt: float
    min_rr: float
    max_leverage: int
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
            cooldown_minutes=env_int("COOLDOWN_MINUTES", 180),
            run_once=env_bool("RUN_ONCE", False),
            dry_run=env_bool("DRY_RUN", False),
            use_ai_commentary=env_bool("USE_AI_COMMENTARY", True),
            send_telegram=env_bool("SEND_TELEGRAM", True),
            close_delay_seconds=env_int("CLOSE_DELAY_SECONDS", 20),
            risk_per_trade_pct=env_float("RISK_PER_TRADE_PCT", 1.0),
            account_balance_usdt=env_float("ACCOUNT_BALANCE_USDT", 1000.0),
            min_rr=env_float("MIN_RR", 1.2),
            max_leverage=env_int("MAX_LEVERAGE", 10),
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
    volume_spike: bool
    reason: str
    ai_commentary: str = ""
    risk_amount_usdt: float = 0.0
    position_size_coin: float = 0.0
    position_value_usdt: float = 0.0


class SymbolFormatter:
    @staticmethod
    def to_binance_symbol(symbol: str) -> str:
        cleaned = symbol.strip().upper()
        cleaned = cleaned.replace("BINANCE:", "").replace(".P", "")
        cleaned = cleaned.replace("/", "").replace("-", "")
        return cleaned

    @staticmethod
    def to_tradingview_symbol(symbol: str) -> str:
        return f"BINANCE:{SymbolFormatter.to_binance_symbol(symbol)}.P"


class MarketDataClient:
    BASE_URL = "https://fapi.binance.com/fapi/v1/klines"

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()

    def fetch_klines(self, symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
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


class IndicatorEngine:
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

    def add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        enriched = df.copy()
        enriched["rsi14"] = self.rsi(enriched["close"])
        enriched["ema9"] = enriched["close"].ewm(span=9, adjust=False).mean()
        enriched["ema20"] = enriched["close"].ewm(span=20, adjust=False).mean()
        enriched["ema21"] = enriched["close"].ewm(span=21, adjust=False).mean()
        enriched["ema50"] = enriched["close"].ewm(span=50, adjust=False).mean()
        enriched["atr14"] = self.atr(enriched)
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


class SignalScorer:
    def __init__(self, support_resistance: SupportResistanceEngine, regime_detector: MarketRegimeDetector) -> None:
        self.support_resistance = support_resistance
        self.regime_detector = regime_detector

    def score(self, symbol: str, df_1h: pd.DataFrame, df_15m: pd.DataFrame) -> TradeSignal | None:
        latest_1h = df_1h.iloc[-1]
        latest_15m = df_15m.iloc[-1]
        previous_20 = df_1h.iloc[-21:-1]
        support, resistance = self.support_resistance.calculate(df_1h)
        regime = self.regime_detector.detect(df_1h)

        price = float(latest_1h["close"])
        atr = float(latest_1h["atr14"])
        if math.isnan(atr) or atr <= 0:
            return None

        trend_long = latest_1h["close"] > latest_1h["ema20"] > latest_1h["ema50"]
        trend_short = latest_1h["close"] < latest_1h["ema20"] < latest_1h["ema50"]
        entry_long = latest_15m["close"] > latest_15m["ema9"] > latest_15m["ema21"] and latest_15m["rsi14"] >= 52
        entry_short = latest_15m["close"] < latest_15m["ema9"] < latest_15m["ema21"] and latest_15m["rsi14"] <= 48
        volume_spike = latest_1h["volume"] >= latest_1h["volume_sma20"] * 1.2
        breakout_long = latest_1h["close"] > previous_20["high"].max()
        breakout_short = latest_1h["close"] < previous_20["low"].min()

        long_score = self._direction_score(
            trend=trend_long,
            entry=entry_long,
            volume_spike=volume_spike,
            breakout=breakout_long,
            regime=regime.name,
            rsi=float(latest_1h["rsi14"]),
            direction="LONG",
        )
        short_score = self._direction_score(
            trend=trend_short,
            entry=entry_short,
            volume_spike=volume_spike,
            breakout=breakout_short,
            regime=regime.name,
            rsi=float(latest_1h["rsi14"]),
            direction="SHORT",
        )

        if long_score < 1 and short_score < 1:
            return None

        direction = "LONG" if long_score >= short_score else "SHORT"
        score = max(long_score, short_score)
        if direction == "LONG":
            entry = price
            sl = entry - atr * 1.15
            tp1 = entry + atr * 1.15
            tp2 = entry + atr * 1.90
        else:
            entry = price
            sl = entry + atr * 1.15
            tp1 = entry - atr * 1.15
            tp2 = entry - atr * 1.90

        risk = abs(entry - sl)
        reward = abs(tp2 - entry)
        rr = reward / risk if risk > 0 else 0
        confidence = min(95, max(1, score + (5 if rr >= 1.4 else 0)))
        reason = (
            f"{regime.name}; 1H trend {'ok' if (trend_long if direction == 'LONG' else trend_short) else 'weak'}; "
            f"15m confirmation {'ok' if (entry_long if direction == 'LONG' else entry_short) else 'weak'}; "
            f"volume spike {'yes' if volume_spike else 'no'}; RR {rr:.2f}"
        )

        return TradeSignal(
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
            volume_spike=volume_spike,
            reason=reason,
        )

    @staticmethod
    def _direction_score(
        trend: bool,
        entry: bool,
        volume_spike: bool,
        breakout: bool,
        regime: str,
        rsi: float,
        direction: str,
    ) -> int:
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
            score -= 5

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
        position_value = size_coin * signal.entry

        signal.risk_amount_usdt = risk_amount
        signal.position_size_coin = size_coin
        signal.position_value_usdt = position_value
        return signal


class AICommentaryEngine:
    def __init__(self, config: ScannerConfig) -> None:
        self.config = config
        self.client = None
        if config.use_ai_commentary and config.gemini_api_key and genai:
            self.client = genai.Client(api_key=config.gemini_api_key)

    def summarize(self, signal: TradeSignal) -> str:
        if not self.client:
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
        response = self.client.models.generate_content(
            model=self.config.gemini_model,
            contents=prompt,
        )
        return (response.text or "").strip().replace("\n", " ")[:240]


class TelegramNotifier:
    def __init__(self, config: ScannerConfig) -> None:
        self.config = config
        self.session = requests.Session()

    def build_message(self, signal: TradeSignal) -> str:
        commentary = f"\nAI Note: {signal.ai_commentary}" if signal.ai_commentary else ""
        return (
            f"Crypto Multi-Coin Scanner\n"
            f"{signal.tradingview_symbol} | TF 1H\n\n"
            f"Coin: {signal.symbol}\n"
            f"Direction: {signal.direction}\n"
            f"Exchange: Binance Futures\n"
            f"Leverage: Cross {self.config.max_leverage}x\n"
            f"Entry: {format_price(signal.entry)}\n"
            f"Take Profit 1: {format_price(signal.tp1)}\n"
            f"Take Profit 2: {format_price(signal.tp2)}\n"
            f"Stop Target: {format_price(signal.sl)}\n\n"
            f"Support: {format_price(signal.support)}\n"
            f"Resistance: {format_price(signal.resistance)}\n"
            f"RR: {signal.rr:.2f}\n"
            f"Confidence: {signal.confidence}%\n"
            f"Score: {signal.score}\n"
            f"Regime: {signal.regime}\n"
            f"Volume Spike: {'YES' if signal.volume_spike else 'NO'}\n"
            f"Risk: {signal.risk_amount_usdt:.2f} USDT ({self.config.risk_per_trade_pct:.2f}%)\n"
            f"Position Size: {signal.position_size_coin:.6f} {signal.symbol.replace('USDT', '')}\n"
            f"Reason: {signal.reason}"
            f"{commentary}"
        )

    def send(self, signal: TradeSignal) -> bool:
        message = self.build_message(signal)
        if self.config.dry_run:
            LOGGER.info("DRY_RUN Telegram message for %s:\n%s", signal.symbol, message)
            return True
        if not self.config.send_telegram:
            LOGGER.info("SEND_TELEGRAM=0, skipped Telegram for %s", signal.symbol)
            return True
        if not self.config.telegram_bot_token or not self.config.telegram_chat_id:
            LOGGER.warning("Telegram credentials are missing; skipped %s", signal.symbol)
            return False

        url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage"
        payload = {"chat_id": self.config.telegram_chat_id, "text": message}
        response = self.session.post(url, data=payload, timeout=15)
        if response.status_code != 200:
            LOGGER.error("Telegram send failed for %s: %s", signal.symbol, response.text)
            return False
        LOGGER.info("Telegram signal sent for %s %s", signal.symbol, signal.direction)
        return True


class AgentRunner:
    def __init__(self, config: ScannerConfig) -> None:
        self.config = config
        self.data_client = MarketDataClient()
        self.indicators = IndicatorEngine()
        self.scorer = SignalScorer(SupportResistanceEngine(), MarketRegimeDetector())
        self.risk_manager = RiskManager(config)
        self.ai_commentary = AICommentaryEngine(config)
        self.notifier = TelegramNotifier(config)
        self.state = self._load_state()

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
        key = f"{signal.symbol}:{signal.direction}"
        last_sent = self.state.get(key)
        if not last_sent:
            return False
        elapsed = time.time() - float(last_sent)
        return elapsed < self.config.cooldown_minutes * 60

    def mark_sent(self, signal: TradeSignal) -> None:
        self.state[f"{signal.symbol}:{signal.direction}"] = time.time()
        self._save_state()

    def seconds_until_next_1h_close(self) -> int:
        now = datetime.now(timezone.utc)
        seconds_into_hour = now.minute * 60 + now.second
        wait_seconds = 3600 - seconds_into_hour + self.config.close_delay_seconds
        return max(self.config.close_delay_seconds, wait_seconds)

    def run_forever(self) -> None:
        LOGGER.info("Watchlist: %s", ", ".join(self.config.watchlist))
        LOGGER.info(
            "Threshold=%s min_rr=%.2f cooldown=%sm dry_run=%s ai_commentary=%s",
            self.config.score_threshold,
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
        LOGGER.info("Scanning latest closed 1H candles")
        for symbol in self.config.watchlist:
            try:
                self.scan_symbol(symbol)
            except Exception as exc:
                LOGGER.exception("Scan failed for %s: %s", symbol, exc)
            time.sleep(0.4)

    def scan_symbol(self, symbol: str) -> None:
        df_1h = self.indicators.add_indicators(self.data_client.fetch_closed_klines(symbol, "1h", 200))
        df_15m = self.indicators.add_indicators(self.data_client.fetch_closed_klines(symbol, "15m", 200))
        signal = self.scorer.score(symbol, df_1h, df_15m)
        if not signal:
            LOGGER.info("%s WAIT: no valid setup", symbol)
            return
        if signal.score < self.config.score_threshold:
            LOGGER.info("%s WAIT: score %s below threshold %s", symbol, signal.score, self.config.score_threshold)
            return
        if signal.rr < self.config.min_rr:
            LOGGER.info("%s WAIT: RR %.2f below minimum %.2f", symbol, signal.rr, self.config.min_rr)
            return
        if self.is_in_cooldown(signal):
            LOGGER.info("%s skipped: cooldown active for %s", symbol, signal.direction)
            return

        signal = self.risk_manager.apply(signal)
        try:
            signal.ai_commentary = self.ai_commentary.summarize(signal)
        except Exception as exc:
            LOGGER.warning("AI commentary failed for %s: %s", symbol, exc)

        if self.notifier.send(signal) and not self.config.dry_run:
            self.mark_sent(signal)


def main() -> None:
    config = ScannerConfig.from_env()
    if len(config.watchlist) != 10:
        LOGGER.warning("WATCHLIST has %s symbols; expected 10", len(config.watchlist))
    AgentRunner(config).run_forever()


if __name__ == "__main__":
    main()
