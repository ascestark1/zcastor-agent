#!/usr/bin/env python3
"""
Dump receipt logs and recover the numeric id a write returned.

Works for addRegistry and addRecord alike: both assign an id that appears only
in the receipt, and a client that cannot decode it cannot address what it just
created. addRecord's event signature is still unknown, which is why every
anchored record currently carries record_id 0 and every status update fails.

Run it against an addRecord transaction to see the raw event, then we can name
the topic the same way we named RegistryAdded.

The chain assigns each registry a numeric ID and returns it in the transaction
receipt, not in anything the sender can predict. Our transport did not decode
receipt logs, so successful creations came back with id=0 and roles could not be
granted.

Rather than guess at the event signature, this prints the raw logs first and
then applies a best-effort extraction. Run it, read what the chain actually
emitted, and if the extraction is wrong the raw output is right there.

    python3 ops/scripts/read_registry_ids.py <txhash> [<txhash> ...]

Read only. Sends nothing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zcastor.anchor.nvnm import ENVIRONMENTS  # noqa: E402

# Registry names in the order setup_registries.py creates them, so a run of
# four hashes maps onto the four registries positionally.
ORDER = ["policy", "decisions", "dossiers", "archive"]

KNOWN_TOPICS = {
    "181791bc379acedd3615cf065d3c275dfa6a3c4614c9065d54c98773f576108d":
        "RegistryAdded(creator, registryId, name)",
}


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    try:
        from web3 import Web3
    except ImportError:
        print("web3 is required: pip install web3")
        return 1

    env = os.environ.get("NVNM_ENV", "testnet")
    rpc = os.environ.get("NVNM_RPC_URL") or ENVIRONMENTS[env]["rpc_url"]
    w3 = Web3(Web3.HTTPProvider(rpc))
    print(f"\n{env} via {rpc}\n")

    found: dict[str, int] = {}

    for position, raw in enumerate(sys.argv[1:]):
        tx_hash = raw if raw.startswith("0x") else "0x" + raw
        try:
            receipt = w3.eth.get_transaction_receipt(tx_hash)
        except Exception as exc:  # noqa: BLE001
            print(f"  {tx_hash[:18]}...  could not fetch: {exc}")
            continue

        status = int(receipt.get("status", 0))
        logs = receipt.get("logs", [])
        print(f"  {tx_hash[:18]}...  status={status}  logs={len(logs)}")

        candidate = None
        for log in logs:
            topics = [t.hex() if hasattr(t, "hex") else str(t)
                      for t in log.get("topics", [])]
            data = log.get("data")
            data = data.hex() if hasattr(data, "hex") else str(data or "")
            bare = topics[0].replace("0x", "").lower() if topics else ""
            label = KNOWN_TOPICS.get(bare, "UNKNOWN EVENT")
            print(f"      event:  {label}")
            print(f"      topic0: {bare}")
            for t in topics[1:]:
                print(f"      topic:  {t}")
            print(f"      data:   {data}")
            # Decode the data words so the id is readable without arithmetic.
            body = data[2:] if data.startswith("0x") else data
            for i in range(0, min(len(body), 64 * 4), 64):
                word = body[i:i + 64]
                if len(word) == 64:
                    print(f"        word{i // 64}: {int(word, 16)}")

            # Registry IDs are small integers. Look through indexed topics
            # first, then 32-byte data words, and take the first plausible one.
            #
            # Strip any 0x before slicing. Assuming the prefix was present when
            # it was not shifted every word by one byte and multiplied every id
            # by 256 — which produced plausible-looking numbers rather than an
            # error, so only the sequential pattern in the raw logs gave it away.
            body = data[2:] if data.startswith("0x") else data
            words = [t[2:] if t.startswith("0x") else t for t in topics[1:]]
            words += [body[i:i + 64] for i in range(0, len(body), 64)]
            for word in words:
                try:
                    value = int(word.replace("0x", ""), 16)
                except ValueError:
                    continue
                if 0 < value < 10_000_000:
                    candidate = value
                    break
            if candidate:
                break

        if candidate is None and status == 1:
            print("      no id found in logs — read it from the explorer")
        elif candidate:
            name = ORDER[position] if position < len(ORDER) else f"reg{position}"
            found[name] = candidate
            print(f"      -> registry id {candidate}  ({name})")
        print()

    if found:
        import json
        print("── add to config/runtime.json ──")
        print(json.dumps({"registry_ids": found}, indent=2))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
