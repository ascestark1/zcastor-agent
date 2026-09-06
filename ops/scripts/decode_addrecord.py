#!/usr/bin/env python3
"""
Decode a successful addRecord call and compare it to ours.

The selector is confirmed deployed — hundreds of successful calls carry it — so
our revert is about arguments or state, not the signature. This pulls a
transaction that worked, decodes the Record struct field by field, and shows
exactly what a working call looks like.

    python3 ops/scripts/decode_addrecord.py
    python3 ops/scripts/decode_addrecord.py <txhash>    # decode a specific one

Read only.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

ADDRECORD = "9b7b7869"
EXPLORER = {"testnet": "https://explorer.evm.testnet.nvnmchain.io"}

FIELDS = ["registry", "uri", "checksum", "checksumAlgo", "metadata",
          "timestamp", "status", "recordId", "index", "isLatest"]


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Zcastor/2.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def word(data: str, i: int) -> int:
    return int(data[i * 64:(i + 1) * 64], 16)


def read_string(data: str, offset_bytes: int) -> str:
    start = offset_bytes * 2
    length = int(data[start:start + 64], 16)
    raw = data[start + 64: start + 64 + length * 2]
    return bytes.fromhex(raw).decode("utf-8", "replace")


def decode(input_hex: str) -> dict:
    """
    Decode addRecord((...)) calldata.

    A single dynamic struct argument is encoded as one head word pointing at
    the tuple body; string members are then offsets relative to that body.
    """
    data = input_hex[2:] if input_hex.startswith("0x") else input_hex
    data = data[8:]                              # drop the selector

    tuple_at = word(data, 0)                     # byte offset of the struct
    body = data[tuple_at * 2:]

    out = {}
    for i, name in enumerate(FIELDS):
        raw = word(body, i)
        if name in ("recordId", "index"):
            out[name] = raw
        elif name == "isLatest":
            out[name] = bool(raw)
        else:
            out[name] = read_string(body, raw)
    return out


def main() -> int:
    env = os.environ.get("NVNM_ENV", "testnet")
    base = EXPLORER.get(env)
    if not base:
        print(f"no explorer for {env!r}")
        return 1

    # txlist already carries each transaction's input, so there is no need for
    # a second lookup — gettxinfo does not reliably return it on this explorer.
    print("\nscanning for addRecord transactions...\n")
    rows, page = [], 1
    while page <= 10:
        url = (f"{base}/api?module=account&action=txlist"
               f"&address=0x0000000000000000000000000000000000000A00"
               f"&page={page}&offset=100&sort=asc")
        batch = fetch(url).get("result") or []
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    wanted = sys.argv[1].lower().replace("0x", "") if len(sys.argv) > 1 else ""
    if wanted:
        targets = [r for r in rows
                   if str(r.get("hash", "")).lower().replace("0x", "") == wanted]
        if not targets:
            print(f"  {wanted[:16]}… not found in {len(rows)} indexed txs")
            return 1
    else:
        targets = [r for r in rows
                   if str(r.get("input", ""))[2:10].lower() == ADDRECORD
                   and str(r.get("isError", "0")) != "1"][:2]
        if not targets:
            print(f"  no successful addRecord found in {len(rows)} txs")
            return 1

    for row in targets:
        tx_hash = row.get("hash", "?")
        input_hex = row.get("input", "")
        if not input_hex or len(input_hex) < 10:
            print(f"  {tx_hash}: no input data in the listing")
            continue
        status = "FAILED" if str(row.get("isError", "0")) == "1" else "ok"

        print(f"── {tx_hash}  [{status}]")
        print(f"   from {row.get('from', '?')}")
        print(f"   selector {input_hex[2:10]}\n")
        try:
            fields = decode(input_hex)
        except Exception as exc:  # noqa: BLE001
            print(f"   could not decode: {exc}")
            print(f"   raw: {input_hex[:200]}...")
            continue

        for name, value in fields.items():
            shown = value if not isinstance(value, str) else f"{value!r}"
            if isinstance(value, str) and len(value) > 90:
                shown = f"{value[:87]!r}..."
            print(f"   {name:14} {shown}")
        print()

        print("   ── compare with ours ──")
        print("   registry       we sent the NAME, e.g. 'afritensor-decisions'")
        print("   status         we sent 'Active'")
        print("   timestamp      we sent ''")
        print("   recordId/index we sent 0/0, isLatest False\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
