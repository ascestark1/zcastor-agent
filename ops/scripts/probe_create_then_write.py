#!/usr/bin/env python3
"""
Create a registry and immediately write a record into it.

Every field variant of addRecord reverts against our existing registries, so the
fault is the registry or the role, not the payload. This isolates that: one
sender, a brand-new registry, a write moments later. It reproduces the shape of
a call that is known to have succeeded on this chain.

  - If the write succeeds, our four existing registries are the problem —
    either their names do not resolve or the editor grants did not bind.
  - If it fails, creating a registry does not confer write access on it, and
    the role model needs explaining.

Then it tries the same write against each existing registry by name, so we can
see whether one specific registry is broken or all of them.

SENDS REAL TRANSACTIONS on testnet. Gas only.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zcastor.anchor.nvnm import (  # noqa: E402
    ANCHORING_ABI, ANCHORING_PRECOMPILE, ENVIRONMENTS,
    registry_id_from_receipt,
)

EXISTING = {"afritensor-policy": 4296, "afritensor-decisions": 4297,
            "afritensor-dossiers": 4298, "afritensor-archive": 4299}


def main() -> int:
    from eth_account import Account
    from web3 import Web3

    key = os.environ.get("NVNM_PRIVATE_KEY")
    if not key:
        print("NVNM_PRIVATE_KEY not set")
        return 1

    env = os.environ.get("NVNM_ENV", "testnet")
    cfg = ENVIRONMENTS[env]
    w3 = Web3(Web3.HTTPProvider(os.environ.get("NVNM_RPC_URL") or cfg["rpc_url"]))
    acct = Account.from_key(key)
    c = w3.eth.contract(address=w3.to_checksum_address(ANCHORING_PRECOMPILE),
                        abi=ANCHORING_ABI)
    nonce = w3.eth.get_transaction_count(acct.address)

    def send(fn, label):
        nonlocal nonce
        try:
            tx = fn.build_transaction({"from": acct.address, "nonce": nonce,
                                       "chainId": cfg["chain_id"],
                                       "gas": 3_000_000})
            signed = acct.sign_transaction(tx)
            h = w3.eth.send_raw_transaction(signed.raw_transaction)
            r = w3.eth.wait_for_transaction_receipt(h, timeout=90)
            nonce += 1
            ok = int(r.get("status", 0)) == 1
            print(f"  [{' OK ' if ok else 'FAIL'}] {label:44} {h.hex()[:18]}…")
            time.sleep(1)
            return ok, r
        except Exception as exc:  # noqa: BLE001
            print(f"  [ERR ] {label:44} {str(exc)[:70]}")
            return False, None

    stamp = int(time.time())
    fresh = f"afritensor-probe-{stamp}"
    print(f"\n{env}  sender {acct.address}\n")

    print("── create a fresh registry, then write into it ──")
    ok, receipt = send(c.functions.addRegistry(fresh, "write probe", ""),
                       f"addRegistry({fresh})")
    new_id = registry_id_from_receipt(receipt) if ok else 0
    print(f"         assigned id: {new_id}\n")

    def rec(registry, suffix):
        return (registry, f"ipfs://{'bb' * 31}{suffix}", f"{'bb' * 31}{suffix}",
                "sha256", '{"name":"probe"}', "", "active", 0, 0, False)

    if ok:
        send(c.functions.addRecord(rec(fresh, "01")),
             f"addRecord into {fresh} by NAME")
        if new_id:
            send(c.functions.addRecord(rec(str(new_id), "02")),
                 f"addRecord into '{new_id}' by ID-as-string")

    print("\n── the same write against each existing registry ──")
    for i, (name, rid) in enumerate(EXISTING.items(), start=10):
        send(c.functions.addRecord(rec(name, f"{i}")),
             f"addRecord into {name}")

    print("\n  A fresh registry that accepts writes while the existing four")
    print("  refuse means the problem is those registries, not the method.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
