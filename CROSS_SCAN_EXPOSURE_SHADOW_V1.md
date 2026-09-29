# Cross-Scan Exposure Shadow V1

## Scope and safety

This component is analytics-only. It records what a prospective correlated-
exposure guard would have done while the existing scanner continues to log and
route every live signal exactly as before. No shadow decision is returned to a
filter, selector, Telegram route, Cornix route, or order path.

## Existing correlation audit

The production scanner currently has two same-scan concentration controls in
`AgentRunner.select_top_candidates`:

- `MAX_SIGNALS_PER_DIRECTION_PER_CANDLE` limits signals with the same side in
  one scan.
- `MAX_MAJOR_CORRELATED_SIGNALS` limits a static major-symbol set
  (`BTCUSDT`, `ETHUSDT`, `SOLUSDT`, `LTCUSDT`) in one scan.

There was no pairwise return-correlation engine and no stateful cross-scan
correlation cap. V1 therefore retains those live controls unchanged and adds a
separate quantitative shadow comparison.

## V1 prospective rule

For a candidate that has already passed all live filters and routing checks:

1. Reconstruct explicit active lifecycles from `logs/signals.csv` using only
   `signal_status=SENT` and `result=OPEN`.
2. Exclude an unresolved row after 24 hours as a stale-state fail-safe.
3. Compare the candidate with shadow-retained, same-side open positions using
   Pearson correlation of 1H percentage returns.
4. Cache each close against its UTC candle `close_time`; sort and deduplicate
   timestamps, inner-join raw prices, then calculate returns on the common
   timeline. Positional/RangeIndex histories fail open and are never correlated.
5. Use 72 bars, require at least 48 aligned observations, and define material
   positive correlation as `r >= 0.75`.
6. `WOULD_BLOCK` when one materially correlated same-side shadow-retained
   position is already open; otherwise `ALLOW`. `CAUTION` is supported when a
   future/configured threshold permits more than one correlated position.
7. Missing correlation data always fails open (`ALLOW`). BTC regime, session,
   setup strength, confidence, and MFI are not rule inputs.

The live default is one retained position per correlated same-side cluster.
Thresholds are recorded on every row so prospective evidence remains auditable.

## Exposure and representative state

Outcome-updated `signals.csv` is the lifecycle authority. WIN/LOSS rows and
report-only signals are not treated as open. State is rebuilt from disk on every
evaluation, so close events and scanner restarts do not leave an in-memory
position behind.

Representative selection is `FIRST_IN_CLUSTER`: the oldest materially
correlated, same-side, shadow-retained open signal remains the representative.
A live signal marked `WOULD_BLOCK` is still sent, but is excluded from later
shadow-retained exposure so the counterfactual remains internally consistent.
Both actual open counts and shadow-retained counts are recorded.

Clusters are direct-to-retained, not transitive graphs. If A is retained, B is
correlated only to A and becomes `WOULD_BLOCK`, then C correlated only to B but
not A is `ALLOW`: B never enters the retained counterfactual state.

## Prospective boundary and persistence

On first initialization the logger writes
`logs/cross_scan_exposure_shadow_v1.state.json` with
`CROSS_SCAN_EXPOSURE_SHADOW_START_UTC`. Restarts reuse that boundary. Operators
may provide `CROSS_SCAN_EXPOSURE_SHADOW_START_UTC` only before the first state is
created. Candidate timestamps equal to or later than the boundary are eligible;
timestamps even one microsecond earlier are excluded and cannot enter the
primary CSV. Historical/design replay must use separate output.

Primary output is `logs/cross_scan_exposure_shadow_v1.csv`. Runtime logs and
state remain Git-ignored. A persistent `.lock` file uses an OS advisory lock;
file existence is not ownership, so crashed processes cannot leave a stale
logical lock. State creation, schema migration, candidate persistence, and
outcome enrichment write a same-directory temporary file, flush/fsync it, and
atomically replace the target. Lock acquisition is bounded; contention skips
the uncertain shadow write while live routing continues unchanged.

## Outcome linkage and evaluation

At the start of each scan, existing shadow rows are enriched from the outcome-
updated signal journal. When the read-only execution-truth CSV contains a
matching lifecycle, its status, PnL, and R are copied into the shadow output.
Neither source file is modified.

Promotion must not be considered until there are at least 30 `WOULD_BLOCK`
observations, preferably 50, across multiple separate clusters. Required review
metrics are blocked wins/losses, Net R delta, avoided cluster severity, and
winner opportunity cost.

## Sep-23 historical design sanity check

A read-only check against Binance Futures 1H candles available before each
historical signal produced these pair correlations against the then-retained
open exposure:

- ARB–DOGE: `0.241`
- XRP–DOGE: `0.568` (XRP–ARB: `0.231`)
- LTC–DOGE: `0.456`
- APT–LTC: `0.470`
- ADA–LTC: `0.704` (ADA–APT: `0.497`)

At the untuned V1 material-correlation threshold of `0.75`, DOGE, ARB, XRP,
LTC, APT, and ADA would all be `ALLOW`. Therefore the earlier broad same-side
counterfactual improvement of about `+1.8R` must not be attributed to this
pair-correlation rule. Lowering the threshold solely to catch Sep-23 would be
event-specific tuning and is intentionally not done. The shadow is suitable for
prospective measurement, not live promotion.

The historical candle sets used for this check had complete common timestamps,
so correcting timestamp alignment did not materially change these correlations.

## Runtime schema

The CSV records candidate identity and timestamp; symbol/side; setup strength
and confidence; actual and shadow-retained exposure counts; correlated symbols;
maximum pair correlation; cluster and representative identity; decision and
reason; oldest exposure age; BTC regime/session; stale exclusions; exact rule
parameters; unchanged live result; final theoretical outcome/R; execution-truth
status/PnL/R; prospective boundary; version; and generation time.
`persistence_status` records persisted, duplicate, pre-boundary, invalid-time,
or lock-contention outcomes. Enrichment performs no rewrite when values are
unchanged.

## Operational switches

- `CROSS_SCAN_EXPOSURE_SHADOW_ENABLED` (default `true`)
- `CROSS_SCAN_EXPOSURE_CORRELATION_THRESHOLD` (default `0.75`)
- `CROSS_SCAN_EXPOSURE_LOOKBACK_BARS` (default `72`)
- `CROSS_SCAN_EXPOSURE_MIN_OBSERVATIONS` (default `48`)
- `CROSS_SCAN_EXPOSURE_MAX_OPEN` (default `1`)
- `CROSS_SCAN_EXPOSURE_STALE_HOURS` (default `24`)
- `CROSS_SCAN_EXPOSURE_SHADOW_START_UTC` (first initialization only)

There is deliberately no live-enable switch in V1.
