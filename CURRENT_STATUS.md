# Current Status

## Production State

- Scanner: active production workflow uses `cornix_agent.py`
- Deployment target: VPS at `/opt/Crypto-Multi-Coin-Scanner`
- Service manager: systemd
- Main scanner service: `crypto-scanner.service`
- Outcome checker: `crypto-outcome-checker.service` run by `crypto-outcome-checker.timer`
- Daily summary: `crypto-daily-summary.service` run by `crypto-daily-summary.timer`
- External inbox listener: `crypto-external-inbox.service`
- Position watcher: `crypto-position-watcher.service`
- Execution model: Telegram signal assistant only
- Auto trading: not implemented
- Manual execution: required

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
- Real-Time Position Watcher V1 via `position_watcher.py`
- Tier C Experimental Report-Only Mode via `ENABLE_TIER_C_REPORT_ONLY`
- External Signal Refine V2 via `external_signal_analyzer.py`
- External Signal Inbox polling via `telegram_external_inbox.py`
- External inbox VPS listener loop via `telegram_external_inbox.py --loop`

## Telegram Channels

- Signals: `TELEGRAM_SIGNALS_CHAT_ID`
  - Full LONG/SHORT signal message and chart image only
- Cornix: `TELEGRAM_CORNIX_CHAT_ID`
  - Cornix-format LONG/SHORT dry-run execution text only
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

Performance reports are routed to `TELEGRAM_REPORTS_CHAT_ID` only. They do not fall back to Signals or Cornix.

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

Tier mode uses:

- `WATCHLIST_TIER_A`
- `WATCHLIST_TIER_B`
- `WATCHLIST_TIER_C`

Legacy `SYMBOLS` still works if tier variables are not configured.

## Pending / Next

- Position Exit Advisor: pending
- Advanced TP Engine: pending
- Dashboard V2 optimization views: implemented; ongoing work is deployment hardening and calibration after more outcomes
- Confidence/setup strength calibration: pending more real outcome data

## Production Notes

- Runtime CSV/log/chart/dashboard output should stay out of Git
- `.env` and real API/chat IDs must not be committed
- External inbox messages must not affect scanner-generated signals
- External analyzer approval must be explicit before routing to Signals/Cornix
- Cornix channel is dry-run format only until explicitly connected
