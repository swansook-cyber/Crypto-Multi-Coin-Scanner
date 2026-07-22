# Support / Resistance Trade Weight Gate V1 Shadow

Status: Implemented as Phase 1 shadow analytics only.

This layer measures whether a candidate has enough room before the nearest
direction-aware opposing support/resistance level. It does not change live
signal approval, score, setup strength, candidate ordering, Telegram routing,
Cornix routing, TP/SL/RR, or outcome review.

## Output

Shadow rows are written to:

`logs/sr_trade_weight_shadow.csv`

The main journal remains unchanged:

`logs/signals.csv`

## Decisions

- `SAFE`: opposing S/R has enough trade room.
- `CAUTION`: opposing S/R is near enough to deserve a future penalty study.
- `SKIP`: trade room is poor under the proposed thresholds.
- `UNKNOWN`: required S/R, ATR, SL, or direction context is missing/invalid.

In Phase 1, even `SKIP` is analytics-only.

## Metrics

- `opposing_level`
- `opposing_distance`
- `opposing_distance_pct`
- `opposing_distance_atr`
- `risk_distance`
- `effective_sr_rr`
- `tp1_clearance`
- `sr_score_penalty_shadow`
- `breakout_context`
- `sr_gate_reason`

## Direction Rules

LONG:

- Opposing level is resistance above entry.
- Support is context only and is not treated as the opposing trade-room cap.

SHORT:

- Opposing level is support below entry.
- Resistance is context only and is not treated as the opposing trade-room cap.

## Breakout Exception

If the candidate already broke through the old opposing level:

- confirmed breakout can be `SAFE`;
- weak breakout becomes `CAUTION`;
- it is not hard-skipped simply because the old level is near.

Confirmation uses data already present on `TradeSignal`: breakout flag, candle
body quality, opposite wick ratio, volume spike, and MFI confirmation.

## Configuration

Defaults in `.env.example`:

```env
SR_GATE_SHADOW_ENABLED=true
SR_GATE_LIVE_ENABLED=false
SR_GATE_HARD_SKIP_EFFECTIVE_RR=1.2
SR_GATE_CAUTION_EFFECTIVE_RR=1.8
SR_GATE_HARD_SKIP_ATR=0.65
SR_GATE_CAUTION_ATR=1.0
```

`SR_GATE_LIVE_ENABLED` is a non-operative marker in Phase 1. It is present so a
future weighted rollout can be explicit, but current code does not use it to
alter production decisions.

## Dedupe

Each row has a stable `shadow_key` generated from:

- symbol
- side
- signal status
- entry
- stop loss
- TP1
- TP2

The logger skips duplicate keys, preventing repeated shadow rows for the same
candidate across repeated scanner runs.

## Rollout Rule

Do not enable live penalties or hard skips until enough closed outcome samples
exist to compare `SAFE`, `CAUTION`, `SKIP`, and `UNKNOWN` groups.
