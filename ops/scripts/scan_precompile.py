#!/usr/bin/env python3
"""
Tally every method anyone has called on the anchoring precompile.

Answers a question the MCP server cannot right now: which functions does this
chain's precompile actually accept? Every transaction to 0x…0A00 carries a
four-byte selector, and the explorer indexes them all. Group by selector, split
by success, and the deployed surface is visible.

If nobody on the chain has ever successfully called addRecord, the method is
not deployed here — which is a different and much more useful statement than
"our client could not find the signature".

    python3 ops/scripts/scan_precompile.py

Read only. Uses the Blockscout API, no key required.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_abi import _type, keccak256  # noqa: E402
from zcastor.anchor.nvnm import ANCHORING_ABI, ANCHORING_PRECOMPILE  # noqa: E402

EXPLORERS = {
    "testnet": "https://explorer.evm.testnet.nvnmchain.io",
    "mainnet": "https://explorer.evm.nvnmchain.io",
}

# Selectors we can name, from our ABI plus the candidates we probed.
EXTRA = {
    "addRegistry(string,string,string)",
    "addRegistry(string,string)",
    "grantRole(uint64,string,address,string)",
    "revokeRole(uint64,string,address,string)",
    "addRecord((string,string,string,string,string,string,string,uint64,uint64,bool))",
    "updateRecordStatus(uint64,uint64,string,uint64,string)",
}


def known_selectors() -> dict[str, str]:
    out = {}
    for entry in ANCHORING_ABI:
        sig = f"{entry['name']}({','.join(_type(i) for i in entry.get('inputs', []))})"
        out[keccak256(sig.encode()).hex()[:8]] = sig
    for sig in EXTRA:
        out[keccak256(sig.encode()).hex()[:8]] = sig
    return out


def fetch(url: str) -> dict:
    request = urllib.request.Request(
        url, headers={"User-Agent": "Zcastor/2.0", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode())


def main() -> int:
    env = os.environ.get("NVNM_ENV", "testnet")
    base = EXPLORERS.get(env)
    if not base:
        print(f"no explorer configured for {env!r}")
        return 1

    address = ANCHORING_PRECOMPILE
    names = known_selectors()

    print(f"\n{env}: transactions to {address}\n")

    seen, page = [], 1
    while page <= 10:
        url = (f"{base}/api?module=account&action=txlist&address={address}"
               f"&page={page}&offset=100&sort=asc")
        try:
            payload = fetch(url)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"  explorer request failed: {exc}")
            break

        rows = payload.get("result") or []
        if not isinstance(rows, list) or not rows:
            break
        seen.extend(rows)
        if len(rows) < 100:
            break
        page += 1

    if not seen:
        print("  no transactions found — either the explorer indexes")
        print("  precompile calls differently, or nobody has called it.\n")
        return 0

    ok, failed = Counter(), Counter()
    for tx in seen:
        selector = str(tx.get("input", ""))[2:10].lower()
        # Blockscout marks failures with isError=1.
        (failed if str(tx.get("isError", "0")) == "1" else ok)[selector] += 1

    print(f"  {len(seen)} transactions\n")
    print(f"  {'selector':10}  {'ok':>5}  {'failed':>6}  signature")
    print("  " + "-" * 74)
    for selector in sorted(set(ok) | set(failed),
                           key=lambda s: -(ok[s] + failed[s])):
        print(f"  {selector:10}  {ok[selector]:>5}  {failed[selector]:>6}  "
              f"{names.get(selector, '(unknown)')}")

    deployed = {s for s in ok if ok[s]}
    print(f"\n  {len(deployed)} selector(s) with at least one successful call.")
    unnamed = [s for s in deployed if s not in names]
    if unnamed:
        print("\n  Successful but unrecognised — these are deployed methods we")
        print("  have not identified. Look one up on the explorer to see its")
        print("  decoded arguments:")
        for s in unnamed:
            print(f"    {s}   {base}/address/{address}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
