# Coding Rules

## Production Safety

- Do not break the production scanner
- Do not change entry, TP, or SL logic unless explicitly requested
- Do not change confidence/setup strength scoring unless explicitly requested
- Preserve Telegram message structure unless the task is about message formatting
- Preserve signal logging
- Preserve the review/outcome engine
- Preserve manual execution only
- Never add auto trading
- Analytics changes must not alter scanner strategy, filters, scoring, RR, TP/SL, or watchlists

## Development Style

- Prefer small improvements
- Avoid unnecessary indicators
- Avoid feature bloat
- Every optimization should be measurable
- Add columns and analytics with backward compatibility for old CSV files
- Fail safely if optional data, API keys, Telegram channels, or journal files are missing
- If one Telegram channel fails, continue other channel sends where possible

## Strategy Rules

- Rule engine remains the decision maker
- AI commentary is optional explanation only
- External Signal Analyzer must be approved-only
- External Signal Inbox must not affect scanner-generated logic
- Cornix channel is dry-run format unless explicitly changed
- New filters should reduce bad signals, not increase signal volume
- Do not add indicators without statistical evidence from collected outcomes

## Watchlist Rules

- Preserve Tier A / B / C architecture
- Tier A is core/high-liquidity
- Tier B is standard/momentum
- Tier C is experimental/high-filter
- Keep legacy `SYMBOLS` fallback unless explicitly removed

## Telegram Rules

- Signals channel receives full signal and chart
- Cornix channel receives Cornix-format dry-run text
- Reports channel receives daily summaries, reports, and position advisories
- Daily Performance Report sends to `TELEGRAM_REPORTS_CHAT_ID` only
- External Inbox messages must not affect scanner-generated decisions
- Only APPROVED external signals may be sent to Signals or Cornix
- WAIT / SKIP / RISKY / FAILED external signals are CSV-only and summary-report only; no immediate Telegram message

## Validation

Before commit, run:

```bash
python -m compileall -q .
python tests/smoke_test.py
python performance_report.py
```

When scanner behavior changes, also verify startup with safe settings:

```bash
python cornix_agent.py
```

Use dry-run or safe environment settings for startup verification when Telegram/API credentials are not intended for live use.

## Git Workflow

Every successful code change must include:

```bash
git add .
git commit
git push origin main
```

Update `CURRENT_STATUS.md` when needed, especially when changing:

- production workflow
- systemd service names
- Telegram channel architecture
- dashboard/reporting status
- roadmap status
- scanner behavior that affects operators
