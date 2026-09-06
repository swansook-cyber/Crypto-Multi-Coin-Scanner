# Reporting Truth V1

## Scope and safety

This change is reporting and accounting only. It does not modify scanner decisions, scoring, setup strength, TP/SL/RR, configured watchlists, Telegram/Cornix routing, outcome handling, position watching, or VPS/systemd configuration. `R` remains a model/strategy estimate, not realized exchange execution PnL.

## Read-only audit: previous behavior

| Output | Input and filter | Formula / previous semantic issue |
| --- | --- | --- |
| Daily Performance Summary / Overall Net R / Closed / Wins / Losses / TP1 / TP2 | `logs/signals.csv` (or `logs/signals_history.csv` only if journal is empty), date by `timestamp`; `signal_status == sent`; closed means `result in {WIN, LOSS}` | `performance_v1`: explicit `real_rr` when present; loss `-1R`; TP3/TP2 use RR (fallback 3/2); TP1 uses `min(RR, 1.2)` (fallback 1). |
| V1 symbol/tier/session performance | same sent-only scanner population | same `performance_v1` estimator. |
| V3 general performance tables | scanner rows passed to V3; V3 group tables themselves apply `signal_status == sent` and closed `WIN/LOSS` | `analytics_v3`: `real_rr`, then `net_r_estimate`, loss `-1R`, TP3/TP2 RR/fallback, TP1 `1R`. |
| Old “Production Universe” rank, Tier S/A/Watch/Report Only, old Core WR / Core Net R | all scanner statuses with `result in {WIN, LOSS}`; no `signal_status` restriction in V3 ranking | retrospective V3 outcome ranking and classification. This could include a report-only BNB row and was not routing truth. |
| Old “Post-Filter Live Pool” | all-status V3 rows except the four report-only statuses | neither a sent-only population nor configured routing truth. |

The `signal_status` values `tier_c_report_only`, `weak_symbol_report_only`, `session_risk_report_only`, and `london_long_report_only` remain outcome-trackable research rows. `SKIPPED` rows are non-terminal and are excluded from closed performance. No status is changed by reporting.

## Explicit populations

| Population | Definition | Allowed label |
| --- | --- | --- |
| `LIVE_SENT_PERFORMANCE` | `signal_status == sent`; terminal metrics additionally require `result in {WIN, LOSS}` | Production Performance (Sent Only) |
| `RESEARCH_ALL_STATUS_PERFORMANCE` | normalized scanner rows across all statuses; retrospective ranking uses terminal rows | Research Analytics (All Status) |
| `LIVE_ROUTING_UNIVERSE` | non-secret projection of `ScannerConfig.from_env()`: `watchlist`, `watchlist_tiers`, and report-only controls | Live Routing Universe |
| `PERFORMANCE_QUALIFIED_RESEARCH_SYMBOLS` | outcome-ranked, dynamic V3 symbol classifications | Performance-Qualified Research Symbols (Retrospective) |

Configured routing is read from the scanner configuration but never written or inferred from outcomes. A symbol in `PERFORMANCE_QUALIFIED_RESEARCH_SYMBOLS` is not evidence that it was live-routed.

## Label migration

| Previous label | Reporting Truth V1 label |
| --- | --- |
| Production Universe Ranking | Performance-Qualified Research Symbols (Retrospective) |
| Recommended Production Universe / Core symbols | Research Classifications (not live routing) |
| Core WR / Core Net R | Performance-qualified WR / Net R (research) |
| Post-Filter Live Performance | Research Status Comparison (not production performance) |
| Production Universe Performance | Performance-Qualified Research Symbol Performance |

The new Sent-Only Production Symbol Performance CSV is the appropriate symbol-level production view.

## Formula provenance

Every primary Production Performance Net R is marked `Population: LIVE_SENT_PERFORMANCE` and `Formula: performance_v1`. Retrospective research ranking is marked `Population: RESEARCH_ALL_STATUS_PERFORMANCE` and `Formula: analytics_v3`. Historical arithmetic is intentionally not converted between versions.

## Immutable snapshot manifest

Only a concrete UTC date writes `logs/report_snapshots/YYYY-MM-DD.json`. `Date=ALL` is a mutable aggregate view and deliberately writes no immutable daily manifest; it therefore cannot create `ALL.json` conflicts.

### Evidence identity

`content_sha256` is calculated only from `evidence_identity`:

- snapshot version and concrete report date;
- the named production and research population definitions;
- the V1/V3 formula version names;
- the date-scoped normalized population row count;
- stable hashes of every relevant normalized accounting row and the sent/closed canonical-key population.

Relevant rows use stable ordering and include timestamps, symbol, side, status, result/outcome/hit target, and the entry, stop, target, RR, `real_rr`, and `net_r_estimate` inputs used by the estimators. Rows outside the requested date are not included. A source CSV append for another date therefore cannot invalidate an existing daily manifest.

### Provenance and presentation metadata

Source path/size/full-file SHA and row count, generation time, report-text/output hashes, Git commit, and current-generation routing configuration remain in the manifest for audit context but do **not** affect `content_sha256`. File mtime is not stored. Current routing is explicitly marked `UNKNOWN` for historical routing: it is current-generation provenance, not a claim about routing on the report date.

The manifest excludes API keys, Telegram tokens/chat IDs, Cornix secrets, and all other credentials.

If a report for the same date has identical evidence, generation is an `IDENTICAL` no-op for the manifest. If the evidence identity differs, the existing manifest remains byte-for-byte unchanged and a `<date>.conflict-<hash>.json` artifact is preserved. The artifact records both evidence hashes and the reason. Repeating the same conflict is idempotent. Conflicts are logged locally but do not block exports, console output, Telegram reporting, or a successful report-process exit. It never silently overwrites audit evidence.

## Limitations

- Model R is not realized execution PnL, fees, slippage, fills, or exchange reconciliation.
- A snapshot only proves the date-scoped normalized evidence available to the report process at generation time.
- Routing provenance is current configured state, not a historical routing configuration archive. Future configuration-version capture can extend this without changing the present reporting contract.
