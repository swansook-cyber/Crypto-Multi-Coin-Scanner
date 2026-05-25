# Crypto Multi-Coin Scanner

AI-assisted Binance Futures signal scanner for 10 symbols. The trading decision is rule-based; Gemini is optional and only adds a short commentary after a setup already passes scoring.

## Features

- Binance Futures candles via `fapi.binance.com`
- Binance symbol format: `BTCUSDT`
- TradingView display format: `BINANCE:BTCUSDT.P`
- Scans only closed 1H candles
- 1H trend confirmation plus 15m entry confirmation
- ATR-based TP/SL, not fixed percentages
- Support and resistance from recent candles
- Market regime detection: `Trending`, `Sideway`, `High Volatility`
- Volume spike filter
- Score threshold and cooldown to prevent duplicate alerts
- Risk manager display: risk amount, estimated position size, RR
- Telegram messages formatted for manual review and Cornix-style fields
- Rule-based mode works without Gemini API key

## Setup

1. Install Python 3.12.
2. Copy `config.example.bat` to `config.bat`.
3. Edit `config.bat`.

Required for live Telegram delivery:

```bat
set TELEGRAM_BOT_TOKEN=your_bot_token
set TELEGRAM_CHAT_ID=your_chat_id
```

Optional Gemini commentary:

```bat
set GEMINI_API_KEY=your_gemini_key
set GEMINI_MODEL=gemini-2.5-flash
```

Run:

```bat
run_cornix_agent.bat
```

## Important Settings

```bat
set WATCHLIST=BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,LTCUSDT,ZECUSDT,HYPEUSDT,LABUSDT
set SCORE_THRESHOLD=70
set MIN_RR=1.2
set COOLDOWN_MINUTES=180
set RUN_ONCE=0
set DRY_RUN=0
set SEND_TELEGRAM=1
set ACCOUNT_BALANCE_USDT=1000
set RISK_PER_TRADE_PCT=1
set MAX_LEVERAGE=10
```

Use `RUN_ONCE=1` and `DRY_RUN=1` for testing without waiting for the next 1H close or sending Telegram.

## Signal Logic

The scanner builds a candidate signal only when:

- 1H trend aligns with EMA20/EMA50
- 15m entry confirms with EMA9/EMA21 and RSI
- Volume spike or breakout improves score
- Market regime is acceptable
- ATR is available for TP/SL
- Score is at least `SCORE_THRESHOLD`
- RR is at least `MIN_RR`
- Symbol/direction is not inside cooldown

Gemini cannot override the rule engine. It only writes a brief reason after a signal passes.

## Files

- `cornix_agent.py` - main scanner
- `config.example.bat` - safe config template
- `requirements.txt` - Python dependencies
- `signal_state.json` - cooldown state, created at runtime
- `cornix_agent.log` - runtime log, created at runtime

## Safety

This tool is for analysis support only. Futures trading is high risk. Always review the chart, market conditions, position size, and leverage before using any signal.
