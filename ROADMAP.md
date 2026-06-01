# Roadmap

## Current Roadmap Priorities

1. Daily Performance Report
2. Dashboard V1
3. Position Management Advisor
4. Position Exit Advisor
5. Advanced TP Engine

## Current / Active

- Daily Performance Report is implemented through `performance_report.py`
- Complete Performance Analytics V1 is implemented for scanner, external, and position-management logs
- Dashboard V1 is implemented as local HTML through `dashboard.py`
- Position Management Advisor is implemented through `position_manager.py` and scanner Telegram advisory routing
- External Signal Analyzer V1 is implemented as approved-only routing for forwarded VIP signals
- Production monitoring now focuses on collecting enough clean outcomes for calibration decisions

Performance Analytics V1 exports:

- `logs/daily_performance.csv`
- `logs/symbol_performance.csv`
- `logs/source_performance.csv`
- `logs/position_management.csv`

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
- Performance analytics by symbol, tier, session, BTC regime, market regime, source, and direction
- Dashboard V1 usability improvements

## Long Term

- Closed beta subscription model
- VPS-hosted production service

## Rule

Data first.
Optimization second.
New indicators/features last.

Do not add strategy complexity without real performance evidence from collected outcomes.
