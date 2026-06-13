# VelaHub External Watchdog

Lightweight external monitor for VelaHub public services. It runs separately
from the Crypto Multi Coin Scanner and does not touch trading logic, signal
generation, Cornix routing, or scanner state.

## Services

Configured in `watchdog/services.json`:

- `https://dashboard.velalab.net`
- `https://hub.velalab.net`
- `https://media.velalab.net`
- `https://files.velalab.net`
- `https://flow.velalab.net`
- `https://hr.velalab.net`

## Commands

Run one check:

```bash
python watchdog/monitor.py --once
```

Run continuously:

```bash
python watchdog/monitor.py --loop
```

Send daily status report:

```bash
python watchdog/monitor.py --daily-report
```

## Environment

```env
WATCHDOG_ENABLED=true
WATCHDOG_INTERVAL_SECONDS=300
WATCHDOG_FAILURE_THRESHOLD=3
WATCHDOG_TELEGRAM_ENABLED=true
```

Telegram uses `TELEGRAM_BOT_TOKEN` and sends to
`TELEGRAM_REPORTS_CHAT_ID`. If reports chat is missing, it falls back to
`TELEGRAM_CHAT_ID`. It never sends to Cornix.

## Alert Rules

- Success: HTTP `200-399`
- Failure: timeout, connection error, or HTTP `>= 400`
- Offline alert: after 3 consecutive failures by default
- Recovery alert: sent when an offline service becomes healthy again

State is saved in `watchdog/state.json`.
