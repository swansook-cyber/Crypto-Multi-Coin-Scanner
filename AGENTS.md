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
- `dashboard.py` creates local HTML Dashboard V1
- `position_manager.py` prevents duplicate/opposite/stale position confusion
- `telegram_external_inbox.py` logs external messages for debug only

## Telegram Routing

- Signals: `TELEGRAM_SIGNALS_CHAT_ID`
- Cornix: `TELEGRAM_CORNIX_CHAT_ID`
- Reports: `TELEGRAM_REPORTS_CHAT_ID`
- External Inbox: `TELEGRAM_EXTERNAL_INBOX_CHAT_ID`

Do not forward External Inbox messages into scanner signals or Cornix.

Cornix output is dry-run format unless the user explicitly changes production mode.

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
