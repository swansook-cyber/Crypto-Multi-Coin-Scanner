# Crypto Scanner Production V1

This document records the V1 production operating state. The scanner remains a
Telegram signal assistant only. It does not auto-trade, auto-close, or change
exchange positions.

## Release

- Release marker: `SCANNER_RELEASE=V1.0`
- Release commit: verify on deployment with `git rev-parse HEAD`
- Status command: `python system_status.py`

## Production Architecture

- `cornix_agent.py`: 1H Binance Futures scanner and signal router.
- `review_signals.py`: outcome review for TP/SL resolution.
- `position_watcher.py`: real-time TP1 breakeven advisory and optional Cornix
  breakeven command mode.
- `performance_report.py`: Executive Telegram summary and full analytics report.
- `dashboard.py`: read-only local analytics dashboard.
- `system_status.py`: compact read-only production status console.

## Active Services

- `crypto-scanner.service`: scanner loop.
- `crypto-position-watcher.service`: TP1/breakeven watcher loop.
- `crypto-performance-report.timer`: scheduled performance report.
- `crypto-performance-report.service`: report execution unit.

## Reporting Behavior

- Signals channel: approved internal/external open signals only.
- Cornix channel: Cornix-ready open signal text and configured breakeven commands.
- Reports channel: outcomes, position management, Executive Report, diagnostics.
- VelaHub Monitor channel: external infrastructure watchdog messages only.

## Experimental Modes

These modes are report-only experiments and must not change scanner scoring:

- Tier C report-only mode.
- Weak symbol report-only mode.
- Session risk report-only mode.
- London LONG report-only experiment.
- Entry Timing Engine remains shadow mode.

## Known Limitations

- V1 uses collected CSV outcomes for analytics; sample size may still be small.
- Entry Timing recommendations are not enforced in production.
- Cornix breakeven command format may require live channel validation.
- Dashboard and reports are read-only and depend on journal data quality.

## Backup And Rollback

Create a runtime backup:

```bash
cd /opt/Crypto-Multi-Coin-Scanner
.venv/bin/python backup_runtime_data.py
```

Deploy safely:

```bash
cd /opt/Crypto-Multi-Coin-Scanner
./scripts/update_production.sh
```

Rollback:

```bash
cd /opt/Crypto-Multi-Coin-Scanner
./scripts/rollback_production.sh <commit>
```

## V1 Freeze Policy

For 7 days after V1 deployment, make bug fixes only.

Do not change:

- scanner scoring
- signal filters
- Telegram routing
- TP/SL/RR calculations
- Cornix signal format
- Entry Timing enforcement
