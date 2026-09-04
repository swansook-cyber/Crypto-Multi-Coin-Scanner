# Position Reconciliation Diagnostic V1

## Purpose

`core.position_reconciliation_diagnostic` is a standalone read-only diagnostic
for checking whether the scanner's local signal lifecycle state is internally
consistent after downtime, restarts, clock drift, or manual recovery.

Primary command:

```bash
python -m core.position_reconciliation_diagnostic
```

JSON mode:

```bash
python -m core.position_reconciliation_diagnostic --json
```

Verbose text mode:

```bash
python -m core.position_reconciliation_diagnostic --verbose
```

The command exits `0` when the diagnostic completes, even if it reports
`WARNING`, `STALE`, or `CONFLICT`. A non-zero exit code should mean the tool
itself failed.

## Non-Goals

This tool does not:

- place, modify, cancel, or close orders
- move stop loss or take profit
- send Telegram messages
- send Cornix messages
- restart or stop services
- repair CSV files
- delete watcher locks
- change scanner decisions, scoring, TP/SL/RR, routing, or production universe
- prove Binance/Cornix execution truth

## Read-Only Safety

The diagnostic reads local files and optionally fetches Binance Futures public
server time. It does not write to `logs/signals.csv`, shadow CSVs, state JSON
files, `.env`, systemd units, or Telegram channels.

Read inputs:

- `logs/signals.csv`
- `logs/signals_history.csv` freshness only
- `logs/moving_sl_prospective_shadow.csv`
- `signal_state.json`
- `watchdog/state.json`
- `.env`
- position watcher lock directory metadata

Network input:

- `https://fapi.binance.com/fapi/v1/time`

## Statuses

Statuses are diagnostic labels only:

- `OK`: no issue detected for that check
- `WARNING`: issue should be reviewed but is not deterministically broken
- `STALE`: data/time is materially old or drifted
- `CONFLICT`: trusted local sources disagree
- `UNKNOWN`: not enough data or optional source unavailable

Overall status uses severity ordering:

`CONFLICT > STALE > WARNING > UNKNOWN/incomplete health > OK`

The public overall enum intentionally remains `OK`, `WARNING`, `STALE`, or
`CONFLICT`. If any unresolved check is `UNKNOWN` and no higher-severity issue is
present, the overall status is `WARNING`. This prevents incomplete health from
being reported as fully healthy.

## Clock Thresholds

Clock health compares local UTC time to Binance Futures public server time:

| Drift | Status |
|---:|---|
| `<= 5s` | `OK` |
| `> 5s` and `<= 30s` | `WARNING` |
| `> 30s` | `STALE` |
| Binance time unavailable | `UNKNOWN` |

The diagnostic never modifies the system clock.

## Stale OPEN Thresholds

Open signal age is classified conservatively:

| OPEN age | Status |
|---:|---|
| `<= 24h` | `OK` |
| `> 24h` and `<= 48h` | `WARNING` |
| `> 48h` | `STALE` |

For suspicious open rows the report shows symbol, side, timestamp, age, entry,
SL, TP1, and TP2. It does not auto-close anything.

## Checks

### Clock Health

Reports local UTC, Binance Futures server UTC, and absolute drift seconds.

### Signals CSV Health

Reads `logs/signals.csv` and reports:

- total rows
- `signal_status == sent` rows
- `OPEN` rows
- closed `WIN/LOSS` rows
- latest signal timestamp
- latest sent timestamp
- duplicate canonical signal keys
- duplicate terminal outcome keys
- future timestamp rows

### Stale OPEN Detection

Lists `OPEN` rows older than the configured diagnostic thresholds.

### Signal / Outcome Consistency

Flags:

- outcome fields present while `result` remains `OPEN`
- `WIN/LOSS` rows missing `closed_at`
- `closed_at` before signal timestamp
- multiple terminal outcomes for the same canonical signal
- old unresolved sent rows

### Watcher / Observability State

Inspects freshness and existence for:

- `watchdog/state.json`
- `signal_state.json`
- `logs/signals_position_watcher_locks/`

### Service Freshness

The portable diagnostic does not call `systemctl`. It reports freshness from
existing local files and marks service runtime status as `UNKNOWN`.

### Moving-SL Shadow Sanity

Checks:

- `MOVING_SL_SHADOW_ENABLED`
- `MOVING_SL_LIVE_ENABLED`
- `MOVING_SL_PROSPECTIVE_START_UTC`
- output file freshness
- rows before prospective start
- duplicate canonical lifecycle rows

It does not trigger collection and never calls `--run`.

If the output file does not exist while Moving-SL shadow is enabled, live mode is
disabled, the prospective boundary is valid, and `logs/signals.csv` contains zero
`signal_status == sent` rows on or after the boundary, the diagnostic reports
`OK` with the message `no prospective observations yet`. It does not create the
output file. If prospective sent rows exist but the output file is absent, the
state remains `UNKNOWN` because absence cannot be safely distinguished from a
collector failure.

### Clock-Sensitive Timestamp Sanity

Checks prospective start timestamps and signal/outcome timestamp columns for
invalid or future values.

## Limitations

This diagnostic proves local consistency only. It cannot prove:

- the user entered a trade
- Cornix accepted or acted on a Telegram command
- Binance order fills
- actual partial exit weights
- realized account PnL
- manually modified stop loss or take profit

Read-only Binance execution truth is still required for full reconciliation.

## Manual Escalation Guidance

Escalate manually when:

- clock status is `STALE`
- any stale `OPEN` sent signal exists
- outcome consistency returns `CONFLICT`
- Moving-SL live flag is unexpectedly enabled
- duplicate terminal outcomes appear
- watcher locks exist for closed identities and cleanup has not been reviewed

Recommended manual sequence after VPS downtime:

```bash
cd /opt/Crypto-Multi-Coin-Scanner
.venv/bin/python -m core.position_reconciliation_diagnostic --verbose
.venv/bin/python review_signals.py
.venv/bin/python system_status.py
```

Only restart scanner services after clock and local outcome state have been
verified.
