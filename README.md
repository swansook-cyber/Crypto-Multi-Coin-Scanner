# Crypto Multi-Coin Scanner

Telegram signal assistant for Binance Futures. The scanner is rule-based and quality-first. Gemini commentary is optional and never places trades or overrides the rule engine.

## What It Does

- Scans tiered Binance Futures symbols using `BTCUSDT` format
- Shows TradingView symbols like `BINANCE:BTCUSDT.P`
- Waits for closed 1H candles, then uses 15m confirmation
- Sends Telegram alerts only for high-quality signals
- Logs every generated candidate signal to `logs/signals.csv`
- Sends a daily Telegram summary at UTC day rollover
- Exports chart screenshots to `charts/` and sends them with alerts
- Uses ATR for TP/SL, not fixed percentages
- Applies no-trade filters for Sideway, low volume, and unusually low ATR
- Adds MFI confirmation as a lightweight money-flow layer
- Uses cooldown to avoid spam, with override only for much higher confidence
- Optional Fear & Greed filter can reduce long/short scores

This project does not auto trade. It is a Telegram signal assistant only.

## Files

- `cornix_agent.py` - main scanner
- `.env.example` - environment config template
- `requirements.txt` - Python dependencies
- `review_signals.py` - outcome tracker for WIN / LOSS / OPEN
- `stats_dashboard.py` - CSV analytics dashboard and report exporter
- `tier_review.py` - tier promotion/demotion recommendation report
- `logs/signals.csv` - generated signal journal
- `reports/` - generated analytics CSV reports
- `charts/` - generated chart screenshots
- `signal_state.json` - generated cooldown and summary state

## Setup

1. Install Python 3.12.
2. Copy `.env.example` to `.env`.
3. Edit `.env`.
4. Run:

```bat
run_cornix_agent.bat
```

Manual install:

```bat
py -3.12 -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python cornix_agent.py
```

## Telegram Bot Setup

1. Open Telegram and message `@BotFather`.
2. Create a bot with `/newbot`.
3. Copy the bot token into `.env`:

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
```

4. Send a message to your bot or add it to your channel/group.
5. Find your chat ID and add:

```env
TELEGRAM_CHAT_ID=your_chat_id_here
TELEGRAM_SIGNALS_CHAT_ID=
TELEGRAM_CORNIX_CHAT_ID=
TELEGRAM_REPORTS_CHAT_ID=
TELEGRAM_EXTERNAL_INBOX_CHAT_ID=
SEND_TELEGRAM=1
```

Optional multi-channel routing:

- `TELEGRAM_SIGNALS_CHAT_ID`: full scanner signal and chart
- `TELEGRAM_CORNIX_CHAT_ID`: production-ready Cornix-format signal text
- `TELEGRAM_REPORTS_CHAT_ID`: daily summaries, reports, and position advisor messages
- `TELEGRAM_EXTERNAL_INBOX_CHAT_ID`: external message intake for approved-only analysis

For production channel routing, set the channel-specific IDs. External inbox messages never affect scanner-generated signals. Only APPROVED external analyzer results may be routed to Signals and Cornix. WAIT, SKIP, RISKY, and FAILED external signals are CSV-only and appear in summary reporting. External Signal Refine V2 fetches fresh Binance Futures candles and requires scanner-style agreement before approval.

To poll the external inbox once:

```bat
python telegram_external_inbox.py
```

For VPS production, run it as a long-running listener:

```bat
python telegram_external_inbox.py --loop
```

Received external messages are parsed, scored, stored in `logs/external_signals.csv`, and reported to the reports channel. WAIT, SKIP, RISKY, and FAILED messages are never sent to Signals or Cornix.

For testing without sending alerts:

```env
DRY_RUN=1
SEND_TELEGRAM=0
RUN_ONCE=1
```

To test Telegram delivery directly without waiting for a trading signal:

```bat
python test_telegram.py
```

This sends a test-only message and, if present, `charts/test_chart.png`. It does not use Gemini and does not write a fake signal to `logs/signals.csv`.

## Important Config

```env
WATCHLIST_TIER_A=BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT
WATCHLIST_TIER_B=HYPEUSDT,SUIUSDT,DOGEUSDT,LINKUSDT,AVAXUSDT,ADAUSDT,DOTUSDT,NEARUSDT,OPUSDT,ARBUSDT,APTUSDT,INJUSDT,FILUSDT,LTCUSDT,ZECUSDT
WATCHLIST_TIER_C=PEPEUSDT,WIFUSDT,FLOKIUSDT,BONKUSDT,SEIUSDT,ORDIUSDT,ATOMUSDT,AAVEUSDT,UNIUSDT,RUNEUSDT
SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,LTCUSDT,ZECUSDT,HYPEUSDT,LABUSDT
SCORE_THRESHOLD=70
MIN_CONFIDENCE=75
MIN_RR=1.8
COOLDOWN_MINUTES=240
CONFIDENCE_OVERRIDE_DELTA=12
LOSS_COOLDOWN_MINUTES=180
MAX_SIGNALS_PER_SCAN=3
MAX_SIGNALS_PER_DIRECTION_PER_CANDLE=2
MAX_MAJOR_CORRELATED_SIGNALS=1
USE_BTC_REGIME_FILTER=1
BTC_SIDEWAY_PENALTY=10
BTC_LOW_VOL_SKIP=1
USE_CANDLE_BODY_FILTER=1
MIN_BODY_RATIO=0.45
USE_WICK_FILTER=1
MAX_OPPOSITE_WICK_RATIO=0.45
USE_ATR_EXPANSION_FILTER=1
MIN_ATR_EXPANSION_RATIO=1.05
USE_LOSING_STREAK_PROTECTION=1
MAX_SYMBOL_LOSS_STREAK=2
SYMBOL_PAUSE_AFTER_LOSS_MINUTES=360
USE_DAILY_RISK_GUARD=1
MAX_DAILY_LOSSES=5
MAX_DAILY_SIGNALS=12
USE_SESSION_FILTER=1
ACTIVE_SESSIONS=London,NewYork
ALLOW_ASIA_SESSION=1
SESSION_PENALTY_ASIA=3
USE_4H_REGIME_FILTER=1
HTF_TIMEFRAME=4h
TREND_TIMEFRAME=1h
ENTRY_TIMEFRAME=15m
HTF_CONFLICT_PENALTY=8
USE_AUTO_TIER_REVIEW=0
TIER_REVIEW_MIN_TRADES=20
TIER_PROMOTE_WINRATE=60
TIER_DEMOTE_WINRATE=40
MIN_VOLUME_RATIO=0.80
MIN_ATR_PCT=0.35
VOLUME_SPIKE_MULTIPLIER=1.20
USE_MFI_FILTER=1
MFI_PERIOD=14
MFI_BULLISH_THRESHOLD=55
MFI_BEARISH_THRESHOLD=45
MFI_SCORE_BONUS=8
USE_LIQUIDATION_CONTEXT=0
```

Quality filter behavior:

- If score is below `SCORE_THRESHOLD`, the candidate is logged only
- If confidence is below `MIN_CONFIDENCE`, it is logged only
- If RR is below `MIN_RR`, it is logged only
- Telegram receives only high-quality signals
- Only the strongest `MAX_SIGNALS_PER_SCAN` candidates are sent each scan
- Major correlated coins (`BTCUSDT`, `ETHUSDT`, `SOLUSDT`, `LTCUSDT`) are capped by direction
- If the same symbol and direction recently hit SL, loss cooldown blocks repeats
- Candle body, opposite wick, and ATR expansion filters reduce weak breakout setups
- Daily risk guard stops new signals after too many daily losses or signals
- Tier C setups require stronger confirmation and use a longer cooldown

No-trade filter behavior:

- Sideway market: skip
- Low volume ratio: skip
- ATR below `MIN_ATR_PCT`: skip

## Watchlist Tiers

The scanner supports three watchlist tiers, about 30 symbols total:

- Tier A: core/high liquidity coins. These receive a small confidence bonus and higher priority when candidates are tied.
- Tier B: standard momentum coins. These use the normal rule engine.
- Tier C: experimental or higher-noise coins. These receive a small confidence penalty, require volume spike or MFI confirmation, require score 80+, and use 1.5x cooldown.

If `WATCHLIST_TIER_A`, `WATCHLIST_TIER_B`, or `WATCHLIST_TIER_C` is set, tier mode is used. If tier variables are not set, legacy `SYMBOLS` is still supported and symbols default to Tier B.

## Market Session Filter

Sessions are tagged by UTC time:

- Asia: 00:00-08:00 UTC
- London: 08:00-16:00 UTC
- NewYork: 13:00-21:00 UTC

The scanner does not hard-block inactive sessions by default. It applies a small confidence adjustment so the rule engine remains the decision maker.

## Multi-Timeframe Regime

The default workflow is:

- `HTF_TIMEFRAME=4h`: higher-timeframe regime and big trend context
- `TREND_TIMEFRAME=1h`: setup trend
- `ENTRY_TIMEFRAME=15m`: entry confirmation

Aligned 4H and 1H direction can add a small bonus. Conflicting 4H context reduces score and confidence, and weak setups are filtered by the existing quality thresholds.

## Stats Dashboard

Run:

```bat
python stats_dashboard.py
```

It prints a console report and exports:

- `reports/stats_summary.csv`
- `reports/symbol_performance.csv`
- `reports/tier_performance.csv`
- `logs/performance_report.txt`

The dashboard also groups performance by score bucket, setup strength range, HTF alignment/conflict, and market session. Public signal messages use `Setup Strength` instead of `Confidence` so the score is not presented as win probability.

## Daily Performance Report

Run:

```bat
python performance_report.py
python performance_report.py --send
python performance_report.py --executive
```

This reads `logs/signals.csv` and focuses on closed outcomes, not just signal activity.

- `python performance_report.py` prints the complete detailed report, refreshes CSV exports, and writes `reports/report.html`.
- `python performance_report.py --executive` prints the Executive Report V2 summary locally.
- `python performance_report.py --send` sends only the Executive Report V2 summary to `TELEGRAM_REPORTS_CHAT_ID`.

Detailed analytics such as score calibration, strategy simulator, production universe ranking, root-cause analytics, entry timing analytics, and CSV downloads belong in the dashboard/web report, not the Telegram message. Set `ANALYTICS_DASHBOARD_URL` to show a full analytics link in Telegram.

Executive Report V2 shows compact sections for Performance, Best, Watch, Production Universe, Entry Timing Shadow, Decision, and the full analytics link. Entry Timing market status is report-only:

- `COLLECTING DATA`: fewer than 10 evaluated Entry Timing rows.
- `ENTERABLE`: `ENTER NOW` is at least 40% of evaluated rows.
- `WAITING`: pullback/breakout/retest wait recommendations are at least 60%.
- `POOR TIMING`: skip recommendations are at least 60%.
- `MIXED`: no dominant timing condition.

## Production V1 Status Console

Preferred daily command on VPS:

```bash
cd /opt/Crypto-Multi-Coin-Scanner
.venv/bin/python system_status.py
```

Optional JSON output:

```bash
.venv/bin/python system_status.py --json
```

Optional shell alias:

```bash
alias scanner-status='cd /opt/Crypto-Multi-Coin-Scanner && .venv/bin/python system_status.py'
```

Do not add the alias automatically in deployment scripts. The status console is
read-only: it does not send Telegram, modify CSV logs, repair data, or restart
services.

## Production Readiness And V1 Operations

V1 freezes feature expansion and adds operational tools for stable daily use. These commands do not change scanner logic, scoring, routing, Cornix format, TP/SL, RR, or outcomes.

```bat
python system_status.py
python production_health.py
python data_integrity_audit.py
python data_integrity_audit.py --profile
python data_integrity_audit.py --benchmark
python backup_runtime_data.py
python entry_timing_operational_summary.py
python position_watcher_state_cleanup.py
python production_v1_readiness.py
```

- `production_health.py` checks environment, Telegram channel configuration, Binance Futures reachability, CSV read/write, report generation, web report generation, duplicate-alert locks, disk space, UTC clock sanity, imports, and systemd service health when available.
- `data_integrity_audit.py` audits runtime CSVs read-only by default. `--profile` shows stage timings, and `--benchmark` runs a synthetic provenance benchmark without touching runtime logs. `--repair-safe` only normalizes safe boolean values and removes exact duplicate analytics rows after backup; it never changes wins, losses, prices, TP/SL, or outcomes.
- `backup_runtime_data.py` writes `backups/runtime_<UTC_TIMESTAMP>.zip` and excludes `.env`, tokens, passwords, private keys, and real config files.
- `entry_timing_operational_summary.py` summarizes Entry Timing shadow rows and reports data readiness: `NOT ENOUGH DATA`, `EARLY DATA`, or `REVIEW READY`.
- `position_watcher_state_cleanup.py` lists stale active Position Watcher state for already-closed rows. Default is dry-run only.
- `production_v1_readiness.py` summarizes health, data integrity, backups, reports, Entry Timing status, duplicate-alert protection, and active stale state count.
- `system_status.py` prints the compact V1 production status dashboard for mobile SSH.

Safe cleanup flow:

```bat
python position_watcher_state_cleanup.py
python position_watcher_state_cleanup.py --apply
```

Run dry-run first. Never use `--apply` without reviewing the listed stale state keys. Apply mode preserves CSV history and removes only active runtime lock files tied to confirmed closed positions after creating a backup.

Release record and operations checklist:

```text
PRODUCTION_V1.md
RELEASE_CANDIDATE_V1.md
DAILY_OPERATIONS.md
```

Safe VPS update and rollback scripts:

```bash
cd /opt/Crypto-Multi-Coin-Scanner
./scripts/update_production.sh
./scripts/rollback_production.sh <commit>
```

## Dashboard V3

Run:

```bat
streamlit run dashboard.py
```

Dashboard V3 is a read-only Streamlit dashboard for optimization decisions. It reads existing CSV logs only and never sends Telegram messages, calls Binance, modifies logs, changes scanner strategy, or places trades.

The full static analytics report is also written to:

```bat
reports\report.html
reports\analytics.html
```

Sections include Executive Summary, Equity/PnL/Drawdown/Monthly Analytics, Win/Loss Analytics, Drawdown Analytics, Symbol/Tier/Session Analytics, Long vs Short Analytics, Score/Confidence Analytics, TP/SL Analytics, External VIP Signal Analytics, Position Manager Analytics, and Risk-Quality Views.

It shows KPI cards, equity curve/cumulative Net R, green/red daily PnL histogram, drawdown curve, max drawdown R, monthly performance summary, account growth simulator for 100/500/1000 USDT examples, daily wins/losses, daily net R, win rate and net R by symbol/tier/session/direction, score and confidence bucket performance, TP/SL distribution, external VIP approval/rejection summary, high-score losses, low-score wins, high-drawdown winners, fast SL trades, slow TP trades, repeated losses, Tier C risk review, recent signals, closed trades, open positions, and position manager history.

The recommendation panel is rule-based and labeled as analytics suggestion only. It does not auto-change `.env`, filters, watchlists, or strategy settings.

Data sources:

```text
logs/signals.csv
logs/daily_performance.csv
logs/symbol_performance.csv
logs/source_performance.csv
logs/position_management.csv
logs/external_signals.csv
```

## Position Management Advisor

Run:

```bat
python position_manager.py
```

The advisor reads open signals from `logs/signals.csv` and flags duplicate, opposite, or stale positions. V2 adds action guidance such as HOLD, HOLD WITH CAUTION, TAKE PARTIAL PROFIT, MOVE SL TO BREAKEVEN, REDUCE EXPOSURE, or CLOSE POSITION. It is Telegram advisory only and never places orders.

## Tier C Experimental Mode

Set this in `.env`:

```text
ENABLE_TIER_C_REPORT_ONLY=true
```

When enabled, Tier A and Tier B signals keep normal routing. Tier C signals that pass existing quality rules are logged and sent to the Reports channel only. They are not sent to the Signals channel or Cornix channel. Performance reports track Tier C report-only outcomes separately so Tier C can be evaluated without mixing it into production signal delivery.

## Data-Driven Validation

The outcome checker keeps a persistent derived history at:

```text
logs/signals_history.csv
```

It is synced from `logs/signals.csv` after outcome review finishes and includes timestamp, symbol, side, tier, session, entry, SL, TP1/TP2, RR, setup strength, score, market regime, HTF alignment, volume spike, MFI, ATR, result, realized PnL percent, and holding minutes.

`stats_dashboard.py` reads `logs/signals_history.csv` first and falls back to `logs/signals.csv` if the history file does not exist. It prints and writes adaptive filtering suggestions to `logs/performance_report.txt`, such as symbols with weak historical winrate or underperforming HTF-misaligned setups. These are recommendations only; the scanner does not auto-trade and does not rewrite strategy settings automatically.

The quant validation layer also maintains:

- `logs/daily_summary.csv`
- `logs/equity_curve.csv`
- `logs/rejected_signals.csv`
- `logs/adaptive_filters.json`

Adaptive analytics can recommend a 7-day symbol blacklist, HTF score reductions, session threshold increases, daily consecutive-loss pauses, max daily drawdown guards, and ATR spike protection. These are research controls by default; keep `USE_ADAPTIVE_FILTERS=0` unless you intentionally wire the generated state into scanner filtering.

## Tier Review

Run:

```bat
python tier_review.py
```

This only recommends promotions/demotions from historical outcomes. It never edits `.env` automatically.

## Recommended Weekly Workflow

```bat
python stats_dashboard.py
python tier_review.py
python review_signals.py
```

Overtrade controls:

- `LOSS_COOLDOWN_MINUTES` blocks the same symbol + direction after a recent SL.
- `MAX_SIGNALS_PER_DIRECTION_PER_CANDLE` limits same-direction exposure in one scan.
- `MAX_MAJOR_CORRELATED_SIGNALS` keeps highly correlated majors from firing together.
- `USE_BTC_REGIME_FILTER` reduces altcoin confidence when BTC is sideway and can skip weak altcoin setups when BTC volatility is too low.
- `MAX_SIGNALS_PER_SCAN` sends only the best ranked setups after all candidates are scored.
- `USE_LOSING_STREAK_PROTECTION` pauses a symbol after repeated losses.
- `USE_DAILY_RISK_GUARD` pauses new signals for the day when risk limits are reached.

## Strategy Components

- EMA trend: 1H EMA20/EMA50 define the main direction.
- Multi-timeframe regime: 4H defines higher-timeframe context, 1H defines the setup, and 15m confirms entry timing.
- ATR volatility: ATR controls TP/SL distance and avoids fixed percentage targets.
- Volume: volume ratio and spike detection improve signal quality.
- MTF confirmation: 15m EMA/RSI confirms entry timing inside the 1H trend.
- Market session filter: Asia, London, and New York sessions are tagged in the journal. Inactive sessions apply a small confidence penalty rather than a hard block.
- MFI confirmation: Money Flow Index confirms buying/selling pressure. LONG setups get a bonus when MFI is above `MFI_BULLISH_THRESHOLD`; SHORT setups get a bonus when MFI is below `MFI_BEARISH_THRESHOLD`. If MFI is against direction, confidence is reduced slightly.
- Candle quality: body strength and opposite wick filters reduce fake breakouts and wick traps.
- ATR expansion: current ATR is compared with recent ATR to avoid breakouts without volatility expansion.
- Optional liquidation context: disabled by default. It is a lightweight placeholder for future context sources and only adjusts confidence; it is never the main signal source.

The scanner stays lightweight: no websocket stream, no heavy polling, and no realtime orderflow engine. It is intended to run comfortably on a small VPS.

## Optional Gemini Commentary

Gemini is optional and may have API costs or quota limits. The scanner is designed to run rule-based without Gemini.

Gemini is never called for every coin or every candidate. The rule engine scans first, then Gemini is called only when a signal already passed the high-quality filters and:

- `confidence >= AI_MIN_CONFIDENCE`
- `risk_reward >= MIN_RR`
- `AI_MAX_CALLS_PER_RUN` has not been reached

If Gemini returns 403, 429, quota, or timeout-like errors, AI commentary is disabled for that scan run. The Telegram alert still shows the rule-based `Reason`, but it will not show a duplicated AI summary. It does not retry aggressively.

```env
AI_COMMENTARY=1
AI_MIN_CONFIDENCE=88
AI_MAX_CALLS_PER_RUN=1
GEMINI_API_KEY=your_gemini_key
GEMINI_MODEL=gemini-2.5-flash
```

Leave `AI_COMMENTARY=0` or `GEMINI_API_KEY` empty for rule-based mode.

## Recommended Low-Cost AI Settings

```env
AI_COMMENTARY=1
GEMINI_MODEL=gemini-2.5-flash
AI_MIN_CONFIDENCE=88
AI_MAX_CALLS_PER_RUN=1
```

`gemini-2.5-flash` is the practical choice for this scanner because the AI task is only short commentary. Do not call AI for every coin or every candidate; the rule engine is the decision maker, and AI is only a commentary layer for the strongest setups. Keeping `AI_MAX_CALLS_PER_RUN=1` helps control cost, quota usage, and API spam.

## Optional Fear & Greed Filter

Uses the public Alternative.me Fear & Greed API.

```env
USE_FEAR_GREED=1
FEAR_GREED_GREED_THRESHOLD=75
FEAR_GREED_FEAR_THRESHOLD=25
FEAR_GREED_SCORE_ADJUSTMENT=8
```

Extreme greed reduces long score. Extreme fear reduces short score.

## Telegram Alert Format

Alerts are mobile-friendly and include:

- Long/Short
- Entry
- SL
- TP1/TP2
- RR
- Setup Strength
- Market regime
- Volume spike
- MFI
- Support/Resistance
- Reason
- Chart screenshot

Example:

```text
🚀 LONG SIGNAL
🪙 BTCUSDT.P

💰 Entry: 68420
🛑 SL: 67180

🎯 TP1: 69700
🎯 TP2: 71000

📈 RR: 1:2.10
🔥 Setup Strength: 82%
```

## Trade Journal

Every generated candidate signal is appended to:

```text
logs/signals.csv
```

Columns:

- timestamp
- symbol
- side
- entry
- stop_loss
- tp1
- tp2
- risk_reward
- confidence
- setup_strength
- market_regime
- volume_spike
- score
- raw_score
- score_bucket
- watchlist_tier
- mfi
- mfi_confirmed
- ai_summary
- body_ratio
- opposite_wick_ratio
- atr_expansion_ratio
- quality_flags
- market_session
- htf_regime
- htf_alignment
- htf_conflict
- signal_version
- result
- hit_target
- closed_at
- max_profit_pct
- max_drawdown_pct
- outcome_alert_sent
- outcome_alert_at
- outcome_id
- tp1_alert_sent
- tp2_alert_sent
- sl_alert_sent
- outcome_alert_sent_at

## Review Signals

Run once:

```bat
python review_signals.py
```

To run once and send Telegram alerts for newly closed signals:

```bat
python review_signals.py --notify
```

For production use, keep the outcome checker open like the scanner. Set:

```env
OUTCOME_LOOP_MODE=1
OUTCOME_LOOP_INTERVAL_SECONDS=900
```

Then run:

```bat
python review_signals.py
```

When loop mode is enabled, `review_signals.py` keeps running, reviews open trades every 15 minutes by default, sends outcome alerts when TP/SL is hit, logs the next run time, and continues after Binance or Telegram errors. It does not use Gemini.

For Windows, you can also run:

```bat
run_outcome_checker_loop.bat
```

Daily operation:

- Scanner: keep `cornix_agent.py` running.
- Outcome checker: keep `review_signals.py` running with `OUTCOME_LOOP_MODE=1`.
- Task Scheduler is no longer required for the outcome checker.

It reports:

- signal count
- wins
- losses
- open trades
- win rate
- average RR
- best coin
- worst coin
- a compact summary table such as `BTCUSDT LONG WIN TP2`

The review command updates `logs/signals.csv` in place. It uses Binance Futures candles only and does not use Gemini.

Outcome logic:

- LONG: TP1/TP2 before SL is `WIN`; SL before TP is `LOSS`
- SHORT: TP1/TP2 below entry before SL is `WIN`; SL before TP is `LOSS`
- Conservative mode: if a candle touches TP and SL in the same candle, SL wins first
- If no TP/SL is touched within `REVIEW_LOOKAHEAD_HOURS`, result stays `OPEN`

Config:

```env
REVIEW_LOOKAHEAD_HOURS=24
OUTCOME_CHECK_INTERVAL_MINUTES=15
OUTCOME_LOOP_MODE=1
OUTCOME_LOOP_INTERVAL_SECONDS=900
SEND_OUTCOME_ALERTS=1
SEND_DAILY_SUMMARY=1
DAILY_SUMMARY_HOUR=23
DAILY_SUMMARY_MINUTE=55
PROMO_ENABLED=false
```

If `SEND_TELEGRAM=0` or `SEND_OUTCOME_ALERTS=0`, review still updates `logs/signals.csv` but does not send outcome alerts.

Outcome alerts are sent once only. After an alert is sent, `outcome_alert_sent=1`, the matching `tp1_alert_sent` / `tp2_alert_sent` / `sl_alert_sent` flag, and `outcome_alert_at` are recorded.

## Real-Time Position Watcher

Run one check:

```bat
python position_watcher.py --once
```

Run continuously:

```bat
python position_watcher.py
```

The watcher checks open rows in `logs/signals.csv` every `POSITION_WATCHER_INTERVAL_SECONDS` seconds. When TP1 is reached, it sends a Reports-channel advisory to move SL to breakeven and records `tp1_alert_sent`, `tp1_alert_at`, `breakeven_recommended`, and `breakeven_price` to prevent duplicate alerts. It does not place orders, auto-close positions, or change scanner entry logic.

V2 adds optional Cornix command mode. Default is still report-only.

Config:

```env
POSITION_WATCHER_ENABLED=1
POSITION_WATCHER_INTERVAL_SECONDS=60
SEND_TP1_BREAKEVEN_ALERTS=1
POSITION_WATCHER_COMMAND_MODE=report_only
POSITION_WATCHER_CORNIX_CHAT_ID=
POSITION_WATCHER_SEND_REPORT_COPY=1
POSITION_WATCHER_DRY_RUN=0
CORNIX_BREAKEVEN_FORMAT=v1
```

Allowed command modes:

- `report_only`: send advisory message to Reports only.
- `cornix_command`: send a simple Cornix-compatible breakeven command to `POSITION_WATCHER_CORNIX_CHAT_ID`; send a Reports copy when `POSITION_WATCHER_SEND_REPORT_COPY=1`.

Cornix breakeven command format is isolated in `format_cornix_breakeven_command()` inside `position_watcher.py`. Set `CORNIX_BREAKEVEN_FORMAT=v1`, `v2`, `v3`, or `v4` to test a format without changing TP1 detection. Keep `POSITION_WATCHER_DRY_RUN=1` while validating a Cornix channel.

## Daily Summary

Run a daily journal summary:

```bat
python daily_summary.py --dry-run
```

To send it to Telegram, set:

```env
SEND_DAILY_SUMMARY=1
```

Then run:

```bat
python daily_summary.py
```

The report includes total signals, TP1/TP2/SL counts, pending signals, best/worst symbol, best session, best score bucket, and average holding time. It is research tracking only and does not place trades.

## VPS systemd Examples

Create `/etc/systemd/system/crypto-scanner.service`:

```ini
[Unit]
Description=Crypto Multi-Coin Scanner
After=network-online.target

[Service]
WorkingDirectory=/opt/Crypto-Multi-Coin-Scanner
EnvironmentFile=/opt/Crypto-Multi-Coin-Scanner/.env
ExecStart=/opt/Crypto-Multi-Coin-Scanner/.venv/bin/python cornix_agent.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

Create `/etc/systemd/system/crypto-outcome-checker.service`:

```ini
[Unit]
Description=Crypto Outcome Checker
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/Crypto-Multi-Coin-Scanner
EnvironmentFile=/opt/Crypto-Multi-Coin-Scanner/.env
ExecStart=/opt/Crypto-Multi-Coin-Scanner/.venv/bin/python review_signals.py --notify
```

Create `/etc/systemd/system/crypto-outcome-checker.timer`:

```ini
[Unit]
Description=Run Crypto Outcome Checker every 15 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
Unit=crypto-outcome-checker.service

[Install]
WantedBy=timers.target
```

Create `/etc/systemd/system/crypto-daily-summary.service`:

```ini
[Unit]
Description=Crypto Daily Summary
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/Crypto-Multi-Coin-Scanner
EnvironmentFile=/opt/Crypto-Multi-Coin-Scanner/.env
ExecStart=/opt/Crypto-Multi-Coin-Scanner/.venv/bin/python daily_summary.py
```

Create `/etc/systemd/system/crypto-daily-summary.timer`:

```ini
[Unit]
Description=Run Crypto Daily Summary once per day

[Timer]
OnCalendar=*-*-* 23:55:00
Persistent=true
Unit=crypto-daily-summary.service

[Install]
WantedBy=timers.target
```

Create `/etc/systemd/system/crypto-performance-report.service`:

```ini
[Unit]
Description=Crypto Multi-Coin Scanner Performance Report
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/Crypto-Multi-Coin-Scanner
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/Crypto-Multi-Coin-Scanner/.venv/bin/python /opt/Crypto-Multi-Coin-Scanner/performance_report.py --send
StandardOutput=journal
StandardError=journal
```

Create `/etc/systemd/system/crypto-performance-report.timer`:

```ini
[Unit]
Description=Run Crypto Scanner Performance Report daily

[Timer]
OnCalendar=*-*-* 23:58:00
Persistent=true
Unit=crypto-performance-report.service

[Install]
WantedBy=timers.target
```

Create `/etc/systemd/system/crypto-external-inbox.service`:

```ini
[Unit]
Description=Crypto External Signal Inbox Listener
After=network-online.target

[Service]
WorkingDirectory=/opt/Crypto-Multi-Coin-Scanner
EnvironmentFile=/opt/Crypto-Multi-Coin-Scanner/.env
ExecStart=/opt/Crypto-Multi-Coin-Scanner/.venv/bin/python telegram_external_inbox.py --loop
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

Create `/etc/systemd/system/crypto-position-watcher.service`:

```ini
[Unit]
Description=Crypto Multi-Coin Scanner Position Watcher
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/Crypto-Multi-Coin-Scanner
EnvironmentFile=/opt/Crypto-Multi-Coin-Scanner/.env
ExecStart=/opt/Crypto-Multi-Coin-Scanner/.venv/bin/python position_watcher.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

The same unit template is also available at:

```text
deploy/systemd/crypto-external-inbox.service
deploy/systemd/crypto-position-watcher.service
```

Enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-scanner.service
sudo systemctl enable --now crypto-outcome-checker.timer
sudo systemctl enable --now crypto-daily-summary.timer
sudo systemctl enable --now crypto-performance-report.timer
sudo systemctl enable --now crypto-external-inbox.service
sudo systemctl enable --now crypto-position-watcher.service
```

The daily timer example runs at 23:55 server time. Keep `PROMO_ENABLED=false`; this project is internal lab signal tracking only.

## Troubleshooting

No Telegram messages:

- Check `SEND_TELEGRAM=1`
- Check `DRY_RUN=0`
- Check bot token and chat ID
- Check `cornix_agent.log`

No signals:

- This is expected when quality filters are strict
- Try `RUN_ONCE=1` and inspect logs
- Lower `SCORE_THRESHOLD`, `MIN_CONFIDENCE`, or `MIN_RR` only if you accept more noise

Gemini errors:

- Leave `AI_COMMENTARY=0` or `GEMINI_API_KEY` empty to run rule-based only
- The scanner does not require AI to work

Charts not sent:

- Check that `matplotlib` installed
- Check `charts/` for generated PNG files

## Safety

Futures trading is high risk. This project is for analysis support only. It does not place orders, manage exchange accounts, or guarantee outcomes. Always review the chart and manage risk manually.
