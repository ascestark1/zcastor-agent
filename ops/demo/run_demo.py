#!/usr/bin/env python3
"""
Zcastor demo: an agent decides, and the decision is recorded before the outcome.

Shows the whole loop end to end in one run:

    1. a signal arrives, carrying live Binance market state
    2. twenty gates evaluate it and MOST OF THEM REFUSE
    3. the decision is written as a document and hashed
    4. the hash is anchored on NVNM Chain
    5. anyone recomputes the hash and checks it, with nothing from us

The point is step 2. Every agent demo shows what the agent did. This one shows
what it declined, why, and under which policy — recorded before anyone knew
whether declining was right.

    python3 ops/demo/run_demo.py                  # offline, no venue needed
    python3 ops/demo/run_demo.py --market data.json   # live Agent OS snapshot

Places no orders. Reads nothing it is not given.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from zcastor.anchor.base import NullBackend  # noqa: E402
from zcastor.anchor.index import AnchorIndex  # noqa: E402
from zcastor.anchor.outbox import Outbox  # noqa: E402
from zcastor.data.journal import Journal  # noqa: E402
from zcastor.data.ledger import EdgeLedger  # noqa: E402
from zcastor.entry.watcher import EntryWatcher  # noqa: E402
from zcastor.execution.engine import Engine  # noqa: E402
from zcastor.gates.coherence import ModelCoherenceGate, SignalCoherenceGate  # noqa: E402
from zcastor.gates.confidence import ConfidenceGate  # noqa: E402
from zcastor.gates.cooldown import EntryCooldownGate  # noqa: E402
from zcastor.gates.deadman import DeadmanGate  # noqa: E402
from zcastor.gates.direction import DirectionGate  # noqa: E402
from zcastor.gates.exhaustion import SessionExhaustionGate  # noqa: E402
from zcastor.gates.feed import FeedGate  # noqa: E402
from zcastor.gates.ledger import EdgeLedgerGate  # noqa: E402
from zcastor.gates.london import LondonBiasGate  # noqa: E402
from zcastor.gates.pipeline import GatePipeline  # noqa: E402
from zcastor.gates.range import RangeAdequacyGate, RiskRewardGate  # noqa: E402
from zcastor.gates.regime import RegimeContextGate  # noqa: E402
from zcastor.gates.session import SessionGate  # noqa: E402
from zcastor.gates.spread import SpreadHardGate, SpreadViabilityGate  # noqa: E402
from zcastor.gates.stack import StackLimitGate  # noqa: E402
from zcastor.gates.targets import TargetStage  # noqa: E402
from zcastor.gates.viability import ViabilityGate  # noqa: E402
from zcastor.gates.zone import FlipConfirmGate, ZoneGate  # noqa: E402
from zcastor.market.session import current_session  # noqa: E402
from zcastor.record.canonical import checksum_bytes  # noqa: E402
from zcastor.record.decision_log import DecisionLog  # noqa: E402
from zcastor.record.proof import process_hash  # noqa: E402
from zcastor.record.publisher import Publisher  # noqa: E402
from zcastor.record.rollover import SessionRollover  # noqa: E402
from zcastor.risk.coherence import ModelCoherence, RiskAdapter, SignalCoherence  # noqa: E402
from zcastor.risk.zones import ZoneTracker  # noqa: E402
from zcastor.targets.optimizer import TargetOptimizer  # noqa: E402

BAR = "─" * 68


def say(text: str = "") -> None:
    print(text)


def heading(n: int, text: str) -> None:
    say(f"\n{BAR}\n  {n}. {text}\n{BAR}")


class DemoMarket:
    """A frozen snapshot of the venue, so a run is reproducible."""

    volume = 0.001

    def __init__(self, snapshot: dict) -> None:
        self.s = snapshot

    def begin(self):
        pass

    def get_tick(self):
        return {"price": (self.s["ask"] + self.s["bid"]) / 2,
                "ask": self.s["ask"], "bid": self.s["bid"],
                "spread_pts": round(self.s["ask"] - self.s["bid"], 2),
                "time": self.s.get("time", time.time())}

    def get_balance(self):
        return self.s.get("balance", 10_000.0)

    def get_free_margin(self):
        return self.s.get("balance", 10_000.0)

    def get_margin_for(self, d, v, p):
        return v * p

    def get_open_positions(self):
        return []

    def get_closed_deal(self, ticket):
        return None

    def session_open_price(self, session):
        return self.s.get("session_open", 0.0)

    def get_rates_range(self, a, b):
        return []

    def state(self, session=""):
        tick = self.get_tick()
        return {"tick_time": tick["time"], "ask": tick["ask"], "bid": tick["bid"],
                "spread_pts": tick["spread_pts"], "balance": self.get_balance(),
                "free_margin": self.get_free_margin(), "open_positions": 0,
                "session": session, "session_open": self.s.get("session_open")}


class NoOrders:
    volume = 0.001

    def __init__(self):
        self.placed = []

    def market_order(self, **kw):
        self.placed.append(kw)
        return {"ticket": 900_001, "side": kw["side"], "price": kw["price"],
                "sl": kw["sl"], "tp": kw.get("tp")}

    def close_position(self, ticket, side):
        return {"ticket": ticket, "close_price": 0.0}


def build(tmp: Path, policy: dict, market, uri_template: str,
          publish_root: Path = None):
    journal = Journal(tmp / "trades.jsonl")
    ledger = EdgeLedger(policy, tmp / "edge.jsonl")
    risk = RiskAdapter(zones=ZoneTracker(policy),
                       signal_coherence=SignalCoherence(policy),
                       model_coherence=ModelCoherence(policy, tmp / "out.json"),
                       journal=journal)
    # When publishing into a real repo, its root IS the dossier root — the
    # URL in the record has to match where the file actually lands.
    publisher = Publisher(publish_root or (tmp / "dossiers"),
                          uri_template=uri_template)
    outbox = Outbox(tmp / "outbox.jsonl")
    index = AnchorIndex(tmp / "index.jsonl")

    policy_path = ROOT / "config" / "policy.json"
    policy_hash = checksum_bytes(policy_path.read_bytes())

    gates = [DirectionGate(), ConfidenceGate(), FeedGate(), SpreadHardGate(),
             DeadmanGate(), StackLimitGate(), EntryCooldownGate(),
             ZoneGate(), FlipConfirmGate(), SignalCoherenceGate(),
             ModelCoherenceGate(), SessionGate(), RegimeContextGate(),
             LondonBiasGate(), TargetStage(), SpreadViabilityGate(),
             RangeAdequacyGate(), RiskRewardGate(), SessionExhaustionGate(),
             EdgeLedgerGate(enforce=False), ViabilityGate()]

    rollover = SessionRollover(
        publisher=publisher, outbox=outbox, anchor_index=index,
        policy_version=str(policy.get("policy_version", "")),
        process_hash=process_hash(policy, "demo"), registry_id=4297)

    engine = Engine(
        pipeline=GatePipeline(gates), market=market, orders=NoOrders(),
        risk=risk, targets=TargetOptimizer(policy), ledger=ledger,
        journal=journal, positions=type("P", (), {"live": lambda s: []})(),
        policy=policy, policy_hash=policy_hash, decision_log=rollover.log,
        outbox=outbox, publisher=publisher, anchor_index=index,
        code_version="demo", environment="testnet",
        registries={"dossiers": "afritensor-dossiers"},
        registry_ids={"dossiers": 4298})
    # A real watcher, so a routed signal arms at its level instead of
    # reporting that no watcher is running.
    engine.watcher = EntryWatcher(policy=policy, market=market,
                                  orders=engine.orders)
    outbox.on_anchored = engine.on_anchored
    return engine, rollover, publisher, outbox, index, len(gates)


def signals(market: dict) -> list[dict]:
    """
    Five signals a dashboard might emit in one session. Deliberately varied so
    the gate stack has something to disagree about — a demo where everything
    passes shows nothing.
    """
    mid = (market["ask"] + market["bid"]) / 2
    base = {"dashboard_version": "16.1", "session": "London",
            "regime": "trending", "tf_regime": "trending", "zone_phase": "fresh",
            "atr": 200, "block_confidence": "high"}
    # Sessions are set explicitly rather than taken from the clock, so the run
    # is reproducible and the stack has something to disagree about. A demo
    # where every signal fails the same gate demonstrates nothing.
    return [
        # clears everything — the one that trades
        {**base, "direction": "UP", "confidence": "high", "timeframe": "1h",
         "nearest_resistance_pts": 1400, "nearest_support_pts": 900},
        # the model was not confident enough
        {**base, "direction": "UP", "confidence": "medium", "timeframe": "15m",
         "nearest_resistance_pts": 900, "nearest_support_pts": 700},
        # a session measured at an 11% hit rate
        {**base, "direction": "DOWN", "confidence": "high", "timeframe": "15m",
         "session": "NY Close", "nearest_support_pts": 900,
         "nearest_resistance_pts": 800},
        # ranging 5m: sent to the watcher to enter at the level instead
        {**base, "direction": "UP", "confidence": "high", "timeframe": "5m",
         "regime": "ranging", "tf_regime": "ranging", "block_confidence": "low",
         "nearest_resistance_pts": 900, "nearest_support_pts": 800,
         "nearest_support_price": round(mid - 300, 2)},
        # less room to the level than the stop needs
        {**base, "direction": "DOWN", "confidence": "high", "timeframe": "1h",
         "nearest_support_pts": 120, "nearest_resistance_pts": 1500},
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", help="JSON snapshot from Agent OS")
    ap.add_argument("--uri", default="https://raw.githubusercontent.com/"
                                     "ascestark1/zcastor-dossiers/main/{path}")
    ap.add_argument("--publish-to", metavar="DIR",
                    help="also write the record into a real dossier repo, so "
                         "the printed URL actually resolves once pushed")
    args = ap.parse_args()

    if args.market:
        market = json.loads(Path(args.market).read_text())
    else:
        market = {"ask": 79627.39, "bid": 79627.38, "balance": 10_000.0,
                  "session": current_session(), "session_open": 79_100.0,
                  "source": "offline default"}

    policy = json.loads((ROOT / "config" / "policy.json").read_text())
    # Commission is a property of the venue, not of the decision policy, so it
    # is applied here rather than committed. Binance charges 0.1% per side;
    # a spread-only broker charges nothing and the default stays 0.
    if "commission_rate" in market:
        policy["commission_rate"] = float(market["commission_rate"])

    say(f"\n{BAR}")
    say("  ZCASTOR — decisions recorded before outcomes are known")
    say(f"{BAR}")

    heading(1, "Market state, read live through Binance Agent OS")
    say(f"     ask {market['ask']:,.2f}   bid {market['bid']:,.2f}   "
        f"spread {market['ask']-market['bid']:.2f}")
    say(f"     source: {market.get('source', 'Agent OS')}")
    commission = float(policy.get("commission_rate", 0)) * 2 * market["ask"]
    if commission:
        say(f"     commission {commission:,.2f}pts round trip — the venue's real")
        say(f"     cost is the fee, not the spread")

    with tempfile.TemporaryDirectory() as d:
        # A demo that prints a URL nobody can fetch is a demo of nothing, so
        # --publish-to writes the record where the URL says it lives.
        tmp = Path(d)
        engine, rollover, publisher, outbox, index, n_gates = build(
            tmp, policy, DemoMarket(market), args.uri,
            publish_root=Path(args.publish_to) if args.publish_to else None)

        heading(2, f"{n_gates} gates evaluate each signal")
        say(f"     policy {engine.policy_hash[:26]}…\n")

        traces = []
        for i, sig in enumerate(signals(market), 1):
            trace = engine.process(sig)
            traces.append(trace)
            binding = trace.binding()
            shadows = [r.name for r in trace.shadow_objections()]
            say(f"     {i}. {sig['direction']:4} {sig['timeframe']:3} "
                f"{sig['session']:9} → {trace.action.value.upper():8} "
                f"{trace.reason or '-'}")
            if trace.action.value == "route":
                armed = engine.watcher.snapshot()
                if armed:
                    a = armed[-1]
                    say(f"        armed at {a['zone_price']:,.2f}, "
                        f"stop {a['stop_pts']}pts, expires in {a['expiry_min']:.0f}min")
            if binding and trace.action.value == "suppress":
                say(f"        refused at {binding.name}"
                    + (f", also objected: {', '.join(shadows)}" if shadows else ""))

        counts: dict[str, int] = {}
        for t in traces:
            counts[t.action.value] = counts.get(t.action.value, 0) + 1
        summary = "   ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        say(f"\n     {summary}")
        say("     Most signals are refused. That is the normal case, and it is")
        say("     the part no performance report ever shows.")

        heading(3, "The session's decisions become a document, and a hash")
        result = rollover.close(start_new=False)
        say(f"     decisions  {result['decisions']}")
        say(f"     totals     {result['totals']}")
        say(f"     checksum   {result['checksum']}")
        say(f"     published  {result['uri'] or '(no uri configured)'}")

        heading(4, "The hash is anchored on NVNM Chain")
        stats = outbox.drain(NullBackend())
        say(f"     queued {stats['sent']} record(s) → registry afritensor-decisions")
        say("     (NullBackend here so the demo is reproducible; the live")
        say("      engine anchors to NVNM testnet, chain 787111)")

        heading(5, "Anyone can verify it, with nothing from us")
        path = publisher.latest("decisions", rollover.day)["path"]
        raw = (publisher.root / path).read_bytes()
        say(f"     $ sha256sum {Path(path).name}")
        say(f"     {checksum_bytes(raw)[7:]}")
        say(f"\n     anchored:  {result['checksum'][7:]}")
        say(f"     match:     {checksum_bytes(raw) == result['checksum']}")

        if args.publish_to:
            say(f"\n     written to {publisher.root / path}")
            say("     commit and push it, and the URL above resolves for anyone")

        say(f"\n{BAR}")
        say("  The trades are a by-product. The record is the product.")
        say(f"{BAR}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
