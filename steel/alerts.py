"""Alerts: the events an operator wants on a phone, pushed to Telegram, Discord or any webhook.

What alerts: every error-level trace line (the kill switch, promotion and demotion, an unhedged
arbitrage leg, a failed engine), a feed stall, a trusted BUY or SELL call, a paper fill, a live
mirror result (fills and refusals alike), and the vault's recovery key still pending. The
destinations are a KEY_SERVICES entry ("alerts": the panel on Venues saves them sealed), so
nothing here needs a restart. Delivery is batched: `send()` queues, the engine's loop drains
every few seconds into one message per destination. Each kind is throttled on its own clock so
a flapping signal cannot flood a chat; the error kinds never wait.
"""
from __future__ import annotations

import json
import logging
import platform
import time
from collections import deque

import httpx

log = logging.getLogger(__name__)

SERVICE = {"id": "alerts", "name": "Alerts", "file": "alerts.json", "kind": "json_multi", "venue": "alerts", "url": "https://core.telegram.org/bots#botfather",
           "note": "Optional. Any of: a Telegram bot token and chat id, a Discord webhook URL, or a plain webhook URL "
                   "(JSON POST). Fills, trusted calls, live orders, refusals, stalls, demotions and the kill switch are sent.",
           "fields": [{"id": "telegram_token", "label": "Telegram bot token"}, {"id": "telegram_chat", "label": "Telegram chat id"},
                      {"id": "discord_webhook", "label": "Discord webhook URL"}, {"id": "webhook_url", "label": "Webhook URL (JSON POST)"}]}

# seconds between two alerts of one kind for one key (0 = never throttled)
THROTTLE = {"error": 0.0, "kill": 0.0, "live": 0.0, "demoted": 0.0, "unhedged": 0.0, "refused": 300.0,
            "fill": 0.0, "signal": 1800.0, "stall": 3600.0, "venue": 1800.0, "vault": 86400.0, "warn": 600.0, "info": 600.0}
FIELDS = [f["id"] for f in SERVICE["fields"]]


class Alerts:
    def __init__(self, values: dict | None = None, *, host: str = ""):
        self.telegram_token = self.telegram_chat = self.discord_webhook = self.webhook_url = ""
        self.configure(values or {})
        self.host = host or platform.node()
        self.queue: deque[dict] = deque(maxlen=500)
        self.last: dict[tuple[str, str], float] = {}
        self.sent = 0
        self.failed = 0
        self.last_error = ""
        self.last_sent = 0.0
        self.dropped = 0

    # Config -------------------------------------------------------------------------
    def configure(self, values: dict) -> None:
        for k in FIELDS:
            v = str(values.get(k) or "").strip()
            if v:
                setattr(self, k, v)

    def values(self) -> dict:
        return {k: getattr(self, k, "") for k in FIELDS}

    @property
    def configured(self) -> bool:
        return bool((self.telegram_token and self.telegram_chat) or self.discord_webhook or self.webhook_url)

    # Queue --------------------------------------------------------------------------
    def send(self, kind: str, text: str, *, key: str = "", level: str = "info", now: float | None = None) -> bool:
        """Queue one alert. False when nothing is configured or the kind is throttled for the key."""
        if not self.configured:
            return False
        now = time.time() if now is None else now
        gap = THROTTLE.get(kind, 600.0)
        k = (kind, key)
        if gap and now - self.last.get(k, -1e12) < gap:
            self.dropped += 1
            return False
        self.last[k] = now
        self.queue.append({"kind": kind, "text": text, "level": level, "t": now, "key": key})
        return True

    def on_trace(self, ev: dict) -> None:
        """The trace hook: every error-level line is an alert."""
        if ev.get("lvl") == "error":
            self.send("error", f"[{ev.get('ch')}] {ev.get('text')}", key=str(ev.get("text", ""))[:40], level="error", now=ev.get("t"))

    # Delivery -----------------------------------------------------------------------
    async def flush(self) -> int:
        """Deliver everything queued, one message per destination. Returns the number of alerts delivered."""
        if not self.queue:
            return 0
        batch = []
        while self.queue:
            batch.append(self.queue.popleft())
        head = f"Steel @ {self.host}"
        lines = [f"{'!! ' if a['level'] == 'error' else ''}{a['text']}" for a in batch]
        text = head + "\n" + "\n".join(lines)
        ok = True
        async with httpx.AsyncClient(timeout=10.0) as c:
            if self.telegram_token and self.telegram_chat:
                ok &= await self._post(c, f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
                                       {"chat_id": self.telegram_chat, "text": text[:4000], "disable_web_page_preview": True})
            if self.discord_webhook:
                ok &= await self._post(c, self.discord_webhook, {"content": text[:1990]})
            if self.webhook_url:
                ok &= await self._post(c, self.webhook_url, {"source": "steel", "host": self.host, "alerts": batch})
        if ok:
            self.sent += len(batch)
            self.last_sent = time.time()
        else:
            self.failed += len(batch)
        return len(batch) if ok else 0

    async def _post(self, client: httpx.AsyncClient, url: str, body: dict) -> bool:
        try:
            r = await client.post(url, json=body)
            if r.status_code >= 300:
                self.last_error = f"{url.split('/')[2]}: HTTP {r.status_code}"
                return False
            return True
        except (httpx.HTTPError, OSError) as e:
            self.last_error = f"{url.split('/')[2]}: {type(e).__name__}"
            return False

    def status(self) -> dict:
        return {"configured": self.configured, "channels": [n for n, on in (("telegram", bool(self.telegram_token and self.telegram_chat)),
                                                                            ("discord", bool(self.discord_webhook)), ("webhook", bool(self.webhook_url))) if on],
                "sent": self.sent, "failed": self.failed, "dropped": self.dropped, "queued": len(self.queue), "last_sent": self.last_sent,
                "last_error": self.last_error}


def _json(d: dict) -> str:
    return json.dumps(d, sort_keys=True)
