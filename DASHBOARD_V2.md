# Dashboard V2

Dashboard V2 is a read-only Production Season 1 control center for monitoring
scanner activity, signal quality, open positions, performance, and system health.
It does not send Telegram messages, call Binance, place orders, modify CSV logs,
or change scanner strategy.

## Run

```bash
streamlit run dashboard.py
```

On VPS:

```bash
cd /opt/Crypto-Multi-Coin-Scanner
.venv/bin/streamlit run dashboard.py --server.address 0.0.0.0 --server.port 8501
```

## Data Sources

- `logs/signals.csv`: signals, active positions, outcomes, score, RR, session,
  tier, source, TP/SL, and position-management fields.
- `logs/external_signals.csv`: External VIP/refiner approval and rejection
  summary when available.
- `logs/position_management.csv`: position advisor history when available.
- `logs/*.csv`: analytics export previews.
- `logs/cornix_agent.log` or `cornix_agent.log`: read-only timeline and latest
  error view.
- `.env`: read-only checks for Telegram/Gemini configured status only.
- local disk usage: read-only health metric.

## Sections

- Production Overview: scanner status, last scan, next scan, server UTC time,
  market regime, active positions, today's signals, 7-day win rate, average RR,
  and stale-data warning.
- Active Positions: open signals only, shown as mobile-friendly cards.
- Position Review: stale, same-direction, opposite-direction, and near-SL review
  queue. Advisory only.
- Signal Funnel / Quality: candidate and rejection counts from existing journal
  fields. Missing granular counters show `N/A`.
- Performance: Today, 7 Days, and 30 Days, separated by `All` and each real
  `source` value present in `logs/signals.csv`. Missing source is shown as
  `Unknown`; it is not assumed to be Scanner.
- Scanner Health: read-only status from systemd when available, plus CSV/log/env
  and disk data. If process status cannot be verified, the dashboard shows
  `UNKNOWN` or `DATA STALE` rather than pretending the scanner is running.
- Logs Timeline: short parsed events with filters and raw log view.
- Daily Summary: deterministic daily summary from current logs.
- Existing V3 analytics: equity curve, drawdown, monthly performance, score and
  confidence analytics, source analytics, and risk-quality views.

## Metrics That May Show N/A

- Current Price: no API calls are made from the dashboard.
- Binance/Gemini latency: no active health request is made by the dashboard.
- CPU/RAM usage: no platform-specific process probe is currently used.
- Total Coins Scanned, AI Approved, AI Rejected: only shown when explicit
  counters exist in logs.
- Market Regime: shown only when present in `logs/signals.csv`.
- Rejected by RR / Confidence: shown only when `skip_reason` contains those
  real logged reasons.
- Scanner Status: `UNKNOWN` when `systemctl` is unavailable and data is not
  stale; `DATA STALE` when latest scanner data is older than the dashboard
  threshold.

## Real Data Metrics

- Today / 7 Days / 30 Days: calculated from UTC `timestamp` values in
  `logs/signals.csv`.
- Total signals, open signals, wins, losses, and win rate: calculated from
  `signal_status` and `result`; `OPEN` and `SKIPPED` are not counted as wins or
  losses.
- Average RR: calculated from `risk_reward` on the selected rows.
- Realized R / Total R / Profit Factor: calculated from closed `WIN` / `LOSS`
  rows using the existing performance report `estimate_r` helper.
- Best/Worst symbol and session: calculated from closed outcomes only.
- Active positions: rows with `signal_status=sent` and `result=OPEN`, deduped to
  the latest row per symbol.
- Position PnL/progress: calculated only when `current_price` exists in the CSV;
  otherwise shown as `N/A`.
- Source split: uses the real `source` column. Missing source is `Unknown`.

## Read-Only Safety

Dashboard V2 intentionally does not:

- modify scanner logic
- change filters, score, RR, TP, or SL
- send Telegram messages
- modify outcome logic
- write or repair CSV logs
- create simulated production data
- execute trades or Cornix commands

## Rollback

Tracked-code rollback:

```bash
cd /opt/Crypto-Multi-Coin-Scanner
git log --oneline -5
git revert <dashboard_v2_commit>
```

If using a standalone dashboard service, restart only that service after the
rollback.

## Files Changed

- `dashboard.py`
- `tests/smoke_test.py`
- `README.md`
- `VPS_COMMANDS.md`
- `CURRENT_STATUS.md`
- `DASHBOARD_V2.md`
