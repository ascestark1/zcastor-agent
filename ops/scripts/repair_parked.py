#!/usr/bin/env python3
"""
Repair parked outbox entries that are missing a numeric registry_id.

Entries queued before the client knew that addRecord identifies registries by
ID carry only a name. They can never be sent as they stand, and requeuing them
just parks them again.

This maps each entry's registry NAME to the numeric id in config/runtime.json,
rewrites the entry, and requeues it. Entries whose name has no known id are
left parked and reported rather than guessed at.

    python3 ops/scripts/repair_parked.py --dry-run
    python3 ops/scripts/repair_parked.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from zcastor.anchor.outbox import Outbox  # noqa: E402


def registry_ids() -> dict[str, int]:
    path = ROOT / "config" / "runtime.json"
    if not path.exists():
        return {}
    ids = json.loads(path.read_text()).get("registry_ids", {})
    return {f"afritensor-{short}": int(value)
            for short, value in ids.items() if value}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    ids = registry_ids()
    if not ids:
        print("no registry_ids in config/runtime.json — nothing to map")
        return 1

    box = Outbox(ROOT / "data" / "anchor_outbox.jsonl")
    parked = box.parked
    print(f"\n  {len(parked)} parked entr(ies)")
    print(f"  known registries: "
          f"{', '.join(f'{k}={v}' for k, v in sorted(ids.items()))}\n")

    fixable, unfixable = [], []
    for entry in parked:
        if entry.registry_id:
            continue
        target = ids.get(entry.registry)
        (fixable if target else unfixable).append((entry, target))

    for entry, target in fixable:
        print(f"  fix  {entry.op:14} {entry.registry:22} -> id {target}")
    for entry, _ in unfixable:
        print(f"  SKIP {entry.op:14} {entry.registry:22} (no id known)")

    if args.dry_run:
        print("\n  DRY RUN — nothing changed\n")
        return 0

    for entry, target in fixable:
        entry.registry_id = int(target)

    requeued = box.requeue_parked()
    print(f"\n  repaired {len(fixable)}, requeued {requeued}")
    print("  The drain loop will pick them up within a cycle.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
