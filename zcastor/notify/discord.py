"""
Discord notifications.

Deliberately quiet. The system refuses most of what it sees, so posting every
decision would produce hundreds of messages a day and you would stop reading
them by the second afternoon. A notifier nobody reads is worse than none,
because it creates the impression of oversight without providing it.

So: fills, closes, and things that need a human. Refusals go to the decision log
and the daily record, which is where they belong.

Never blocks and never raises. A webhook outage must not delay an order or take
down the process. Failures are logged once and dropped.

The URL comes from DISCORD_WEBHOOK_URL in the environment. v1 kept it in
config.json, which was committed, which is why that webhook had to be
regenerated.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from typing import Any, Optional

logger = logging.getLogger("zcastor.notify")

TIMEOUT_SECONDS = 5


class Discord:
    def __init__(self, webhook_url: Optional[str] = None, *,
                 enabled: bool = True) -> None:
        # None means "look at the environment". An empty string means the
        # caller explicitly wants this off, and must not be quietly overridden
        # by whatever happens to be exported in the shell — which is exactly
        # what `webhook_url or os.environ.get(...)` did.
        if webhook_url is None:
            self.url = os.environ.get("DISCORD_WEBHOOK_URL", "")
        else:
            self.url = webhook_url
        self.enabled = bool(enabled and self.url)
        if enabled and not self.url:
            logger.info("no DISCORD_WEBHOOK_URL set — notifications disabled")

    # ── transport ─────────────────────────────────────────────────────────────

    def _post(self, content: str) -> None:
        if not self.enabled:
            return
        payload = json.dumps({"content": content[:1900]}).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=payload,
            headers={
                "Content-Type": "application/json",
                # Discord's edge rejects the default Python-urllib agent with
                # a 403 before the request ever reaches the webhook. The URL
                # can be perfectly valid and still fail without this.
                "User-Agent": "Zcastor/2.0 (+https://afritensor.com)",
            })
        try:
            urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS).close()
        except urllib.error.HTTPError as exc:
            # Read the body: Discord explains itself, and the status alone
            # cannot distinguish a deleted webhook from a blocked client.
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                detail = ""
            logger.warning("discord post failed: HTTP %s %s %s",
                           exc.code, exc.reason, detail)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            logger.warning("discord post failed: %s", exc)

    def send(self, content: str) -> None:
        """Fire and forget on a background thread. Never delays the caller."""
        if not self.enabled:
            return
        threading.Thread(target=self._post, args=(content,), daemon=True).start()

    # ── events ────────────────────────────────────────────────────────────────

    def filled(self, *, signal_id: str, side: str, price: float, sl: float,
               tp: Optional[float], route: str = "market",
               timeframe: str = "", session: str = "") -> None:
        target = f"{tp:,.2f}" if tp else "none"
        self.send(
            f"**FILL** `{side}` {timeframe} {session}\n"
            f"entry `{price:,.2f}`  stop `{sl:,.2f}`  target `{target}`\n"
            f"route `{route}`  id `{signal_id}`"
        )

    def closed(self, trade: dict) -> None:
        pnl = float(trade.get("pnl", 0.0))
        mark = "🟢" if pnl > 0 else "🔴"
        self.send(
            f"{mark} **CLOSED** `{trade.get('direction', '')}` "
            f"{trade.get('timeframe', '')} {trade.get('session', '')}\n"
            f"pnl `{pnl:+.2f}`  entry `{float(trade.get('entry_price', 0)):,.2f}` "
            f"exit `{float(trade.get('close_price', 0)):,.2f}`\n"
            f"route `{trade.get('route', '')}`  id `{trade.get('signal_id', '')}`"
        )

    def armed(self, *, signal_id: str, direction: str, zone: float,
              stop: float, route_kind: str) -> None:
        self.send(
            f"**ARMED** `{direction}` at `{zone:,.2f}`  "
            f"stop `{stop:,.2f}`  ({route_kind})  id `{signal_id}`"
        )

    def halted(self, reason: str, detail: Any = "") -> None:
        """Something a human needs to see. Dead-man halt, feed failure, parked anchor."""
        self.send(f"⚠️ **ATTENTION** `{reason}`\n{detail}")

    def session_summary(self, *, day: str, totals: dict, reasons: list,
                        journal: dict) -> None:
        top = "\n".join(f"  `{r['reason']}` × {r['count']}" for r in reasons[:5])
        self.send(
            f"**SESSION {day}**\n"
            f"decisions `{sum(totals.values())}`  "
            f"traded `{totals.get('allow', 0)}`  "
            f"armed `{totals.get('route', 0)}`  "
            f"refused `{totals.get('suppress', 0)}`\n"
            f"closed `{journal.get('closed', 0)}`  "
            f"net `{journal.get('net_pnl', 0):+.2f}`  "
            f"win rate `{journal.get('win_rate', 0):.0%}`\n"
            f"top refusals:\n{top or '  none'}"
        )
