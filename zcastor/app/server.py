"""
The server. Wiring, and nothing else.

Every decision this file makes is about which objects to construct and in what
order — no trading logic, no thresholds, no reasoning. If a rule is being applied
here, it is in the wrong place.

Two ordering constraints that matter:

**The policy is hashed once at startup**, from the file's bytes, and that hash
goes on every decision made by this process. Rehashing per signal would let a
mid-session edit change the recorded policy without a restart, which is exactly
the kind of silent drift the record exists to rule out. Changing policy means
restarting, and the restart is the event.

**Anchoring never blocks a signal.** The outbox drains on its own timer. The
`/signal` handler touches disk once — an append — and returns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from ..anchor.base import NullBackend
from ..anchor.index import AnchorIndex
from ..anchor.outbox import Outbox
from ..data.journal import Journal
from ..data.ledger import EdgeLedger
from ..execution.booking import Booker, ClosePoller
from ..execution.engine import Engine
from ..entry.watcher import EntryWatcher
from ..execution.orders import OrderPlacer
from ..gates.coherence import ModelCoherenceGate, SignalCoherenceGate
from ..gates.confidence import ConfidenceGate
from ..gates.cooldown import EntryCooldownGate
from ..gates.deadman import DeadmanGate
from ..gates.direction import DirectionGate
from ..gates.exhaustion import SessionExhaustionGate
from ..gates.feed import FeedGate
from ..gates.ledger import EdgeLedgerGate
from ..gates.london import LondonBiasGate
from ..gates.pipeline import GatePipeline
from ..gates.range import RangeAdequacyGate, RiskRewardGate
from ..gates.regime import RegimeContextGate
from ..gates.session import SessionGate
from ..gates.spread import SpreadHardGate, SpreadViabilityGate
from ..gates.stack import StackLimitGate
from ..gates.targets import TargetStage
from ..gates.viability import ViabilityGate
from ..gates.zone import FlipConfirmGate, ZoneGate
from ..market.mt5 import MarketData, connect
from ..market.levels import HourlyLevels
from ..market.session import SessionOpens, current_session
from ..notify.discord import Discord
from ..record.canonical import checksum_bytes
from ..record.proof import process_hash
from ..record.publisher import Publisher
from ..record.rollover import SessionRollover
from ..resolve.resolver import OutcomeResolver
from ..risk.coherence import ModelCoherence, RiskAdapter, SignalCoherence
from ..risk.zones import ZoneTracker
from ..targets.optimizer import TargetOptimizer

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
LOGS = ROOT / "logs"

logger = logging.getLogger("zcastor")


def _environment() -> str:
    return os.environ.get("NVNM_ENV", "testnet")


def _anchor_backend():
    """
    NullBackend unless ANCHOR_BACKEND=nvnm is set explicitly.

    Opt-in, not opt-out. A misconfigured environment should leave records
    queued on disk, not send half-formed transactions to a live chain — and
    the outbox means nothing is lost while the backend is null.
    """
    if os.environ.get("ANCHOR_BACKEND", "null").lower() != "nvnm":
        return NullBackend()

    from ..anchor.nvnm import ENVIRONMENTS, build_backend   # noqa: PLC0415
    env = _environment()
    backend = build_backend(
        private_key=os.environ["NVNM_PRIVATE_KEY"],
        environment=env,
        rpc_url=os.environ.get("NVNM_RPC_URL", ""),
        chain_id=int(os.environ.get("NVNM_CHAIN_ID", 0) or 0),
        legacy_tx=os.environ.get("NVNM_LEGACY_TX", "").lower() == "true",
    )
    logger.info("anchoring to NVNM %s (chain %s, gas %s) as %s",
                env, ENVIRONMENTS.get(env, {}).get("chain_id"),
                ENVIRONMENTS.get(env, {}).get("gas_symbol"), backend.address)
    return backend


def _setup_logging() -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(LOGS / "zcastor.log")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger("zcastor")
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler())
    # uvicorn's access log buries gate decisions. In v1 this looked like the
    # gates had gone silent; they were being drowned by "POST /signal 200 OK".
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


class Zcastor:
    """Everything, assembled once."""

    def __init__(self) -> None:
        policy_path = ROOT / "config" / "policy.json"
        self.policy = json.loads(policy_path.read_text())
        self.policy_hash = checksum_bytes(policy_path.read_bytes())

        runtime_path = ROOT / "config" / "runtime.json"
        self.runtime = (json.loads(runtime_path.read_text())
                        if runtime_path.exists() else {})

        venue = os.environ.get("ZCASTOR_VENUE",
                               self.runtime.get("venue", "mt5")).lower()
        symbol = self.runtime.get("symbol", "BTCUSD")
        volume = float(self.runtime.get("volume", 0.01))

        self.journal = Journal(DATA / "trades.jsonl")
        sessions = SessionOpens(lambda f, t: self.market.get_rates_range(f, t))

        # One port, two venues. Nothing below this line knows which.
        if venue == "binance":
            from ..execution.binance_orders import BinanceOrderPlacer
            from ..market.binance import (MAINNET, TESTNET, BinanceClient,
                                          BinanceMarketData)
            testnet = os.environ.get("BINANCE_TESTNET", "true").lower() != "false"
            symbol = self.runtime.get("binance_symbol", "BTCUSDT")
            volume = float(self.runtime.get("binance_volume", 0.001))
            client = BinanceClient(base_url=TESTNET if testnet else MAINNET)
            self.market = BinanceMarketData(client, symbol=symbol,
                                            volume=volume, journal=self.journal,
                                            session_opens=sessions)
            self.orders = BinanceOrderPlacer(client, symbol=symbol,
                                             volume=volume)
            logger.info("venue: Binance %s, %s",
                        "testnet" if testnet else "LIVE", symbol)
        else:
            mt5 = connect()
            self.market = MarketData(mt5, symbol=symbol, volume=volume,
                                     session_opens=sessions)
            self.orders = OrderPlacer(mt5, symbol=symbol, volume=volume,
                                      magic=int(self.runtime.get("magic", 777001)))
            logger.info("venue: MT5, %s", symbol)
        self.ledger = EdgeLedger(self.policy, DATA / "edge_ledger.jsonl",
                                 policy_hash=self.policy_hash)
        self.zones = ZoneTracker(self.policy)
        self.risk = RiskAdapter(
            zones=self.zones,
            signal_coherence=SignalCoherence(self.policy),
            model_coherence=ModelCoherence(self.policy,
                                           DATA / "signal_outcomes.json"),
            journal=self.journal,
        )
        self.anchor_index = AnchorIndex(DATA / "anchor_index.jsonl")
        self.outbox = Outbox(DATA / "anchor_outbox.jsonl")
        self.backend = _anchor_backend()

        # The dossier repo. Separate from the code repo: this one is public,
        # the code repo holds credentials and must not be.
        self.publisher = Publisher(
            Path(self.runtime.get("dossier_root", ROOT.parent / "zcastor-dossiers")),
            uri_template=self.runtime.get("dossier_uri_template", ""),
        )
        self.code_version = os.environ.get("ZCASTOR_COMMIT", "unknown")
        self.process_hash = process_hash(self.policy, self.code_version)

        # Publish the policy before anything decides anything. A decision
        # referencing a policy hash must always resolve to a retrievable policy.
        self.publisher.publish_policy(self.policy, self.policy_hash)

        registry_ids = self.runtime.get("registry_ids", {})
        self.rollover = SessionRollover(
            publisher=self.publisher,
            outbox=self.outbox,
            anchor_index=self.anchor_index,
            policy_version=str(self.policy.get("policy_version", "")),
            process_hash=self.process_hash,
            # addRecord identifies the registry by numeric id. Without this the
            # rollover queues records that can never be sent.
            registry_id=int(registry_ids.get("decisions", 0)),
        )
        if not registry_ids.get("decisions"):
            logger.warning("no decisions registry id in runtime.json — "
                           "decision logs will queue but cannot anchor")

        self.resolver = OutcomeResolver(
            policy=self.policy,
            market=self.market,
            pending_path=DATA / "pending_signals.json",
            outcomes_path=DATA / "signal_outcomes.json",
        )

        self.discord = Discord()

        levels_cfg = dict(self.policy.get("watcher", {}).get("hourly_levels", {}))
        self.levels = HourlyLevels(
            lambda f, t: self.market.get_rates_range(f, t),
            lookback_hours=int(levels_cfg.get("lookback_hours", 168)),
            k=int(levels_cfg.get("swing_k", 3)),
            refresh_seconds=float(levels_cfg.get("refresh_seconds", 900)),
        ) if levels_cfg.get("enabled", True) else None

        # Held on the instance so the engine can report fills to it.
        self.cooldown = EntryCooldownGate()

        self.booker = Booker(market=self.market, journal=self.journal,
                             ledger=self.ledger,
                             on_closed=self.discord.closed,
                             policy_hash=self.policy_hash)
        self.poller = ClosePoller(market=self.market, journal=self.journal,
                                  booker=self.booker)

        self.engine = Engine(
            pipeline=GatePipeline(self._gates()),
            market=self.market,
            orders=self.orders,
            risk=self.risk,
            targets=TargetOptimizer(self.policy),
            ledger=self.ledger,
            journal=self.journal,
            positions=_Positions(self.market),
            policy=self.policy,
            policy_hash=self.policy_hash,
            decision_log=self.rollover.log,
            outbox=self.outbox,
            watcher=None,                  # set immediately below
            publisher=self.publisher,
            resolver=self.resolver,
            anchor_index=self.anchor_index,
            code_version=self.code_version,
            registries={"dossiers": "afritensor-dossiers"},
            registry_ids=self.runtime.get("registry_ids", {}),
            environment=_environment(),
        )

        # The watcher needs the engine's fill handler and the engine needs the
        # watcher, so one of them is wired after construction. The watcher is
        # the leaf, so it goes second.
        self.watcher = EntryWatcher(
            policy=self.policy,
            market=self.market,
            orders=self.orders,
            on_fill=self._on_watcher_fill,
            levels=self.levels,
        )
        self.engine.watcher = self.watcher

        # Two callbacks that close the record lifecycle: remember what landed,
        # then move it to resolved once price has spoken.
        self.outbox.on_anchored = self.engine.on_anchored
        self.engine.on_fill_notify = self._notify_fill
        self.resolver.on_resolved = self.engine.on_outcome_resolved

    def _on_watcher_fill(self, entry, result) -> None:
        self.engine.on_watcher_fill(entry, result)
        self.discord.filled(
            signal_id=entry.signal_id, side=result["side"],
            price=result["price"], sl=result["sl"], tp=result.get("tp"),
            route="watcher", timeframe=entry.timeframe, session=entry.session,
        )

    def _notify_fill(self, **kw) -> None:
        # Start the cooldown from an actual fill, never from a decision — a
        # refused signal must not block the next one.
        self.cooldown.record_fill(kw.get("price", 0), kw.get("side", ""))
        self.discord.filled(**kw)

    def _gates(self) -> list:
        return [
            DirectionGate(), ConfidenceGate(), FeedGate(), SpreadHardGate(),
            DeadmanGate(), StackLimitGate(), self.cooldown,
            ZoneGate(), FlipConfirmGate(), SignalCoherenceGate(),
            ModelCoherenceGate(), SessionGate(), RegimeContextGate(),
            LondonBiasGate(),
            TargetStage(),
            SpreadViabilityGate(), RangeAdequacyGate(), RiskRewardGate(),
            SessionExhaustionGate(),
            EdgeLedgerGate(enforce=bool(self.policy.get("ledger_enforce"))),
            ViabilityGate(),
        ]


class _Positions:
    """The `ctx.positions` gates hold — a count, nothing more."""

    def __init__(self, market) -> None:
        self._market = market

    def live(self) -> list:
        return self._market.get_open_positions()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _setup_logging()
    app.state.z = Zcastor()
    logger.info("zcastor v2 up — policy %s", app.state.z.policy_hash[:19])

    tasks = [asyncio.create_task(_poll_closes(app.state.z)),
             asyncio.create_task(_drain_outbox(app.state.z)),
             asyncio.create_task(_roll_sessions(app.state.z)),
             asyncio.create_task(_poll_watcher(app.state.z)),
             asyncio.create_task(_resolve_outcomes(app.state.z))]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        # Publish the partial day on the way out. A restart mid-session must
        # not strand the decisions made before it.
        try:
            app.state.z.rollover.close(start_new=False)
            app.state.z.outbox.drain(app.state.z.backend)
        except Exception:  # noqa: BLE001
            logger.exception("shutdown rollover failed")


async def _poll_closes(z: Zcastor, interval: int = 20) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(z.poller.poll)
        except Exception:  # noqa: BLE001
            logger.exception("close poller cycle failed")


async def _drain_outbox(z: Zcastor, interval: int = 30) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(z.outbox.drain, z.backend)
            z.outbox.maybe_compact()
        except Exception:  # noqa: BLE001
            logger.exception("outbox drain cycle failed")


async def _poll_watcher(z: Zcastor) -> None:
    """
    The tightest loop in the system. An armed entry is waiting for a price it
    may only touch briefly, so a slow poll means a missed fill.
    """
    interval = float(z.policy.get("watcher", {}).get("poll_seconds", 5))
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(z.watcher.poll)
        except Exception:  # noqa: BLE001
            logger.exception("watcher cycle failed")


async def _resolve_outcomes(z: Zcastor) -> None:
    interval = float(z.policy.get("resolver", {}).get("poll_seconds", 60))
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(z.resolver.tick)
        except Exception:  # noqa: BLE001
            logger.exception("outcome resolver cycle failed")


async def _roll_sessions(z: Zcastor, interval: int = 60) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            result = await asyncio.to_thread(z.rollover.tick)
            if result:
                # The engine holds a reference to the old log; repoint it.
                z.engine.decision_log = z.rollover.log
                logger.info("rolled into %s", z.rollover.day)
                z.discord.session_summary(
                    day=result["day"], totals=result["totals"],
                    reasons=z.rollover.log.ranked_reasons(),
                    journal=z.journal.stats())
        except Exception:  # noqa: BLE001
            logger.exception("session rollover failed")


app = FastAPI(title="Zcastor v2", lifespan=lifespan)

# The dashboard runs in a browser on a different port, so a JSON POST triggers
# a preflight the API has to answer or the signal never arrives. Origins are
# restricted to localhost: this binds to 127.0.0.1 and is not a public API, so
# there is no reason to accept a signal from an arbitrary website.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(https?://)?(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.post("/signal")
async def signal(request: Request):
    payload = await request.json()
    z: Zcastor = request.app.state.z
    trace = await asyncio.to_thread(z.engine.process, payload)
    return {
        "signal_id": trace.signal_id,
        "action": trace.action.value,
        "reason": trace.reason,
        "route_kind": trace.route_kind or None,
    }


@app.get("/health")
async def health(request: Request):
    z: Zcastor = request.app.state.z
    return {
        "ok": True,
        "policy": z.policy_hash,
        "session": current_session(),
        "balance": z.market.get_balance(),
        "open_trades": len(z.journal.open_tickets()),
        "armed_entries": z.watcher.pending_count(),
        "awaiting_measurement": z.resolver.pending_count(),
        "outbox": z.outbox.stats(),
        "anchor": z.backend.name,
        "environment": _environment(),
        "venue": os.environ.get("ZCASTOR_VENUE", "mt5"),
        "records": z.anchor_index.stats(),
        "levels": z.levels.stats() if z.levels else None,
    }


@app.get("/decisions")
async def decisions(request: Request):
    z: Zcastor = request.app.state.z
    return {"day": z.rollover.day,
            "totals": z.rollover.log.summary(),
            "reasons": z.rollover.log.ranked_reasons(),
            "count": len(z.rollover.log.entries)}


@app.post("/rollover")
async def force_rollover(request: Request):
    """Publish and anchor the current session now. Operator action, for testing
    the record path without waiting for UTC midnight."""
    z: Zcastor = request.app.state.z
    result = await asyncio.to_thread(z.rollover.close, start_new=True)
    z.engine.decision_log = z.rollover.log
    return result


@app.get("/pending")
async def pending(request: Request):
    z: Zcastor = request.app.state.z
    return {"count": z.watcher.pending_count(), "entries": z.watcher.snapshot()}


@app.get("/published")
async def published(request: Request):
    return request.app.state.z.publisher.stats()


@app.get("/outcomes")
async def outcomes(request: Request):
    return request.app.state.z.resolver.stats()


@app.get("/edge")
async def edge(request: Request):
    z: Zcastor = request.app.state.z
    return {"stats": z.ledger.stats(), "segments": z.ledger.snapshot()}


@app.get("/journal")
async def journal(request: Request):
    return request.app.state.z.journal.stats()
