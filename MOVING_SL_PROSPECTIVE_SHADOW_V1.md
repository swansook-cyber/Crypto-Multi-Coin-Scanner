# Moving-SL Prospective Shadow V1

## Purpose

Research-only lifecycle tracking for production `signal_status=sent` signals.

The study observes what happens after TP1 is reached if the remaining-position
stop is hypothetically moved to breakeven at entry.

This does not change live scanner logic, signal approval, score/confidence,
TP/SL/RR generation, Telegram routing, Cornix messages, position watcher
behavior, or outcome review behavior.

## Configuration

```env
MOVING_SL_SHADOW_ENABLED=true
MOVING_SL_LIVE_ENABLED=false
MOVING_SL_PROSPECTIVE_START_UTC=
```

`MOVING_SL_LIVE_ENABLED` is a no-op in V1. It must not alter Telegram, Cornix,
scanner, outcome, or position watcher behavior.

## Population

The primary population is production `logs/signals.csv` rows where:

- `signal_status == sent`
- symbol, side, timestamp, entry, original SL, TP1, and TP2 are valid
- canonical signal identity can be built

Rejected, report-only, shadow-only, quality-filter-only, malformed, duplicate,
or unresolvable rows are ignored.

## Output

Shadow output is written with idempotent upsert semantics:

```text
logs/moving_sl_prospective_shadow.csv
```

One logical lifecycle row is kept per `canonical_signal_key`. If the same signal
is observed again, the row is updated instead of duplicated.

## Collection Command

Run the collector explicitly:

```bash
python -m core.moving_sl_prospective_shadow --run
```

The collector:

- reads production `logs/signals.csv`
- selects `signal_status == sent` only
- loads the project `.env` before reading `MOVING_SL_PROSPECTIVE_START_UTC`
- enforces `MOVING_SL_PROSPECTIVE_START_UTC` before any candle retrieval
- discovers new sent signals
- re-evaluates unresolved rows on later runs
- leaves resolved rows idempotent
- uses Binance Futures 15m closed candles only
- paginates Binance kline requests so multi-day lifecycles are not truncated by
  the 1000-kline limit
- never writes to `logs/signals.csv`
- never sends Telegram or Cornix messages

The collector pipeline is intentionally ordered for production safety:

```text
read signals.csv
-> signal_status == sent
-> parse/normalize UTC timestamp
-> timestamp >= MOVING_SL_PROSPECTIVE_START_UTC
-> canonical/dedupe
-> skip terminal shadow records
-> fetch Binance candles only for remaining candidates
```

If there are zero sent signals on or after the prospective start boundary, the
collector exits successfully with zero Binance requests. In that case it does
not create a historical output row. If the shadow CSV does not already exist, it
is left absent rather than creating a header-only file.

Each `--run` prints a compact summary:

- shadow enabled
- live enabled (`NO-OP` if true)
- prospective start UTC
- sent rows total
- prospective sent rows
- valid prospective candidates
- candidates needing candle evaluation
- terminal rows skipped
- Binance request count
- output rows
- elapsed seconds

`--lookahead-hours 0` means evaluate from signal timestamp through the latest
closed candle available now. A positive value caps analysis to that many hours.

## Lifecycle Classes

- `TP2_BEFORE_BE`: TP2 is reached after TP1 before price returns to entry.
- `BE_BEFORE_TP2`: price returns to entry after TP1 before TP2.
- `TP1_REACHED_UNRESOLVED`: TP1 reached, but neither TP2 nor BE is observed yet.
- `SL_BEFORE_TP1`: original SL is reached before TP1.
- `AMBIGUOUS`: candle granularity cannot prove ordering.
- `DATA_INSUFFICIENT`: reliable candles or required fields are unavailable.

## Intrabar Ambiguity Policy

The engine uses Binance Futures 15m OHLCV candles. Exact intrabar order is not
known.

If SL and TP1 occur in the same candle before the shadow action can be proven,
the lifecycle is `AMBIGUOUS`.

If TP1, BE, or TP2 ordering happens inside the same 15m candle after TP1, the
lifecycle is also `AMBIGUOUS`.

The engine does not guess from candle direction. A future V2 may use finer
granularity if it is available.

## Geometry Fields

The CSV includes:

- `mfe_after_tp1_price`
- `mfe_after_tp1_r_multiple_if_geometry_only`
- `mae_after_tp1_price`
- `mae_after_tp1_r_multiple_if_geometry_only`

These are structural measurements only. They are not portfolio R or realized
profit claims.

## Reporting

Run:

```bash
python -m core.moving_sl_prospective_shadow --report
```

`--report` is read-only. It prints the existing
`logs/moving_sl_prospective_shadow.csv` state and does not collect new candles.

The report shows:

- total prospective sent signals observed
- TP1 reached population
- resolved TP1 population
- SL before TP1
- TP2 before BE
- BE before TP2
- unresolved
- ambiguous
- data insufficient
- LONG/SHORT, session, setup-strength, tier, and month breakdowns

The report intentionally does not claim Net R improvement.

## Prospective Gate

Initial gate:

```text
KEEP RESEARCH
```

Future review target:

```text
at least 100 NEW prospective TP1-reached observations
```

Decision question:

```text
Does moving SL to BE after TP1 protect enough remaining positions
without materially reducing TP2 continuation?
```

No live Cornix recommendation should be made from this V1 shadow alone.

## Runtime Impact

The module is standalone analytics. It can run as a separate recurring pass and
does not need to run inside the scanner loop.

It performs zero Binance requests when no prospective candidates need
evaluation. It skips terminal lifecycle rows on repeated runs. New prospective
rows fetch from the signal timestamp. `TP1_REACHED_UNRESOLVED` rows with a saved
`tp1_reached_at` fetch only the continuation range from that TP1 timestamp.
Longer lifecycles may require additional paginated requests because Binance
returns a maximum of 1000 candles per request.

## Deployment Model

Recommended production deployment is a dedicated systemd oneshot service plus a
timer running every 15 minutes, separate from `crypto-scanner.service`.

Do not run the collector inside the scanner loop. It is observability-only and
should fail independently from live scanning.

Example intended command:

```bash
cd /opt/Crypto-Multi-Coin-Scanner
.venv/bin/python -m core.moving_sl_prospective_shadow --run
```

No service/timer files are created in V1 until the deployment is explicitly
approved.

## Rollback

Disable the observer:

```env
MOVING_SL_SHADOW_ENABLED=false
```

Remove the generated shadow CSV if a clean research reset is needed. Do not
alter production `logs/signals.csv`.
