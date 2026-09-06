#!/usr/bin/env python3
"""
Find which function signatures a precompile actually implements.

The anchoring precompile rejects an unknown method id BEFORE decoding any
arguments, and says so:

    unknown method id: 2608560233

That makes it an oracle. Send a bare 4-byte selector with no arguments: if the
answer is "unknown method id" the signature is wrong; any other error means the
method exists and only the arguments were bad. Costs nothing, sends nothing.

Written after addRecord reverted with a signature taken from the documentation,
which turned out not to be deployed.

    python3 ops/scripts/find_selector.py addRecord
    python3 ops/scripts/find_selector.py            # everything in our ABI
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zcastor.anchor.nvnm import ANCHORING_PRECOMPILE, ENVIRONMENTS  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_abi import keccak256  # noqa: E402

RECORD_FIELDS_DOCS = "string,string,string,string,string,string,string,uint64,uint64,bool"

# The docs' struct is not deployed, and neither is any near variant of it, so
# the method NAME may differ too. Cosmos EVM precompiles are usually generated
# from the Msg service, and the Msg name does not always match the docs.
NAME_ALIASES = [
    "addRecord", "anchorRecord", "createRecord", "addDocument",
    "anchor", "addAnchor", "recordAdd", "submitRecord", "writeRecord",
]

ARG_LAYOUTS = [
    # struct forms
    f"({RECORD_FIELDS_DOCS})",
    f"({RECORD_FIELDS_DOCS.replace('uint64', 'uint256')})",
    "(uint64,string,string,string,string,string,string,uint64,uint64,bool)",
    "(uint64,string,string,string,string,string)",
    "(string,string,string,string,string,string)",
    "(string,string,string,string,string)",
    # flat forms, registry by id
    "uint64,string,string,string,string,string",
    "uint64,string,string,string,string",
    "uint64,string,string,string,string,string,string",
    "uint256,string,string,string,string,string",
    # flat forms, registry by name
    "string,string,string,string,string,string",
    "string,string,string,string,string",
    "string,string,string,string,string,string,string",
]


def generated() -> list:
    """Every name crossed with every plausible argument layout."""
    return [f"{name}({args})" for name in NAME_ALIASES for args in ARG_LAYOUTS]


CANDIDATES = {
    "addRecord": [
        f"addRecord(({RECORD_FIELDS_DOCS}))",
        f"addRecord(({RECORD_FIELDS_DOCS.replace('uint64', 'uint256')}))",
        "addRecord((string,string,string,string,string,string,uint64,uint64,bool))",
        "addRecord((string,string,string,string,string,string,string))",
        "addRecord((string,string,string,string,string,string,string,uint64,uint64,bool,string))",
        "addRecord((uint64,string,string,string,string,string,string,uint64,uint64,bool))",
        "addRecord(string,string,string,string,string,string)",
        "addRecord(string,string,string,string,string,string,string)",
        "addRecord(uint64,string,string,string,string,string)",
        "addRecord(uint64,(string,string,string,string,string,string,string,uint64,uint64,bool))",
    ],
    "addRegistry": [
        "addRegistry(string,string)",
        "addRegistry(string,string,string)",
    ],
    "grantRole": [
        "grantRole(uint64,string,address,string)",
        "grantRole(uint64,address,string)",
        "grantRole(uint256,string,address,string)",
    ],
    "updateRecordStatus": [
        "updateRecordStatus(uint64,uint64,string,uint64,string)",
        "updateRecordStatus(uint64,uint64,string)",
        "updateRecordStatus(uint256,uint256,string,uint256,string)",
    ],
}


def selector(sig: str) -> str:
    return keccak256(sig.encode()).hex()[:8]


def main() -> int:
    try:
        from web3 import Web3
    except ImportError:
        print("web3 required")
        return 1

    env = os.environ.get("NVNM_ENV", "testnet")
    rpc = os.environ.get("NVNM_RPC_URL") or ENVIRONMENTS[env]["rpc_url"]
    sender = os.environ.get("ZCASTOR_AGENT_EXECUTION")
    w3 = Web3(Web3.HTTPProvider(rpc))

    if sys.argv[1:] == ["--wide"]:
        wanted, extra = ["addRecord"], generated()
    else:
        wanted, extra = sys.argv[1:] or list(CANDIDATES), []
    print(f"\n{env} via {rpc}\n")

    hits = []
    for name in wanted:
        print(f"── {name} ──")
        for sig in (extra or CANDIDATES.get(name, [])):
            sel = selector(sig)
            tx = {"to": w3.to_checksum_address(ANCHORING_PRECOMPILE),
                  "data": "0x" + sel}
            if sender:
                tx["from"] = sender
            try:
                w3.eth.call(tx)
                verdict = "EXISTS (call returned)"
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if "unknown method id" in msg:
                    verdict = "not implemented"
                else:
                    short = msg.split("desc =")[-1].strip()[:70]
                    verdict = f"EXISTS -> {short}"
            if "not implemented" not in verdict:
                hits.append((sel, sig))
            # In wide mode only report hits; hundreds of misses is noise.
            if extra:
                if "not implemented" not in verdict:
                    print(f"  {sel}  {verdict:46}  {sig}")
            else:
                print(f"  {sel}  {verdict:46}  {sig}")
        print()

    if extra:
        print(f"  probed {len(extra)} signatures, {len(hits)} implemented")
        if not hits:
            print("\n  None matched. The deployed signature is something we have")
            print("  not guessed. Ask Inveniam for the precompile ABI JSON —")
            print("  it is a thirty-second answer and ends the search.\n")
            return 2

    print("  A signature that is 'not implemented' has the wrong argument")
    print("  types. Anything else exists and only the arguments were bad.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
