#!/usr/bin/env python3
"""
Diagnose order_send across the rpyc bridge.

PLACES A REAL ORDER at minimum volume, then closes it immediately. Costs one
spread — a few cents at 0.01 lots. Run it only when you mean to.

It exists because `order_send returning None` is indistinguishable from a dead
bridge in the logs, and there are three separate candidate causes: a netref dict
the Windows side cannot read, a symbol not selected in Market Watch, or a
request field the broker rejects. This tries them in order and says which.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zcastor.market.mt5 import connect  # noqa: E402

SYMBOL = "BTCUSD"
VOLUME = 0.01


def main() -> int:
    mt5 = connect()

    info = mt5.symbol_info(SYMBOL)
    if info is None:
        print(f"FAIL  symbol_info({SYMBOL}) is None — wrong symbol name?")
        return 1
    print(f"ok    symbol {SYMBOL} visible={getattr(info, 'visible', '?')} "
          f"digits={getattr(info, 'digits', '?')} "
          f"min_volume={getattr(info, 'volume_min', '?')}")

    if not getattr(info, "visible", True):
        print("··    not in Market Watch — selecting it")
        mt5.symbol_select(SYMBOL, True)

    tick = mt5.symbol_info_tick(SYMBOL)
    ask = float(tick.ask.item() if hasattr(tick.ask, "item") else tick.ask)
    print(f"ok    ask={ask:.2f}")

    request = {
        # ints, not netref constants — they have to survive repr().
        "action": int(mt5.TRADE_ACTION_DEAL),
        "symbol": SYMBOL,
        "volume": VOLUME,
        "type": int(mt5.ORDER_TYPE_BUY),
        "price": ask,
        "deviation": 20,
        "magic": 777001,
        "comment": "zcastor-probe",
        "type_filling": int(mt5.ORDER_FILLING_IOC),
    }

    print("\n-- attempt 1: request built locally, passed as a netref --")
    result = mt5.order_send(request)
    print(f"      result={result if result is None else getattr(result, 'retcode', '?')}"
          f"  last_error={mt5.last_error()}")

    print("\n-- attempt 2: dict built inside the remote interpreter --")
    result = mt5.send_request(dict(request))
    retcode = None if result is None else int(getattr(result, "retcode", 0))
    comment = "" if result is None else getattr(result, "comment", "")
    print(f"      result={retcode} {comment}  last_error={mt5.last_error()}")

    if retcode == 10009:
        ticket = int(getattr(result, "order", 0))
        print(f"\nok    FILLED ticket={ticket} — closing it now")
        bid = float(tick.bid.item() if hasattr(tick.bid, "item") else tick.bid)
        closed = mt5.send_request({
            "action": int(mt5.TRADE_ACTION_DEAL), "symbol": SYMBOL,
            "volume": VOLUME, "type": int(mt5.ORDER_TYPE_SELL),
            "position": ticket, "price": bid, "deviation": 20,
            "magic": 777001, "comment": "zcastor-probe-close",
            "type_filling": int(mt5.ORDER_FILLING_IOC),
        })
        print(f"      close retcode="
              f"{None if closed is None else getattr(closed, 'retcode', '?')}")
        print("\nREMOTE CONSTRUCTION IS THE FIX — the engine's change is correct.")
        return 0

    print(f"\nNeither worked. retcode={retcode}, last_error={mt5.last_error()}")
    print("Check: is AutoTrading enabled in the terminal? Is the account")
    print("permitted to trade this symbol? Is volume below the minimum?")
    return 1


if __name__ == "__main__":
    sys.exit(main())
