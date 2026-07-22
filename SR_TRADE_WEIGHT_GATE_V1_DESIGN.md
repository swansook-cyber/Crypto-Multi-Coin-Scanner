# Support / Resistance Trade Weight Gate V1 Design

Status: Design plus Phase 1 shadow implementation  
Scope: Scanner quality gate proposal  
Mode: Phase 1 implementation is analytics-only  
Safety: No trading logic, scanner loop, Telegram routing, outcome logic, database,
or Dashboard edits are changed by this design.

## Objective

Add a production-safe Support / Resistance Trade Weight Gate so that:

- LONG setups close to valid resistance receive real score/setup-strength impact.
- SHORT setups close to valid support receive real score/setup-strength impact.
- Extremely poor trade room can be hard-skipped before routing.
- Breakout signals are not forced to wait for retest.
- Retest remains optional/future, not a required entry condition.

The first rollout should be measurable and conservative.

## Phase 1 Shadow Implementation

Implemented as a separate analytics layer:

- Module: `core/sr_trade_weight_gate.py`
- Scanner integration: `cornix_agent.py`, after `RiskManager.apply(...)` in
  `AgentRunner.scan_symbol(...)`
- Output path: `logs/sr_trade_weight_shadow.csv`
- Main journal schema: unchanged (`logs/signals.csv`)
- Live behavior: unchanged

Phase 1 evaluates every final scanner candidate after entry, TP, SL, and RR are
known. It records the proposed S/R gate decision but does not use that decision
to approve, reject, sort, reroute, resize, or modify any signal.

Configuration defaults:

```env
SR_GATE_SHADOW_ENABLED=true
SR_GATE_LIVE_ENABLED=false
SR_GATE_HARD_SKIP_EFFECTIVE_RR=1.2
SR_GATE_CAUTION_EFFECTIVE_RR=1.8
SR_GATE_HARD_SKIP_ATR=0.65
SR_GATE_CAUTION_ATR=1.0
```

`SR_GATE_LIVE_ENABLED` is intentionally inert in Phase 1.

## 1. Current Capability Audit

### Scanner Entry Flow

Current execution path:

1. `cornix_agent.py`
   - `AgentRunner.run_forever()`
   - waits for the next closed 1H candle.
2. `cornix_agent.py`
   - `AgentRunner.scan_once()`
   - scans every watchlist symbol and collects candidates.
3. `cornix_agent.py`
   - `AgentRunner.scan_symbol(symbol)`
   - fetches closed 1H, 15m, and optional 4H candles.
   - calls `SignalScorer.score(...)`.
4. `cornix_agent.py`
   - `SignalScorer.score(...)`
   - creates the `TradeSignal` candidate.
5. `cornix_agent.py`
   - `AgentRunner.process_candidates(...)`
   - applies quality filters, cooldowns, BTC regime, position manager, report-only
     routing, journal logging, Entry Timing shadow logging, and Telegram routing.

### Data Fields Already Available

In `cornix_agent.py`, `TradeSignal` currently includes:

- `entry`
- `tp1`
- `tp2`
- `sl`
- `rr`
- `confidence`
- `score`
- `support`
- `resistance`
- `regime`
- `volume_spike`
- `volume_ratio`
- `atr_pct`
- `mfi`
- `mfi_confirmed`
- `body_ratio`
- `opposite_wick_ratio`
- `atr_expansion_ratio`
- `quality_flags`
- `wave_score`
- `wave_structure`
- `wave_phase`
- `wave_notes`
- `btc_regime`
- `risk_mode`
- `market_session`
- `htf_regime`
- `htf_alignment`

In `SignalScorer.score(...)`, local values already available before `TradeSignal`
is returned:

- 1H latest candle:
  - `price`
  - `candle_open`
  - `candle_high`
  - `candle_low`
  - `atr`
  - `atr_pct`
- Support / resistance:
  - `support, resistance = SupportResistanceEngine.calculate(df_1h)`
- 1H trend:
  - `trend_long`
  - `trend_short`
- 15m momentum confirmation:
  - `entry_long`
  - `entry_short`
- Breakout state:
  - `breakout_long`
  - `breakout_short`
- Volume:
  - `volume_spike`
  - `volume_ratio`
- MFI:
  - `mfi`
  - `long_mfi_confirmed`
  - `short_mfi_confirmed`
- Candle quality:
  - `body_ratio`
  - `upper_wick_ratio`
  - `lower_wick_ratio`
  - `opposite_wick_ratio`
- ATR expansion:
  - `atr_expansion_ratio`
- Market regime:
  - `regime.name`
- HTF context:
  - `htf_regime`
  - `htf_alignment`

### Current Support / Resistance Implementation

File: `cornix_agent.py`  
Class: `SupportResistanceEngine`  
Function: `calculate(df, lookback=30)`

Current formula:

```python
recent = df.tail(lookback)
support = recent["low"].min()
resistance = recent["high"].max()
```

Current limitation:

- It returns the lowest low and highest high over the latest 30 candles.
- It does not verify whether support/resistance is on the correct side of entry.
- It is displayed in Telegram/chart and used in Entry Timing shadow analytics.
- It is not currently a live hard reject gate in `SignalScorer` or
  `process_candidates`.

### Entry Timing Engine

File: `core/entry_timing_engine.py`  
Class: `EntryTimingEngine`

Current capabilities:

- `distance_to_support_pct`
- `distance_to_resistance_pct`
- `pullback_opportunity`
- `breakout_confirmation`
- `breakout_retest_confirmation`
- `overextended_move`
- `entry_quality_score`
- `recommendation`

Current mode:

- Shadow analytics only.
- Does not alter selection, routing, score, TP/SL, RR, Cornix, or outcome review.

## 2. Define Opposing S/R

The gate must not blindly use the 30-bar min/max as an opposing level unless the
level is on the correct side of entry.

### LONG

Opposing level:

- `resistance` only when `resistance > entry`

Not opposing:

- `support`

Support usage:

- Invalidation/SL context only.
- Can later help detect whether SL is structurally reasonable.

If `resistance <= entry`, then the setup is not "approaching resistance". It is
either already above the old resistance, at the level, or the level is invalid
for opposing-room calculations.

### SHORT

Opposing level:

- `support` only when `support < entry`

Not opposing:

- `resistance`

Resistance usage:

- Invalidation/SL context only.
- Can later help detect whether SL is structurally reasonable.

If `support >= entry`, then the setup is not "approaching support". It is either
already below the old support, at the level, or the level is invalid for
opposing-room calculations.

## 3. Trading Room Metrics

All formulas below should be computed after `entry`, `sl`, `tp1`, `tp2`, `atr`,
`support`, and `resistance` exist.

### Base Values

```text
entry = signal entry price
sl = stop loss price
tp1 = first target
atr = latest 1H atr14
```

Direction-aware opposing level:

```text
LONG:
  opposing_level = resistance if resistance > entry else None

SHORT:
  opposing_level = support if support < entry else None
```

### Metrics

```text
opposing_distance = abs(opposing_level - entry)
risk_distance = abs(entry - sl)
effective_sr_rr = opposing_distance / risk_distance
opposing_distance_atr = opposing_distance / atr
opposing_distance_pct = opposing_distance / entry * 100
```

TP1 clearance:

```text
tp1_distance = abs(tp1 - entry)
tp1_clearance = opposing_distance / tp1_distance
```

Interpretation:

- `tp1_clearance >= 1.0`: opposing S/R is at or beyond TP1 distance.
- `0.75 <= tp1_clearance < 1.0`: opposing S/R is close to TP1.
- `tp1_clearance < 0.75`: opposing S/R blocks much of the TP1 path.

Phase 1 stores this normalized ratio directly as `tp1_clearance`.

### Invalid / Missing Cases

Return a non-blocking unknown decision if:

- support/resistance missing
- opposing level missing because it is not on the correct side of entry
- entry <= 0
- ATR <= 0
- SL invalid or equal to entry
- risk_distance <= 0
- level is NaN or infinite

Suggested decision for unknown:

```text
sr_gate_decision = UNKNOWN
sr_score_penalty = 0
sr_gate_reason = "S/R gate unavailable: invalid or missing structure"
```

UNKNOWN should not hard skip in Phase 1 or Phase 2.

## 4. Decision Levels

### SAFE

Conditions:

- opposing level exists on the correct side of entry
- `effective_sr_rr >= 1.8`
- `opposing_distance_atr >= 1.0`
- opposing S/R does not block before TP1 in a severe way

Effect:

- no penalty
- no skip
- reason: `S/R room sufficient`

### CAUTION

Conditions:

- `1.2 <= effective_sr_rr < 1.8`
- or `0.65 <= opposing_distance_atr < 1.0`
- or TP1 is close to or slightly beyond opposing level

Phase 1 shadow effect:

- records proposed `sr_score_penalty_shadow`
- does not reduce live score/setup strength
- does not skip

Initial conservative penalty proposal:

```text
sr_score_penalty = 6 to 10
confidence_penalty = 4 to 8
```

Reasons:

- LONG: `Resistance limits upside room`
- SHORT: `Support limits downside room`

### SKIP

Proposed hard skip when the trade room is severely constrained.

Initial conservative thresholds:

```text
effective_sr_rr < 1.2
opposing_distance_atr < 0.65
tp1_clearance < 0.75
```

Phase 1 implementation logs `SKIP` when any severe room flag is true. This is a
shadow decision only, so it does not block live routing. A future live phase can
tighten this with combined conditions if shadow outcomes show over-filtering.

```text
severe_room =
  effective_sr_rr < 1.2
  or opposing_distance_atr < 0.65
  or tp1_clearance < 0.75
```

This avoids any production over-filtering in Phase 1 because `SKIP` is not used
by live signal approval.

## 5. Breakout Exception

The system should not require every signal to wait for retest.

### APPROACHING LEVEL

LONG:

- `entry < resistance`

SHORT:

- `entry > support`

Action:

- Use normal opposing S/R gate.
- Apply SAFE / CAUTION / SKIP.

### CONFIRMED BREAKOUT

LONG confirmed breakout when all are true:

- 1H candle closes above resistance or above previous 20-bar high.
- `body_ratio >= MIN_BODY_RATIO` from current config.
- `volume_spike == True` or `mfi_confirmed == True`.
- `opposite_wick_ratio <= MAX_OPPOSITE_WICK_RATIO`.

SHORT confirmed breakdown when all are true:

- 1H candle closes below support or below previous 20-bar low.
- `body_ratio >= MIN_BODY_RATIO`.
- `volume_spike == True` or `mfi_confirmed == True`.
- `opposite_wick_ratio <= MAX_OPPOSITE_WICK_RATIO`.

Action:

- Do not hard skip only because entry is close to the old level.
- Evaluate extension/fake-breakout risk instead.
- Still allow CAUTION penalty if:
  - body is weak
  - wick rejection is high
  - volume/MFI is not confirmed
  - entry is too extended from the old level.

### WEAK BREAKOUT

LONG weak breakout:

- entry is slightly above resistance or previous high
- but volume/MFI not confirmed
- or upper wick risk is high
- or body ratio is weak

SHORT weak breakdown:

- entry is slightly below support or previous low
- but volume/MFI not confirmed
- or lower wick risk is high
- or body ratio is weak

Action:

- Apply CAUTION penalty.
- Escalate to SKIP only when poor room and weak breakout conditions both exist.

Suggested weak breakout penalty:

```text
score -8 to -12
confidence -5 to -8
```

## 6. Recommended Integration Point

Recommended integration point:

```text
cornix_agent.py
SignalScorer.score(...)
after entry/sl/tp/rr are calculated
before confidence is finalized and before TradeSignal is returned
```

Reason:

- At this point, the gate has:
  - entry
  - SL
  - TP1/TP2
  - ATR
  - support/resistance
  - breakout state
  - volume spike
  - MFI confirmation
  - candle body/wick quality
  - market regime
- It can adjust final `score` and `confidence`.
- It affects candidate ranking in `select_top_candidates`.
- It can return `None` for hard skip before candidate selection.

Alternative:

`process_candidates(...)`

Pros:

- Easy to log as quality filter.
- Has complete `TradeSignal`.

Cons:

- Candidate ranking may still see inflated score before the S/R gate.
- Requires adding ATR or opposing metrics to `TradeSignal` because raw ATR is not
  currently stored as `atr14`, only `atr_pct`.

Design recommendation:

- Implement the formula in a small helper class/function.
- Call it inside `SignalScorer.score(...)`.
- For SKIP, return `None` or create a skipped candidate with a clear reason
  depending on how much measurement is desired.
- For measurement of rejected signals, prefer logging in `process_candidates`
  after preserving the decision on `TradeSignal`, or add a dedicated journal path
  for `sr_gate_shadow.csv` in Phase 1.

AI must not be involved in this gate.

## 7. Proposed Journal / Report Fields

Add fields later, not in this design-only task:

- `sr_gate_decision`
- `opposing_level`
- `opposing_distance_pct`
- `opposing_distance_atr`
- `effective_sr_rr`
- `tp1_clearance_r`
- `sr_score_penalty`
- `sr_gate_reason`
- `breakout_context`

Suggested `breakout_context` values:

- `APPROACHING_LEVEL`
- `CONFIRMED_BREAKOUT`
- `WEAK_BREAKOUT`
- `BREAKOUT_EXTENDED`
- `UNKNOWN`

Rejected signals should remain measurable:

- `signal_status = skipped_sr_gate`
- `skip_reason = sr_gate_reason`

For Phase 1 shadow mode, store:

- `sr_gate_decision_shadow`
- `sr_gate_reason_shadow`

or use the final fields with no live effect.

## 8. Rollout Plan

### Phase 1: Shadow

Behavior:

- Calculate SAFE / CAUTION / SKIP.
- Do not change score.
- Do not change confidence.
- Do not block.
- Write metrics to journal/report.
- Compare future outcomes by S/R decision.

Minimum sample:

- At least 100 closed sent signals.
- At least 30 closed CAUTION signals.
- At least 20 closed would-be SKIP signals.

### Phase 2: Weighted Live

Behavior:

- CAUTION applies penalty.
- SKIP only applies to severe cases:
  - `effective_sr_rr < 1.0`
  - and `opposing_distance_atr < 0.50`
  - and opposing level blocks before TP1.

Minimum sample before Phase 2:

- 150 to 200 closed signals total.
- CAUTION underperforms SAFE by at least 10 percentage points win rate or has
  clearly worse Net R.

### Phase 3: Calibrated

Behavior:

- Tune thresholds using actual closed outcomes.
- Consider a retest queue only for specific breakout contexts.
- Keep retest optional, not universal.

Minimum sample before full hard skip:

- 300+ closed signals.
- 50+ signals per major S/R decision bucket where possible.
- Confirm SKIP bucket has materially negative Net R and low TP1 rate.

## 9. Test Matrix

Required tests when implemented:

1. LONG with resistance far above entry
   - expect SAFE.
2. LONG with resistance before TP1
   - expect CAUTION or SKIP depending effective SR RR.
3. LONG with resistance very close
   - expect SKIP when severe room thresholds are hit.
4. SHORT with support far below entry
   - expect SAFE.
5. SHORT with support before TP1
   - expect CAUTION or SKIP depending effective SR RR.
6. SHORT with support very close
   - expect SKIP when severe room thresholds are hit.
7. Confirmed LONG breakout
   - entry above resistance with volume/MFI/body confirmation.
   - expect no hard skip solely from old resistance.
8. Weak LONG breakout
   - entry barely above resistance with weak volume/MFI or wick rejection.
   - expect CAUTION or SKIP if room is also poor.
9. Confirmed SHORT breakdown
   - entry below support with volume/MFI/body confirmation.
   - expect no hard skip solely from old support.
10. Missing S/R
    - expect UNKNOWN, no hard skip.
11. Invalid ATR
    - expect UNKNOWN or no signal, no division by zero.
12. Invalid SL
    - expect UNKNOWN, no hard skip due to metric failure.
13. Price exactly at S/R
    - expect CAUTION or SKIP based on valid direction and distance rules.
14. Resistance below LONG entry
    - not an opposing level; evaluate breakout context.
15. Support above SHORT entry
    - not an opposing level; evaluate breakout context.

## 10. Risks

- The current S/R engine is simple 30-bar high/low. It may not represent the most
  meaningful liquidity level.
- Hard skip too early may remove valid continuation breakouts.
- If S/R is not side-aware, LONG above old resistance or SHORT below old support
  may be incorrectly punished.
- If no shadow data is collected first, thresholds may overfit assumptions.
- The gate should not duplicate Entry Timing Engine recommendations unless its
  output is explicitly used for live scoring.

## 11. Files Inspected

- `cornix_agent.py`
  - `ScannerConfig`
  - `TradeSignal`
  - `MarketDataClient.fetch_closed_klines`
  - `IndicatorEngine.add_indicators`
  - `SupportResistanceEngine.calculate`
  - `MarketRegimeDetector.detect`
  - `SignalScorer.score`
  - `RiskManager.apply`
  - `TradeJournalLogger.FIELDNAMES`
  - `TradeJournalLogger.log_signal`
  - `AgentRunner.scan_symbol`
  - `AgentRunner.process_candidates`
  - `AgentRunner.select_top_candidates`
- `core/entry_timing_engine.py`
  - `EntryTimingEngine.evaluate`
  - `EntryTimingEngine._recommendation`
  - `EntryTimingLogger`

## 12. Files Changed

Design-only file added:

- `SR_TRADE_WEIGHT_GATE_V1_DESIGN.md`

No scanner logic was changed.

## 13. Non-Touched Files Confirmation

The following pre-existing working tree items were intentionally not touched by
this design task:

- `dashboard.py`
- `DASHBOARD_V2.md`
- `Command/`

No staging, commit, or push is part of this task.
