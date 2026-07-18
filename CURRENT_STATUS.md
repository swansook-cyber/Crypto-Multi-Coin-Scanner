# Current Status

## Production State

- Scanner: active production workflow uses `cornix_agent.py`
- Deployment target: VPS at `/opt/Crypto-Multi-Coin-Scanner`
- Service manager: systemd
- Main scanner service: `crypto-scanner.service`
- Outcome checker: `crypto-outcome-checker.service` run by `crypto-outcome-checker.timer`
- Daily summary: `crypto-daily-summary.service` run by `crypto-daily-summary.timer`
- Daily performance report: `crypto-performance-report.service` run by `crypto-performance-report.timer`
- External inbox listener: `crypto-external-inbox.service`
- Position watcher: `crypto-position-watcher.service`
- Execution model: Telegram signal assistant only
- Auto trading: not implemented
- Manual execution: required
- Release marker: `SCANNER_RELEASE=RC1`

## Implemented

- Binance Futures multi-coin scanner
- Tier A / B / C watchlist architecture
- 1H setup timeframe with 15m confirmation and optional 4H context
- ATR-based TP/SL calculation
- Rule-based signal scoring and quality filters
- BTC regime filter
- Wave Structure scoring layer
- Loss cooldown and daily risk guard
- Telegram multi-channel routing
- Outcome checker and TP/SL alerts
- Daily summary reporting
- Daily Performance Report via `performance_report.py`
- Complete Performance Analytics V1 via `core/performance_analytics_v1.py`
- Dashboard V3 as read-only Streamlit analytics app via `dashboard.py`
- Position Management Advisor V2 via `position_manager.py`
- Real-Time Position Watcher V2 via `position_watcher.py`
- Tier C Experimental Report-Only Mode via `ENABLE_TIER_C_REPORT_ONLY`
- Weak Symbol Experimental Report-Only Mode via `WEAK_SYMBOL_REPORT_ONLY_SYMBOLS`
- Session Risk Report-Only Mode via `SESSION_REPORT_ONLY_SESSIONS`
- External Signal Refine V2 via `external_signal_analyzer.py`
- External Signal Inbox polling via `telegram_external_inbox.py`
- External inbox VPS listener loop via `telegram_external_inbox.py --loop`
- Production Health command via `production_health.py`
- Data Integrity Audit via `data_integrity_audit.py`
- Runtime backup command via `backup_runtime_data.py`
- Entry Timing operational summary via `entry_timing_operational_summary.py`
- Position Watcher stale-state cleanup via `position_watcher_state_cleanup.py`
- Production V1 readiness summary via `production_v1_readiness.py`
- RC1 release snapshot via `RELEASE_CANDIDATE_V1.md`
- Daily VPS operations checklist via `DAILY_OPERATIONS.md`

## Telegram Channels

- Signals: `TELEGRAM_SIGNALS_CHAT_ID`
  - Full LONG/SHORT signal message and chart image only
- Cornix: `TELEGRAM_CORNIX_CHAT_ID`
  - Production-ready Cornix-format LONG/SHORT text
- Reports: `TELEGRAM_REPORTS_CHAT_ID`
  - TP/SL outcomes, position management advisories, daily summaries, performance reports, and analytics
- External Inbox: `TELEGRAM_EXTERNAL_INBOX_CHAT_ID`
  - Incoming external signal messages are parsed, scored, logged, and reviewed

External analyzer routing:

- APPROVED external signals may go to Signals and Cornix
- WAIT / SKIP / RISKY / FAILED external signals must not go to Signals or Cornix
- WAIT / SKIP / RISKY / FAILED external signals are CSV-only and appear in summaries; no immediate Telegram message is sent
- External Refine V2 fetches fresh Binance Futures candles, recalculates scanner-style market context, and requires scanner agreement before approval

Channel-specific chat IDs are required for production channel routing. This prevents Signals, Cornix, and Reports messages from mixing in one destination.

Performance reports are routed to `TELEGRAM_REPORTS_CHAT_ID` only. They do not fall back to Signals or Cornix. Telegram receives Executive Report V2 by default: compact Performance, Best, Watch, Production Universe, Entry Timing Shadow, and Decision sections. Entry Timing market status is reporting-only (`COLLECTING DATA`, `ENTERABLE`, `WAITING`, `POOR TIMING`, or `MIXED`) and does not affect production behavior. Complete analytics stay in `performance_report.py` console output, CSV exports, and `reports/report.html`. Scheduled `--send` exits with failure if Telegram delivery fails so systemd/journal can surface the problem.

## Performance Analytics V1

- Reads scanner outcomes from `logs/signals.csv` and `logs/signals_history.csv`
- Uses production `logs/signals.csv` as the primary analytics source; `logs/signals_history.csv` is a fallback when the journal is unavailable
- Counts sent trades only when `signal_status=sent`
- Counts closed trades only when `result` is `WIN` or `LOSS`; `SKIPPED` rows are excluded from sent/closed performance
- Reads approved/rejected external signal records from `logs/external_signals.csv`
- Counts position-management events logged by the scanner
- Shows missing metrics as `N/A` instead of crashing
- Exports dashboard-ready CSVs:
  - `logs/daily_performance.csv`
  - `logs/symbol_performance.csv`
  - `logs/source_performance.csv`
  - `logs/position_management.csv`

Tracked metrics include win/loss, win rate, TP1/TP2/TP3/SL hits, cumulative Net R equity curve, daily PnL histogram, drawdown curve, max drawdown R, monthly performance, account growth simulation, average profit/drawdown/max profit, time to TP/SL, best/worst symbol, long/short win rate, tier/session/BTC regime/market regime performance, scanner vs external signal performance, and position manager HOLD/OPPOSITE/EXIT/stale counts.

## Watchlist Architecture

- Tier A: core/high-liquidity symbols
- Tier B: standard momentum symbols
- Tier C: experimental/high-filter symbols
- Optional `ENABLE_TIER_C_REPORT_ONLY=true` sends qualifying Tier C setups to Reports only and keeps them out of Signals/Cornix
- `WEAK_SYMBOL_REPORT_ONLY_SYMBOLS` sends selected symbols to Reports only with the `WEAK SYMBOL EXPERIMENTAL REPORT ONLY` header; these symbols are kept out of Signals/Cornix while outcomes remain trackable in performance reports
- `SESSION_REPORT_ONLY_SESSIONS` sends selected sessions to Reports only with the `SESSION RISK REPORT ONLY` header; these setups are kept out of Signals/Cornix while outcomes remain trackable in performance reports

Tier mode uses:

- `WATCHLIST_TIER_A`
- `WATCHLIST_TIER_B`
- `WATCHLIST_TIER_C`

Legacy `SYMBOLS` still works if tier variables are not configured.

## Pending / Next

- Current priority: production readiness, observability, data integrity, and stable VPS operations
- Position Exit Advisor: pending
- Advanced TP Engine: pending
- Dashboard V2 optimization views: implemented; ongoing work is deployment hardening and calibration after more outcomes
- Confidence/setup strength calibration: pending more real outcome data

## Production Notes

- Runtime CSV/log/chart/dashboard output should stay out of Git
- `.env` and real API/chat IDs must not be committed
- Use `python production_health.py`, `python data_integrity_audit.py`, and `python backup_runtime_data.py` before production updates
- Run `python position_watcher_state_cleanup.py` in dry-run mode before any cleanup; use `--apply` only after reviewing the listed stale keys
- Use `python production_v1_readiness.py` for the final V1 readiness summary
- Use `scripts/update_production.sh` for guarded VPS updates and `scripts/rollback_production.sh <commit>` for tracked-code rollback
- External inbox messages must not affect scanner-generated signals
- External analyzer approval must be explicit before routing to Signals/Cornix
- Cornix channel receives clean production-ready signal text; breakeven command formats are selectable with `CORNIX_BREAKEVEN_FORMAT`
