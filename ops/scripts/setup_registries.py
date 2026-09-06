#!/usr/bin/env python3
"""
One-time NVNM bootstrap: create the registries, grant the agent roles.

Run once, deliberately, by a human. Not from the engine — creating a registry is
an act of governance, not a step in a trade.

    export NVNM_ENV=testnet              # testnet | mainnet
    export NVNM_PRIVATE_KEY=...          # operator key, never committed
    export ZCASTOR_AGENT_EXECUTION=0x...  # one address per agent
    python3 ops/scripts/setup_registries.py --dry-run
    python3 ops/scripts/setup_registries.py

--dry-run prints every transaction without sending one. Use it first: this
script is the only thing in the project that changes chain state permanently,
and a typo in a registry name is not something you can take back.

The chain assigns each registry a numeric ID. CAPTURE THOSE IDS and put them in
config/runtime.json under "registry_ids" — names are neither unique nor
queryable on chain, so the ID is the only safe reference.

BEFORE RUNNING THIS FOR REAL:
  1. Confirm the registry names with Inveniam. There may be conventions.
  2. Confirm whether KYA expects one credential per system or one per agent.
     This assumes four agents; it may need to collapse to one.
  3. Rotate the operator key if it has ever left the machine.

Gas differs by environment: testnet charges in NVNM, mainnet in mmUSD.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zcastor.anchor.base import AnchorRejected, AnchorUnavailable  # noqa: E402
from zcastor.anchor.nvnm import (  # noqa: E402
    AGENT_ROLES, REGISTRIES, NvnmBackend, Transport, build_backend,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

OK, BAD, DRY = "  ok  ", " FAIL ", " dry  "


class DryRunTransport(Transport):
    """
    Prints instead of sending, and hands back plausible sequential record ids
    so the role-granting path is genuinely exercised. A dry run that skipped
    roles because every id was zero would hide the bug it exists to catch.
    """
    address = "0xDRYRUN"

    def __init__(self) -> None:
        self.calls: list = []
        self._next_id = 900

    def call(self, function, args):
        self.calls.append((function, args))
        print(f"{DRY} {function}({', '.join(repr(a) for a in args)})")
        record_id = 0
        if function == "addRegistry":
            self._next_id += 1
            record_id = self._next_id
        return {"tx": "0xdryrun", "status": 1, "block": 0,
                "record_id": record_id}


def _ids_from_runtime() -> dict[str, int]:
    """
    Registry ids already recorded in config/runtime.json, keyed by full name.

    Lets a second run grant roles without recreating registries — which would
    otherwise produce duplicates, since the chain does not enforce unique names.
    """
    path = Path(__file__).resolve().parents[2] / "config" / "runtime.json"
    if not path.exists():
        return {}
    try:
        recorded = json.loads(path.read_text()).get("registry_ids", {})
    except (json.JSONDecodeError, OSError):
        return {}
    return {f"afritensor-{short}": int(value)
            for short, value in recorded.items() if value}


def agent_addresses() -> dict[str, str]:
    """One address per agent, from the environment. Missing agents are skipped."""
    found = {}
    for agent in AGENT_ROLES:
        value = os.environ.get(f"ZCASTOR_AGENT_{agent.upper()}")
        if value:
            found[agent] = value
    return found


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="print every transaction, send none")
    parser.add_argument("--skip-registries", action="store_true",
                        help="roles only, for when registries already exist")
    args = parser.parse_args()

    env = os.environ.get("NVNM_ENV", "testnet")

    if args.dry_run:
        backend = NvnmBackend(DryRunTransport())
    else:
        key = os.environ.get("NVNM_PRIVATE_KEY")
        if not key:
            print(f"{BAD} NVNM_PRIVATE_KEY must be set")
            return 1
        backend = build_backend(
            private_key=key, environment=env,
            rpc_url=os.environ.get("NVNM_RPC_URL", ""),
            chain_id=int(os.environ.get("NVNM_CHAIN_ID", 0) or 0),
            legacy_tx=os.environ.get("NVNM_LEGACY_TX", "").lower() == "true")

    agents = agent_addresses()
    created: dict[str, int] = {}
    print(f"\nenvironment: {env}")
    print(f"operator: {backend.address}")
    print(f"agents:   {', '.join(agents) or 'NONE FOUND'}\n")

    if not agents:
        print("No ZCASTOR_AGENT_* addresses set. Registries can still be created,")
        print("but nothing will be able to write to them.\n")

    failures = 0

    if not args.skip_registries:
        print("── registries ──")
        for name, description in REGISTRIES.items():
            try:
                result = backend.add_registry(name, description)  # metadata optional
                created[name] = result.record_id
                print(f"{OK} {name}  id={result.record_id}  {result.transaction}")
            except AnchorRejected as exc:
                # Already existing is fine and expected on a re-run.
                if "already exists" in str(exc).lower():
                    print(f"{OK} {name}  (already exists)")
                else:
                    print(f"{BAD} {name}: {exc}")
                    failures += 1
            except AnchorUnavailable as exc:
                print(f"{BAD} {name}: chain unavailable — {exc}")
                failures += 1

    # Roles are granted against numeric IDs, so they need the ids from this
    # run or, when registries already exist, from runtime.json. Granting
    # against a name silently targets nothing.
    ids = dict(created)
    ids.update(_ids_from_runtime())

    print("\n── roles ──")
    if not args.dry_run and not any(ids.values()):
        print(f"{BAD} no registry ids available — cannot grant roles.")
        print("      Read them from the explorer and add them to")
        print("      config/runtime.json under registry_ids, then re-run")
        print("      with --skip-registries.")
        return 1

    for agent, registries in AGENT_ROLES.items():
        address = agents.get(agent)
        if not address:
            print(f"{DRY} {agent}: no address set, skipped")
            continue
        for registry in registries:
            registry_id = int(ids.get(registry, 0))
            if not registry_id and not args.dry_run:
                print(f"{BAD} {agent} → {registry}: no numeric id known")
                failures += 1
                continue
            try:
                result = backend.grant_role(registry_id, address, "editor")
                print(f"{OK} {agent} → {registry} (id={registry_id})  "
                      f"{result.transaction}")
            except (AnchorRejected, AnchorUnavailable) as exc:
                print(f"{BAD} {agent} → {registry}: {exc}")
                failures += 1

    print(f"\n{'DRY RUN — nothing was sent' if args.dry_run else 'done'}"
          f"{'' if not failures else f' with {failures} failure(s)'}\n")

    if created:
        print("\n── record these in config/runtime.json ──")
        print(json.dumps({"registry_ids": {
            "policy": created.get("afritensor-policy", 0),
            "decisions": created.get("afritensor-decisions", 0),
            "dossiers": created.get("afritensor-dossiers", 0),
            "archive": created.get("afritensor-archive", 0),
        }}, indent=2))
        print("\nIf any id is 0, read it off the explorer before anchoring:")
        print("  https://explorer.evm.testnet.nvnmchain.io\n")

    if not args.dry_run and not failures:
        print("Next: set ANCHOR_BACKEND=nvnm and anchor the policy record")
        print("first. Verify it resolves before any trade flows.\n")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
