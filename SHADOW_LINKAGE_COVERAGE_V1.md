# Shadow Linkage Coverage V1

## Purpose

Improve how shadow analytics rows link back to real sent signal outcomes without
changing trading behavior.

This is analytics infrastructure only.

It does not change:

- scanner logic
- score/confidence/ranking
- signal approval
- TP/SL/RR
- Telegram/Cornix routing
- Production Universe logic
- dashboard behavior

## Canonical Signal Key V1

Primary helper:

```text
core/signal_identity.py
```

Canonical key format:

```text
sig:v1:<SYMBOL>|<SIDE>|<UTC_TIMESTAMP_SECONDS>|<ENTRY_6DP>
```

Example:

```text
sig:v1:BTCUSDT|LONG|2026-01-01T00:00:00Z|100.000000
```

If a true `signal_id` or `candidate_id` exists, the helper can use:

```text
id:v1:<id>
```

Current production signal rows mostly do not have a stable ID, so the derived key
uses:

- normalized symbol
- normalized LONG/SHORT side
- UTC timestamp rounded to seconds
- entry rounded to 6 decimals

## Normalization Rules

Symbol:

- `BINANCE:BTCUSDT.P` -> `BTCUSDT`
- `BTC/USDT` -> `BTCUSDT`
- `#BTC/USDT` -> `BTCUSDT`

Side:

- `BUY` -> `LONG`
- `SELL` -> `SHORT`
- case-insensitive

Timestamp:

- UTC normalized
- `Z` and `+00:00` treated equally
- milliseconds/microseconds removed
- second precision by default

Entry:

- fixed 6 decimal places
- `100`, `100.0`, and `100.000000` match

## Shadow CSV Schema

New shadow rows include:

```text
canonical_signal_key
```

This is added only to shadow CSVs:

- `logs/entry_timing_engine.csv`
- `logs/sr_trade_weight_shadow.csv`
- `logs/market_exhaustion_shadow.csv`

`logs/signals.csv` is not migrated or modified.

Existing shadow rows remain compatible. The resolver derives the same key from
their existing timestamp/symbol/side/entry fields when `canonical_signal_key` is
missing.

## Resolver Fallback Order

The read-only resolver uses:

1. `canonical_signal_key`, if present.
2. signal/candidate ID, if present.
3. normalized timestamp + symbol + side + entry.
4. conservative timestamp tolerance fallback when exact key misses.

Timestamp tolerance:

```text
<= 1 second
```

The tolerance fallback only matches when there is exactly one candidate with the
same symbol, side, and normalized entry. It avoids broad fuzzy joins.

## Population Semantics

Shadow rows can represent different populations:

- `CLOSED_SIGNAL`
- `OPEN_SIGNAL`
- `SENT_MATCH`
- `REJECTED_CANDIDATE`
- `UNMATCHED`

Rejected candidates are not counted as linkage failures. They are a valid shadow
population because some shadow engines evaluate candidates before final routing.

Outcome correlation should use only:

```text
link_status == CLOSED_SIGNAL
```

## Archive Handling

`shadow_linkage_coverage.py` indexes:

- current `logs/signals.csv`
- current `logs/signals_history.csv`
- archived `archive/**/logs/signals.csv`
- archived `archive/**/logs/signals_history.csv`

When duplicate active/archive rows exist, the resolver prefers:

1. active `logs/signals.csv`
2. active `logs/signals_history.csv`
3. archived `signals.csv`
4. archived `signals_history.csv`

This prevents old archive rows from overriding newer active outcomes.

## Coverage Tool

Command:

```bash
python shadow_linkage_coverage.py
```

JSON:

```bash
python shadow_linkage_coverage.py --json
```

Custom base path:

```bash
python shadow_linkage_coverage.py --base /opt/Crypto-Multi-Coin-Scanner
```

The tool is read-only. It prints:

- total shadow rows
- unique shadow signals
- matched sent
- matched closed
- matched open
- rejected candidates
- unmatched
- ambiguous
- duplicate shadow rows
- closed coverage percentage
- unmatched cause counts
- benchmark timing

## Production Snapshot Context

From `PRODUCTION_SHADOW_OUTCOME_CORRELATION_AUDIT_V1.md`, before this identity
standardization:

| Shadow | Rows | Matched Closed | Confidence |
|---|---:|---:|---|
| Entry Timing | 242 | 27 | MODERATE |
| SR Trade Weight | 3,604 | 25 | MODERATE |
| Market Exhaustion | 703 | 12 | WEAK |

Expected immediate effect:

- Historical rows still join through derived keys.
- New rows will carry `canonical_signal_key` directly.
- False unmatched rows from timestamp/float formatting should decrease.
- Coverage must not decrease.

This change does not make any shadow layer live-ready by itself.

Read-only VPS validation with the V1 compatibility resolver:

| Shadow | Rows | Matched Sent | Matched Closed | Rejected Candidates | Unmatched | Ambiguous |
|---|---:|---:|---:|---:|---:|---:|
| Entry Timing | 242 | 81 | 79 | 56 | 105 | 0 |
| SR Trade Weight | 3,604 | 79 | 77 | 1,061 | 2,464 | 0 |
| Market Exhaustion | 703 | 14 | 12 | 617 | 72 | 0 |

Indexed production signals:

- Signal rows indexed: 5,270.
- Closed sent signals: 364.
- Benchmark: 11.47 seconds, about 856 rows/second.

The large Entry Timing and SR improvement comes from deterministic
timestamp/float normalization, active/archive source-priority dedupe, and
separating rejected candidates from true linkage failures. Market Exhaustion
matched-closed coverage did not improve because most existing rows belong to
rejected candidates rather than sent closed signals.

## Ambiguity Rules

Ambiguous rows are reported, not guessed.

A match is considered ambiguous when multiple shadow rows resolve to the same
canonical signal identity. Duplicate rows are excluded from coverage inflation by
counting matched closed signals by unique canonical key.

## Rollback

Rollback is simple because this is additive:

1. Revert the code that writes `canonical_signal_key`.
2. Existing shadow CSVs can keep the extra column; older readers ignore it.
3. No scanner journal schema migration is required.
4. No trading behavior depends on the new field.

## Safety

This feature is designed to improve analytics confidence only.

It does not enable:

- live SR Gate penalty
- Market Exhaustion penalty
- Entry Timing routing
- hard skip
- score changes
- Telegram routing changes
- Cornix changes
