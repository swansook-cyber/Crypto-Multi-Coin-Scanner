@echo off
REM Copy this file to config.bat, then edit the values below.
REM Do not commit config.bat because it contains secrets.

set WATCHLIST=BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,LTCUSDT,ZECUSDT,HYPEUSDT,LABUSDT

REM Optional Gemini commentary. Leave GEMINI_API_KEY empty for rule-based mode.
set GEMINI_API_KEY=
set GEMINI_MODEL=gemini-2.5-flash

set TELEGRAM_BOT_TOKEN=put_your_telegram_bot_token_here
set TELEGRAM_CHAT_ID=put_your_telegram_chat_id_here

REM Scanner behavior
set USE_AI_COMMENTARY=1
set SEND_TELEGRAM=1
set DRY_RUN=0
set RUN_ONCE=0
set SCORE_THRESHOLD=70
set MIN_RR=1.2
set COOLDOWN_MINUTES=180
set CLOSE_DELAY_SECONDS=20

REM Risk manager display values
set ACCOUNT_BALANCE_USDT=1000
set RISK_PER_TRADE_PCT=1
set MAX_LEVERAGE=10
