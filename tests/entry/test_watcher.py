"""
Entry watcher tests.

The pair that matters most are test_fresh_route_cancels_when_the_level_breaks
and test_exhaustion_route_survives_the_sweep. They are the same price sequence
with different route kinds, and they must behave oppositely: breaking the level
kills a continuation entry and is the whole setup for a flush-reversal one.
Getting them backwards means buying falling knives.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from zcastor.entry.pending import PendingEntry, expiry_minutes  # noqa: E402
from zcastor.entry.watcher import EntryWatcher  # noqa: E402
from zcastor.entry.zones import ZoneDerivation  # noqa: E402

POLICY = json.loads((ROOT / "config" / "policy.json").read_text())
W = POLICY["watcher"]


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class FakeMarket:
    def __init__(self, ask=62860.0, bid=62840.0, balance=1000.0, tick=True):
        self.ask, self.bid, self._balance, self._tick = ask, bid, balance, tick

    def get_tick(self):
        if not self._tick:
            return {"error": "no connection"}
        return {"ask": self.ask, "bid": self.bid, "price": (self.ask + self.bid) / 2,
                "spread_pts": round(self.ask - self.bid), "time": 1}

    def move(self, price):
        half = (self.ask - self.bid) / 2
        self.ask, self.bid = price + half, price - half

    def get_balance(self):
        return self._balance

    def get_free_margin(self):
        return (self._balance or 0) * 0.9


class FakeOrders:
    volume = 0.05

    def __init__(self, fail=False):
        self.placed = []
        self._fail = fail

    def market_order(self, *, side, price, sl, tp=None):
        if self._fail:
            return {"error": "rejected"}
        self.placed.append({"side": side, "price": price, "sl": sl, "tp": tp})
        return {"ticket": 5001, "side": side, "price": price, "sl": sl, "tp": tp}


def pending(direction="up", zone=62500.0, route="fresh", clock=None):
    return PendingEntry.create(
        policy=POLICY, clock=clock or Clock(),
        signal_id="sig_1", direction=direction, zone_price=zone,
        stop_price=zone - W["stop_buffer_pts"] if direction == "up"
        else zone + W["stop_buffer_pts"],
        timeframe="15m", session="London", route_kind=route,
    )


# ── geometry ──────────────────────────────────────────────────────────────────

def test_buy_confirms_above_and_invalidates_below():
    e = pending("up", 62500.0)
    assert e.confirm_price == 62500.0 + W["confirm_pts"]
    assert e.invalidate_price == 62500.0 - W["invalidate_pts"]
    assert e.stop_pts == W["stop_buffer_pts"]


def test_sell_mirrors_the_geometry():
    e = pending("down", 63000.0)
    assert e.confirm_price == 63000.0 - W["confirm_pts"]
    assert e.invalidate_price == 63000.0 + W["invalidate_pts"]


def test_expiry_is_per_timeframe():
    table = W["expiry_min_by_tf"]
    assert expiry_minutes(POLICY, "5m") == table["5m"]
    assert expiry_minutes(POLICY, "4h") == table["4h"]
    assert expiry_minutes(POLICY, "nonsense") == W["expiry_min_default"]


# ── fresh route ───────────────────────────────────────────────────────────────

def test_fresh_route_waits_until_price_reaches_the_zone():
    e = pending("up", 62500.0)
    assert e.evaluate(62900.0) == "wait"
    assert not e.reached_zone


def test_fresh_route_fills_when_the_level_holds():
    e = pending("up", 62500.0)
    assert e.evaluate(62510.0) == "wait"      # touched
    assert e.reached_zone
    assert e.evaluate(62545.0) == "fill"      # reclaimed by confirm_pts


def test_fresh_route_cancels_when_the_level_breaks():
    """Support that breaks is not support."""
    e = pending("up", 62500.0)
    e.evaluate(62510.0)
    assert e.evaluate(62430.0) == "cancel"


def test_fresh_sell_fills_on_rejection():
    e = pending("down", 63000.0)
    e.evaluate(62990.0)
    assert e.evaluate(62950.0) == "fill"


def test_fresh_sell_cancels_on_breakout():
    e = pending("down", 63000.0)
    e.evaluate(62990.0)
    assert e.evaluate(63070.0) == "cancel"


# ── exhaustion route ──────────────────────────────────────────────────────────

def test_exhaustion_route_needs_a_sweep_before_filling():
    e = pending("up", 62500.0, route="exhaustion")
    assert e.evaluate(62545.0) == "wait", "no sweep yet — this is not the setup"
    assert not e.swept


def test_exhaustion_route_survives_the_sweep():
    """
    The same break that kills a fresh entry IS the exhaustion setup. Price
    flushes through the level, traps the last sellers, and reclaims.
    """
    e = pending("up", 62500.0, route="exhaustion")
    assert e.evaluate(62450.0) == "wait"      # swept — a fresh entry would cancel
    assert e.swept
    assert e.evaluate(62545.0) == "fill"      # reclaimed


def test_exhaustion_route_cancels_only_on_a_real_breakdown():
    e = pending("up", 62500.0, route="exhaustion")
    e.evaluate(62450.0)
    assert e.evaluate(62400.0) == "wait"      # still just a deep sweep
    assert e.evaluate(62370.0) == "cancel"    # collapsed well past


def test_exhaustion_sell_mirrors():
    e = pending("down", 63000.0, route="exhaustion")
    assert e.evaluate(63050.0) == "wait" and e.swept
    assert e.evaluate(62955.0) == "fill"


# ── expiry ────────────────────────────────────────────────────────────────────

def test_entry_expires_on_its_timeframe_window():
    clock = Clock()
    e = pending(clock=clock)
    assert not e.expired(clock.t)
    clock.advance((W["expiry_min_by_tf"]["15m"] + 1) * 60)
    assert e.expired(clock.t)


# ── zone derivation ───────────────────────────────────────────────────────────

def zones():
    return ZoneDerivation(POLICY)


def test_absolute_level_price_is_preferred():
    """v16.1's addition — a distance measured at emission is already stale."""
    out = zones().derive(
        {"direction": "UP", "nearest_support_price": 62500.0,
         "nearest_support_pts": 999}, current_price=62860.0)
    assert out["zone_price"] == 62500.0
    assert out["source"] == "level_price"


def test_falls_back_to_distance_when_v16_sends_no_price():
    out = zones().derive({"direction": "UP", "nearest_support_pts": 200},
                         current_price=62860.0)
    assert out["zone_price"] == 62660.0
    assert out["source"] == "level_pts"


def test_no_level_means_no_arming():
    assert zones().derive({"direction": "UP"}, current_price=62860.0) is None


def test_zone_too_close_is_refused():
    out = zones().derive({"direction": "UP", "nearest_support_price": 62800.0},
                         current_price=62860.0)
    assert out is None


def test_zone_too_far_is_refused():
    out = zones().derive({"direction": "UP", "nearest_support_price": 61000.0},
                         current_price=62860.0)
    assert out is None


def test_support_above_price_is_a_mislabelled_level():
    out = zones().derive({"direction": "UP", "nearest_support_price": 63500.0},
                         current_price=62860.0)
    assert out is None


def test_stop_sits_beyond_the_level():
    out = zones().derive({"direction": "UP", "nearest_support_price": 62500.0},
                         current_price=62860.0)
    assert out["stop_price"] == 62500.0 - W["stop_buffer_pts"]


# ── watcher ───────────────────────────────────────────────────────────────────

def watcher(market=None, orders=None, clock=None, now=None, **kw):
    return EntryWatcher(policy=POLICY, market=market or FakeMarket(),
                        orders=orders or FakeOrders(),
                        clock=clock or Clock(),
                        now=now or (lambda: datetime(2026, 8, 27, 12, 0,
                                                     tzinfo=timezone.utc)),
                        **kw)


def arm(w, zone=62500.0, route="fresh", signal_id="sig_1"):
    return w.arm(signal_id=signal_id,
                 signal={"direction": "UP", "timeframe": "15m",
                         "session": "London", "nearest_support_price": zone},
                 route_kind=route,
                 derived={"direction": "UP", "execution_price": 62860.0,
                          "mid": 62850.0})


def test_arming_reports_the_structural_stop():
    w = watcher()
    out = arm(w)
    assert out["armed"] and out["stop_pts"] == W["stop_buffer_pts"]
    assert w.pending_count() == 1


def test_arming_without_a_level_is_refused_honestly():
    w = watcher()
    out = w.arm(signal_id="sig_x", signal={"direction": "UP"},
                route_kind="fresh",
                derived={"direction": "UP", "execution_price": 62860.0})
    assert out["error"] == "no usable level"
    assert w.pending_count() == 0


def test_double_arming_is_refused():
    w = watcher()
    arm(w)
    assert arm(w)["error"] == "already armed"


def test_poll_fills_when_the_level_holds():
    market, orders = FakeMarket(), FakeOrders()
    w = watcher(market, orders)
    arm(w)

    market.move(62505.0)
    assert w.poll()["filled"] == 0          # touched only
    market.move(62560.0)
    assert w.poll()["filled"] == 1

    assert orders.placed[0]["sl"] == 62500.0 - W["stop_buffer_pts"]
    assert w.pending_count() == 0


def test_poll_cancels_when_the_level_breaks():
    market, orders = FakeMarket(), FakeOrders()
    w = watcher(market, orders)
    arm(w)
    market.move(62505.0)
    w.poll()
    market.move(62420.0)
    assert w.poll()["cancelled"] == 1
    assert orders.placed == []


def test_fill_is_refused_when_the_structural_stop_is_too_expensive():
    """
    The reason the market-quality gates skip routed signals: they judged a
    market stop, and this is a different, wider number.
    """
    market = FakeMarket(balance=15.0)       # stop * 0.05 well above the cap
    orders = FakeOrders()
    w = watcher(market, orders)
    arm(w)
    market.move(62505.0)
    w.poll()
    market.move(62560.0)
    assert w.poll()["filled"] == 0
    assert orders.placed == []


def test_order_failure_removes_the_entry():
    market, orders = FakeMarket(), FakeOrders(fail=True)
    w = watcher(market, orders)
    arm(w)
    market.move(62505.0)
    w.poll()
    market.move(62560.0)
    w.poll()
    assert w.pending_count() == 0


def test_expired_entries_are_swept_on_poll():
    clock = Clock()
    w = watcher(clock=clock)
    arm(w)
    clock.advance((W["expiry_min_by_tf"]["15m"] + 1) * 60)
    assert w.poll()["expired"] == 1


def test_max_hold_sweeps_an_entry():
    clock = Clock()
    w = watcher(clock=clock)
    arm(w)
    clock.advance(25 * 3600)
    w.poll()
    assert w.pending_count() == 0


def test_day_boundary_closes_carried_entries():
    """An entry armed yesterday reasons about a market that no longer exists."""
    day = {"now": datetime(2026, 8, 27, 23, 30, tzinfo=timezone.utc)}
    w = watcher(now=lambda: day["now"])
    arm(w)
    day["now"] = datetime(2026, 8, 28, 0, 10, tzinfo=timezone.utc)
    w.poll()
    assert w.pending_count() == 0


def test_a_dead_feed_holds_entries_rather_than_cancelling_them():
    market = FakeMarket(tick=False)
    w = watcher(market)
    arm(w)
    result = w.poll()
    assert result.get("stalled") == 1
    assert w.pending_count() == 1


def test_fill_hook_receives_the_entry_and_the_order():
    seen = []
    market, orders = FakeMarket(), FakeOrders()
    w = watcher(market, orders, on_fill=lambda e, r: seen.append((e, r)))
    arm(w)
    market.move(62505.0)
    w.poll()
    market.move(62560.0)
    w.poll()
    assert seen[0][0].signal_id == "sig_1"
    assert seen[0][1]["ticket"] == 5001


def test_a_broken_fill_hook_does_not_lose_the_trade():
    def boom(entry, result):
        raise RuntimeError("record failed")

    market, orders = FakeMarket(), FakeOrders()
    w = watcher(market, orders, on_fill=boom)
    arm(w)
    market.move(62505.0)
    w.poll()
    market.move(62560.0)
    w.poll()
    assert len(orders.placed) == 1, "the order stands even if recording fails"


def test_snapshot_describes_pending_entries():
    w = watcher()
    arm(w)
    snap = w.snapshot()[0]
    assert snap["zone_price"] == 62500.0
    assert snap["route_kind"] == "fresh"
    assert snap["stop_pts"] == W["stop_buffer_pts"]


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
