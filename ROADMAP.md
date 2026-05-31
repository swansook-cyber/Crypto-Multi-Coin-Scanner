# Roadmap

## Current Roadmap Priorities

1. Daily Performance Report
2. Dashboard V1
3. Position Management Advisor
4. Position Exit Advisor
5. Advanced TP Engine

## Current / Active

- Daily Performance Report is implemented through `performance_report.py`
- Dashboard V1 is implemented as local HTML through `dashboard.py`
- Position Management Advisor is implemented through `position_manager.py` and scanner Telegram advisory routing
- Production monitoring now focuses on whether these tools produce useful operational decisions

## Next

- Position Exit Advisor
  - Recommend exit / hold / wait / review based on open signal state
  - Telegram advisory only
  - No auto trading
- Advanced TP Engine
  - Research-only until enough outcome data supports changes
  - Must not replace ATR TP/SL without statistical evidence

## Future

- Strategy optimization using collected results
- Confidence/setup strength calibration
- Performance analytics by symbol, tier, session, BTC regime, wave score, and direction
- Dashboard V1 usability improvements

## Long Term

- Closed beta subscription model
- VPS-hosted production service

## Rule

Data first.
Optimization second.
New indicators/features last.

Do not add strategy complexity without real performance evidence from collected outcomes.
