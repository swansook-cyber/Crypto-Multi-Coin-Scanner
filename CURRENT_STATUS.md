# Current Status

- Scanner running: active production workflow is built around `cornix_agent.py`
- Telegram integration: working for signal alerts, outcome alerts, test messages, and daily summaries
- Statistics collection: active through `logs/signals.csv`, `logs/signals_history.csv`, and analytics exports
- Outcome review: active through `review_signals.py`
- Daily reports: implemented, still being refined for production usefulness
- Dashboard V1: pending
- Position Management Advisor: pending
- Auto trading: not implemented and not planned for the current phase

## Production Notes

- Scanner is a Telegram signal assistant only
- Execution remains manual
- Existing signal quality filters and cooldown systems should remain stable
- Runtime CSV/log/chart output should stay out of Git
