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

## Active Format

Implemented in `format_cornix_breakeven_command()`:

```text
MOVE SL TO BREAKEVEN

Symbol: HYPEUSDT
Direction: LONG
New Stop: 70.744

Reason:
TP1 reached.
```

## Alternative Formats To Test

These are not active. Test one at a time by changing only
`format_cornix_breakeven_command()`.

### Format A: Compact

```text
HYPEUSDT
Move SL to breakeven
Stop: 70.744
```

### Format B: Stop Update

```text
UPDATE STOP
HYPEUSDT LONG
SL: 70.744
```

### Format C: Cornix-Like Command

```text
LONG HYPEUSDT

Move Stop:
70.744
```

## Validation Checklist

1. Confirm Telegram delivered the command to the Cornix channel.
2. Check watcher logs for `CORNIX COMMAND RESPONSE`.
3. Record the Telegram `message_id`.
4. Verify whether Cornix modified the stop loss.
5. If Cornix ignores the command, test the next format.

Keep `POSITION_WATCHER_DRY_RUN=1` until the channel behavior is verified.
