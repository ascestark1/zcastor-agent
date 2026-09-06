#!/usr/bin/env python3
"""
Market port smoke test — READ ONLY.

Places no orders, closes nothing, writes nothing. Safe to run against a live
funded account.

What it is actually for: every assumption in `market/mt5.py` about what the
bridge returns was written from the v1 source and verified against a fake. The
fake already caught one bug — `int()` on a numpy scalar, which works locally and
fails over rpyc. This script checks the remaining assumptions against the real
thing before any decision logic depends on them.

Usage:
    export MT5_LOGIN=... MT5_PASSWORD=... MT5_SERVER=...
    python3 ops/scripts/smoke_market.py
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zcastor.market.mt5 import MarketData, MT5Unavailable, connect  # noqa: E402
from zcastor.market.session import SessionOpens, current_session  # noqa: E402
from zcastor.record import proof  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

OK, BAD, INFO = "  ok  ", " FAIL ", "  ··  "


def main() -> int:
    print("\n=== MT5 market port smoke test (read only) ===\n")

    try:
        mt5 = connect()
    except MT5Unavailable as exc:
        print(f"{BAD} connect: {exc}\n")
        print("Checks:")
        print("  - is ops/start_mt5_bridge.sh running, in the SAME Wine prefix")
        print("    as the terminal? A logged-in terminal is not a running bridge.")
        print("  - are MT5_LOGIN / MT5_PASSWORD / MT5_SERVER exported?")
        return 1

    sessions = SessionOpens(lambda f, t: market.get_rates_range(f, t))
    market = MarketData(mt5, symbol="BTCUSD", volume=0.05, session_opens=sessions)
    market.begin()

    failures = 0

    # ── tick ──
    tick = market.get_tick()
    if "error" in tick:
        print(f"{BAD} tick: {tick['error']}")
        failures += 1
    else:
        print(f"{OK} tick   ask={tick['ask']:.2f} bid={tick['bid']:.2f} "
              f"spread={tick['spread_pts']}pts")
        for field in ("ask", "bid", "price"):
            if not isinstance(tick[field], float):
                print(f"{BAD} {field} is {type(tick[field]).__name__}, not float "
                      f"— numpy unwrapping is wrong")
                failures += 1
        if not tick["time"]:
            print(f"{INFO} tick has no broker time — the stale-feed gate cannot work")

    # ── account ──
    balance = market.get_balance()
    if balance is None:
        print(f"{BAD} balance unreadable — the dead-man gate will suppress everything")
        failures += 1
    else:
        print(f"{OK} account balance={balance:.2f} free_margin="
              f"{market.get_free_margin():.2f} equity={market.get_equity():.2f}")

    # ── margin for a hypothetical order (calculates only, places nothing) ──
    if "error" not in tick:
        margin = market.get_margin_for("UP", 0.05, tick["ask"])
        if margin > 0:
            print(f"{OK} margin  0.05 lots would need {margin:.2f}")
        else:
            print(f"{INFO} order_calc_margin unavailable — viability falls back "
                  f"to its conservative estimate")

    # ── positions ──
    positions = market.get_open_positions()
    print(f"{OK} positions open={len(positions)}")
    for p in positions[:5]:
        print(f"       #{p['ticket']} {p['side']} {p['volume']} @ "
              f"{p['open_price']:.2f} pnl={p['profit']:.2f}")

    # ── session open ──
    session = current_session()
    open_price = market.session_open_price(session)
    if open_price > 0:
        print(f"{OK} session {session} opened at {open_price:.2f}")
    else:
        print(f"{INFO} session {session} open unavailable — London and "
              f"exhaustion gates will skip")

    # ── caching ──
    before = market.get_tick()
    after = market.get_tick()
    print(f"{OK} cache   two reads returned the same instant: "
          f"{before['time'] == after['time']}")

    # ── proof of state ──
    state = market.state(session=session)
    print(f"{OK} state   {proof.state_hash(**state)}")
    print(f"{INFO}         balance is hashed, never published")

    print(f"\n=== {'PASSED' if not failures else f'{failures} FAILURE(S)'} "
          f"at {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC ===\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
