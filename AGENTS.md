# Agent Operating Guide

## Before Major Work

- Read `PROJECT_CONTEXT.md`
- Preserve the production workflow
- Protect signal quality
- Avoid feature bloat
- Confirm that the change does not introduce auto trading

## Product Priorities

- Signal quality > signal quantity
- Statistics > opinions
- Evidence > assumptions
- Optimization > new indicators

## Current Workflow To Preserve

- `cornix_agent.py` scans closed candles and sends Telegram signals
- `review_signals.py` tracks TP/SL outcomes
- `daily_summary.py` creates daily performance reporting
- `stats_dashboard.py` generates analytics reports
- Journal files are runtime data and must not expose API keys or private trade data

## Validation

- Run compile checks
- Run tests
- Verify scanner still starts
- Verify Telegram output format when signal formatting changes
- Verify journal and outcome review compatibility when data columns change

## After Success

```bash
git add .
git commit
git push
```
