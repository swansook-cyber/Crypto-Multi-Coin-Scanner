# Agent Operating Guide

## Before Major Work

- Read `PROJECT_CONTEXT.md`
- Read `CURRENT_STATUS.md`
- Preserve the production workflow
- Protect signal quality
- Avoid feature bloat
- Confirm that the change does not introduce auto trading

## Product Priorities

- Signal quality > signal quantity
- Statistics > opinions
- Evidence > assumptions
- Optimization > new indicators
- Telegram advisory > exchange automation

## Current Workflow To Preserve

- `cornix_agent.py` scans closed candles and sends Telegram signals
- `review_signals.py` checks outcomes and sends TP/SL alerts
- `daily_summary.py` sends daily summaries
- `performance_report.py` creates true closed-outcome performance reporting
- `core/performance_analytics_v1.py` aggregates scanner, external, and position-management analytics
- `dashboard.py` creates local HTML Dashboard V1
- `position_manager.py` prevents duplicate/opposite/stale position confusion
- `external_signal_analyzer.py` parses and reviews forwarded external/VIP signals
- `telegram_external_inbox.py` polls the External Inbox

## Telegram Routing

- Signals: `TELEGRAM_SIGNALS_CHAT_ID`
- Cornix: `TELEGRAM_CORNIX_CHAT_ID`
- Reports: `TELEGRAM_REPORTS_CHAT_ID`
- External Inbox: `TELEGRAM_EXTERNAL_INBOX_CHAT_ID`

Do not forward External Inbox messages into scanner-generated signals.

Only APPROVED external analyzer results may be sent to Signals or Cornix. WAIT, SKIP, RISKY, and FAILED results are CSV-only and summary-report only; do not send immediate Telegram messages for them.

Cornix output is production-ready text. Do not add dry-run banners unless the user explicitly requests test output.

Daily Performance Report output belongs in `TELEGRAM_REPORTS_CHAT_ID` only.

## Analytics Outputs

Performance Analytics V1 should keep these dashboard-ready CSVs current when `performance_report.py` runs:

- `logs/daily_performance.csv`
- `logs/symbol_performance.csv`
- `logs/source_performance.csv`
- `logs/position_management.csv`

Missing data must display as `N/A` or empty tables. Analytics work must not modify scanner strategy, filters, TP/SL, RR, watchlists, or external-signal approval routing.

## VPS Workflow

Production app path:

```bash
/opt/Crypto-Multi-Coin-Scanner
```

Current systemd names:

- `crypto-scanner.service`
- `crypto-outcome-checker.service`
- `crypto-outcome-checker.timer`
- `crypto-daily-summary.service`
- `crypto-daily-summary.timer`
- `crypto-external-inbox.service`

Common production checks:

```bash
systemctl status crypto-scanner.service --no-pager
journalctl -u crypto-scanner.service -n 140 --no-pager
systemctl list-timers crypto-outcome-checker.timer crypto-daily-summary.timer --no-pager
```

## Validation

- Run compile checks
- Run smoke tests
- Verify scanner imports/startup when scanner behavior changes
- Verify Telegram output format when signal/report formatting changes
- Verify journal and outcome review compatibility when CSV columns change

Required local validation:

```bash
python -m compileall -q .
python tests/smoke_test.py
```

## After Success

```bash
git add .
git commit
git push origin main
```

Update `CURRENT_STATUS.md` when a change affects production state, deployed workflow, service names, Telegram routing, or roadmap status.
