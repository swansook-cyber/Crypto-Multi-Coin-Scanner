# Crypto Multi-Coin Scanner Release Candidate V1

Version marker:

`SCANNER_RELEASE=RC1`

This marker documents the release state only. It does not change trading behavior.

## Scope

RC1 freezes feature expansion and focuses on stable daily production operation:

- Scanner signal assistant only
- Telegram delivery only
- No auto trading
- No automatic Entry Timing enforcement
- No scoring, TP/SL, RR, or routing changes in this release candidate

## Current Production Behavior

### Candidate Generation

- Binance Futures candles are scanned on the existing scanner schedule.
- The 1H timeframe remains the primary setup timeframe.
- 15m confirmation remains the entry confirmation layer.
- Existing EMA, ATR, volume, market regime, MFI, support/resistance, and quality filters remain unchanged.

### Final Approval

- Rule engine remains the decision maker.
- Gemini/OpenAI commentary is optional and commentary-only.
- High quality filters and cooldowns remain active.
- Report-only experiments do not modify score calculation or entry/TP/SL logic.

### Report-Only Routing Priority

Existing report-only modes are preserved:

1. Tier C report-only mode
2. Weak symbol report-only mode
3. Session risk report-only mode
4. London LONG experimental report-only mode

Report-only signals go to Reports only and are not sent to Signals or Cornix.

### Telegram Channels

- Signals Channel: approved live scanner/external signals only
- Cornix Channel: production Cornix-ready signal text only
- Reports Channel: outcomes, position management, health/reporting, analytics summaries
- External Inbox: receives forwarded VIP signals for approved-only analyzer flow

### Cornix Routing

- Cornix receives clean production-ready LONG/SHORT text for approved live signals.
- Cornix breakeven command mode remains optional and isolated in `position_watcher.py`.
- This release candidate does not change Cornix signal format.

### Position Watcher

- `position_watcher.py` checks open journal rows for TP1 reach.
- It can send a Reports advisory and optional Cornix breakeven command.
- Persistent CSV fields and lock files prevent duplicate TP1/NEW STOP alerts.
- Closed/report-only rows are not sent as Cornix breakeven commands.
- `position_watcher_state_cleanup.py` is the maintenance tool for stale active runtime state. It is dry-run by default and preserves CSV history.

### Outcome Review

- `review_signals.py` remains the candle-based outcome authority.
- Binance Futures klines are used for outcome checks.
- Conservative same-candle TP/SL behavior is preserved.

### Performance Report Scheduling

- `crypto-performance-report.timer` runs the performance report.
- `crypto-performance-report.service` sends the Executive Telegram Report to Reports only.
- Full analytics remain in console/CSV/web report outputs.

### Executive Telegram Report

- `python performance_report.py --executive` prints the concise report to stdout only.
- Scheduled `--send` sends Executive Report V2 to Reports only.
- Telegram failures return non-zero so systemd can surface delivery problems.

### Full Web Analytics

- `performance_report.py` writes:
  - `reports/report.html`
  - `reports/analytics.html`
- Dashboard and CSV exports are read-only analytics surfaces.

### Entry Timing Shadow

- Entry Timing Engine V1 is report-only.
- Every final approved/report-only candidate should write one shadow row.
- Recommendations do not affect live routing.
- Data readiness:
  - fewer than 30 linked closed outcomes: NOT ENOUGH DATA
  - 30-99 linked closed outcomes: EARLY DATA
  - 100+ linked closed outcomes: REVIEW READY

## Current Production Universe

Production universe is still evaluated from real outcomes in reports. Current symbol trust classes are report-only and must not auto-change `.env` or live filters.

## Active Experimental Modes

- Tier C report-only
- Weak symbol report-only
- Session risk report-only
- London LONG report-only
- Entry Timing shadow mode

## Known Limitations

- Analytics quality depends on journal completeness.
- Entry Timing has no live effect until enough linked closed outcomes exist.
- Cornix breakeven command behavior depends on Cornix parser compatibility.
- Health checks can warn on non-Linux development machines where systemd is unavailable.

## Rollback Steps

On VPS:

```bash
cd /opt/Crypto-Multi-Coin-Scanner
python3 backup_runtime_data.py
./scripts/rollback_production.sh <known-good-commit>
systemctl status crypto-scanner --no-pager
systemctl status crypto-position-watcher --no-pager
systemctl status crypto-performance-report.timer --no-pager
```

Rollback preserves logs, reports, backups, `.env`, lock files, and runtime state.

## V1 Readiness Commands

Run these before promoting RC1 to Production V1:

```bash
cd /opt/Crypto-Multi-Coin-Scanner
.venv/bin/python data_integrity_audit.py
.venv/bin/python position_watcher_state_cleanup.py
.venv/bin/python production_v1_readiness.py
```

If stale active Position Watcher state is listed, review the keys first. Apply cleanup only after review:

```bash
.venv/bin/python position_watcher_state_cleanup.py --apply
```

Never use `--apply` without reviewing the listed keys. Cleanup removes only active runtime lock files for confirmed closed positions and does not alter wins, losses, prices, TP/SL, signal status, or CSV history.
