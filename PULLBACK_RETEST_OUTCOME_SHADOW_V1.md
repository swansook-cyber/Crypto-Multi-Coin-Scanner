# Pullback / Retest Outcome Shadow V1

Status: analytics-only shadow research  
Production mode: no live enforcement  
Created: 2026-08-27

## Hypothesis

After a strong directional move or highly profitable period, the scanner may enter late continuation signals while earlier traders are taking profit. A deterministic pullback/retest entry may reduce SL frequency, improve effective RR, or reveal that many trades simply run away without a retest.

This module tests that hypothesis without changing production behavior.

## Safety

This module does not:

- change scanner entry logic
- change score or setup strength
- change SR/Entry Timing/Market Exhaustion classifications
- change correlation, cooldown, quality, BTC regime, or production universe filters
- change TP/SL/RR production calculations
- send Telegram or Cornix messages
- execute trades
- add any live enforcement path

`PULLBACK_RETEST_LIVE_ENABLED` is intentionally ignored in V1.

## Source Populations

The module reads historical CSVs and evaluates candidates with enough structure:

- `logs/signals.csv`
- `logs/sr_trade_weight_shadow.csv`
- `logs/entry_timing_engine.csv`
- `logs/market_exhaustion_shadow.csv`

Source populations are preserved in output:

- `sent`
- `rejected`
- `wait_pullback`
- `sr_caution_skip`
- `exhaustion`

The CLI can filter with:

```bash
python -m core.pullback_retest_outcome_shadow --population sent
python -m core.pullback_retest_outcome_shadow --population wait_pullback
python -m core.pullback_retest_outcome_shadow --population sr
python -m core.pullback_retest_outcome_shadow --population exhaustion
```

## Canonical Linkage

Candidate identity uses `core/signal_identity.py` semantics:

- normalized symbol
- normalized side
- UTC timestamp rounded to second
- entry rounded to 6 decimals

This keeps linkage compatible with existing shadow analytics and older logs that do not contain `canonical_signal_key`.

## Context Enrichment V1

The shadow module now enriches each candidate with frozen pre-signal context so pullback/retest performance can be segmented by market exhaustion and prior profit-taking pressure.

Context sources:

- `logs/sr_trade_weight_shadow.csv`
- `logs/entry_timing_engine.csv`
- `logs/market_exhaustion_shadow.csv`
- closed outcomes in `logs/signals.csv`
- Binance Futures 15m candles fetched around the candidate window

Linkage is deterministic:

- primary match: `canonical_signal_key`
- fallback match: normalized `timestamp_utc + symbol + side + entry`
- ambiguous fallback matches are ignored rather than guessed

No loose time-window matching is used.

Frozen pre-signal fields:

- `prior_24h_move_atr`
- `prior_session_move_atr`
- `directional_run`
- `ema20_distance_atr`
- `ema50_distance_atr`
- `atr_expansion_ratio`
- `prior_day_known_net_r`
- `prior_day_known_wins`
- `prior_day_known_losses`
- `trailing_24h_known_net_r`
- `trailing_24h_known_wins`
- `trailing_24h_known_losses`
- `context_flags`

Context flags:

- `PRIOR_MOVE_STRONG`
- `PRIOR_MOVE_VERY_STRONG`
- `PRIOR_DAY_PROFITABLE`
- `PRIOR_DAY_STRONGLY_PROFITABLE`
- `EXTENDED_CONTEXT`
- `EXHAUSTED_CONTEXT`
- `SR_NEAR_OPPOSING`

Prior performance context uses only outcomes with `closed_at` earlier than the candidate timestamp. Open, unresolved, and future-closed trades are excluded.

The candle cache fetches a bounded pre-signal lookback for context, but retest fill and outcome simulation still evaluate only candles at or after the candidate timestamp. Context enrichment does not change retest targets, production entries, scores, routing, or any live scanner behavior.

## Retest Strategies

All targets are deterministic and are calculated before future candles are inspected.

| Strategy | LONG target | SHORT target |
|---|---|---|
| `ATR_PULLBACK_0_30` | `entry - 0.30 * ATR` | `entry + 0.30 * ATR` |
| `ATR_PULLBACK_0_50` | `entry - 0.50 * ATR` | `entry + 0.50 * ATR` |
| `ATR_PULLBACK_0_75` | `entry - 0.75 * ATR` | `entry + 0.75 * ATR` |
| `EMA_RETEST` | frozen EMA20-distance proxy | frozen EMA20-distance proxy |
| `BREAKOUT_RETEST` | known resistance below entry | known support above entry |
| `SR_RETEST` | known support below entry | known resistance above entry |

If a strategy cannot be computed from data available at the candidate timestamp, it records `NOT_APPLICABLE`.

## Wait Windows

Fixed wait windows:

- 45 minutes
- 90 minutes
- 180 minutes

No strategy waits indefinitely.

## Fill And Invalidation

LONG:

- retest fills only if future low reaches target entry
- original SL/invalidation before retest creates `INVALIDATED_BEFORE_RETEST`

SHORT:

- retest fills only if future high reaches target entry
- original SL/invalidation before retest creates `INVALIDATED_BEFORE_RETEST`

If retest and SL ambiguity appears in the same candle before entry, invalidation wins conservatively.

Possible retest statuses:

- `RETEST_FILLED`
- `NO_RETEST`
- `INVALIDATED_BEFORE_RETEST`
- `DATA_INSUFFICIENT`
- `NOT_APPLICABLE`

`NO_RETEST` is never counted as a win. It is opportunity cost.

## Outcome Models

Model A: `ORIGINAL_STRUCTURE`

- keep original SL
- keep original TP1/TP2
- use retest entry
- effective RR changes naturally
- `effective_rr_tp1_at_retest` and `effective_rr_tp2_at_retest` show the improved or worsened geometry from the retest entry

Model B: `PRESERVED_RISK_RR`

- use retest entry
- preserve original risk distance
- derive TP1/TP2 from original RR convention

Reports must not mix Model A and Model B. Model A is primary unless a later audit justifies otherwise.

## R Accounting

V1 keeps outcome-R mapping conservative and explicit:

- `LOSS = -1R`
- `WIN_TP1 = +1R`
- `WIN_TP2 = effective_rr_tp2_at_retest` when calculable
- `NO_RETEST = 0R` in full-policy comparison
- `INVALIDATED_BEFORE_RETEST = 0R` in full-policy comparison
- `DATA_INSUFFICIENT` is excluded from policy performance
- `NOT_APPLICABLE` is excluded from all performance denominators

`effective_rr_at_retest` is kept for backward compatibility and represents TP2 effective RR. New fields make this explicit:

- `effective_rr_tp1_at_retest`
- `effective_rr_tp2_at_retest`

This means improved entry geometry is visible separately even when `WIN_TP1` is still scored as `+1R`.

## Comparison Types

Summary output separates two comparisons.

Matched-fill comparison:

- population is only rows where retest filled and hypothetical outcome resolved
- compares retest R against original R for the exact same candidates
- reports matched original WR/Net R, retest WR/Net R, matched R delta, loser-to-winner, and winner-to-loser

Full-policy / opportunity-cost comparison:

- population is all applicable rows except `DATA_INSUFFICIENT`
- filled trades use retest R
- `NO_RETEST` and `INVALIDATED_BEFORE_RETEST` use `0R`
- compares policy R against original R for the same applicable/evaluable candidates
- reports winners missed and losses avoided due to no entry

## Output

CSV:

```text
logs/pullback_retest_outcome_shadow.csv
```

Important fields:

- `dedupe_key`
- `canonical_signal_key`
- `source_population`
- `model`
- `strategy`
- `wait_window_minutes`
- `target_retest_entry`
- `retest_status`
- `effective_rr_at_retest`
- `retest_outcome`
- `retest_r`
- `r_delta_vs_original`
- `sl_avoided`
- `winner_missed`
- `loser_to_winner`
- `sr_class`
- `entry_timing_class`
- `exhaustion_class`
- `prior_24h_move_atr`
- `prior_session_move_atr`
- `prior_day_known_net_r`
- `trailing_24h_known_net_r`
- `context_flags`
- `pre_retest_mae_atr`
- `pre_retest_mfe_atr`

Dedupe key:

```text
canonical_signal_key|strategy|wait_window|model
```

## CLI

Dry run:

```bash
python -m core.pullback_retest_outcome_shadow --dry-run --limit 20
python -m core.pullback_retest_outcome_shadow --dry-run --limit 100
```

Write analytics output:

```bash
python -m core.pullback_retest_outcome_shadow --limit 100
python -m core.pullback_retest_outcome_shadow
```

Dry run never creates or writes the output CSV.

## API And Backfill Strategy

The module fetches Binance Futures 15m candles only for historical simulation when no test candle provider is injected.

Cost controls:

- groups candle fetches by symbol and time range
- reuses candle cache across strategies/windows/models
- never performs one API request per candidate/strategy
- prints estimated Binance API request count
- `--limit` should be used before any large backfill

Recommended rollout:

1. `--dry-run --limit 20`
2. `--dry-run --limit 100`
3. `--limit 100` only after cost and results are acceptable
4. Full backfill only after estimating request count

## Anti-Bias Controls

- Retest target is calculated before future candles are inspected.
- Changing future outcome does not change target entry.
- Future candles may only determine retest fill and hypothetical outcome.
- Fixed wait windows prevent hindsight-selected waiting.
- `NO_RETEST` is preserved separately from wins/losses.
- Same-candle ambiguity is conservative.
- Prior-day outcome context must only use outcomes resolved before candidate timestamp.

## Limitations

- EMA retest uses frozen distance context when actual EMA level is unavailable.
- Prior 24h/session move and prior-day known Net R fields are placeholders in V1 unless data is available without leakage.
- Backtest results are not live trading recommendations.
- A strong retest result does not automatically justify live enforcement without stable sample size across symbols, sessions, months, and regimes.

## Decision Framework

Strategy labels for future reports:

- `NOT USEFUL`
- `KEEP SHADOW`
- `PROMISING`
- `STRONG SHADOW CANDIDATE`
- `LIVE CANDIDATE`

`LIVE CANDIDATE` requires:

- adequate sample size
- stable month/session/direction behavior
- positive or materially improved R
- acceptable `NO_RETEST` opportunity cost
- no severe instability by symbol, session, or direction

No strategy is promoted live in V1.
