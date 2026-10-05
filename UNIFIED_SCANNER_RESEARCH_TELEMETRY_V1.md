# Unified Scanner Research Telemetry V1

## Scope and safety contract

This is an analytics-only sidecar. `cornix_agent.py` completes its existing live
decision and journal write before asking the telemetry adapter to persist an
immutable snapshot. Database initialization, lock, corruption, migration, and
write failures are caught by `FailOpenResearchTelemetry`; they emit one concise
warning and do not change selection, scoring, confidence, setup strength,
filters, TP/SL/RR, universe, BTC logic, correlation logic, Telegram, Cornix, or
exchange behavior. The telemetry module has no Binance or Telegram client.

The default database is `research/scanner_research_v1.db`. SQLite database,
WAL, and shared-memory files under `research/` are ignored by Git.

## Existing research-data inventory

The inventory below records what is available from current source code and
runtime artifacts. Empty or absent runtime files remain `N/A`; V1 does not
invent values.

| Source | Candidate-time evidence available | Outcome/execution evidence | V1 disposition |
|---|---|---|---|
| `logs/signals.csv` / `TradeJournalLogger` | timestamp, symbol, side, entry, SL, TP1/2, RR, confidence, setup strength, score/raw score/bucket, tier, MFI/confirmation, body ratio, opposite wick, ATR expansion, quality flags, wave score/structure/phase/notes, BTC regime/risk notes, session, 1H/4H regime/alignment, status and skip reason | result, target, close time, max profit/drawdown, outcome alert identity | Keep; historical backfill and outcome-enrichment source |
| `logs/signals_history.csv` | tier/session, entry/SL/TP, RR, setup strength, score, regime/alignment, volume spike, MFI, ATR, body, ATR expansion, BTC regime | result, PnL, holding time, outcome | Optional historical backfill only |
| Entry Timing (`logs/entry_timing_engine.csv`) | canonical key, support/resistance, ATR proxy and distances, pullback, breakout/retest, overextension, timing score/recommendation/reason, source score/setup strength/regime/volume/MFI/status | none | Continue CSV; dual-evaluate into DB at final decision |
| SR trade-weight shadow | canonical key, support/resistance, ATR, opposing level/distance/%/ATR, effective SR RR, clearance, decision, penalty, breakout context/reason | none | Continue CSV; dual-write decision/metrics into DB |
| Market Exhaustion shadow | swing and EMA distances in ATR, 1H/15m directional run, ATR expansion, RSI, MFI, breakout/momentum context, class, penalty, reason | none | Continue CSV; dual-write decision/metrics into DB |
| Setup Strength prospective shadow | canonical key, setup strength/class, score/confidence/tier/session/status/reason, entry/SL/TP, SR/exhaustion/timing classes, BTC/cooldown/risk state | linked signals outcome in report path | Continue CSV; DB computes the same observational class |
| Cross-scan exposure shadow | existing/retained open totals and same-side counts, correlation count/symbols/max, cluster/exposure count, decision/reason, representative identity, open age, BTC/session and rule parameters | final outcome/R, execution truth status/PnL/R | Keep the SENT-only live CSV unchanged; independently snapshot cached correlation evidence into SQLite for every constructed final candidate |
| Cluster representative shadow | cluster identity, candidate/representative identity and selection evidence | hypothetical outcome/R reports | Compatibility ingestion/offline computation; no scanner dependency |
| Moving-SL prospective shadow | canonical key, original risk/targets, moving-SL state and prospective metadata | modeled comparison outcome | Compatibility ingestion; remains independent because it observes post-entry path state |
| Rejected candidate outcome shadow / `logs/rejected_signals.csv` | timestamp, symbol, side, tier, session, score, setup strength, regime/alignment, rejection reason/status; richer values when present in `signals.csv` | hypothetical result/R, target flags, resolution timestamp/hours | DB captures constructed rejected candidates prospectively; existing outcome collector remains independent in V1 |
| Pullback/retest outcome shadow | canonical candidate facts and pullback/retest observations | hypothetical result/R | Compatibility ingestion/offline analysis |
| Binance Execution Truth (`logs/binance_execution_truth_v1.csv`) | canonical key and signal facts copied by its collector | match status, fills/VWAP, realized PnL, commission, funding, finality, evidence validity, gross/net/execution R | Read-only enrichment source; telemetry never calls Binance |
| Performance/diagnostic CSVs | aggregated direction/session/symbol/tier/score and loss/win-cluster reports | aggregated outcomes | Reports remain independent; not treated as row-level truth |

Additional values present in the live `TradeSignal` and candle frames are now
snapshotted before later candles can replace them: 1H RSI/MFI/ATR/ATR%, 1H
EMA20/50, 15m EMA9/21 and RSI, raw 1H volume and volume ratio, upper/lower and
opposite wick ratios, breakout confirmation, wave fields, support/resistance
diagnostics, closed-candle timestamp, and volatility/regime context.

Unavailable values are stored as SQL `NULL`, not zero or guessed. Every
`TradeSignal` that is constructed and reaches a final `SENT`, `REJECTED`,
`SKIPPED`, or `REPORT_ONLY` status is captured. Early scorer/fetch exits remain
separate immutable `WAIT` observations; no fake `TradeSignal` or candidate key
is manufactured.

## Schema

Schema version 3 uses these normalized tables (v3 changes only enrichment
operations metadata; candidate evidence remains immutable):

- `research_meta`: schema version and the single prospective boundary.
- `scanner_runs`: deterministic scan identity and source provenance.
- `candidates`: immutable identity, final decision, prices, quality, and source.
- `candidate_features`: typed common feature columns plus `extras_json`.
- `market_context`: BTC, 1H/4H trend, volatility, session/time and mix context.
- `exposure_snapshots`: open/same-side/prior-window risk, correlation and cluster context.
- `shadow_decisions`: many versioned evaluator decisions per candidate.
- `signal_outcomes`: idempotent modeled-outcome enrichment by canonical signal key.
- `execution_truth_links`: idempotent execution enrichment by canonical signal key.
- `enrichment_status`: attempt/success timestamps, timestamp high-water,
  source/matched/updated counts, and error/health status.
- `pre_candidate_observations`: deterministic pre-construction WAIT evidence,
  including exit category, available scores/indicators/regime/session, and
  source provenance.

Foreign keys are enabled and child rows cascade only if a candidate is manually
removed; no retention/deletion command is supplied. Typed columns cover stable
features, while experimental evidence belongs in sorted JSON with
`feature_schema_version=1`.

## Candidate identity

`candidate_key` is SHA-256 of:

`v1 | run_id | normalized symbol | normalized side | closed-candle UTC | candidate stage`

`run_id` is deterministic from the scheduled scan-candle UTC (falling back to
scan start only for ad-hoc callers without a candle). Therefore a process
restart replay of the same scheduled candle is idempotent, while the same symbol
and side on a later candle/scan run remains distinct. The canonical
sent-signal key is stored separately and remains the join key for outcomes and
execution truth. `INSERT OR IGNORE` plus the primary key guarantees one primary
candidate row; feature/context/exposure rows are inserted in the same short
transaction and are immutable on replay.

`observation_key` is SHA-256 of `v1 | run_id | normalized symbol | scan-candle
UTC | stage | normalized side hint | category`. Replay of the same exit is
idempotent and the next candle is distinct. Observation timestamps use the same
prospective boundary as candidates; the scan candle may legitimately predate
activation because it identifies the closed market-data interval.

## Prospective boundary and historical policy

The database creates `prospective_start_utc` exactly once, at first database
activation. A `PROSPECTIVE` candidate older than that boundary is rejected.
The boundary is never inferred from an old CSV and is not reset by restart or
migration.

Historical import uses `source_mode=HISTORICAL_BACKFILL`, `source_name`,
`source_version`, and `imported_at_utc`. It imports only source-backed columns.
It never recomputes missing historical indicators from newer candles. Analysis
queries default to prospective rows so backfill cannot silently contaminate
prospective evidence. Backfill run identity is based on logical candle and
source name, never CSV row position, so reordering an input file is idempotent.

Safe import and enrichment examples:

```bash
python -m core.research_telemetry backfill --signals logs/signals.csv
python -m core.research_telemetry backfill --signals logs/signals.csv \
  --shadow setup_strength_v1=logs/setup_strength_prospective_shadow.csv
python -m core.research_telemetry enrich-outcomes --signals logs/signals.csv
python -m core.research_telemetry enrich-execution --execution logs/binance_execution_truth_v1.csv
python -m core.research_telemetry enrich-all
```

Both enrichment functions are incremental-safe/idempotent upserts. Resolved
outcomes cannot regress to `OPEN`, and authoritative execution evidence cannot
regress to pending/ambiguous evidence when duplicate rows are encountered.
They read CSV artifacts and never write `signals.csv` or call Binance.

## Same-side exposure and regime evidence

At the final decision, the scanner snapshots actual open total/same/opposite,
same-side altcoin count, same-scan LONG/SHORT/side counts, same-side entries in
the prior 1/3/6 hours, cumulative modeled same-side risk when derivable (one
normalized R per current open signal), open-position ages, candidate sequence
after first exposure, BTC regime, and available cross-scan correlation/cluster
metrics. Missing evidence remains null or is clearly derived from the existing
signals journal. No blocking rule is added.

Correlation is evaluated for all constructed final candidates from the already
cached per-symbol close series and one open-exposure-state load per scan. It
performs no network request. Stored evidence includes evaluation status,
unavailability reason, lookback/minimum-observation/threshold parameters,
pair-observation counts, all correlated signal keys, maximum correlation over
evaluable same-side pairs, and deterministic cluster representative when the
threshold is met. If pair evidence is unavailable, correlation values and the
correlated count remain SQL `NULL`; a known absence of same-side open exposure
records count zero with an explicit reason.

All-candidate same-scan counts and SENT-only same-scan exposure counts are kept
separately. Rejected candidates therefore do not inflate prior-entry windows,
cumulative modeled exposure, or sequence-after-exposure.

Regime research fields include ATR/ATR%, ATR expansion, breakout confirmation,
body and both wick ratios, EMA values and exhaustion-derived EMA/ATR distances,
BTC regime, current LONG/SHORT mix, session, UTC/local hour, day, 1H/4H trend,
and existing volatility context. No new market provider is used.

## Shadow migration matrix

| Shadow | V1 class | Reason / next state |
|---|---|---|
| Setup strength | B: dual-write | Simple decision from immutable setup strength; keep prospective CSV boundary |
| SR trade weight | B: dual-write | Computed live already; capture decision/metrics without enforcement |
| Market exhaustion | B: dual-write | Computed live already; capture existing result without enforcement |
| Entry timing | B: dual-write | Re-evaluate the same pure rule for DB snapshot; keep CSV writer |
| Cross-scan exposure | B: dual-write | Preserve the stateful SENT-only CSV collector exactly; use cached runtime inputs for SQLite coverage of all constructed decisions |
| Cluster representative | C: ingest / D: offline | Join existing rows now; future rules can use exposure snapshot offline |
| Moving SL | A: independent / C: ingest | Requires future price-path observations, so it cannot be only a decision-time rule |
| Rejected outcome | A: independent enrichment | Requires future candles; candidate facts now centralize in DB |
| Pullback/retest outcome | A: independent / C: ingest | Requires post-candidate path observations |
| Future MFI/session/breakout/ATR/exposure hypotheses | D: offline | Required typed evidence is captured; no scanner collector needed |

No existing shadow file or prospective boundary is deleted or replaced in V1.

## SQLite safety and operations

Every connection enables `foreign_keys=ON`, `busy_timeout=1500ms`, WAL journal
mode, and `synchronous=NORMAL`. A candidate uses one short atomic transaction.
No network work, Telegram delivery, charting, or analytical query is performed
while holding the write transaction. Read-only reporting opens SQLite in
`mode=ro`. Indexed paths cover time, run, population, signal key, symbol/side/
candle, shadow decision, result, and execution eligibility. Schema DDL and the
version marker commit in one explicit transaction. Unsupported future versions
are rejected before any DDL, and each ordered schema migration plus version
marker is committed atomically or rolled back completely.

Read-only commands:

```bash
python -m core.research_telemetry status
python -m core.research_telemetry summary
python -m core.research_telemetry shadows
python -m core.research_telemetry coverage
python -m core.research_telemetry health
python -m core.research_telemetry analysis long_vs_short
```

The `analysis` command includes side, session,
setup-strength band, MFI band, ATR-expansion band, breakout, same-side exposure,
cross-scan shadow, BTC regime, decision populations, theoretical/execution R,
commission drag, loss-cluster concentration, scan/candidate population,
pre-candidate category rates, SENT/REJECTED correlation distributions, and
correlation plus same-side exposure versus resolved outcome.

Health reports readability, quick integrity, WAL mode, schema/boundary, last
candidate and enrichment time, pre-candidate observation count/latest timestamp/
category counts, duplicate keys, orphan children, critical-null rate, and
database size. Zero pre-candidate rows is informational, not a health failure.

## Backup and retention

No automatic deletion or retention limit exists. For a consistent backup, use
SQLite's online backup API or `sqlite3 research/scanner_research_v1.db
".backup backups/scanner_research_v1-YYYYMMDD.db"`; do not copy only the main
file while WAL writes are active. A later monthly job may perform the same
non-destructive online backup. Credentials, balances, raw secrets, and account
configuration are outside the schema.

Dashboard V3 is unchanged. A future dashboard should use a read-only SQLite
URI and run analytical queries outside the scanner process.

## No-new-silo rule

Future research hypotheses default to an offline query or a computed shadow
from this research database. A new live-path collector is justified only when
the hypothesis needs decision-time evidence that this schema did not capture
prospectively. Any such addition should first extend the unified snapshot and
reuse the same boundary rather than create an independent population.

The scanner now has two unified prospective populations: constructed final
candidates and pre-construction observations. This closes the candidate/no-
candidate denominator without forcing outcomes onto WAIT rows or creating a
second collector silo.

## Local performance validation (2026-09-30)

On the current Windows development host after the v2 remediation, 1,000 writes
through the real one-candidate/one-transaction API measured 8.0769 ms median
and 11.4404 ms p95; the indexed population query measured 1.1637 ms. This overhead is negligible
relative to the scanner's network-bound multi-symbol cycle, but VPS storage
latency should be re-measured after activation.

Batched capacity fixtures using the same schema, indexes, typed feature/
market/exposure rows, and one shadow row per candidate produced:

| Candidates | Checkpointed DB size | Common grouped query |
|---:|---:|---:|
| 10,000 | 16,650,240 bytes (15.88 MiB) | 5.471 ms |
| 100,000 | 169,426,944 bytes (161.58 MiB) | 62.285 ms |

The batched fixture time is a capacity-loading measurement, not scanner write
latency. Live writes retain the safer short transaction per candidate.
