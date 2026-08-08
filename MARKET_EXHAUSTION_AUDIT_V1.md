# Market Exhaustion Audit V1

Status: read-only audit and design only  
Repository: `D:\Project AI\Crypto Multi-Coin Scanner`  
Audit date: 2026-08-08 ICT  
Production mode: Season 1, no live trading logic changes  

## Executive Summary

PASS with severe sample limitation.

The scanner has enough runtime indicator data to design a Market Exhaustion
Shadow layer, but the local journal does not currently contain closed signal
rows or candle snapshots that can prove or reject the exhaustion hypothesis.

Current local data state:

- `logs/signals.csv`: header only, 0 signal rows.
- `logs/signals_history.csv`: header only, 0 completed rows.
- `logs/entry_timing_engine.csv`: 0 bytes.
- `logs/sr_trade_weight_shadow.csv`: not present locally.
- `logs/daily_performance.csv`: 1 validation/sample row, but no underlying
  `signals.csv` rows in this local workspace, so it is not valid for this audit.

Conclusion:

- Historical outcome comparison: INSUFFICIENT DATA.
- Next-day profit/loss hypothesis: INSUFFICIENT DATA.
- Pullback/profit-taking evidence: INSUFFICIENT DATA.
- Recommended next action: implement Market Exhaustion Shadow only after
  deciding which runtime metrics to persist. Do not enable live penalties yet.

Implementation update:

- Market Exhaustion Shadow V1 has been implemented as analytics-only.
- Output file: `logs/market_exhaustion_shadow.csv`.
- Live routing, score, confidence, TP/SL, RR, approval, and ranking are unchanged.
- `MARKET_EXHAUSTION_LIVE_ENABLED` is intentionally non-enforcing in Phase 1.

## Files Inspected

- `cornix_agent.py`
- `core/entry_timing_engine.py`
- `core/wave_structure_analyzer.py`
- `core/sr_trade_weight_gate.py`
- `logs/signals.csv`
- `logs/signals_history.csv`
- `logs/entry_timing_engine.csv`
- `logs/daily_performance.csv`
- `logs/performance_report.txt`

Dashboard files were intentionally not inspected for modification:

- `dashboard.py`
- `DASHBOARD_V2.md`

`Command/` was not touched.

## Data Availability

| Data item | Runtime available | Journal available | Notes |
|---|---:|---:|---|
| ATR | Yes | Partial | `atr14` exists in runtime; journal has `atr_expansion_ratio`, not raw ATR in `signals.csv`. `signals_history.csv` has `atr` column but no rows. |
| entry | Yes | Yes | `TradeSignal.entry`, `logs/signals.csv` column exists. |
| EMA9 | Yes | No | Runtime only in 15m/1H enriched candles. |
| EMA20 | Yes | No | Runtime only; chart uses it, journal does not store raw EMA20. |
| EMA21 | Yes | No | Runtime only for 15m confirmation. |
| EMA50 | Yes | No | Runtime only; chart uses it, journal does not store raw EMA50. |
| RSI | Yes | No | `rsi14` calculated, not stored in journal. |
| MFI | Yes | Yes | `mfi` and `mfi_confirmed` are stored in `signals.csv`. |
| Volume spike | Yes | Yes | `volume_spike` stored; exact volume ratio is not stored in current journal. |
| Candle body / wick | Yes | Yes | `body_ratio`, `opposite_wick_ratio` stored. |
| Recent swing high / low | Partial | No | Wave module detects swings at runtime but does not persist swing identities. |
| Breakout state | Yes | Partial | `breakout_confirmed` exists on `TradeSignal`; not stored in `signals.csv`. |
| Wave structure | Yes | Yes | `wave_score`, `wave_structure`, `wave_phase`, `wave_notes` stored. |
| Recent N-candle high/low | Yes | No | `previous_20` high/low used in scoring, not persisted. |
| 1H candles | Yes | No | Fetched during scan, not archived. |
| 15m candles | Yes | No | Fetched during scan, not archived. |
| 4H candles | Yes | No | Optional HTF runtime data, not archived. |
| Realized WIN/LOSS/OPEN | Yes | Yes | Columns exist, but local `signals.csv` has 0 rows. |
| Realized R | Partial | Partial | `risk_reward`, `max_profit_pct`, `max_drawdown_pct`, `hit_target`; no rows locally. |
| TP/SL time | Yes | Yes | `closed_at` / alert columns exist; no rows locally. |
| Signal timestamp | Yes | Yes | `timestamp` column exists. |
| Stable linkage key | Partial | Partial | `outcome_id` exists after outcome review. Entry Timing has `candidate_id`, but local file is empty. SR Shadow uses `shadow_key`, but local SR file is absent. |

## Runtime Source Notes

`cornix_agent.py` computes these before or during `TradeSignal` creation:

- Indicators in `IndicatorEngine.add_indicators(...)`:
  - `rsi14`
  - `ema9`
  - `ema20`
  - `ema21`
  - `ema50`
  - `atr14`
  - `mfi`
  - `volume_sma20`
  - `atr_pct`
- Signal setup in `SignalScorer.score(...)`:
  - 1H trend from close/EMA20/EMA50.
  - 15m confirmation from close/EMA9/EMA21/RSI.
  - breakout from previous 20-candle high/low.
  - candle body and wick ratios.
  - ATR expansion ratio using prior ATR mean.
  - MFI confirmation.
  - wave score and structure.
  - support/resistance from recent 30 candles.
- `AgentRunner.scan_symbol(...)` fetches:
  - 1H candles.
  - 15m candles.
  - optional 4H candles.

These data are enough for a shadow implementation, but not enough for
historical reconstruction unless future shadow rows persist the needed metrics.

## Proposed Exhaustion Metrics

All metrics should be direction-neutral and ATR-normalized.

### A. Swing Distance ATR

LONG:

```text
swing_distance_atr = (entry - recent_swing_low) / atr
```

SHORT:

```text
swing_distance_atr = (recent_swing_high - entry) / atr
```

Reliability:

- Use only if recent swing detection returns a clear swing within a bounded
  lookback.
- If swing map is unclear, return `UNKNOWN`, not a penalty.

### B. Distance From EMA

```text
ema20_distance_atr = abs(entry - ema20) / atr
ema50_distance_atr = abs(entry - ema50) / atr
```

Interpretation:

- Low distance often means entry is near mean/trend structure.
- High distance means chasing a move may be more likely.

### C. Consecutive Directional Candles

LONG:

```text
count consecutive candles where close > open before entry
```

SHORT:

```text
count consecutive candles where close < open before entry
```

Track separately:

- 1H directional run.
- 15m directional run.

### D. Cumulative Move ATR

Define an impulse start using the most recent directional swing or EMA pullback.

LONG:

```text
cumulative_move_atr = (entry - impulse_start_low) / atr
```

SHORT:

```text
cumulative_move_atr = (impulse_start_high - entry) / atr
```

V1 should use this only when impulse start is deterministic.

### E. ATR Expansion

```text
atr_expansion_median = current_atr / median(atr, lookback=20)
```

Current scanner stores `atr_expansion_ratio` against mean ATR. Median is more
robust for the exhaustion layer, but this requires a new shadow metric.

### F. MFI / RSI Exhaustion

LONG risk context:

- MFI very high.
- RSI very high.

SHORT risk context:

- MFI very low.
- RSI very low.

Important:

- MFI/RSI alone must never become a hard skip.
- Treat as a supporting risk signal only.

### G. Fibonacci Extension

Potential levels:

- 127.2%
- 161.8%
- 200%
- 261.8%

Recommendation:

Do not use Fibonacci extension in V1. Current swing detection is lightweight and
not persisted, so Fib extension would be fragile and hard to audit. Mark it
`NOT RELIABLE` until swing identity and impulse legs are stored consistently.

## Historical Outcome Comparison

Required closed-trade buckets:

Swing Distance ATR:

- `<1.5`
- `1.5 to <2.5`
- `2.5 to <3.5`
- `3.5 to <5.0`
- `>=5.0`

EMA20 Distance ATR:

- `<0.5`
- `0.5 to <1.0`
- `1.0 to <1.5`
- `1.5 to <2.0`
- `>=2.0`

Directional candle run:

- `1-2`
- `3-4`
- `5-6`
- `7+`

Metrics to report per bucket:

- trades
- wins
- losses
- win rate
- net R
- average R
- TP1 rate
- average TP time
- average SL time

Additional splits:

- LONG / SHORT
- Core / Report Only
- Tier A / B / C
- Session
- Market regime

Current local result:

```text
Closed trades available: 0
Historical exhaustion buckets: INSUFFICIENT DATA
```

## Next-Day Performance Hypothesis

Hypothesis:

Days with high profit may be followed by weaker days because price has already
moved far and many setups become late-cycle entries.

Required daily fields:

- daily net R
- daily wins/losses
- direction mix
- average exhaustion score per signal
- next-day net R
- next-day win rate

Previous-day buckets:

- `<= -3R`
- `-3R to <0`
- `0 to <+2R`
- `+2R to <+4R`
- `>= +4R`

Report:

- sample days
- average next-day R
- median next-day R
- next-day win rate
- probability next day negative
- average exhaustion metrics

Current local result:

```text
Valid production days available: 0
Next-day hypothesis: INSUFFICIENT DATA
```

Do not infer causation from this analysis even after data exists. This can only
show correlation and should include confidence level and sample limitations.

## Pullback / Profit-Taking Evidence

Desired post-entry checks:

Within 1-4 15m candles or 1-2 1H candles after entry:

- maximum favorable excursion
- maximum adverse excursion
- pullback before TP1
- pullback depth in ATR
- whether extended setups see adverse movement before favorable movement

Current limitation:

The local logs do not contain candle snapshots after signal entry. The outcome
checker can fetch candles for resolving TP/SL, but the journal does not persist
the early post-entry path needed for this audit.

Current local result:

```text
Pullback/profit-taking evidence: INSUFFICIENT DATA
```

## Comparison With Existing Shadows

### Entry Timing Shadow

`core/entry_timing_engine.py` already evaluates:

- distance to support/resistance
- pullback opportunity
- breakout confirmation
- breakout-retest confirmation
- overextended move
- entry quality score
- recommendation

Local status:

```text
logs/entry_timing_engine.csv: 0 bytes
Overlap analysis: INSUFFICIENT DATA
```

### SR Trade Weight Shadow

`core/sr_trade_weight_gate.py` evaluates:

- direction-aware opposing S/R
- opposing distance
- effective S/R RR
- ATR-normalized opposing distance
- TP1 clearance
- breakout context

Local status:

```text
logs/sr_trade_weight_shadow.csv: not present
Overlap analysis: INSUFFICIENT DATA
```

### Double Penalty Risk

There is conceptual overlap:

- Entry Timing `overextended_move` can penalize late entries.
- Market Exhaustion would also penalize late entries.
- SR Shadow can also penalize poor room into opposing structure.

Design guard:

Do not stack full penalties blindly. If future live mode is enabled, aggregate
these into one `timing_quality_context` or cap total timing penalty.

## Market Exhaustion Weight V1 Design

This is design only. Do not implement live behavior yet.

### Decision Levels

#### FRESH

Conditions:

- swing distance below early threshold.
- EMA20 distance low.
- directional run short.
- ATR expansion normal.

Effect:

- no penalty.

#### NORMAL

Conditions:

- move is within ordinary trend continuation range.
- one mild risk factor allowed.

Effect:

- no penalty or small shadow penalty only.

#### EXTENDED

Conditions:

- move is far from swing/EMA.
- directional run is long.
- ATR expansion is elevated.
- MFI/RSI may be stretched.

Proposed future penalty:

```text
score_penalty_shadow = 5 to 10
confidence_penalty_shadow = 3 to 6
```

No hard skip in V1 live.

#### EXHAUSTED

Conditions:

- cumulative/swing distance is very high.
- EMA distance is high.
- directional run is long.
- volatility expansion is high.
- confirmation quality is weak.

Proposed future behavior:

- `WAIT FOR PULLBACK` or `SKIP` shadow recommendation.
- Do not enable live skip until statistically proven.

Proposed future penalty:

```text
score_penalty_shadow = 10 to 18
confidence_penalty_shadow = 6 to 12
```

### Suggested Initial Thresholds

These are design seeds only:

| Metric | FRESH | NORMAL | EXTENDED | EXHAUSTED |
|---|---:|---:|---:|---:|
| swing_distance_atr | `<1.5` | `1.5-3.5` | `3.5-5.0` | `>=5.0` |
| ema20_distance_atr | `<0.5` | `0.5-1.5` | `1.5-2.0` | `>=2.0` |
| directional_run_1h | `1-2` | `3-4` | `5-6` | `7+` |
| atr_expansion_median | `<1.1` | `1.1-1.5` | `1.5-2.0` | `>=2.0` |

Do not activate these in live mode without outcome evidence.

## Momentum Continuation Exception

Strong trend continuation should not be incorrectly blocked.

Reduce EXHAUSTED to EXTENDED, or reduce penalty, when most of these are true:

- volume expansion is strong.
- candle body is strong.
- opposite wick rejection is low.
- 15m confirmation passes.
- 4H alignment passes.
- opposing S/R has room.
- breakout is confirmed.
- market regime is Trending.

Important:

- RSI/MFI stretched alone is not a skip condition.
- Extended move plus strong continuation should remain eligible in shadow until
  outcomes prove otherwise.

## Recommended Integration Point

Best future integration point:

1. Compute metrics in `SignalScorer.score(...)` while 1H/15m/4H enriched candle
   data and raw ATR/EMA/RSI are still available.
2. Attach shadow fields to the candidate or write a separate shadow CSV after
   `RiskManager.apply(...)`.
3. In Phase 2 only, apply capped score/confidence adjustments before
   `select_top_candidates(...)`.

Do not use AI for this gate.

Proposed config names:

```env
MARKET_EXHAUSTION_SHADOW_ENABLED=true
MARKET_EXHAUSTION_LIVE_ENABLED=false
MARKET_EXHAUSTION_MIN_CLOSED_TRADES=150
MARKET_EXHAUSTION_MAX_TIMING_PENALTY=12
```

Default live must remain false.

## Rollout Plan

### Phase 1: Shadow

- Persist exhaustion metrics.
- Persist decision: FRESH / NORMAL / EXTENDED / EXHAUSTED.
- Persist shadow penalty and reason.
- Persist enough identifiers for outcome joins.
- Do not alter live signals.

Minimum sample before Phase 2:

- 150 closed signals total.
- 30+ EXTENDED closed signals.
- 20+ EXHAUSTED closed signals.
- At least 10 closed signals per major side if possible.

### Phase 2: Weighted Live

- Apply small capped penalties only.
- No hard skip unless extremely poor and statistically validated.
- Preserve a single total cap with Entry Timing and SR gate to avoid double
  penalty.

### Phase 3: Calibrated

- Refit thresholds from real outcomes.
- Consider `WAIT FOR PULLBACK` only for clusters proven weak.
- Keep rollback simple: set `MARKET_EXHAUSTION_LIVE_ENABLED=false`.

## Test Matrix

Minimum tests for a future shadow module:

- LONG fresh move.
- LONG 2 ATR.
- LONG 4 ATR.
- LONG 6 ATR.
- SHORT mirror cases.
- Strong confirmed breakout.
- Weak late breakout.
- High MFI but fresh move.
- High MFI and extended move.
- Low MFI SHORT fresh.
- Missing swing.
- Invalid ATR.
- Missing EMA.
- Flat market.
- Extreme gap.
- SR SAFE but exhausted.
- SR CAUTION and extended.
- No candle history.
- No outcome row.

## Final Recommendation

### Does "big profit day then loss next day" have data support?

Not yet.

The local workspace has no valid production signal rows or closed trades for
this audit. The hypothesis is plausible, but currently unproven here.

### Do extended entries affect WR / Net R / pullback?

Unknown.

Runtime can compute the needed metrics, but historical local logs do not
contain enough data to compare outcomes.

### Which metric has highest predictive value?

Unknown from current data.

Design priority for shadow collection:

1. `swing_distance_atr`
2. `ema20_distance_atr`
3. `directional_run_1h`
4. `atr_expansion_median`
5. `mfi/rsi_exhaustion_context`

### Should Fibonacci be used in V1?

No.

Fib extension needs reliable swing identity. Current swing detection is useful
context, but not yet strong enough for a production V1 exhaustion gate.

### Should Market Exhaustion Shadow be implemented next?

Yes, but shadow only.

It should persist runtime-only fields that are currently lost:

- raw ATR
- EMA20/EMA50 distance in ATR
- RSI
- recent swing high/low identity
- directional candle run
- cumulative move ATR
- breakout state
- exhaustion decision
- stable join key

### How much more data is needed?

Recommended minimum before live weighting:

- 150 closed signals total.
- 30+ EXTENDED outcomes.
- 20+ EXHAUSTED outcomes.
- At least 20 production days for next-day analysis.

## Safety Confirmation

- Trading logic was not changed.
- Scanner scoring was not changed.
- Signal approval was not changed.
- TP/SL/RR logic was not changed.
- Telegram/Cornix routing was not changed.
- Dashboard edits were not touched.
- `Command/` was not touched.
- SR Trade Weight Shadow was not changed.
- Nothing was staged, committed, or pushed by this audit.

## Files Changed

- `MARKET_EXHAUSTION_AUDIT_V1.md`
