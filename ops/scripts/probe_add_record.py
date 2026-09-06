#!/usr/bin/env python3
"""
Diagnose an addRecord revert by simulating variants with eth_call.

eth_call executes against current state WITHOUT sending a transaction, so it
costs nothing and often returns the revert reason a mined transaction throws
away. It tries the variants that plausibly differ, and reports which succeed.

    python3 ops/scripts/probe_add_record.py

Read only. Sends no transactions.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zcastor.anchor.nvnm import ANCHORING_ABI, ANCHORING_PRECOMPILE, ENVIRONMENTS  # noqa: E402

CHECKSUM = "7e16e4d90c9077ddc0f14b643bd5ae07bf8ebc40051d791b1795cd59612bb2de"
URI = "https://raw.githubusercontent.com/ascestark1/zcastor-dossiers/main/decisions/2026-08-28.json"


def main() -> int:
    try:
        from web3 import Web3
    except ImportError:
        print("web3 required")
        return 1

    env = os.environ.get("NVNM_ENV", "testnet")
    rpc = os.environ.get("NVNM_RPC_URL") or ENVIRONMENTS[env]["rpc_url"]
    sender = os.environ.get("ZCASTOR_AGENT_EXECUTION", "")
    if not sender:
        print("set ZCASTOR_AGENT_EXECUTION to the sending address")
        return 1

    w3 = Web3(Web3.HTTPProvider(rpc))
    c = w3.eth.contract(address=w3.to_checksum_address(ANCHORING_PRECOMPILE),
                        abi=ANCHORING_ABI)

    def rec(registry="afritensor-decisions", uri=URI, checksum=CHECKSUM,
            algo="sha256", metadata='{"kind":"decision_log"}', ts="",
            status="Active", rid=0, index=0, latest=False):
        return (registry, uri, checksum, algo, metadata, ts, status,
                rid, index, latest)

    variants = [
        ("as the engine sends it", rec()),
        ("lowercase status 'active'", rec(status="active")),
        ("short uri", rec(uri="https://example.test/a.json")),
        ("plain-label metadata", rec(metadata="decision log 2026-08-28")),
        ("registry by numeric id as string", rec(registry="4297")),
        ("docs example shape", rec(registry="afritensor-decisions",
                                   uri="ipfs://QmABC123",
                                   checksum="abc123def456",
                                   metadata='{"document":"..."}',
                                   status="active")),
        ("isLatest=True", rec(latest=True)),
    ]

    print(f"\n{env} via {rpc}\nsender {sender}\n")
    for label, record in variants:
        try:
            c.functions.addRecord(record).call({"from": sender})
            print(f"  OK    {label}")
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).split("\n")[0][:150]
            print(f"  FAIL  {label}\n          {msg}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
