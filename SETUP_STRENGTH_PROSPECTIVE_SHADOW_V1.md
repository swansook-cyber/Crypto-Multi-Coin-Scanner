# Setup Strength Prospective Shadow V1

## Purpose

Track the frozen production research hypothesis prospectively:

```text
setup_strength <= 79
```

This is shadow analytics only. It does not block signals, change score,
change confidence, rerank candidates, alter TP/SL/RR, or change Telegram/Cornix
routing.

## Historical Trigger Evidence

Production Setup Quality + Session Interaction Audit V1 found:

```text
Production sent/closed sample: N = 144
LOW setup_strength <= 79: N = 30
Win rate: 30.0%
Net R: -11R
```

The weakness appeared across LONG/SHORT, Asia/Non-Asia, and late-entry control
buckets. The finding is promising enough for prospective tracking, but not for
live enforcement.

## Frozen Hypothesis

Classification:

- `LOW_SETUP_SHADOW`: `setup_strength <= 79`
- `NORMAL_SETUP_SHADOW`: `setup_strength >= 80`
- `UNKNOWN`: setup strength missing or invalid

No session, direction, score, confidence, or late-entry condition is added.

## Output

Rows are written to:

```text
logs/setup_strength_prospective_shadow.csv
```

Required fields include:

- `canonical_signal_key`
- `timestamp_utc`
- `symbol`
- `side`
- `setup_strength`
- `setup_shadow_class`
- `score`
- `confidence`
- `quality_tier`
- `market_session`
- `signal_status`
- `rejection_reason`
- `entry`
- `sl`
- `tp1`
- `tp2`
- `shadow_version`
- `prospective_start_timestamp_utc`
- `generated_at_utc`

Optional context fields are included when available:

- `sr_class`
- `market_exhaustion_class`
- `entry_timing_class`
- `btc_regime`
- `loss_cooldown_active`
- `daily_risk_state`

## Prospective Start Boundary

Each row records:

```text
shadow_version = SETUP_STRENGTH_PROSPECTIVE_V1
prospective_start_timestamp_utc
generated_at_utc
```

Historical observations are not counted toward the prospective target. The
logger reuses the first existing `prospective_start_timestamp_utc` in the CSV
when present. `SETUP_STRENGTH_PROSPECTIVE_START_UTC` can be set for an explicit
deployment boundary.

## Dedupe

The logger uses `canonical_signal_key` for idempotency. Re-running the same
candidate/status path does not append duplicates. Production outcomes are linked
at report time from `logs/signals.csv`, so the shadow log remains append-only.

## Reporting

Run:

```text
python -m core.setup_strength_prospective_shadow --report
```

The report shows:

- prospective LOW setup observed/sent/closed/wins/losses
- win rate, Net R, Avg R, open/unresolved
- NORMAL setup comparator
- LOW setup by side
- LOW setup by session
- monthly LOW setup summary
- progress toward `100` new sent/closed LOW setup observations

## Decision Rules After Target

Target:

```text
>=100 NEW sent/closed LOW_SETUP_SHADOW observations
```

Possible outcomes:

- `CONFIRMED WEAKNESS`: LOW setup remains materially worse than >=80 comparator
  and is not confined to one tiny session/direction.
- `NOT CONFIRMED`: LOW setup performance normalizes materially.
- `MIXED`: effect varies strongly by period, direction, or session.

No automatic live promotion is allowed. Even confirmed weakness requires a
separate audit and explicit approval before any production rule change.

## Config

```text
SETUP_STRENGTH_SHADOW_ENABLED=true
SETUP_STRENGTH_LIVE_ENABLED=false
SETUP_STRENGTH_PROSPECTIVE_START_UTC=
```

`SETUP_STRENGTH_LIVE_ENABLED` is intentionally ignored in V1.

## Runtime Impact

The scanner appends one small CSV row per final candidate status. No network
request, indicator calculation, or routing call is added.

## Rollback

Set:

```text
SETUP_STRENGTH_SHADOW_ENABLED=false
```

Restart the scanner normally. Existing shadow CSV history can be preserved for
research.
