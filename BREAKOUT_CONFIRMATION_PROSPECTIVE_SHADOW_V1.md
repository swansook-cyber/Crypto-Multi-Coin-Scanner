# BREAKOUT_CONFIRMATION_PROSPECTIVE_SHADOW_V1 — precommit review

Verdict: **PASS WITH LIMITATION**. Collection is implemented and disabled by
default. No deployment, activation, restart, staging, commit or push occurred.
No new prospective population exists yet. Current evidence classification is
**INSUFFICIENT_EVIDENCE**; a live strategy change is not authorized.

## A. Exact breakout definition

Version `BREAKOUT_CONFIRMATION_V1`, shadow name
`breakout_confirmation_prospective_v1`.

Use the latest closed 1h candle from the scanner's existing scorer input.
LONG is confirmed iff its close is strictly greater than the maximum high of
the immediately preceding 20 closed 1h candles. SHORT is confirmed iff its close
is strictly less than their minimum low. Equality is unconfirmed. The current
candle is excluded from the lookback. This is the existing scorer's definition;
the observer checks agreement with its Boolean without replacing that Boolean.

Auditable capture requires 21 finite, positive, ordered, contiguous 1h candles,
each ending 1ms before the next hour, all closed at signal time. The latest close
must be less than one hour old. Invalid/missing/stale inputs cause a capture gap,
never a trade rejection. Raw components, full definition and SHA256 definition
hash permit reproduction. Any definition change requires a new V2 name/version;
V1 observations remain intact. Existing V1 rows with a different hash prevent
observer activation and do not stop the scanner.

## B. Exact captured data

Observations reside atomically with the SENT candidate in the existing v5 SQLite
`shadow_decisions` table, using its candidate FK and unique name/version key.
The scanner takes a detached copy of the same 1h frame used for scoring and
temporarily carries it in research metadata. It is persisted only for SENT.

- Canonical signal key, UTC signal timestamp, symbol, LONG/SHORT.
- Breakout Boolean, full definition, version, definition SHA256.
- Latest source close timestamp, `1h` timeframe, provenance
  `BINANCE_USDM_FETCH_CLOSED_KLINES:scorer_input`.
- Open/close UTC timestamps and high/low/close for each of 21 source candles;
  preceding-20 high/low bounds, latest close and raw-candle SHA256.
- Immutable activation boundary, PROSPECTIVE mode, signal-time session and BTC
  regime, UTC day and descriptive episode ID.

Raw source is revalidated at attachment and reporting. Duplicate candidate writes
do not replace evidence. Scoped SQLite triggers reject UPDATE/DELETE and ignore
duplicate INSERT/REPLACE on V1 observations/boundary. These are application-level
database protections, not tamper-proof storage against someone replacing the
database or dropping triggers. Other shadows and lifecycle enrichment remain
unchanged. Runtime capture/write failures remain fail-open; reports expose
missing observation keys and invalid evidence instead of manufacturing a flag.

## C. Activation boundary and operation

`.env.example` adds `BREAKOUT_CONFIRMATION_SHADOW_ENABLED=0`.
The flag alone does not activate on import/construction/reporting. At the start
of an explicitly enabled non-dry-run `AgentRunner.scan_once`, before scoring,
the observer creates its boundary through existing `shadow_boundaries`. No
backdated time parameter exists. Restart/re-enable reuses the first boundary.
Telemetry must be available. Production activation remains a later operational
action outside this precommit task; a non-dry-run scanner with this flag enabled
is the designated activation path.

Only prospective `SENT` / `sent` candidate timestamps at or after that boundary
qualify. Historical backfills and report-only/rejected candidates never qualify.
There is no observer backfill command. Previously recorded candidates cannot be
retrofitted through duplicate writes. While disabled after activation, missing
eligible SENT observations remain visible as gaps and prohibit promising status.

The existing scanner labels journal SENT before notifier delivery; this observer
preserves that semantics. SENT is not a new proof of Cornix acceptance or a fill.

## D. Proof of no strategy effect

Only four runner methods receive additive observation code: constructor,
scan_once, scan_symbol and record_research_telemetry. No decision, score,
confidence, setup strength, ranking, cooldown, TP/SL/RR or risk rule changes.
Observation attachment returns a new immutable CandidateSnapshot rather than
mutating its live decision. Invalid source or observer errors are caught.

AST comparison with the captured starting dirty working tree confirms every
other existing class method is identical, including scorer and risk methods,
process_candidates, select_top_candidates and BTC filtering. Integration tests
exercise actual scan_symbol, ranking, process_candidates and journal paths with
shadow on/off, confirmed/unconfirmed and invalid sources: strategy fields,
selected order, SENT and mark-sent calls match exactly.

## E. Proof of no routing effect

No Telegram/Cornix delivery, gateway, Binance order or stop-control method was
changed by this task. Integration tests compare exact Cornix payload and ordered
journal/delivery/mark-sent calls with shadow on/off. Existing routing tests remain
in the full suite. The reporter imports no cornix_agent and has no sending or
activation option. Pre-existing live-command gateway work is preserved and is
not part of this change.

## F. Canonical modeled outcome linkage

The read-only CLI uses existing
`load_canonical_reporting_population(signals, history, db)` and joins solely by
canonical key. Exactly one canonical terminal outcome supplies TP2_WIN,
TP1_THEN_ORIGINAL_SL or ORIGINAL_SL and finite lifecycle R. Ambiguous or duplicate
truth is not converted to a terminal result. Pending remains pending. The next
report sees later terminal truth; immutable signal-time rows are never rewritten.
No candles are fetched, outcomes simulated or second lifecycle engine created.

Historical 287 / **+49.685052548339R** and frozen prospective 25 /
**+5.799125634998R** are reference metadata only. No baseline population, source
file, certified output or canonical lifecycle calculation is changed or merged.

## G. Execution linkage

The existing execution truth CSV is optional. A unique canonical-owned MATCHED
row must pass existing `valid_initial_risk`, owned-entry/flat-boundary provenance,
valid accounting evidence, complete execution, terminal/flat quantity checks,
and execution/realized-PnL/commission FINAL flags. Net USDT divided by recomputed
initial risk must agree with execution R. Duplicate rows, reused symbol/trade
IDs or ambiguous candidate keys are quarantined. Missing/unmatched/ambiguous
ownership leaves execution R, execution-only net USDT and commission null with
explicit ownership state; it never blocks modeled outcome reporting.

Commission preserves its source sign. Net USDT is execution-only, already from
the authoritative execution ledger; commission is disclosed separately and is
not deducted a second time. Funding-final/accounting-final profit is not claimed.

## H. Periodic report and episode awareness

Run locally or in a later approved read-only scheduled job:

```powershell
python breakout_shadow_report.py
python breakout_shadow_report.py --db research/scanner_research_v1.db --signals logs/signals.csv --history logs/signals_history.csv --execution logs/binance_execution_truth_v1.csv --output breakout_shadow_report.json
```

No schedule was installed. The CLI opens SQLite mode=ro/query_only and never
initializes a store, migrates schema, activates, enriches or sends messages.

CONFIRMED/UNCONFIRMED each show observed N, terminal N, pending N, positive,
negative, net/average R, PF, win rate and maximum drawdown. Average and PF use
resolved outcomes only; no-loss PF is null with an explanatory status. DD uses
signal-time order within the terminal subset and is explicitly labeled, not
terminal-time portfolio DD. Differences are delta average R (also delta net R
per resolved trade) and win-rate difference. Loss R avoided, winner R sacrificed
and hypothetical exclusion delta are descriptive only. Execution metrics are
separate and never substitute for missing modeled truth.

Side, session, BTC regime and UTC day are descriptive splits. Episode proxy is
`UTC day | signal-time BTC regime | side`; report includes largest episode's
terminal-trade share and whether positive separation appears across multiple
UTC days. This is not a fitted combined filter. Adjacent/overlapping market
moves and correlated symbols remain potentially dependent; independent market
episodes require review. Day count is not an independent-trial count.

Classification rules:

- INSUFFICIENT_EVIDENCE: missing comparison, incomplete coverage, one-day benefit
  or unexplained directional/other unmet gates.
- CONTRADICTORY: comparable resolved data has nonpositive average separation or
  exclusion would sacrifice at least as much winner R as loss R avoided.
- PROMISING: untouched coverage, modeled separation, positive net loss avoidance,
  supporting benefit on multiple UTC dates, both directions observed with
  consistent separation, execution directionally consistent where available.
- READY_FOR_CONTROLLED_LIVE_CANDIDATE: reserved for independent review of new
  data, genuinely distinct episodes, direction effects, winner sacrifice and
  execution coverage/consistency. The automatic report deliberately stops at
  PROMISING. No fixed trade-count target and no automatic live promotion exist.

Limitations: collection has not started; execution availability may be sparse;
episode proxy does not establish independence; READY requires review. None of
these authorize changing live strategy.

## I. Tests and results

27 focused tests cover strict LONG/SHORT/equality, raw signal-time copies,
future/gapped/stale/invalid candles, version agreement, immutable rows and
INSERT/REPLACE, restart boundary, historical/pre-boundary/non-SENT exclusion,
canonical identity, later outcomes, missing/ambiguous/owned execution, duplicates,
missing-capture reporting, baseline references, daily/directional gates,
actual scanner SENT/score/ranking/Cornix equivalence and fail-open capture,
and read-only CLI without database creation.

Final exact command results and working-tree snapshots follow below.

## J. Exact files changed by this task

1. `.env.example` — append disabled observer flag; retain existing dirty work.
2. `cornix_agent.py` — four additive observer integration points only.
3. `core/breakout_confirmation_shadow.py` — new capture, protection, linkage/report.
4. `breakout_shadow_report.py` — new read-only CLI.
5. `tests/test_breakout_confirmation_shadow.py` — 27 focused cases.
6. `BREAKOUT_CONFIRMATION_PROSPECTIVE_SHADOW_V1.md` — this review/design report.

The starting tree already contained six modified tracked files and other
untracked work. SHA256 verification confirms byte preservation of unrelated
external_signal_analyzer.py, position_watcher.py, telegram_external_inbox.py,
tests/smoke_test.py, live_command_gateway.py and tests/test_live_command_gateway.py.
The entire original .env.example content remains a prefix of its current text.
Git stats below include pre-existing tracked changes and exclude new untracked
files; they must not be mistaken for this task's six-file scope.

### Final validation results (I)

| Command | Result | Seconds |
|---|---|---:|
| `python -m compileall -q .` | PASS (exit 0) | 4.33 |
| `python tests/smoke_test.py` | PASS (exit 0) | 19.86 |
| `python -m pytest -q` | PASS (exit 0) | 32.08 |
| `git diff --check` | PASS (exit 0) | 0.05 |

Pytest: **522 passed, 157 warnings in 31.06s**. Smoke: **smoke tests passed**. Existing pandas
FutureWarnings remain; no new failure. Smoke and pytest were run sequentially
in the final check because existing tests share fixed temporary directory names.
The first concurrent smoke attempt collided with pytest cleanup; its transient
FileNotFoundError did not recur when run alone. No test-source change was needed.

## K. git status --short

```text
 M .env.example
 M cornix_agent.py
 M external_signal_analyzer.py
 M position_watcher.py
 M telegram_external_inbox.py
 M tests/smoke_test.py
?? ASTRA_FULL_SCANNER_AUDIT_V1.md
?? BINANCE_EXECUTION_TRUTH_ACCOUNTING_V2_1.md
?? BINANCE_READ_ONLY_EXECUTION_TRUTH_V1.md
?? BINANCE_READ_ONLY_PERMISSION_PROBE_V1.md
?? BREAKOUT_CONFIRMATION_PROSPECTIVE_SHADOW_V1.md
?? Command/
?? ENTRY_SR_OUTCOME_CORRELATION_AUDIT_V1.md
?? EXECUTION_COST_AUDIT_V1.md
?? EXECUTION_TRUTH_FINALITY_AND_REALIZED_PROFIT_AUDIT_V1.md
?? FULL_SCANNER_PROFITABILITY_REVIEW_FROM_PROJECT_START_V1.md
?? GITHUB_COMPARATIVE_AUDIT_V1.md
?? PERFORMANCE_DATA_CONSISTENCY_AUDIT.md
?? POSITION_FLAT_PROVENANCE_ENHANCEMENT_V1.md
?? POSITION_RECONCILIATION_AUDIT_V1.md
?? POST_TP1_CONTINUATION_VS_REVERSAL_AUDIT_V1.md
?? POST_TP1_DELAYED_QUARTER_R_PROSPECTIVE_CHECK_V1.md
?? PRODUCTION_RISK_FILTER_EFFECTIVENESS_AUDIT_V1.md
?? PRODUCTION_SHADOW_OUTCOME_CORRELATION_AUDIT_V1.md
?? REALIZED_PNL_DATA_COLLECTION_V1.md
?? TP1_TO_BREAKEVEN_OUTCOME_AUDIT_V1.md
?? TP1_TO_BREAKEVEN_RECONCILIATION_V1.csv
?? TP_TARGET_DISTRIBUTION_AND_REMAINDER_AUDIT_V1.md
?? VPS_PRODUCTION_PERFORMANCE_SNAPSHOT_AUDIT.md
?? breakout_shadow_report.py
?? core/breakout_confirmation_shadow.py
?? live_command_gateway.py
?? tests/test_breakout_confirmation_shadow.py
?? tests/test_live_command_gateway.py
```

## L. git diff --stat

```text
 .env.example                |  13 ++++-
 cornix_agent.py             |  49 ++++++++++++++---
 external_signal_analyzer.py |  16 +++++-
 position_watcher.py         | 124 +++++++++++++++-----------------------------
 telegram_external_inbox.py  |   2 +-
 tests/smoke_test.py         |  82 ++++++++++++++---------------
 6 files changed, 150 insertions(+), 136 deletions(-)
```

This tracked diff includes prior work. New untracked shadow files are listed in
J/K and are excluded by standard git diff --stat. Starting tracked diff was
6 files, 121 insertions, 136 deletions; this task adds 26 lines to cornix_agent.py
and 3 lines to .env.example, plus the four new files.

## M. git diff --cached --name-only

```text
```

Empty: nothing staged. No commit, push, deployment, production access or restart.
Final verdict: **PASS WITH LIMITATION** (implemented and tested; not activated;
new evidence and independent readiness review remain future work).
