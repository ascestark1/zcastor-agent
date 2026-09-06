"""
Publishing and rollover tests.

The one that matters most is test_auditor_reproduces_the_checksum_with_sha256sum:
it does what a counterparty does — takes the published file, hashes it with plain
sha256, and compares to the anchored value. If that ever fails, every dossier we
have ever anchored is unverifiable.
"""

import hashlib
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from zcastor.anchor.base import NullBackend  # noqa: E402
from zcastor.anchor.outbox import Outbox  # noqa: E402
from zcastor.gates.base import Gate, allow, route, suppress  # noqa: E402
from zcastor.gates.base import GateContext  # noqa: E402
from zcastor.gates.pipeline import GatePipeline  # noqa: E402
from zcastor.record import dgml, proof  # noqa: E402
from zcastor.record.decision_log import DecisionLog  # noqa: E402
from zcastor.record.publisher import Publisher  # noqa: E402
from zcastor.record.rollover import SessionRollover  # noqa: E402

URI = "https://raw.example.test/{path}"


def gate(name, decision):
    return type(f"G_{name}", (Gate,),
                {"name": name, "evaluate": lambda self, c: decision})()


def trace_for(gates, signal_id="sig_1"):
    ctx = GateContext(signal={}, origin_hash="sha256:o", policy={})
    return GatePipeline(gates).run(ctx, signal_id, "sha256:p")


def dossier(signal_id="sig_1"):
    return dgml.build_dossier(
        signal_id=signal_id,
        emitted_at="2026-08-27T09:00:00Z",
        agent="execution",
        proofs=proof.ProofSet(origin="sha256:o", process="sha256:p",
                              state="sha256:s"),
        decision=dgml.decision_from_trace(trace_for([gate("a", allow())])),
        thesis={"direction": "UP", "target": 63200.0, "stop": 62660.0},
        execution={"ticket": 1001, "price": 62860.0},
        policy_version="2.0.0-dev",
    )


def publisher(tmp):
    return Publisher(Path(tmp) / "dossiers", uri_template=URI)


# ── publishing ────────────────────────────────────────────────────────────────

def test_dossier_path_contains_its_checksum():
    """
    A path without the checksum is mutable, and a mutable path breaks
    verification indistinguishably from tampering.
    """
    with tempfile.TemporaryDirectory() as d:
        pub = publisher(d)
        out = pub.publish_dossier(dossier(), day="2026-08-27")
        short = out["checksum"].replace("sha256:", "")[:8]
        assert out["path"] == f"trades/2026-08-27/sig_1.{short}.json"
        assert out["uri"] == URI.format(path=out["path"])
        assert (Path(d) / "dossiers" / out["path"]).exists()


def test_republishing_changed_content_never_overwrites():
    with tempfile.TemporaryDirectory() as d:
        pub = publisher(d)
        first = pub.publish_dossier(dossier(), day="2026-08-27")
        changed = dossier()
        changed["thesis"]["direction"] = "DOWN"
        second = pub.publish_dossier(changed, day="2026-08-27")

        assert first["path"] != second["path"]
        assert (Path(d) / "dossiers" / first["path"]).exists()
        assert pub.verify(first["path"], first["checksum"])
        assert pub.verify(second["path"], second["checksum"])


def test_auditor_reproduces_the_checksum_with_sha256sum():
    """
    The whole audit path in four lines: fetch the file, hash it, compare.
    No knowledge of our serialiser, no re-canonicalisation.
    """
    with tempfile.TemporaryDirectory() as d:
        pub = publisher(d)
        out = pub.publish_dossier(dossier(), day="2026-08-27")
        raw = (Path(d) / "dossiers" / out["path"]).read_bytes()
        assert out["checksum"] == "sha256:" + hashlib.sha256(raw).hexdigest()


def test_published_dossier_omits_provenance():
    """
    It must. Provenance is written after anchoring, so a file containing it
    would stop matching its own checksum the moment the transaction confirmed.
    """
    with tempfile.TemporaryDirectory() as d:
        pub = publisher(d)
        out = pub.publish_dossier(dossier(), day="2026-08-27")
        written = json.loads((Path(d) / "dossiers" / out["path"]).read_text())
        assert "provenance" not in written
        assert written["identity"]["signal_id"] == "sig_1"


def test_checksum_matches_the_anchor_layer_exactly():
    with tempfile.TemporaryDirectory() as d:
        doc = dossier()
        out = publisher(d).publish_dossier(doc, day="2026-08-27")
        assert out["checksum"] == dgml.dossier_checksum(doc)


def test_tampering_with_a_published_dossier_is_detected():
    with tempfile.TemporaryDirectory() as d:
        pub = publisher(d)
        out = pub.publish_dossier(dossier(), day="2026-08-27")
        path = Path(d) / "dossiers" / out["path"]
        path.write_bytes(path.read_bytes().replace(b"62860", b"62000"))
        assert not pub.verify(out["path"], out["checksum"])


def test_policy_is_published_under_its_own_hash():
    with tempfile.TemporaryDirectory() as d:
        pub = publisher(d)
        out = pub.publish_policy({"min_risk_reward": 1.5}, "sha256:0ea50bc8ff2c4255")
        assert out["path"].startswith("policy/sha256-")
        again = pub.publish_policy({"min_risk_reward": 1.5}, "sha256:0ea50bc8ff2c4255")
        assert again.get("existing") == "true", "republishing must be a no-op"


def test_stats_count_what_was_published():
    with tempfile.TemporaryDirectory() as d:
        pub = publisher(d)
        pub.publish_dossier(dossier("sig_1"), day="2026-08-27")
        pub.publish_dossier(dossier("sig_2"), day="2026-08-28")
        assert pub.stats()["trades"] == 2


# ── rollover ──────────────────────────────────────────────────────────────────

class Clock:
    def __init__(self, dt=None):
        self.dt = dt or datetime(2026, 8, 27, 22, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.dt

    def advance(self, **kw):
        self.dt += timedelta(**kw)


def rollover(tmp, clock=None):
    pub = publisher(tmp)
    box = Outbox(Path(tmp) / "outbox.jsonl")
    return SessionRollover(publisher=pub, outbox=box,
                           policy_version="2.0.0-dev",
                           process_hash="sha256:policy",
                           clock=clock or Clock()), pub, box


def add(roll, reason=None, signal_id="sig_x"):
    gates = [gate("g", suppress(reason))] if reason else [gate("g", allow())]
    roll.log.add(signal_id=signal_id, at="2026-08-27T22:00:00Z",
                 trace=trace_for(gates, signal_id),
                 origin="sha256:o", process="sha256:p", state="sha256:s")


def test_rollover_does_nothing_before_the_boundary():
    with tempfile.TemporaryDirectory() as d:
        roll, _, _ = rollover(d)
        add(roll, "session_blocked")
        assert roll.tick() is None
        assert len(roll.log.entries) == 1


def test_rollover_publishes_and_anchors_at_the_boundary():
    with tempfile.TemporaryDirectory() as d:
        clock = Clock()
        roll, pub, box = rollover(d, clock)
        add(roll, "session_blocked", "sig_1")
        add(roll, "london_sell_bias", "sig_2")
        add(roll, None, "sig_3")

        clock.advance(hours=3)          # past UTC midnight
        result = roll.tick()

        assert result["day"] == "2026-08-27"
        assert result["decisions"] == 3
        assert result["totals"] == {"allow": 1, "suppress": 2}
        short = result["checksum"].replace("sha256:", "")[:8]
        assert pub.verify(f"decisions/2026-08-27.{short}.json",
                          result["checksum"])
        assert len(box.pending) == 1
        assert box.pending[0].registry == "afritensor-decisions"


def test_a_new_log_starts_after_rollover():
    with tempfile.TemporaryDirectory() as d:
        clock = Clock()
        roll, _, _ = rollover(d, clock)
        add(roll, "session_blocked")
        clock.advance(hours=3)
        roll.tick()
        assert roll.day == "2026-08-28"
        assert roll.log.entries == []


def test_an_empty_day_still_publishes():
    """A missing file is ambiguous between 'no signals' and 'system was down'."""
    with tempfile.TemporaryDirectory() as d:
        clock = Clock()
        roll, pub, _ = rollover(d, clock)
        clock.advance(hours=3)
        result = roll.tick()
        assert result["decisions"] == 0
        short = result["checksum"].replace("sha256:", "")[:8]
        assert pub.verify(f"decisions/2026-08-27.{short}.json",
                          result["checksum"])


def test_the_anchored_log_drains_offline():
    with tempfile.TemporaryDirectory() as d:
        clock = Clock()
        roll, _, box = rollover(d, clock)
        add(roll, "session_blocked")
        clock.advance(hours=3)
        roll.tick()
        assert box.drain(NullBackend())["sent"] == 1


def test_reanchoring_the_same_day_is_a_no_op():
    """Crash between publish and enqueue must not produce two records."""
    with tempfile.TemporaryDirectory() as d:
        clock = Clock()
        roll, _, box = rollover(d, clock)
        add(roll, "session_blocked")
        clock.advance(hours=3)
        first = roll.tick()

        duplicate = box.enqueue(op="add_record", registry="afritensor-decisions",
                                checksum=first["checksum"])
        assert duplicate is None
        assert len(box.pending) == 1


def test_close_on_shutdown_preserves_the_day():
    with tempfile.TemporaryDirectory() as d:
        roll, pub, _ = rollover(d)
        add(roll, "session_blocked")
        result = roll.close(start_new=False)
        assert result["decisions"] == 1
        short = result["checksum"].replace("sha256:", "")[:8]
        assert pub.verify(f"decisions/2026-08-27.{short}.json",
                          result["checksum"])


def test_reasons_are_ranked_in_the_published_log():
    with tempfile.TemporaryDirectory() as d:
        clock = Clock()
        roll, pub, _ = rollover(d, clock)
        for i in range(3):
            add(roll, "session_blocked", f"sig_a{i}")
        add(roll, "london_sell_bias", "sig_b")
        clock.advance(hours=3)
        roll.tick()

        latest = pub.latest("decisions", "2026-08-27")
        published = json.loads(
            (Path(d) / "dossiers" / latest["path"]).read_text())
        assert [r["reason"] for r in published["suppression_reasons"]] == \
               ["session_blocked", "london_sell_bias"]
        assert published["suppression_reasons"][0]["count"] == 3


def test_routed_decisions_appear_in_the_log():
    with tempfile.TemporaryDirectory() as d:
        clock = Clock()
        roll, _, _ = rollover(d, clock)
        roll.log.add(signal_id="sig_r", at="t",
                     trace=trace_for([gate("zone", route("exhaustion"))], "sig_r"),
                     origin="o", process="p", state="s")
        clock.advance(hours=3)
        assert roll.tick()["totals"] == {"route": 1}


def test_manifest_tracks_every_version_of_a_session():
    with tempfile.TemporaryDirectory() as d:
        clock = Clock()
        roll, pub, _ = rollover(d, clock)
        add(roll, "session_blocked", "sig_1")
        first = roll.close(start_new=False)
        add(roll, "london_sell_bias", "sig_2")
        second = roll.close(start_new=False)

        entries = pub.manifest()["decisions"]["2026-08-27"]
        assert [e["checksum"] for e in entries] == [first["checksum"],
                                                    second["checksum"]]
        assert pub.latest("decisions", "2026-08-27")["checksum"] == \
               second["checksum"]


def test_republish_reports_what_it_supersedes():
    with tempfile.TemporaryDirectory() as d:
        clock = Clock()
        roll, _, _ = rollover(d, clock)
        add(roll, "session_blocked", "sig_1")
        first = roll.close(start_new=False)
        add(roll, "london_sell_bias", "sig_2")
        second = roll.close(start_new=False)
        assert second["supersedes"] == first["checksum"]


def test_identical_content_supersedes_nothing():
    """Publishing the same bytes twice is a no-op, not a new version."""
    with tempfile.TemporaryDirectory() as d:
        clock = Clock()
        roll, pub, _ = rollover(d, clock)
        add(roll, "session_blocked", "sig_1")
        roll.close(start_new=False)
        again = roll.close(start_new=False)
        assert again["supersedes"] is None
        assert len(pub.manifest()["decisions"]["2026-08-27"]) == 1


def test_the_earlier_record_is_marked_superseded_on_chain():
    from zcastor.anchor.base import NullBackend
    from zcastor.anchor.index import AnchorIndex
    from zcastor.record.rollover import SessionRollover

    with tempfile.TemporaryDirectory() as d:
        clock = Clock()
        pub = publisher(d)
        box = Outbox(Path(d) / "outbox.jsonl")
        idx = AnchorIndex(Path(d) / "index.jsonl")
        box.on_anchored = lambda e, r: idx.record_anchored(
            signal_id="", registry=e.registry, registry_id=e.registry_id,
            record_id=r.record_id, checksum=e.checksum,
            transaction=r.transaction, status=e.status, uri=e.uri,
            kind="decision_log")

        roll = SessionRollover(publisher=pub, outbox=box, anchor_index=idx,
                               registry_id=4297, clock=clock)
        add(roll, "session_blocked", "sig_1")
        first = roll.close(start_new=False)
        box.drain(NullBackend())
        assert idx.get_by_checksum(first["checksum"])["status"] == "Active"

        add(roll, "london_sell_bias", "sig_2")
        roll.close(start_new=False)
        assert idx.get_by_checksum(first["checksum"])["status"] == "Superseded"
        assert any(e.op == "update_status" for e in box.pending)


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
                passed += 1
            except Exception as e:
                print(f"  FAIL  {name}: {type(e).__name__}: {e}")
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
