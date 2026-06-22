# Cornix Command Tests

This document tracks breakeven command formats for `position_watcher.py`.

Scope:
- Troubleshooting only.
- No scanner logic changes.
- No signal generation changes.
- No auto-close command.

## Test Mode

Set:

```env
CORNIX_TEST_MODE=1
POSITION_WATCHER_COMMAND_MODE=cornix_command
POSITION_WATCHER_CORNIX_CHAT_ID=<cornix_chat_id>
CORNIX_BREAKEVEN_FORMAT=v1
```

Run:

```bash
python position_watcher.py
```

Or run one explicit test:

```bash
python position_watcher.py --test-cornix
```

The watcher sends one test breakeven command to the Cornix chat and exits.
Logs include:
- Exact command text.
- Telegram chat id.
- Telegram payload.
- Telegram API status/body.
- Telegram `message_id` when available.

## Supported Breakeven Formats

Implemented in `format_cornix_breakeven_command(row, version)`.
Change only `CORNIX_BREAKEVEN_FORMAT` in `.env` to test a different format.

### v1 Default

```text
LONG HYPEUSDT

NEW STOP:
70.744
```

### v2

```text
LONG HYPEUSDT

MOVE STOP LOSS

70.744
```

### v3

```text
UPDATE HYPEUSDT

STOP LOSS:
70.744
```

### v4

```text
#HYPE/USDT

MOVE SL TO ENTRY

70.744
```

## Production Cornix Signal Format

```text
LONG LTCUSDT

Entry:
44.735-45.185

Targets:
45.252
45.447

Stop:
44.717

Leverage:
20x
```

Production Cornix signal messages do not include a dry-run banner.

## Diagnostics

Breakeven command logs include:
- selected format version
- symbol
- direction
- final command text
- Telegram chat id
- Telegram payload
- Telegram response status/body
- Telegram `message_id` when available

## Validation Checklist

1. Confirm Telegram delivered the command to the Cornix channel.
2. Check watcher logs for `CORNIX COMMAND RESPONSE`.
3. Record the Telegram `message_id`.
4. Verify whether Cornix modified the stop loss.
5. If Cornix ignores the command, test the next format.

Keep `POSITION_WATCHER_DRY_RUN=1` until the channel behavior is verified.
