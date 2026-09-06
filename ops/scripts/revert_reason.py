#!/usr/bin/env python3
"""
Ask the explorer why a transaction reverted.

The RPC discards revert reasons for precompiles, but Blockscout stores an error
description when it has one. This has been the missing piece all along: every
failure so far has been diagnosed by inference rather than by reading what the
chain said.

    python3 ops/scripts/revert_reason.py <txhash> [<txhash> ...]

Read only.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

EXPLORER = {"testnet": "https://explorer.evm.testnet.nvnmchain.io"}


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Zcastor/2.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    base = EXPLORER[os.environ.get("NVNM_ENV", "testnet")]
    print()

    for raw in sys.argv[1:]:
        tx = raw if raw.startswith("0x") else "0x" + raw
        print(f"── {tx[:22]}…")

        # Blockscout exposes the reason in two places; try both.
        for action in ("getstatus", "gettxreceiptstatus"):
            try:
                out = fetch(f"{base}/api?module=transaction&action={action}"
                            f"&txhash={tx}")
            except Exception as exc:  # noqa: BLE001
                print(f"   {action}: request failed ({exc})")
                continue
            result = out.get("result")
            print(f"   {action}: {json.dumps(result)[:300]}")

        # The v2 API carries a decoded revert reason when one exists.
        try:
            v2 = fetch(f"{base}/api/v2/transactions/{tx}")
            for field in ("status", "result", "revert_reason", "error"):
                if field in v2:
                    print(f"   v2.{field}: {json.dumps(v2[field])[:300]}")
            if v2.get("gas_used") and v2.get("gas_limit"):
                used, limit = int(v2["gas_used"]), int(v2["gas_limit"])
                note = "  <-- consumed the whole limit" if used >= limit * 0.99 else ""
                print(f"   gas: {used} / {limit}{note}")
        except Exception as exc:  # noqa: BLE001
            print(f"   v2: {exc}")
        print()

    print("  'consumed the whole limit' points at an out-of-gas or an")
    print("  invalid-opcode style failure rather than a clean require().\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
