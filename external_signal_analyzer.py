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

import requests


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
    sent_to_signals: bool = False
    sent_to_cornix: bool = False


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

    # Market/regime checks are intentionally conservative for V1. They do not
    # approve weak parses and they do not trade; they only add context.
    reasons.append("⚠️ Market/regime checks use external V1 lightweight validation")

    analysis.analysis_score = max(0, min(100, score))
    if rr < min_rr:
        analysis.recommendation = "SKIP"
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
