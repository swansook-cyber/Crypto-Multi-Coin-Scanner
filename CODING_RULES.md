# Coding Rules

## Production Safety

- Do not break the production scanner
- Do not change entry, TP, or SL logic unless explicitly requested
- Preserve Telegram message structure unless the task is about message formatting
- Preserve signal logging
- Preserve the review/outcome engine
- Preserve manual execution only
- Never add auto trading

## Development Style

- Prefer small improvements
- Avoid unnecessary indicators
- Avoid feature bloat
- Every optimization should be measurable
- Add columns and analytics with backward compatibility for old CSV files
- Fail safely if optional data, API keys, Telegram, or journal files are missing

## Strategy Rules

- Rule engine remains the decision maker
- AI commentary is optional explanation only
- New filters should reduce bad signals, not increase signal volume
- Do not add indicators without statistical evidence from collected outcomes

## Validation

Before commit, run:

```bash
python -m compileall -q .
python tests/smoke_test.py
```

When scanner behavior changes, also verify startup:

```bash
python cornix_agent.py
```

Use dry-run or safe environment settings for startup verification when Telegram/API credentials are not intended for live use.
