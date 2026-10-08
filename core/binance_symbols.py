# -*- coding: utf-8 -*-
"""Binance USD-M market-data symbol translation.

Scanner, signal, journal, and research identities remain canonical.  This
module is used only at Binance public market-data request boundaries where an
exchange-specific contract symbol differs from the canonical scanner symbol.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

from core.signal_identity import normalize_symbol


BINANCE_USDM_MARKET_SYMBOLS: Mapping[str, str] = MappingProxyType(
    {
        "PEPEUSDT": "1000PEPEUSDT",
        "FLOKIUSDT": "1000FLOKIUSDT",
        "BONKUSDT": "1000BONKUSDT",
    }
)


def binance_usdm_market_symbol(symbol: Any) -> str:
    """Return the exchange symbol for a public Binance USD-M market request.

    Unknown and ordinary symbols safely fall back to their canonical normalized
    form.  The caller's value is never mutated and must remain the identity used
    in signals, journals, telemetry, Telegram, Cornix, and execution linkage.
    """

    canonical = normalize_symbol(symbol)
    return BINANCE_USDM_MARKET_SYMBOLS.get(canonical, canonical)
