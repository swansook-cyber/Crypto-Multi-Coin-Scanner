# Manual Live Pilot

The Manual Live Pilot prepares the scanner for controlled real-money manual
trading. It does not add automatic trading. Every entry, stop, target, close,
and position update must be placed manually by the user on the exchange.

## Activation

Default mode is safe:

```env
TRADING_MODE=PAPER
LIVE_PILOT_ENABLED=false
```

To enable the pilot after preflight review:

```env
TRADING_MODE=MANUAL_LIVE_PILOT
LIVE_PILOT_ENABLED=true
```

The runtime kill switch can still block pilot eligibility:

```bash
python manual_live_pilot.py disable
python manual_live_pilot.py status
```

## Preflight

Run before any manual pilot session:

```bash
python live_pilot_preflight.py
```

Final states:

- `PILOT READY`
- `PILOT READY WITH WARNINGS`
- `PILOT BLOCKED`

Entry Timing remains shadow mode and does not approve or block pilot trades.

## Risk Calculator

Use this before placing a manual trade:

```bash
python manual_trade_plan.py \
  --symbol BTCUSDT \
  --direction LONG \
  --entry 100000 \
  --stop 99000 \
  --account-balance 1000 \
  --risk-percent 0.25
```

The output is a maximum risk limit only. It is not a profit forecast and does
not recommend leverage.

## Journal Workflow

Open a manually placed pilot trade:

```bash
python manual_live_pilot.py open \
  --source-signal-id SIGNAL_ID \
  --symbol BTCUSDT \
  --direction LONG \
  --tier A \
  --planned-entry 100000 \
  --planned-sl 99000 \
  --planned-tp1 102000
```

Close a manual pilot trade:

```bash
python manual_live_pilot.py close \
  --pilot-trade-id PILOT_TRADE_ID \
  --outcome WIN \
  --realized-r 1.2
```

List open pilot trades:

```bash
python manual_live_pilot.py list-open
```

Daily pilot summary:

```bash
python manual_live_pilot.py daily-summary
```

The journal is append-only at `logs/manual_live_pilot.csv`. A runtime backup is
created before each journal write.

## Conservative Defaults

- Risk per trade: `0.25%`
- Maximum daily risk: `0.50%`
- Maximum open pilot positions: `1`
- Maximum pilot signals per day: `3`
- Maximum consecutive pilot losses: `2`
- Allowed tiers: `S,A`

## Pilot Scope

- Tier S and Tier A only
- One open manual pilot position
- No averaging down
- No duplicate symbol position
- No opposite signal while the same symbol is open
- No Entry Timing enforcement
- No automatic trading
- No parameter optimization

## Backup And Rollback

Before production changes:

```bash
python backup_runtime_data.py
```

Rollback tracked code:

```bash
./scripts/rollback_production.sh <commit>
```

## Seven-Day Freeze

For the first seven live-pilot days, bug fixes only.

Allowed:

- crash fixes
- journal fixes
- report fixes
- risk-control fixes
- diagnostics and performance fixes

Forbidden:

- scoring changes
- filter changes
- TP/SL changes
- RR changes
- Universe expansion
- Entry Timing enforcement
- automatic order execution
