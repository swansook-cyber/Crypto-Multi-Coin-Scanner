# Rejected Candidate Outcome Shadow V1

Status: Implemented as analytics-only tooling  
Scope: Production Season 1 measurement  
Live enforcement: Disabled / not implemented  

## Objective

Rejected Candidate Outcome Shadow V1 measures what would have happened to scanner candidates rejected by live filters. It is designed to answer whether each filter mainly blocks losses or also blocks recovery winners.

This system does not change live trading behavior.

It never:

- Sends rejected candidates to Telegram
- Sends rejected candidates to Cornix
- Changes scanner approval
- Changes score or setup strength
- Changes RR, TP, or SL
- Changes filter order
- Changes Loss Cooldown, Daily Risk Guard, Correlation, Quality, or BTC Regime behavior

## Inputs

Primary input files:

- `logs/signals.csv`
- `logs/rejected_signals.csv`

Production Season 1 read-only coverage audit:

| File | Rows | Rejected Rows | Usable for Backfill |
|---|---:|---:|---:|
| `logs/signals.csv` | 7,477 | 7,110 | 7,110 |
| `logs/rejected_signals.csv` | 9,547 | 9,243 | 0 |
| `logs/signals_history.csv` | 195 | 0 | 0 |
| `archive/Production_S1/logs/signals.csv` | 3,700 | 3,516 | 3,516 |
| `archive/Production_S1/logs/rejected_signals.csv` | 10,853 | 10,671 | 0 |
| `archive/Production_S1/logs/signals_history.csv` | 284 | 0 | 0 |

Total inspected rows: 32,056  
Total rejected rows: 30,540  
Total currently usable rows: 10,626

Usable rows by normalized rejection category:

| Category | Usable Rows |
|---|---:|
| `QUALITY` | 4,237 |
| `LOSS_COOLDOWN` | 3,180 |
| `DAILY_RISK_GUARD` | 1,840 |
| `CORRELATION` | 959 |
| `NOT_TOP` | 225 |
| `BTC_REGIME` | 174 |
| `OTHER` | 11 |

Key finding:

`logs/signals.csv` has enough rejected candidate fields for backfill. `logs/rejected_signals.csv` is useful for rejected activity counts, but it does not currently contain entry, SL, and TP fields needed for candle outcome simulation.

Usable rejected candidate rows need:

- `timestamp` or `timestamp_utc`
- `symbol`
- `side` or `direction`
- `entry`
- `stop_loss` or `sl`
- `tp1`
- optional `tp2`
- optional `risk_reward` or `rr`
- `signal_status`
- `skip_reason` or `reason`

Rows with missing symbol, side, timestamp, entry, SL, or TP1 are skipped safely.

## Canonical Identity

The tracker uses `core/signal_identity.py` and writes:

- `canonical_signal_key`

The key uses normalized:

- symbol
- side
- timestamp
- entry

This is compatible with existing sent/shadow linkage work and avoids treating `100`, `100.0`, and `100.000000` as different candidates.

## Rejection Categories

Raw `signal_status` and `skip_reason` are normalized into:

- `QUALITY`
- `LOSS_COOLDOWN`
- `DAILY_RISK_GUARD`
- `CORRELATION`
- `BTC_REGIME`
- `NOT_TOP`
- `OTHER`

The normalized category is stored in `rejection_reason`. The original first blocking reason is preserved in `rejection_detail`.

Important limitation:

The current scanner journal usually records the first blocking filter only. Secondary filters that would also have rejected the same candidate cannot always be reconstructed from logs.

## Outcome Methodology

The tracker evaluates candles after the rejected candidate timestamp only.

Price source:

- Binance Futures USDT-M klines
- Endpoint: `https://fapi.binance.com/fapi/v1/klines`
- Timeframe: `15m`
- Default lookahead: `REVIEW_LOOKAHEAD_HOURS`, falling back to 24 hours

LONG logic:

- TP1 hit when candle high >= TP1
- TP2 hit when candle high >= TP2
- SL hit when candle low <= SL

SHORT logic:

- TP1 hit when candle low <= TP1
- TP2 hit when candle low <= TP2
- SL hit when candle high >= SL

Same-candle handling:

- Conservative production rule is used.
- If TP and SL are touched in the same candle, SL wins first.
- No optimistic assumption is used.

## Output

Output file:

- `logs/rejected_outcome_shadow.csv`

Schema:

- `canonical_signal_key`
- `timestamp_utc`
- `symbol`
- `side`
- `entry`
- `sl`
- `tp1`
- `tp2`
- `original_rr`
- `rejection_reason`
- `rejection_detail`
- `score`
- `confidence`
- `tier`
- `session`
- `hypothetical_outcome`
- `hypothetical_r`
- `tp1_hit`
- `tp2_hit`
- `sl_hit`
- `close_timestamp`
- `resolution_hours`
- `source`

Hypothetical outcomes:

- `WIN_TP1`
- `WIN_TP2`
- `LOSS`
- `OPEN`

Hypothetical R:

- `WIN_TP1`: +1R
- `WIN_TP2`: original RR when available, otherwise +2R
- `LOSS`: -1R
- `OPEN`: 0R

## Runtime Model

The module is intentionally separate from the scanner loop:

```bash
python -m core.rejected_outcome_shadow --dry-run --limit 20
python -m core.rejected_outcome_shadow --limit 200
```

This avoids adding latency to signal scanning.

The module has a testable candle-provider interface so smoke tests do not call Binance.

## Configuration

`.env.example` includes:

```text
REJECTED_OUTCOME_SHADOW_ENABLED=true
REJECTED_OUTCOME_LIVE_ENABLED=false
```

`REJECTED_OUTCOME_LIVE_ENABLED` has no enforcement path in V1. If set to true, V1 remains analytics-only.

## Performance and Storage

Expected storage:

- One row per unique rejected candidate in `logs/rejected_outcome_shadow.csv`
- Append-only behavior for new unique canonical keys

API cost:

- Backfill requires Binance Futures kline requests.
- Run with `--limit` for small batches.
- Do not run huge unrestricted backfills during active production hours.
- V1 groups candidates by symbol and fetches reusable 15m candle ranges in Binance-sized chunks.
- It avoids the unsafe one-request-per-candidate pattern.

Future optimization:

- Cache downloaded klines per symbol/day.
- Reuse existing outcome checker market data when available.

## Analysis Enabled

The output allows later reports to estimate:

Loss Cooldown:

- blocked trades
- hypothetical WR
- hypothetical Net R
- streak bucket performance

Quality:

- hypothetical WR / Net R
- split by score, confidence, RR, and rejection detail

Correlation:

- blocked candidate outcome
- comparison against representative sent trade outcome
- cluster-level outcome

Daily Risk Guard:

- hypothetical recovery candidates after guard activation

BTC Regime:

- blocked LONG/SHORT outcomes by BTC regime reason

Not Top:

- selected top candidate versus rejected second/third candidates

## Limitations

- This is hypothetical outcome tracking, not proof that a human would have entered the trade.
- Slippage, spread, funding, execution delay, and missed fills are not modeled.
- If TP and SL touch in the same candle, the conservative SL-first rule may understate best-case performance.
- If rejected rows lack TP/SL/entry, they cannot be backfilled.
- Current logs mainly preserve first blocking reason, not full filter overlap.

## Rollback

Because this feature is isolated:

1. Stop running `python -m core.rejected_outcome_shadow`.
2. Keep or archive `logs/rejected_outcome_shadow.csv`.
3. Leave scanner services untouched.

No production service restart is required to disable this analytics workflow.

## Safety Declaration

Rejected Candidate Outcome Shadow V1 is report-only analytics. It does not affect live signal generation, approval, routing, Cornix output, Telegram output, TP/SL/RR calculations, or production outcome review.
