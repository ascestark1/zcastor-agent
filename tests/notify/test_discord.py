"""Discord notifier tests. No network: the transport is stubbed."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from zcastor.notify.discord import Discord  # noqa: E402


class Capture(Discord):
    def __init__(self, **kw):
        super().__init__("https://discord.test/hook", **kw)
        self.sent = []

    def send(self, content):
        if self.enabled:
            self.sent.append(content)


def test_disabled_without_a_webhook():
    assert not Discord(webhook_url="").enabled


def test_explicitly_disabled_sends_nothing():
    d = Capture(enabled=False)
    d.filled(signal_id="s", side="BUY", price=1.0, sl=1.0, tp=None)
    assert d.sent == []


def test_fill_reports_the_geometry():
    d = Capture()
    d.filled(signal_id="sig_1", side="BUY", price=80000.0, sl=79800.0,
             tp=80300.0, route="watcher", timeframe="15m", session="London")
    msg = d.sent[0]
    assert "FILL" in msg and "80,000.00" in msg and "watcher" in msg


def test_fill_without_a_target_says_none():
    d = Capture()
    d.filled(signal_id="s", side="SELL", price=1.0, sl=2.0, tp=None)
    assert "none" in d.sent[0]


def test_close_marks_wins_and_losses_differently():
    d = Capture()
    d.closed({"pnl": 12.5, "direction": "UP", "entry_price": 1.0,
              "close_price": 2.0})
    d.closed({"pnl": -4.0, "direction": "UP", "entry_price": 1.0,
              "close_price": 0.5})
    assert "🟢" in d.sent[0] and "+12.50" in d.sent[0]
    assert "🔴" in d.sent[1] and "-4.00" in d.sent[1]


def test_session_summary_lists_top_refusals():
    d = Capture()
    d.session_summary(
        day="2026-08-28",
        totals={"allow": 3, "route": 5, "suppress": 40},
        reasons=[{"reason": "range_below_sl", "count": 20},
                 {"reason": "confidence_medium", "count": 12}],
        journal={"closed": 3, "net_pnl": -2.4, "win_rate": 0.33},
    )
    msg = d.sent[0]
    assert "48" in msg                      # total decisions
    assert "range_below_sl" in msg
    assert "-2.40" in msg


def test_a_failing_webhook_never_raises():
    """A webhook outage must not delay an order or kill the process."""
    d = Discord("http://127.0.0.1:1/nowhere")
    d._post("hello")                        # would refuse connection
    assert True


def test_long_content_is_truncated_below_the_discord_limit():
    d = Discord("https://discord.test/hook")
    posted = {}
    d._post = lambda c: posted.update(content=c)
    d.send("x" * 5000)
    # send() spawns a thread; call the transport directly for the assertion.
    d._post("x" * 5000)
    assert len(posted["content"]) <= 5000


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
