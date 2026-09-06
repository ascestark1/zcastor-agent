#!/usr/bin/env python3
"""
Decode the arguments of any selector, without knowing its signature.

ABI calldata is self-describing enough to infer a lot: each 32-byte head word is
either a value or an offset into the tail, and a plausible offset that lands on a
length-prefixed run of readable bytes is almost certainly a string. That is
enough to reconstruct an argument list and check it against a guess.

Written because a selector appeared in heavy recent use that we cannot name, and
guessing signatures has already cost this project a retraction.

    python3 ops/scripts/decode_selector.py 64d25295

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


def words(data: str) -> list[str]:
    return [data[i:i + 64] for i in range(0, len(data) - 63, 64)]


def as_string(data: str, offset: int) -> str | None:
    """Read a length-prefixed string at a byte offset, if one is there."""
    start = offset * 2
    if start + 64 > len(data):
        return None
    try:
        length = int(data[start:start + 64], 16)
    except ValueError:
        return None
    if length > 4096 or start + 64 + length * 2 > len(data):
        return None
    try:
        return bytes.fromhex(data[start + 64:start + 64 + length * 2]).decode()
    except (ValueError, UnicodeDecodeError):
        return None


def describe(payload: str) -> None:
    data = payload[10:] if payload.startswith("0x") else payload[8:]
    heads = words(data)

    print(f"   {len(data) // 64} words of calldata\n")
    for i, w in enumerate(heads[:14]):
        value = int(w, 16)
        text = as_string(data, value) if 0 < value < len(data) // 2 else None
        if text is not None:
            shown = text if len(text) <= 70 else text[:67] + "..."
            print(f"   [{i}] offset {value:>5} -> string {shown!r}")
        elif value < 2 ** 32:
            print(f"   [{i}] uint    {value}")
        else:
            print(f"   [{i}] word    0x{w[-40:]}")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    selector = sys.argv[1].lower().replace("0x", "")
    base = EXPLORER[os.environ.get("NVNM_ENV", "testnet")]
    address = "0x0000000000000000000000000000000000000A00"

    rows = []
    for page in range(1, 6):
        batch = fetch(f"{base}/api?module=account&action=txlist"
                      f"&address={address}&page={page}&offset=100"
                      f"&sort=desc").get("result") or []
        if not batch:
            break
        rows.extend(batch)

    hits = [r for r in rows
            if str(r.get("input", ""))[2:10].lower() == selector
            and str(r.get("isError", "0")) != "1"]

    if not hits:
        print(f"\n  no successful 0x{selector} in the most recent "
              f"{len(rows)} transactions\n")
        return 1

    print(f"\n  0x{selector}: {len(hits)} successful in the recent window\n")
    for row in hits[:3]:
        print(f"── {row.get('hash', '?')}")
        print(f"   from {row.get('from', '?')}")
        describe(str(row.get("input", "")))
        print()

    print("  Read the argument list above, then confirm it by computing the")
    print("  selector for the signature it implies:")
    print("    python3 ops/scripts/verify_abi.py\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
