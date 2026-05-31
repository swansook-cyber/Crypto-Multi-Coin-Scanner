# -*- coding: utf-8 -*-
"""Approved-only analyzer/router for forwarded external VIP signals."""

from __future__ import annotations

import csv
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from core.btc_regime_filter import detect_btc_regime
from cornix_agent import (
    IndicatorEngine,
    MarketDataClient,
    MarketRegimeDetector,
    SupportResistanceEngine,
    alignment_for_direction,
    classify_trend,
)


BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
EXTERNAL_SIGNALS_CSV = LOG_DIR / "external_signals.csv"
LOGGER = logging.getLogger("external_signal_analyzer")

FIELDNAMES = [
    "timestamp_utc",
    "source",
    "message_id",
    "symbol",
    "side",
    "entry_low",
    "entry_high",
    "stop_loss",
    "tp1",
    "tp2",
    "tp3",
    "raw_targets",
    "leverage",
    "raw_text",
    "parse_status",
    "analysis_score",
    "recommendation",
    "reason",
    "rr",
    "refine_status",
    "refine_score",
    "scanner_agreement",
    "scanner_direction",
    "conflict_reason",
    "trend_1h",
    "entry_15m",
    "htf_regime",
    "htf_alignment",
    "mfi",
    "atr_pct",
    "support",
    "resistance",
    "btc_regime",
    "volume_ratio",
    "volume_spike",
    "market_regime",
    "sent_to_signals",
    "sent_to_cornix",
]


@dataclass
class ParsedExternalSignal:
    source: str = "VIP Forwarded Signal"
    message_id: int | str = ""
    raw_text: str = ""
    symbol: str = ""
    side: str = ""
    entry_low: float | None = None
    entry_high: float | None = None
    stop_loss: float | None = None
    targets: list[float] = field(default_factory=list)
    leverage: str = ""
    parse_status: str = "FAILED"
    parse_errors: list[str] = field(default_factory=list)


@dataclass
class ExternalSignalAnalysis:
    parsed: ParsedExternalSignal
    analysis_score: int = 0
    recommendation: str = "FAILED"
    reason: list[str] = field(default_factory=list)
    rr: float = 0.0
    refine_status: str = "NOT_RUN"
    refine_score: int = 0
    scanner_agreement: str = "UNKNOWN"
    scanner_direction: str = "UNKNOWN"
    conflict_reason: str = ""
    refine_details: dict[str, Any] = field(default_factory=dict)
    sent_to_signals: bool = False
    sent_to_cornix: bool = False


@dataclass
class RefineResult:
    status: str = "FAILED"
    score: int = 0
    scanner_agreement: str = "NO"
    scanner_direction: str = "UNKNOWN"
    conflict_reason: str = ""
    reason: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


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


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def format_price(value: float | None) -> str:
    if value is None:
        return "-"
    if abs(value) >= 1000:
        return f"{value:.2f}"
    if abs(value) >= 10:
        return f"{value:.3f}"
    if abs(value) >= 1:
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return f"{value:.6f}".rstrip("0").rstrip(".")


def normalize_symbol(raw_text: str) -> str:
    match = re.search(r"#?\b([A-Z0-9]{2,12})\s*/?\s*USDT\b", raw_text.upper())
    if not match:
        return ""
    base = match.group(1).replace("/", "")
    if base == "USDT":
        return ""
    return f"{base}USDT"


def normalize_side(raw_text: str) -> str:
    text = raw_text.upper()
    if re.search(r"\b(LONG|BUY)\b", text):
        return "LONG"
    if re.search(r"\b(SHORT|SELL)\b", text):
        return "SHORT"
    return ""


def _numbers(text: str) -> list[float]:
    values = []
    for item in re.findall(r"\d+(?:\.\d+)?", text.replace(",", "")):
        try:
            values.append(float(item))
        except ValueError:
            continue
    return values


def _line_values(lines: list[str], labels: tuple[str, ...], max_lines: int = 2) -> list[float]:
    values: list[float] = []
    for index, line in enumerate(lines):
        lower = line.lower()
        if any(label in lower for label in labels):
            values.extend(_numbers(line))
            for extra in range(1, max_lines + 1):
                if index + extra < len(lines):
                    next_line = lines[index + extra]
                    if re.search(r"[A-Za-z]", next_line) and not re.match(r"^\s*(tp|target|\d|[-. ])", next_line, re.I):
                        break
                    values.extend(_numbers(next_line))
    return values


def parse_external_signal(raw_text: str, message_id: int | str = "", source: str = "VIP Forwarded Signal") -> ParsedExternalSignal:
    text = raw_text or ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    parsed = ParsedExternalSignal(source=source, message_id=message_id, raw_text=text)
    parsed.symbol = normalize_symbol(text)
    parsed.side = normalize_side(text)

    entry_values = _line_values(lines, ("entry", "entries", "buy zone", "sell zone"), max_lines=2)
    if entry_values:
        parsed.entry_low = min(entry_values[:2]) if len(entry_values) >= 2 else entry_values[0]
        parsed.entry_high = max(entry_values[:2]) if len(entry_values) >= 2 else entry_values[0]

    stop_values = _line_values(lines, ("stop loss", "stoploss", "stop", "sl"), max_lines=1)
    if stop_values:
        parsed.stop_loss = stop_values[0]

    target_values = _line_values(lines, ("targets", "target", "take profits", "take profit", "tp"), max_lines=5)
    # Remove accidental leverage-like values when target lines include words such as TP1.
    filtered_targets = [value for value in target_values if value != 1 and value != 2 and value != 3]
    seen: list[float] = []
    for value in filtered_targets:
        if value not in seen:
            seen.append(value)
    parsed.targets = seen[:3]

    leverage_match = re.search(r"\b(?:leverage|lev)\s*[:\-]?\s*(\d{1,3})\s*x\b|\b(\d{1,3})\s*x\b", text, re.I)
    if leverage_match:
        parsed.leverage = f"{leverage_match.group(1) or leverage_match.group(2)}x"

    if not parsed.symbol:
        parsed.parse_errors.append("symbol missing")
    if not parsed.side:
        parsed.parse_errors.append("side missing")
    if parsed.entry_low is None or parsed.entry_high is None:
        parsed.parse_errors.append("entry missing")
    if parsed.stop_loss is None:
        parsed.parse_errors.append("stop loss missing")
    if not parsed.targets:
        parsed.parse_errors.append("target missing")
    parsed.parse_status = "SUCCESS" if not parsed.parse_errors else "FAILED"
    LOGGER.info(
        "External signal parsed: message_id=%s status=%s symbol=%s side=%s errors=%s",
        message_id,
        parsed.parse_status,
        parsed.symbol or "-",
        parsed.side or "-",
        ",".join(parsed.parse_errors) if parsed.parse_errors else "-",
    )
    return parsed


def entry_mid(parsed: ParsedExternalSignal) -> float:
    if parsed.entry_low is None and parsed.entry_high is None:
        return 0.0
    if parsed.entry_low is None:
        return float(parsed.entry_high or 0)
    if parsed.entry_high is None:
        return float(parsed.entry_low or 0)
    return (parsed.entry_low + parsed.entry_high) / 2


def calculate_rr(parsed: ParsedExternalSignal) -> float:
    if parsed.entry_low is None or parsed.entry_high is None or parsed.stop_loss is None or not parsed.targets:
        return 0.0
    entry = entry_mid(parsed)
    tp1 = parsed.targets[0]
    if parsed.side == "LONG":
        risk = entry - parsed.stop_loss
        reward = tp1 - entry
    elif parsed.side == "SHORT":
        risk = parsed.stop_loss - entry
        reward = entry - tp1
    else:
        return 0.0
    if risk <= 0 or reward <= 0:
        return 0.0
    return reward / risk


def _latest_float(df: pd.DataFrame, column: str, default: float = 0.0) -> float:
    if df.empty or column not in df.columns:
        return default
    value = pd.to_numeric(pd.Series([df.iloc[-1].get(column)]), errors="coerce").iloc[0]
    if pd.isna(value):
        return default
    return float(value)


def _direction_from_context(trend_long: bool, trend_short: bool, entry_long: bool, entry_short: bool) -> str:
    if trend_long and entry_long:
        return "LONG"
    if trend_short and entry_short:
        return "SHORT"
    if trend_long and not trend_short:
        return "LONG_BIAS"
    if trend_short and not trend_long:
        return "SHORT_BIAS"
    return "UNKNOWN"


def perform_refine_analysis(parsed: ParsedExternalSignal) -> RefineResult:
    """Run fresh scanner-style market analysis for an external VIP signal."""
    try:
        data_client = MarketDataClient(env_float("REQUEST_DELAY_SECONDS", 0.0))
        indicators = IndicatorEngine(env_int("MFI_PERIOD", 14))
        sr_engine = SupportResistanceEngine()
        regime_detector = MarketRegimeDetector()

        trend_tf = os.getenv("TREND_TIMEFRAME", "1h")
        entry_tf = os.getenv("ENTRY_TIMEFRAME", "15m")
        htf_tf = os.getenv("HTF_TIMEFRAME", "4h")

        df_1h = indicators.add_indicators(data_client.fetch_closed_klines(parsed.symbol, trend_tf, 200))
        df_15m = indicators.add_indicators(data_client.fetch_closed_klines(parsed.symbol, entry_tf, 200))
        df_htf = indicators.add_indicators(data_client.fetch_closed_klines(parsed.symbol, htf_tf, 200))
        df_btc = indicators.add_indicators(data_client.fetch_closed_klines("BTCUSDT", trend_tf, 120))
    except Exception as exc:
        LOGGER.warning("External refine unavailable for %s: %s", parsed.symbol, exc)
        return RefineResult(
            status="FAILED",
            scanner_agreement="NO",
            scanner_direction="UNKNOWN",
            conflict_reason="market_data_unavailable",
            reason=["❌ Fresh market data unavailable"],
        )

    if df_1h.empty or df_15m.empty:
        return RefineResult(
            status="FAILED",
            scanner_agreement="NO",
            scanner_direction="UNKNOWN",
            conflict_reason="insufficient_candles",
            reason=["❌ Insufficient candles for refine analysis"],
        )

    latest_1h = df_1h.iloc[-1]
    latest_15m = df_15m.iloc[-1]
    close_1h = float(latest_1h["close"])
    close_15m = float(latest_15m["close"])
    ema_fast_1h = float(latest_1h["ema_fast"])
    ema_slow_1h = float(latest_1h["ema_slow"])
    ema_fast_15m = float(latest_15m["ema_fast"])
    atr_pct = _latest_float(df_1h, "atr_pct")
    mfi = _latest_float(df_1h, "mfi")
    volume_sma = _latest_float(df_1h, "volume_sma20")
    latest_volume = _latest_float(df_1h, "volume")
    volume_ratio = latest_volume / volume_sma if volume_sma > 0 else 0.0
    volume_spike = volume_ratio >= env_float("VOLUME_SPIKE_MULTIPLIER", 1.2)
    support, resistance = sr_engine.calculate(df_1h)
    regime = regime_detector.detect(df_1h)
    htf_regime = classify_trend(df_htf) if not df_htf.empty else "Unknown"
    htf_alignment = alignment_for_direction(parsed.side, htf_regime) if htf_regime != "Unknown" else "Unknown"
    btc_context = detect_btc_regime(df_btc)
    btc_regime = str(btc_context.get("regime", "unclear"))

    trend_long = close_1h > ema_fast_1h > ema_slow_1h
    trend_short = close_1h < ema_fast_1h < ema_slow_1h
    entry_long = close_15m > ema_fast_15m
    entry_short = close_15m < ema_fast_15m
    scanner_direction = _direction_from_context(trend_long, trend_short, entry_long, entry_short)

    agreement = (
        scanner_direction == parsed.side
        or (parsed.side == "LONG" and scanner_direction == "LONG_BIAS")
        or (parsed.side == "SHORT" and scanner_direction == "SHORT_BIAS")
    )
    score = 0
    reasons: list[str] = []
    if agreement:
        score += 30
        reasons.append("✅ VIP direction agrees with scanner trend/entry context")
    else:
        reasons.append("❌ VIP direction conflicts with scanner trend/entry context")
    if regime.name == "Trending":
        score += 15
        reasons.append("✅ 1H market regime supports trend continuation")
    elif regime.name == "Sideway":
        score -= 10
        reasons.append("❌ 1H market regime is sideway")
    if htf_alignment == "Aligned":
        score += 15
        reasons.append("✅ 4H HTF aligned")
    elif htf_alignment == "Conflict":
        score -= 15
        reasons.append("❌ 4H HTF conflict")
    if (parsed.side == "LONG" and mfi >= env_float("MFI_BULLISH_THRESHOLD", 55)) or (
        parsed.side == "SHORT" and mfi <= env_float("MFI_BEARISH_THRESHOLD", 45)
    ):
        score += 10
        reasons.append("✅ MFI confirms direction")
    else:
        reasons.append("⚠️ MFI does not strongly confirm direction")
    if volume_spike:
        score += 10
        reasons.append("✅ Volume spike confirms activity")
    else:
        reasons.append("⚠️ No volume spike")
    if atr_pct >= env_float("MIN_ATR_PCT", 0.35):
        score += 10
        reasons.append("✅ ATR is tradeable")
    else:
        score -= 10
        reasons.append("❌ ATR is too low")
    if btc_regime == "sideways":
        score -= 10
        reasons.append("⚠️ BTC regime is sideways")
    elif (btc_regime == "bullish" and parsed.side == "LONG") or (btc_regime == "bearish" and parsed.side == "SHORT"):
        score += 10
        reasons.append("✅ BTC regime supports direction")
    elif btc_regime in {"bullish", "bearish"}:
        score -= 10
        reasons.append("❌ BTC regime conflicts with direction")

    conflict_parts = []
    if not agreement:
        conflict_parts.append(f"scanner_direction={scanner_direction}")
    if regime.name == "Sideway":
        conflict_parts.append("sideway_regime")
    if htf_alignment == "Conflict":
        conflict_parts.append("htf_conflict")
    if atr_pct < env_float("MIN_ATR_PCT", 0.35):
        conflict_parts.append("low_atr")
    if btc_regime in {"bullish", "bearish"} and not (
        (btc_regime == "bullish" and parsed.side == "LONG") or (btc_regime == "bearish" and parsed.side == "SHORT")
    ):
        conflict_parts.append(f"btc_{btc_regime}_conflict")

    details = {
        "trend_1h": "bullish" if trend_long else "bearish" if trend_short else "mixed",
        "entry_15m": "bullish" if entry_long else "bearish" if entry_short else "mixed",
        "htf_regime": htf_regime,
        "htf_alignment": htf_alignment,
        "mfi": f"{mfi:.2f}",
        "atr_pct": f"{atr_pct:.2f}",
        "support": f"{support:.8f}",
        "resistance": f"{resistance:.8f}",
        "btc_regime": btc_regime,
        "volume_ratio": f"{volume_ratio:.2f}",
        "volume_spike": "YES" if volume_spike else "NO",
        "market_regime": regime.name,
    }
    return RefineResult(
        status="SUCCESS",
        score=max(0, min(100, int(score))),
        scanner_agreement="YES" if agreement else "NO",
        scanner_direction=scanner_direction,
        conflict_reason=";".join(conflict_parts),
        reason=reasons,
        details=details,
    )


def analyze_external_signal(raw_text: str, message_id: int | str = "", source: str = "VIP Forwarded Signal") -> ExternalSignalAnalysis:
    min_rr = env_float("EXTERNAL_SIGNAL_MIN_RR", 1.2)
    score_threshold = env_int("EXTERNAL_SIGNAL_SCORE_THRESHOLD", 70)
    parsed = parse_external_signal(raw_text, message_id, source)
    analysis = ExternalSignalAnalysis(parsed=parsed)
    if parsed.parse_status != "SUCCESS":
        analysis.recommendation = "FAILED"
        analysis.reason = [f"❌ {error}" for error in parsed.parse_errors]
        LOGGER.info(
            "External signal rejected: message_id=%s recommendation=%s reason=%s",
            message_id,
            analysis.recommendation,
            " | ".join(analysis.reason),
        )
        return analysis

    score = 45
    reasons = ["✅ Parsed required fields"]
    rr = calculate_rr(parsed)
    analysis.rr = rr
    if rr >= min_rr:
        score += 25
        reasons.append("✅ RR acceptable")
    else:
        reasons.append("❌ RR below minimum")

    tp1 = parsed.targets[0]
    if parsed.side == "LONG" and parsed.stop_loss < entry_mid(parsed) < tp1:
        score += 10
        reasons.append("✅ LONG price structure valid")
    elif parsed.side == "SHORT" and parsed.stop_loss > entry_mid(parsed) > tp1:
        score += 10
        reasons.append("✅ SHORT price structure valid")
    else:
        reasons.append("❌ Entry/SL/TP structure invalid")

    if len(parsed.targets) >= 2:
        score += 5
        reasons.append("✅ Multiple targets available")
    else:
        reasons.append("⚠️ Single target only")

    if parsed.leverage:
        leverage_value = int(re.sub(r"\D", "", parsed.leverage) or "0")
        if leverage_value <= 25:
            score += 5
            reasons.append("✅ Leverage within scanner safety band")
        else:
            score -= 10
            reasons.append("⚠️ High leverage context")
    else:
        reasons.append("⚠️ Leverage not specified")

    refine = perform_refine_analysis(parsed) if env_bool("EXTERNAL_SIGNAL_REFINE_ENABLED", True) else RefineResult(
        status="DISABLED",
        score=50,
        scanner_agreement="YES",
        scanner_direction=parsed.side,
        reason=["⚠️ External refine disabled by config"],
    )
    analysis.refine_status = refine.status
    analysis.refine_score = refine.score
    analysis.scanner_agreement = refine.scanner_agreement
    analysis.scanner_direction = refine.scanner_direction
    analysis.conflict_reason = refine.conflict_reason
    analysis.refine_details = refine.details
    reasons.extend(refine.reason)
    if refine.status == "SUCCESS":
        score += int(refine.score * 0.30)
    else:
        score -= 20

    analysis.analysis_score = max(0, min(100, score))
    if rr < min_rr:
        analysis.recommendation = "SKIP"
    elif refine.status != "SUCCESS":
        analysis.recommendation = "WAIT"
    elif refine.scanner_agreement != "YES":
        analysis.recommendation = "WAIT"
    elif refine.conflict_reason:
        analysis.recommendation = "WAIT"
    elif analysis.analysis_score < score_threshold:
        analysis.recommendation = "WAIT"
    else:
        analysis.recommendation = "APPROVED"
    analysis.reason = reasons
    if analysis.recommendation == "APPROVED":
        LOGGER.info(
            "External signal approved: message_id=%s symbol=%s side=%s score=%s rr=%.2f",
            message_id,
            parsed.symbol,
            parsed.side,
            analysis.analysis_score,
            analysis.rr,
        )
    else:
        LOGGER.info(
            "External signal rejected: message_id=%s symbol=%s side=%s recommendation=%s score=%s rr=%.2f",
            message_id,
            parsed.symbol,
            parsed.side,
            analysis.recommendation,
            analysis.analysis_score,
            analysis.rr,
        )
    return analysis


def build_signals_message(analysis: ExternalSignalAnalysis) -> str:
    parsed = analysis.parsed
    targets = parsed.targets[:3]
    tp_lines = "\n".join(f"TP{index + 1}: {format_price(target)}" for index, target in enumerate(targets))
    reason = "\n".join(analysis.reason)
    return (
        "🧪 External Signal Analyzer\n\n"
        "✅ APPROVED SIGNAL\n\n"
        "Source:\n"
        f"{parsed.source}\n\n"
        "Symbol:\n"
        f"{parsed.symbol}\n\n"
        "Side:\n"
        f"{parsed.side}\n\n"
        "Entry:\n"
        f"{format_price(parsed.entry_low)} - {format_price(parsed.entry_high)}\n\n"
        "SL:\n"
        f"{format_price(parsed.stop_loss)}\n\n"
        "TP:\n"
        f"{tp_lines}\n\n"
        "Score:\n"
        f"{analysis.analysis_score}/100\n\n"
        "Scanner Agreement:\n"
        f"{analysis.scanner_agreement}\n\n"
        "Refine Score:\n"
        f"{analysis.refine_score}/100\n\n"
        "Reason:\n"
        f"{reason}"
    )


def build_cornix_message(analysis: ExternalSignalAnalysis) -> str:
    parsed = analysis.parsed
    targets = "\n".join(format_price(target) for target in parsed.targets[:3])
    leverage = parsed.leverage or "10x"
    return (
        "🧪 DRY RUN - EXTERNAL SIGNAL CORNIX FORMAT\n"
        "DO NOT AUTO TRADE\n\n"
        f"{parsed.side} {parsed.symbol}\n\n"
        "Entry:\n"
        f"{format_price(parsed.entry_low)}-{format_price(parsed.entry_high)}\n\n"
        "Targets:\n"
        f"{targets}\n\n"
        "Stop:\n"
        f"{format_price(parsed.stop_loss)}\n\n"
        "Leverage:\n"
        f"{leverage}"
    )


def build_report_message(analysis: ExternalSignalAnalysis) -> str:
    parsed = analysis.parsed
    reason = "\n".join(analysis.reason) if analysis.reason else "-"
    action = (
        "Sent to Signals Channel\nSent to Cornix Channel"
        if analysis.recommendation == "APPROVED"
        else "Not sent to Signals Channel\nNot sent to Cornix Channel"
    )
    return (
        "📥 External Signal Review\n\n"
        "Status:\n"
        f"{analysis.recommendation}\n\n"
        "Symbol:\n"
        f"{parsed.symbol or '-'}\n\n"
        "Reason:\n"
        f"{reason}\n\n"
        "Refine:\n"
        f"Scanner agreement: {analysis.scanner_agreement}\n"
        f"Scanner direction: {analysis.scanner_direction}\n"
        f"Conflict: {analysis.conflict_reason or '-'}\n\n"
        "Action:\n"
        f"{action}"
    )


def ensure_log(path: Path = EXTERNAL_SIGNALS_CSV) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with path.open("w", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=FIELDNAMES).writeheader()
        LOGGER.info("External signal log created: %s", path)
        return
    try:
        with path.open("r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            existing_fields = reader.fieldnames or []
            rows = list(reader)
    except OSError:
        return
    if existing_fields == FIELDNAMES:
        return
    migrated = []
    for row in rows:
        migrated.append(
            {
                "timestamp_utc": row.get("timestamp_utc", ""),
                "source": row.get("source", "External Signal Inbox"),
                "message_id": row.get("message_id", ""),
                "symbol": row.get("symbol", ""),
                "side": row.get("side", ""),
                "entry_low": row.get("entry_low", ""),
                "entry_high": row.get("entry_high", ""),
                "stop_loss": row.get("stop_loss", ""),
                "tp1": row.get("tp1", ""),
                "tp2": row.get("tp2", ""),
                "tp3": row.get("tp3", ""),
                "raw_targets": row.get("raw_targets", ""),
                "leverage": row.get("leverage", ""),
                "raw_text": row.get("raw_text", ""),
                "parse_status": row.get("parse_status", ""),
                "analysis_score": row.get("analysis_score", ""),
                "recommendation": row.get("recommendation", row.get("status", "")),
                "reason": row.get("reason", ""),
                "rr": row.get("rr", ""),
                "refine_status": row.get("refine_status", ""),
                "refine_score": row.get("refine_score", ""),
                "scanner_agreement": row.get("scanner_agreement", ""),
                "scanner_direction": row.get("scanner_direction", ""),
                "conflict_reason": row.get("conflict_reason", ""),
                "trend_1h": row.get("trend_1h", ""),
                "entry_15m": row.get("entry_15m", ""),
                "htf_regime": row.get("htf_regime", ""),
                "htf_alignment": row.get("htf_alignment", ""),
                "mfi": row.get("mfi", ""),
                "atr_pct": row.get("atr_pct", ""),
                "support": row.get("support", ""),
                "resistance": row.get("resistance", ""),
                "btc_regime": row.get("btc_regime", ""),
                "volume_ratio": row.get("volume_ratio", ""),
                "volume_spike": row.get("volume_spike", ""),
                "market_regime": row.get("market_regime", ""),
                "sent_to_signals": row.get("sent_to_signals", "NO"),
                "sent_to_cornix": row.get("sent_to_cornix", "NO"),
            }
        )
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(migrated)
    LOGGER.info("External signal log migrated: %s", path)


def log_analysis(analysis: ExternalSignalAnalysis, path: Path = EXTERNAL_SIGNALS_CSV) -> None:
    ensure_log(path)
    parsed = analysis.parsed
    targets = parsed.targets[:3]
    row = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "source": parsed.source,
        "message_id": parsed.message_id,
        "symbol": parsed.symbol,
        "side": parsed.side,
        "entry_low": "" if parsed.entry_low is None else parsed.entry_low,
        "entry_high": "" if parsed.entry_high is None else parsed.entry_high,
        "stop_loss": "" if parsed.stop_loss is None else parsed.stop_loss,
        "tp1": targets[0] if len(targets) > 0 else "",
        "tp2": targets[1] if len(targets) > 1 else "",
        "tp3": targets[2] if len(targets) > 2 else "",
        "raw_targets": ",".join(str(target) for target in parsed.targets),
        "leverage": parsed.leverage,
        "raw_text": parsed.raw_text,
        "parse_status": parsed.parse_status,
        "analysis_score": analysis.analysis_score,
        "recommendation": analysis.recommendation,
        "reason": " | ".join(analysis.reason),
        "rr": f"{analysis.rr:.4f}",
        "refine_status": analysis.refine_status,
        "refine_score": analysis.refine_score,
        "scanner_agreement": analysis.scanner_agreement,
        "scanner_direction": analysis.scanner_direction,
        "conflict_reason": analysis.conflict_reason,
        "trend_1h": analysis.refine_details.get("trend_1h", ""),
        "entry_15m": analysis.refine_details.get("entry_15m", ""),
        "htf_regime": analysis.refine_details.get("htf_regime", ""),
        "htf_alignment": analysis.refine_details.get("htf_alignment", ""),
        "mfi": analysis.refine_details.get("mfi", ""),
        "atr_pct": analysis.refine_details.get("atr_pct", ""),
        "support": analysis.refine_details.get("support", ""),
        "resistance": analysis.refine_details.get("resistance", ""),
        "btc_regime": analysis.refine_details.get("btc_regime", ""),
        "volume_ratio": analysis.refine_details.get("volume_ratio", ""),
        "volume_spike": analysis.refine_details.get("volume_spike", ""),
        "market_regime": analysis.refine_details.get("market_regime", ""),
        "sent_to_signals": "YES" if analysis.sent_to_signals else "NO",
        "sent_to_cornix": "YES" if analysis.sent_to_cornix else "NO",
    }
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writerow(row)
    LOGGER.info(
        "External signal logged: message_id=%s recommendation=%s sent_to_signals=%s sent_to_cornix=%s path=%s",
        parsed.message_id,
        analysis.recommendation,
        row["sent_to_signals"],
        row["sent_to_cornix"],
        path,
    )


def send_telegram_message(token: str, chat_id: str, message: str, channel_name: str) -> bool:
    if not token or not chat_id:
        LOGGER.warning("%s send skipped: token/chat id missing", channel_name)
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": message},
            timeout=20,
        )
    except requests.RequestException as exc:
        LOGGER.error("%s send failed: %s", channel_name, exc)
        return False
    if response.status_code != 200:
        LOGGER.error("%s send failed: %s", channel_name, response.text)
        return False
    return True


def route_analysis(
    analysis: ExternalSignalAnalysis,
    token: str,
    signals_chat_id: str,
    cornix_chat_id: str,
    reports_chat_id: str,
    send: bool = True,
) -> ExternalSignalAnalysis:
    if not send:
        return analysis
    if analysis.recommendation == "APPROVED":
        analysis.sent_to_signals = send_telegram_message(token, signals_chat_id, build_signals_message(analysis), "external signals")
        LOGGER.info("External signal routed to signals: message_id=%s success=%s", analysis.parsed.message_id, analysis.sent_to_signals)
        analysis.sent_to_cornix = send_telegram_message(token, cornix_chat_id, build_cornix_message(analysis), "external cornix")
        LOGGER.info("External signal routed to cornix: message_id=%s success=%s", analysis.parsed.message_id, analysis.sent_to_cornix)
    else:
        LOGGER.info(
            "External signal CSV-only, no Telegram routing: message_id=%s recommendation=%s",
            analysis.parsed.message_id,
            analysis.recommendation,
        )
    return analysis


def process_external_signal(
    raw_text: str,
    message_id: int | str,
    token: str = "",
    signals_chat_id: str = "",
    cornix_chat_id: str = "",
    reports_chat_id: str = "",
    source: str = "VIP Forwarded Signal",
    log_path: Path = EXTERNAL_SIGNALS_CSV,
    send: bool = True,
) -> ExternalSignalAnalysis:
    LOGGER.info("External signal received: message_id=%s source=%s chars=%s", message_id, source, len(raw_text or ""))
    analysis = analyze_external_signal(raw_text, message_id, source)
    route_analysis(analysis, token, signals_chat_id, cornix_chat_id, reports_chat_id, send=send)
    log_analysis(analysis, log_path)
    return analysis
