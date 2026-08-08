# Market Exhaustion Shadow V1

Status: implemented as analytics-only shadow layer  
Output: `logs/market_exhaustion_shadow.csv`  
Live behavior: unchanged

## Purpose

Market Exhaustion Shadow V1 records whether an already-created scanner
candidate looks fresh, normal, extended, or exhausted. It is designed for
future statistical comparison only.

It does not:

- change scanner score or setup strength
- approve or reject signals
- change candidate ranking
- change TP, SL, RR, routing, Cornix, or Telegram output
- modify `logs/signals.csv`

## Runtime Inputs

The shadow evaluator uses only data already available during `scan_symbol()`:

- side and entry from `TradeSignal`
- 1H `atr14`, `ema20`, `ema50`, `rsi14`
- MFI from `TradeSignal`
- volume spike from `TradeSignal`
- candle body and opposite wick ratios
- breakout confirmed flag
- 15m momentum confirmation from existing EMA9/EMA21/RSI rules
- HTF alignment from `TradeSignal`
- recent 1H and 15m candles already fetched by the scanner
- optional SR gate decision field, when available later

No new market API call is added.

## Metrics

The CSV stores:

- `swing_distance_atr`
- `ema20_distance_atr`
- `ema50_distance_atr`
- `directional_run_1h`
- `directional_run_15m`
- `atr_expansion_ratio`
- `rsi`
- `mfi`
- `breakout_context`
- `momentum_exception_applied`
- `exhaustion_class`
- `exhaustion_penalty_shadow`
- `reason`
- `sr_gate_decision`

## Classification

Starting swing thresholds:

- `FRESH`: swing distance below `1.5 ATR`
- `NORMAL`: `1.5` to below `2.5 ATR`
- `EXTENDED`: `2.5` to below `4.0 ATR`
- `EXHAUSTED`: `4.0 ATR` or more, confirmed by other extension factors
- `UNKNOWN`: invalid ATR, missing swing, missing EMA context, or invalid input

The final class is based on a combination of swing distance, EMA distance,
directional run, ATR expansion, and oscillator context. RSI/MFI are supporting
context only and do not create exhaustion by themselves.

## Momentum Continuation Exception

An extended or exhausted candidate is reduced by one severity level when all
of these are true:

- breakout is confirmed
- volume spike is present
- candle body is strong
- opposite wick is low
- 15m momentum confirmation is present
- HTF alignment is aligned
- SR gate is not `SKIP`

The exception is recorded in `momentum_exception_applied` and
`breakout_context`.

## Shadow Penalty

The penalty is recorded only for research:

- `FRESH`: `0`
- `NORMAL`: `0` to `-3`
- `EXTENDED`: `-5` to `-10`
- `EXHAUSTED`: `-10` to `-18`
- `UNKNOWN`: `0`

No live score or confidence is changed in Phase 1.

## Dedupe

Rows are deduped by a stable shadow key:

```text
symbol | side | signal_status | signal timestamp | normalized entry
```

If a real `signal_id` or `candidate_id` exists in the future, it becomes the
primary identity. Entry is normalized to six decimals so values such as
`100`, `100.0`, and `100.0000001` dedupe as the same candidate.

## Config

`.env.example` includes:

```text
MARKET_EXHAUSTION_SHADOW_ENABLED=true
MARKET_EXHAUSTION_LIVE_ENABLED=false
```

`MARKET_EXHAUSTION_LIVE_ENABLED` is logged only. It has no live enforcement
path in Shadow V1.

## Integration Point

The scanner calls Market Exhaustion Shadow inside:

```text
cornix_agent.py -> AgentRunner.scan_symbol()
```

The call happens after:

- candidate generation
- RiskManager TP/SL/RR application
- SR Trade Weight Shadow logging

and before:

- AI commentary
- candidate processing
- ranking
- Telegram/Cornix routing

## Validation

Smoke tests cover:

- LONG and SHORT fresh/normal/extended/exhausted classification
- high RSI/MFI fresh context
- momentum continuation exception
- weak breakout without exception
- invalid ATR, missing swing, missing EMA, NaN/inf/None
- SR `SAFE` and `SKIP` context
- CSV logger dedupe
- no mutation of score/confidence/journal schema

## Rollout

Phase 1 is shadow-only. Collect enough rows and outcomes before using this
metric in reports or live weighting.

Recommended minimum before live consideration:

- at least 100 closed scanner candidates
- at least 20 closed rows per main exhaustion class where possible
- review by symbol, session, direction, and score bucket
