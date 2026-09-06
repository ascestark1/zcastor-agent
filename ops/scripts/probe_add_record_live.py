#!/usr/bin/env python3
"""
Find why addRecord reverts, by sending real transactions one variable at a time.

The selector is confirmed deployed and our struct shape closely matches calls
that succeed, so the fault is in a field value or in state. eth_call cannot help
— a precompile's read path does not expose state-changing methods, which is the
mistake that sent this investigation sideways once already.

So: send. Each variant changes exactly one thing from what we send today, and
the first success names the cause.

SENDS REAL TRANSACTIONS on testnet. Gas only, no economic value.

    python3 ops/scripts/probe_add_record_live.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zcastor.anchor.nvnm import (  # noqa: E402
    ANCHORING_ABI, ANCHORING_PRECOMPILE, ENVIRONMENTS,
)

# A checksum nobody has anchored, so we never hit "already exists".
BASE_CHECKSUM = "aa" * 31
REGISTRY_NAME = "afritensor-decisions"
REGISTRY_ID = 4297
LONG_URI = ("https://raw.githubusercontent.com/ascestark1/zcastor-dossiers/"
            "main/decisions/2026-08-28.7e16e4d9.json")


def record(checksum, registry=REGISTRY_NAME, uri=LONG_URI, algo="sha256",
           metadata='{"kind":"decision_log"}', ts="", status="Active",
           rid=0, index=0, latest=False):
    return (registry, uri, checksum, algo, metadata, ts, status,
            rid, index, latest)


def main() -> int:
    try:
        from eth_account import Account
        from web3 import Web3
    except ImportError:
        print("pip install web3 eth-account")
        return 1

    key = os.environ.get("NVNM_PRIVATE_KEY")
    if not key:
        print("NVNM_PRIVATE_KEY not set")
        return 1

    env = os.environ.get("NVNM_ENV", "testnet")
    cfg = ENVIRONMENTS[env]
    w3 = Web3(Web3.HTTPProvider(os.environ.get("NVNM_RPC_URL")
                                or cfg["rpc_url"]))
    acct = Account.from_key(key)
    c = w3.eth.contract(address=w3.to_checksum_address(ANCHORING_PRECOMPILE),
                        abi=ANCHORING_ABI)

    print(f"\n{env} chain {cfg['chain_id']}  sender {acct.address}")
    print(f"balance {w3.from_wei(w3.eth.get_balance(acct.address), 'ether'):.4f}"
          f" {cfg['gas_symbol']}\n")

    variants = [
        # (label, record) — each differs from "as we send it" in ONE way.
        ("as we send it today", record(BASE_CHECKSUM + "01")),
        ("registry as numeric id string", record(BASE_CHECKSUM + "02",
                                                 registry=str(REGISTRY_ID))),
        ("lowercase status", record(BASE_CHECKSUM + "03", status="active")),
        ("short uri", record(BASE_CHECKSUM + "04",
                             uri="ipfs://" + BASE_CHECKSUM + "04")),
        ("plain metadata", record(BASE_CHECKSUM + "05",
                                  metadata='{"name":"probe"}')),
        ("checksumAlgo '-'", record(BASE_CHECKSUM + "06", algo="-")),
        ("timestamp '-'", record(BASE_CHECKSUM + "07", ts="-")),
        ("isLatest true", record(BASE_CHECKSUM + "08", latest=True)),
        ("recordId 1", record(BASE_CHECKSUM + "09", rid=1)),
        # Byte-for-byte the shape of a call that is known to have succeeded,
        # differing only in registry and checksum.
        ("known-good shape", record(BASE_CHECKSUM + "10", algo="sha256",
                                    uri="ipfs://" + BASE_CHECKSUM + "10",
                                    metadata='{"name": "probe", "ts": 1}',
                                    ts="", status="active")),
    ]

    nonce = w3.eth.get_transaction_count(acct.address)
    results = []

    for label, rec in variants:
        try:
            tx = c.functions.addRecord(rec).build_transaction({
                "from": acct.address, "nonce": nonce,
                "chainId": cfg["chain_id"], "gas": 3_000_000,
            })
            signed = acct.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
            nonce += 1
            ok = int(receipt.get("status", 0)) == 1
            mark = " OK " if ok else "FAIL"
            print(f"  [{mark}] {label:34} {tx_hash.hex()[:20]}…")
            results.append((label, ok))
        except Exception as exc:  # noqa: BLE001
            print(f"  [ERR ] {label:34} {str(exc)[:80]}")
            results.append((label, False))
        time.sleep(1)

    good = [l for l, ok in results if ok]
    print(f"\n  {len(good)}/{len(results)} succeeded")
    if good:
        print("\n  Working variants:")
        for label in good:
            print(f"    - {label}")
        print("\n  The first difference between a working and a failing")
        print("  variant is the cause.\n")
    else:
        print("\n  None succeeded. The fault is not in these fields — most")
        print("  likely the sender lacks an editor role on this registry,")
        print("  or the registry name does not resolve. Check the role grant")
        print("  transactions landed against registry id 4297.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
