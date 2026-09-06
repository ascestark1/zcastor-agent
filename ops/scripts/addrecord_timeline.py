#!/usr/bin/env python3
"""
When did addRecord stop working?

The chain reports "unknown method id: 2608560233" for selector 0x9b7b7869, yet
hundreds of transactions carrying that selector succeeded. Both cannot be true
at once unless the precompile changed: the method was accepted historically and
is not accepted now.

This orders every addRecord transaction by time and shows where success turns
into failure. A clean cutover date means the precompile was changed and the
method id it accepts changed with it.

    python3 ops/scripts/addrecord_timeline.py

Read only.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timezone

ADDRECORD = "9b7b7869"
ADDREGISTRY = "318b38b1"
EXPLORER = {"testnet": "https://explorer.evm.testnet.nvnmchain.io"}


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Zcastor/2.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def when(row: dict) -> str:
    try:
        ts = int(row.get("timeStamp", 0))
        return datetime.fromtimestamp(ts, timezone.utc).strftime(
            "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return "?"


def main() -> int:
    base = EXPLORER[os.environ.get("NVNM_ENV", "testnet")]
    address = "0x0000000000000000000000000000000000000A00"

    # Fetch BOTH ends. A one-directional scan hits the page cap and stops
    # mid-history, which looks exactly like a method that stopped working.
    def scan(order: str, pages: int = 20) -> list:
        out, page = [], 1
        while page <= pages:
            batch = fetch(f"{base}/api?module=account&action=txlist"
                          f"&address={address}&page={page}&offset=100"
                          f"&sort={order}").get("result") or []
            if not batch:
                break
            out.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return out

    oldest = scan("asc")
    newest = scan("desc")
    by_hash = {r.get("hash"): r for r in oldest + newest}
    rows = sorted(by_hash.values(), key=lambda r: int(r.get("timeStamp", 0)))

    truncated = len(oldest) >= 2000 and len(newest) >= 2000
    print(f"\n  {len(rows)} unique transactions "
          f"({len(oldest)} oldest-first, {len(newest)} newest-first)")
    if truncated:
        print("  NOTE: both scans hit the page cap, so the middle of the")
        print("  history is missing. Ends are complete; the gap is not.")
    print()

    calls = [(when(r), str(r.get("input", ""))[2:10].lower(),
              str(r.get("isError", "0")) == "1", r.get("from", ""),
              r.get("hash", ""))
             for r in rows]

    # Where does addRecord flip from working to failing?
    add = [c for c in calls if c[1] == ADDRECORD]
    ok = [c for c in add if not c[2]]
    bad = [c for c in add if c[2]]

    print(f"  addRecord (0x{ADDRECORD}): {len(ok)} ok, {len(bad)} failed")
    if ok:
        print(f"    first success : {ok[0][0]}")
        print(f"    last  success : {ok[-1][0]}")
    if bad:
        print(f"    first failure : {bad[0][0]}")
        print(f"    last  failure : {bad[-1][0]}")

    reg = [c for c in calls if c[1] == ADDREGISTRY and not c[2]]
    if reg:
        print(f"\n  addRegistry (0x{ADDREGISTRY}): {len(reg)} ok")
        print(f"    first success : {reg[0][0]}")
        print(f"    last  success : {reg[-1][0]}")

    print("\n  ── last 25 calls, any method ──")
    print(f"  {'when':17} {'selector':10} {'result':8} from")
    for w, sel, err, sender, _ in calls[-25:]:
        print(f"  {w:17} {sel:10} {'FAILED' if err else 'ok':8} {sender[:14]}…")

    print("\n  ── methods seen, by first and last successful use ──")
    seen: dict[str, list] = {}
    for w, sel, err, _, _ in calls:
        if not err:
            seen.setdefault(sel, []).append(w)
    for sel, times in sorted(seen.items(), key=lambda kv: kv[1][0]):
        print(f"  {sel}  {len(times):>4} ok   {times[0]}  →  {times[-1]}")

    senders = Counter(c[3] for c in ok)
    if senders:
        print("\n  senders with successful addRecord calls:")
        for sender, n in senders.most_common(5):
            print(f"    {sender}  {n}")

    print("\n  If addRecord's last success predates addRegistry's, the")
    print("  precompile changed and the method id moved with it.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
