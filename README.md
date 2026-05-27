# Crypto Multi-Coin Scanner

Telegram signal assistant for Binance Futures. The scanner is rule-based and quality-first. Gemini commentary is optional and never places trades or overrides the rule engine.

## What It Does

- Scans 10 Binance Futures symbols using `BTCUSDT` format
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
- `logs/signals.csv` - generated signal journal
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
SEND_TELEGRAM=1
```

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
WATCHLIST=BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,LTCUSDT,ZECUSDT,HYPEUSDT,LABUSDT
SCORE_THRESHOLD=70
MIN_CONFIDENCE=75
MIN_RR=1.8
COOLDOWN_MINUTES=240
CONFIDENCE_OVERRIDE_DELTA=12
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

No-trade filter behavior:

- Sideway market: skip
- Low volume ratio: skip
- ATR below `MIN_ATR_PCT`: skip

## Strategy Components

- EMA trend: 1H EMA20/EMA50 define the main direction.
- ATR volatility: ATR controls TP/SL distance and avoids fixed percentage targets.
- Volume: volume ratio and spike detection improve signal quality.
- MTF confirmation: 15m EMA/RSI confirms entry timing inside the 1H trend.
- MFI confirmation: Money Flow Index confirms buying/selling pressure. LONG setups get a bonus when MFI is above `MFI_BULLISH_THRESHOLD`; SHORT setups get a bonus when MFI is below `MFI_BEARISH_THRESHOLD`. If MFI is against direction, confidence is reduced slightly.
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
- Confidence
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
🔥 Confidence: 82%
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
- market_regime
- volume_spike
- score
- mfi
- mfi_confirmed
- ai_summary
- result
- hit_target
- closed_at
- max_profit_pct
- max_drawdown_pct
- outcome_alert_sent
- outcome_alert_at

## Review Signals

Run:

```bat
python review_signals.py
```

To update outcomes and send Telegram alerts for newly closed signals:

```bat
python review_signals.py --notify
```

For Windows, you can also run:

```bat
run_outcome_checker.bat
```

For daily use, schedule `run_outcome_checker.bat` with Windows Task Scheduler every 15 minutes, or schedule `python review_signals.py --notify` with cron on Linux/macOS. It does not use Gemini.

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
SEND_OUTCOME_ALERTS=1
```

If `SEND_TELEGRAM=0` or `SEND_OUTCOME_ALERTS=0`, review still updates `logs/signals.csv` but does not send outcome alerts.

Outcome alerts are sent once only. After an alert is sent, `outcome_alert_sent=1` and `outcome_alert_at` is recorded.

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
