# -*- coding: utf-8 -*-
"""Manual trade risk calculator.

Risk calculator only. No exchange connection, no leverage recommendation, no
order placement.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from dotenv import load_dotenv

import manual_live_pilot


def format_plan(plan: dict[str, Any]) -> str:
    direction = f"- Direction: {plan['direction']}\n" if plan.get("direction") else ""
    reasons = plan.get("blocking_reasons") or ["None"]
    return (
        "Manual Trade Plan\n"
        f"- Symbol: {plan['symbol']}\n"
        f"{direction}"
        f"- Account balance: {plan['account_balance']:.2f}\n"
        f"- Risk percentage: {plan['risk_percent']:.2f}%\n"
        f"- Maximum loss amount: {plan['maximum_loss_amount']:.4f}\n"
        f"- Entry: {plan['entry']:.8g}\n"
        f"- Stop: {plan['stop']:.8g}\n"
        f"- Stop distance: {plan['stop_distance']:.8g}\n"
        f"- Maximum position notional: {plan['maximum_position_notional']:.4f}\n"
        f"- Pilot policy result: {plan['pilot_policy_result']}\n"
        f"- Blocking reasons: {'; '.join(reasons)}\n\n"
        "Maximum risk limit only. This is not a profit forecast, not leverage advice, and not an order."
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manual trade risk calculator. No orders are placed.")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--direction", default="")
    parser.add_argument("--entry", required=True)
    parser.add_argument("--stop", required=True)
    parser.add_argument("--account-balance", required=True)
    parser.add_argument("--risk-percent", required=True)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    load_dotenv(manual_live_pilot.BASE_DIR / ".env")
    args = parse_args()
    try:
        plan = manual_live_pilot.calculate_trade_plan(
            args.symbol,
            args.entry,
            args.stop,
            args.account_balance,
            args.risk_percent,
            args.direction,
        )
    except ValueError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            print(f"Manual Trade Plan Error: {exc}")
        return 2
    if args.json:
        print(json.dumps({"ok": True, "plan": plan}, indent=2))
    else:
        print(format_plan(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
