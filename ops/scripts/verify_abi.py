#!/usr/bin/env python3
"""
Check our ABI's function selectors against what the chain actually implements.

Written after four registry-creation transactions reverted because our
addRegistry declared two string arguments where the precompile takes three.
A precompile returns no revert reason for an unknown method, so the only
symptom was "reverted" with no cause. This makes that check free.

Run it against calldata from the MCP server's prepare endpoints:

    python3 ops/scripts/verify_abi.py 318b38b1 addRegistry
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zcastor.anchor.nvnm import ANCHORING_ABI, SELECTORS_VERIFIED  # noqa: E402


def keccak256(data: bytes) -> bytes:
    RC = [0x0000000000000001, 0x0000000000008082, 0x800000000000808A,
          0x8000000080008000, 0x000000000000808B, 0x0000000080000001,
          0x8000000080008081, 0x8000000000008009, 0x000000000000008A,
          0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
          0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
          0x8000000000008003, 0x8000000000008002, 0x8000000000000080,
          0x000000000000800A, 0x800000008000000A, 0x8000000080008081,
          0x8000000000008080, 0x0000000080000001, 0x8000000080008008]
    ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61],
           [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
    M = (1 << 64) - 1

    def rol(x, n):
        return ((x << n) | (x >> (64 - n))) & M

    rate = 136
    padded = bytearray(data) + bytearray([0x01])
    while len(padded) % rate:
        padded.append(0)
    padded[-1] ^= 0x80

    S = [[0] * 5 for _ in range(5)]
    for off in range(0, len(padded), rate):
        blk = padded[off:off + rate]
        for i in range(rate // 8):
            S[i % 5][i // 5] ^= int.from_bytes(blk[i * 8:(i + 1) * 8], "little")
        for rnd in range(24):
            C = [S[x][0] ^ S[x][1] ^ S[x][2] ^ S[x][3] ^ S[x][4] for x in range(5)]
            D = [C[(x - 1) % 5] ^ rol(C[(x + 1) % 5], 1) for x in range(5)]
            for x in range(5):
                for y in range(5):
                    S[x][y] ^= D[x]
            B = [[0] * 5 for _ in range(5)]
            for x in range(5):
                for y in range(5):
                    B[y][(2 * x + 3 * y) % 5] = rol(S[x][y], ROT[x][y])
            for x in range(5):
                for y in range(5):
                    S[x][y] = B[x][y] ^ ((~B[(x + 1) % 5][y] & M) & B[(x + 2) % 5][y])
            S[0][0] ^= RC[rnd]
    return b"".join(S[i % 5][i // 5].to_bytes(8, "little") for i in range(25))[:32]


def _type(component: dict) -> str:
    """
    Solidity type name for a selector.

    Tuples expand into their components in parentheses. Emitting the literal
    word "tuple" produces a selector for a function that does not exist, which
    is the same class of error this script was written to catch.
    """
    if component.get("type", "").startswith("tuple"):
        inner = ",".join(_type(c) for c in component.get("components", []))
        suffix = component["type"][len("tuple"):]     # handles tuple[] etc.
        return f"({inner}){suffix}"
    return component["type"]


def signature(entry: dict) -> str:
    args = ",".join(_type(i) for i in entry.get("inputs", []))
    return f"{entry['name']}({args})"


def selector(sig: str) -> str:
    return keccak256(sig.encode()).hex()[:8]


def main() -> int:
    print("\n  selector  status      signature")
    print("  " + "-" * 66)
    mismatches = 0
    for entry in ANCHORING_ABI:
        sig = signature(entry)
        sel = selector(sig)
        known = SELECTORS_VERIFIED.get(entry["name"])
        if known is None:
            status = "NOT ON CHAIN"
        elif known == sel:
            status = "verified  "
        else:
            status = "MISMATCH  "
            mismatches += 1
        print(f"  {sel}  {status}  {sig}")

    if len(sys.argv) == 3:
        observed, name = sys.argv[1].lower().replace("0x", ""), sys.argv[2]
        entry = next((e for e in ANCHORING_ABI if e["name"] == name), None)
        if entry is None:
            print(f"\n  no ABI entry named {name!r}")
            return 1
        ours = selector(signature(entry))
        print(f"\n  observed {observed}   ours {ours}   "
              f"{'MATCH' if ours == observed else 'MISMATCH'}")
        if ours != observed:
            mismatches += 1

    print(f"\n  {'all checked selectors agree' if not mismatches else f'{mismatches} mismatch(es)'}\n")
    print("  To verify the unconfirmed ones, call the MCP prepare endpoint and")
    print("  pass the first 4 bytes of its data field to this script.\n")
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
