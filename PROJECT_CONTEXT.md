# Project Context

## Vision

Build a reliable crypto signal scanner that focuses on high-quality trade opportunities rather than generating many signals.

Goal:

- Find high probability setups
- Reduce false signals
- Improve consistency
- Collect real statistics
- Optimize based on actual results

Philosophy:

Statistics first.  
Optimization second.  
New features last.

Do not add indicators without evidence from collected data.

## Current Production Workflow

- Binance Futures market data
- Multi-coin scanner
- Tier A / B / C watchlists
- 1H primary setup timeframe
- 15m entry confirmation
- Optional 4H regime context
- Telegram delivery
- Manual execution only
- No auto trading

Main runtime components:

- `cornix_agent.py`: scanner loop and Telegram signal delivery
- `review_signals.py`: outcome checker and TP/SL alerts
- `daily_summary.py`: daily summary
- `performance_report.py`: closed-outcome performance report
- `core/performance_analytics_v1.py`: complete analytics aggregation and dashboard-ready CSV exports
- `dashboard.py`: local HTML Dashboard V1
- `position_manager.py`: duplicate/opposite/stale position advisor
- `external_signal_analyzer.py`: approved-only parser/analyzer/router for forwarded VIP signals
- `telegram_external_inbox.py`: external inbox polling/listener tool

## Current Strategy

- Rule-based decision engine
- EMA trend filter
- ATR risk management and ATR-based TP/SL
- Support / Resistance
- Volume and volume spike analysis
- Market regime detection
- BTC regime filter
- MFI confirmation layer
- Candle body / wick / ATR expansion quality filters
- Wave Structure scoring layer
- Cooldown, loss cooldown, and daily risk guard
- Setup Strength scoring
- Optional Gemini commentary only after rule-based filtering

## Watchlist Architecture

- Tier A: core/high-liquidity symbols
- Tier B: momentum/standard symbols
- Tier C: experimental symbols with stricter filtering

Configured through:

- `WATCHLIST_TIER_A`
- `WATCHLIST_TIER_B`
- `WATCHLIST_TIER_C`

Legacy `SYMBOLS` remains supported as a fallback.

## Telegram Architecture

- Signals channel: full scanner signal and chart
- Cornix channel: Cornix-format dry-run text
- Reports channel: daily summaries, performance reports, and position advisories
- External Inbox: receives outside messages for approved-only analysis

Environment variables:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `TELEGRAM_SIGNALS_CHAT_ID`
- `TELEGRAM_CORNIX_CHAT_ID`
- `TELEGRAM_REPORTS_CHAT_ID`
- `TELEGRAM_EXTERNAL_INBOX_CHAT_ID`

External inbox messages must not affect scanner-generated decisions.

Only APPROVED external signals may be routed to Signals and Cornix. WAIT, SKIP, RISKY, and FAILED external signals are CSV-only and should appear in summary reporting, not immediate Telegram messages.

Performance reports, TP/SL outcomes, and position-management messages must be sent to the Reports channel only. They must never route to Signals or Cornix.

## Current Analytics Layer

Performance Analytics V1 reads:

- `logs/signals.csv`
- `logs/signals_history.csv`
- `logs/external_signals.csv`

It exports:

- `logs/daily_performance.csv`
- `logs/symbol_performance.csv`
- `logs/source_performance.csv`
- `logs/position_management.csv`

It tracks scanner vs external signal performance, Tier A/B/C performance, session performance, BTC and market regime performance, long/short performance, TP/SL hit stats, and Position Management Advisor outcomes.

## Product Principles

Quality > Quantity.

Avoid signal spam.

Prefer fewer high-quality signals.

One open position per symbol.

Position management awareness is required.

## Current Roadmap Priority

1. Daily Performance Report
2. Dashboard V1
3. Position Management Advisor
4. Position Exit Advisor
5. Advanced TP Engine

## Future Direction

Data-driven optimization only.

No indicator additions without statistical justification.
