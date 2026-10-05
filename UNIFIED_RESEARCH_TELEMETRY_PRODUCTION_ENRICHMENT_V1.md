# Unified Research Telemetry Production Enrichment V1

## Scope

This change makes the existing local-artifact enrichment path operational. It
does not change scanner selection, scores, confidence, setup strength, TP/SL,
RR, the universe, Telegram/Cornix routing, shadow rules, or Binance access.
Enrichment reads existing CSV artifacts and writes only the existing unified
research database.

Production deployment and the one-time enrichment run are intentionally not
part of this implementation pass.

## Root cause

`ResearchTelemetryStore.enrich_outcomes()` and `enrich_execution()` already
existed, and the combined command was CLI-accessible as:

```bash
python -m core.research_telemetry enrich
```

The methods were covered by unit tests but had never run on production. There
was no systemd service, timer, cron entry, scanner hook, or other caller. The
production `enrichment_status` table was empty. Therefore `signal_outcomes` and
`execution_truth_links` correctly remained empty; the cause was missing
operations/scheduling, not an unknown database failure.

The original implementation also needed hardening before its first production
run: it did not require a prospective candidate match, treated every rerun as
an upsert, could classify non-outcome `SKIPPED` journal rows as outcomes, used a
row count as high-water, and did not expose inserted/updated/unchanged/missing
counts.

## Sources and lineage

### Modeled outcomes

Default source:

```text
logs/signals.csv
```

The canonical key is read from the source when present or deterministically
constructed from symbol, side, timestamp, and entry. Only keys already present
on `source_mode=PROSPECTIVE` candidates are eligible. `OPEN`, `WIN`, and `LOSS`
are valid. `SKIPPED`, blanks, and unsupported labels are not outcome evidence.

The source supports `result`, `net_r_estimate`, `closed_at`, hit target fields,
and signal geometry. Unknown values remain SQL `NULL`. `OPEN` may progress to a
terminal outcome. A terminal outcome cannot regress to `OPEN`; contradictory
terminal evidence fails the run rather than silently replacing truth.

The production snapshot test predicts 39 matched keys: 3 SENT and 36
REPORT_ONLY. Of these, 37 are resolved (2 SENT and 35 REPORT_ONLY), while two
are OPEN.

### Rejected outcomes

`logs/rejected_signals.csv` contains rejection decisions, not future outcomes.
The existing `logs/rejected_outcome_shadow.csv` contains 7,116 resolved rows,
but its timestamps span 2026-05-28 through 2026-08-26. It contains zero rows at
or after the unified prospective boundary
`2026-09-30T09:02:11.774949Z`, so it matches zero prospective candidates.

The enrichment parser can ingest that established schema when a real matching
row exists (`hypothetical_outcome`, `hypothetical_r`, `close_timestamp`), but it
does not manufacture evidence. Current prospective REJECTED readiness is
therefore limited by incomplete collector coverage, not key ambiguity.

### Execution truth

Default source:

```text
logs/binance_execution_truth_v1.csv
```

Only execution rows whose canonical key matches a prospective candidate are
eligible. Multiple lifecycle rows for one key are reduced deterministically to
the strongest evidence: authoritative, final, MATCHED, PARTIAL, AMBIGUOUS, then
UNMATCHED. Missing numeric fields remain `NULL`. Authoritative evidence cannot
regress.

The production snapshot test predicts 4 matching lifecycle rows across 3
candidate keys, producing 3 pending database links and zero authoritative
links.

## Execution finality diagnosis

The Sep-30+ execution rows are pending because the execution artifact itself is
not accounting-final:

- ADA, ETH, and one APT lifecycle are PARTIAL with an entry fill but no exit
  fill and unknown terminal position state.
- A second APT lifecycle is MATCHED and execution-complete with two exit fills,
  but position terminality remains unknown.
- All four rows have `EXECUTION_PENDING`, `REALIZED_PNL_PENDING`,
  `COMMISSION_PENDING`, `FUNDING_PENDING`, and `ACCOUNTING_PENDING`.
- Reconciliation is pending; flat provenance is absent, boundary exposure is
  unknown, and prospective lifecycle provenance is unproven.
- The recorded issues include `POSITION_SNAPSHOT_RESET`,
  `PROSPECTIVE_PROVENANCE_UNPROVEN`, `FUNDING_FINALITY_UNPROVEN`,
  `OTHER_COST_FINALITY_UNPROVEN`, and, for APT,
  `FUNDING_OWNERSHIP_AMBIGUOUS`.

This is not an enrichment high-water failure. Enrichment must preserve these
pending states and must not relax authoritative accounting rules.

## Commands

```bash
python -m core.research_telemetry enrich-outcomes
python -m core.research_telemetry enrich-execution
python -m core.research_telemetry enrich-all
```

The legacy `enrich` command remains an alias of `enrich-all`. Commands are
incremental, idempotent, local-only, and exit nonzero on a real failure. Their
summaries include source rows, matched rows/keys, inserted, updated, unchanged,
missing keys, pending/authoritative execution counts, and errors.

## Enrichment status and health

Schema version 3 adds the following operational fields to
`enrichment_status`:

- `last_attempt_utc`
- `last_success_utc`
- `high_water`
- `source_rows_seen`
- `rows_matched`
- `rows_updated`
- `last_error`

High-water is the latest source timestamp, not a row count. Health classifies a
source as:

- `HEALTHY`: successful within 45 minutes and containing source rows.
- `STALE`: no recent successful run.
- `ERROR`: last attempt failed.
- `NO_DATA`: successful read of an empty/header-only source.

## Exposure lineage disagreement

The unified database and compatibility CSV measure different populations:

- `exposure_snapshots.actual_open_same_side` is a candidate-time journal
  snapshot. It includes active `sent` and report-only statuses plus same-scan
  sent ordering.
- `cross_scan_exposure_shadow_v1.csv.existing_same_side_count` is built from the
  older cross-scan exposure state sourced from `signals.csv:SENT+OPEN`; it
  excludes report-only observations and may also apply stale-position
  retention semantics.
- `shadow_retained_same_side_count` is the shadow-retained subset, not the live
  journal-wide actual-open count.

At APT candidate time, the unified count of 2 consisted of ETH SENT plus AAVE
Tier-C report-only, while the compatibility shadow counted only ETH (1). At ADA
candidate time, the unified count of 1 was the open HYPE session-risk
report-only observation, while the SENT-only shadow counted 0. Neither value
should be rewritten to equal the other; analyses must name the field and its
population.

## Market-data failure diagnosis

All 333 prospective `MARKET_DATA_FAILURE` observations are deterministic:

| Symbol | Count | Telemetry reason | Log status |
| --- | ---: | --- | --- |
| `PEPEUSDT` | 111 | `HTTPError` | HTTP 400 from `/fapi/v1/klines` |
| `FLOKIUSDT` | 111 | `HTTPError` | HTTP 400 from `/fapi/v1/klines` |
| `BONKUSDT` | 111 | `HTTPError` | HTTP 400 from `/fapi/v1/klines` |

Each symbol failed on every one of 111 scanner runs, ruling out a transient
network/rate pattern. The configured names are invalid for the Binance USD-M
endpoint, which uses multiplier-prefixed contracts for these assets. This is a
production symbol-mapping/universe issue. It is documented only; the universe
is not changed by this task.

## Proposed automation (not deployed)

The dedicated units are:

```text
deploy/systemd/crypto-research-enrichment.service
deploy/systemd/crypto-research-enrichment.timer
```

The oneshot runs `enrich-all` every 15 minutes. It has no network dependency,
does not run in the scanner process, and fails independently without stopping
the scanner.

## Exact controlled VPS run plan (do not run during this implementation pass)

After normal code deployment and before enabling the timer:

```bash
cd /opt/Crypto-Multi-Coin-Scanner

# 1. Read-only baseline. Save the console output externally or in the change record.
.venv/bin/python -m core.research_telemetry status
.venv/bin/python -m core.research_telemetry summary
.venv/bin/python -m core.research_telemetry health

# Expected baseline from the audit:
# signal_outcomes=0, execution_truth_links=0,
# resolved SENT=0, resolved REPORT_ONLY=0, resolved REJECTED=0,
# authoritative execution=0.

# 2. One-time local-artifact enrichment. These are the only DB-mutating steps.
.venv/bin/python -m core.research_telemetry enrich-outcomes
.venv/bin/python -m core.research_telemetry enrich-execution

# 3. Read-only verification.
.venv/bin/python -m core.research_telemetry health
.venv/bin/python -m core.research_telemetry status
.venv/bin/python -m core.research_telemetry summary

# Expected snapshot result (production may have advanced by run time):
# signal_outcomes=39, execution_truth_links=3,
# resolved SENT=2, resolved REPORT_ONLY=35, resolved REJECTED=0,
# authoritative execution=0.

# 4. Idempotency proof: rerun and require inserted=0, updated=0.
.venv/bin/python -m core.research_telemetry enrich-all

# 5. Only after reviewing all output, install/enable the dedicated units.
# Deployment/enablement is a separate authorized change.
```

Do not call Binance, edit source artifacts, or enable the timer as part of the
one-time verification.
