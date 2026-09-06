#!/usr/bin/env python3
"""
Remove records that were "anchored" against the null backend.

During development the outbox drained through `NullBackend`, which accepts
everything and returns a fake transaction. Those entries are in the anchor index
and count toward the totals in /health, where they are indistinguishable from
records that actually landed on chain.

That matters more than it sounds. The index is the basis of every disclosure
bundle: a phantom entry would tell a counterparty that a document is anchored,
with a transaction hash, when nothing exists on chain. An audit trail that
begins with six fictions is worse than one that begins empty.

This rewrites the index keeping only entries with a real transaction hash, and
clears matching dedupe keys from the outbox so genuine records can be re-queued.

    python3 ops/scripts/purge_null_records.py --dry-run
    python3 ops/scripts/purge_null_records.py
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"


def is_phantom(row: dict) -> bool:
    tx = str(row.get("transaction", ""))
    return tx.startswith("null:") or tx in ("", "0xdryrun")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    index_path = DATA / "anchor_index.jsonl"
    outbox_path = DATA / "anchor_outbox.jsonl"

    if not index_path.exists():
        print("no anchor index — nothing to purge")
        return 0

    rows = []
    for line in index_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    # Replay so the surviving state matches what the index would load.
    state: dict[str, dict] = {}
    for row in rows:
        sid = row.get("signal_id")
        if sid:
            state.setdefault(sid, {}).update(row)

    phantom = {k: v for k, v in state.items() if is_phantom(v)}
    real = {k: v for k, v in state.items() if not is_phantom(v)}

    print(f"\n  index entries : {len(state)}")
    print(f"  real (on chain): {len(real)}")
    print(f"  phantom (null) : {len(phantom)}\n")
    for sid, row in list(phantom.items())[:10]:
        print(f"    {sid[:28]:30} tx={row.get('transaction', '')[:24]}")

    if not phantom:
        print("  nothing to remove\n")
        return 0

    if args.dry_run:
        print("\n  DRY RUN — nothing changed\n")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    shutil.copyfile(index_path, index_path.with_suffix(f".{stamp}.bak"))
    if outbox_path.exists():
        shutil.copyfile(outbox_path, outbox_path.with_suffix(f".{stamp}.bak"))

    with index_path.open("w", encoding="utf-8") as fh:
        for row in real.values():
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    # Drop completion markers for phantom checksums so those documents can be
    # anchored for real. Pending work is left untouched.
    if outbox_path.exists():
        checksums = {str(v.get("checksum", "")) for v in phantom.values()}
        kept = []
        for line in outbox_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            dedupe = str(obj.get("dedupe", ""))
            if obj.get("_") == "sent" and any(c and c in dedupe
                                              for c in checksums):
                continue
            kept.append(line)
        outbox_path.write_text("\n".join(kept) + ("\n" if kept else ""))

    print(f"\n  removed {len(phantom)} phantom record(s)")
    print(f"  backups written alongside with suffix .{stamp}.bak\n")
    print("  Those documents can now be re-anchored once addRecord works.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
