# -*- coding: utf-8 -*-
"""Canonical signal identity helpers for analytics linkage.

The helpers in this module are intentionally pure. They do not read files,
write files, or affect scanner decisions. They exist so shadow analytics can be
joined back to signal outcomes with the same normalized identity everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import math
from typing import Any, Mapping


@dataclass(frozen=True)
class SignalIdentity:
    canonical_key: str
    symbol: str
    side: str
    timestamp: str
    entry: str
    source: str = "derived"


def _get_value(source: Any, *names: str, default: Any = "") -> Any:
    for name in names:
        if isinstance(source, Mapping):
            value = source.get(name, "")
        else:
            value = getattr(source, name, "")
        if value is not None and str(value).strip() != "":
            return value
    return default


def normalize_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    if ":" in text:
        text = text.split(":", 1)[-1]
    return text.replace("#", "").replace(".P", "").replace("/", "").replace("-", "").replace("_", "")


def normalize_side(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text == "BUY":
        return "LONG"
    if text == "SELL":
        return "SHORT"
    return text if text in {"LONG", "SHORT"} else text


def normalize_timestamp(value: Any, *, precision: str = "second") -> str:
    """Normalize timestamp to UTC with conservative fixed precision.

    Supported precision values are ``second`` and ``minute``. ``second`` is the
    default because it tolerates millisecond/microsecond formatting differences
    without merging separate scanner cycles in the same minute.
    """
    if value is None or str(value).strip() == "" or str(value).strip().lower() in {"nat", "nan", "none", "null"}:
        return ""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            # Some CSVs store pandas-style offsets that fromisoformat accepts
            # after replacing a space separator.
            try:
                dt = datetime.fromisoformat(text.replace(" ", "T"))
            except ValueError:
                return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    if precision == "minute":
        dt = dt.replace(second=0, microsecond=0)
    else:
        dt = dt.replace(microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_float(value: Any, *, decimals: int = 6) -> str:
    if value is None or str(value).strip() == "":
        return ""
    try:
        numeric = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return ""
    try:
        if not math.isfinite(float(numeric)):
            return ""
    except (OverflowError, ValueError):
        return ""
    quantum = Decimal("1").scaleb(-decimals)
    return str(numeric.quantize(quantum, rounding=ROUND_HALF_UP))


def canonical_signal_key(
    *,
    symbol: Any = "",
    side: Any = "",
    timestamp: Any = "",
    entry: Any = "",
    signal_id: Any = "",
    candidate_id: Any = "",
    timestamp_precision: str = "second",
    float_decimals: int = 6,
) -> str:
    primary_id = str(signal_id or candidate_id or "").strip()
    if primary_id:
        return f"id:v1:{primary_id}"
    norm_symbol = normalize_symbol(symbol)
    norm_side = normalize_side(side)
    norm_timestamp = normalize_timestamp(timestamp, precision=timestamp_precision)
    norm_entry = normalize_float(entry, decimals=float_decimals)
    if not all([norm_symbol, norm_side, norm_timestamp, norm_entry]):
        return ""
    return f"sig:v1:{norm_symbol}|{norm_side}|{norm_timestamp}|{norm_entry}"


def identity_from_record(record: Any, *, timestamp_precision: str = "second") -> SignalIdentity:
    signal_id = _get_value(record, "canonical_signal_key", "signal_id", "source_signal_id", default="")
    if str(signal_id).startswith(("sig:v1:", "id:v1:")):
        key = str(signal_id).strip()
        return SignalIdentity(key, "", "", "", "", "canonical_signal_key")
    symbol = _get_value(record, "symbol", "normalized_symbol", default="")
    side = _get_value(record, "side", "direction", "normalized_direction", default="")
    timestamp = _get_value(record, "timestamp", "signal_timestamp", "final_signal_timestamp", "timestamp_utc", default="")
    entry = _get_value(record, "entry", "entry_low", default="")
    candidate_id = _get_value(record, "candidate_id", default="")
    key = canonical_signal_key(
        symbol=symbol,
        side=side,
        timestamp=timestamp,
        entry=entry,
        signal_id=signal_id if not str(signal_id).startswith(("sig:v1:", "id:v1:")) else "",
        candidate_id=candidate_id,
        timestamp_precision=timestamp_precision,
    )
    return SignalIdentity(
        key,
        normalize_symbol(symbol),
        normalize_side(side),
        normalize_timestamp(timestamp, precision=timestamp_precision),
        normalize_float(entry),
        "derived",
    )
