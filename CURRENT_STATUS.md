# Current Status

## Production State

- Scanner: active production workflow uses `cornix_agent.py`
- Deployment target: VPS at `/opt/Crypto-Multi-Coin-Scanner`
- Service manager: systemd
- Main scanner service: `crypto-scanner.service`
- Outcome checker: `crypto-outcome-checker.service` run by `crypto-outcome-checker.timer`
- Daily summary: `crypto-daily-summary.service` run by `crypto-daily-summary.timer`
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
- Dashboard V1 as local HTML via `dashboard.py`
- Position Management Advisor via `position_manager.py`
- External Signal Inbox logger via `telegram_external_inbox.py`

## Telegram Channels

- Signals: `TELEGRAM_SIGNALS_CHAT_ID`
  - Full signal message and chart image
- Cornix: `TELEGRAM_CORNIX_CHAT_ID`
  - Cornix-format dry-run message only
- Reports: `TELEGRAM_REPORTS_CHAT_ID`
  - Daily summary, reports, and position management advisories
- External Inbox: `TELEGRAM_EXTERNAL_INBOX_CHAT_ID`
  - Incoming external signal messages are logged/debugged only

If a channel-specific chat ID is empty, the scanner falls back to `TELEGRAM_CHAT_ID` where appropriate.

## Watchlist Architecture

- Tier A: core/high-liquidity symbols
- Tier B: standard momentum symbols
- Tier C: experimental/high-filter symbols

Tier mode uses:

- `WATCHLIST_TIER_A`
- `WATCHLIST_TIER_B`
- `WATCHLIST_TIER_C`

Legacy `SYMBOLS` still works if tier variables are not configured.

## Pending / Next

- Position Exit Advisor: pending
- Advanced TP Engine: pending
- Dashboard V1 improvements: ongoing
- Confidence/setup strength calibration: pending more real outcome data

## Production Notes

- Runtime CSV/log/chart/dashboard output should stay out of Git
- `.env` and real API/chat IDs must not be committed
- External inbox messages must not affect scanner signals
- Cornix channel is dry-run format only until explicitly connected
