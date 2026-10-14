"""Google Chat incoming webhook with a persistent outbox, so messages survive network loss and restarts."""
from __future__ import annotations

import datetime as dt
import json
import logging
import urllib.request
from typing import Callable

from .store import Store

log = logging.getLogger(__name__)

# Messages sharing a threadKey are grouped into one thread; we use one thread per day.
THREAD_OPTION = "messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
MAX_AGE = dt.timedelta(hours=24)


def _http_post(url: str, payload: dict) -> None:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=UTF-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


class GoogleChat:
    def __init__(self, webhook_url: str, store: Store, post: Callable[[str, dict], None] = _http_post):
        self.webhook_url = webhook_url.strip()
        self.store = store
        self._post = post

    def send(self, text: str, thread_key: str | None, now: dt.datetime) -> None:
        log.info("Google Chat: %s", text.replace("\n", " | "))
        if not self.webhook_url:
            return
        payload = {"text": text}
        if thread_key:
            payload["thread"] = {"threadKey": thread_key}
        self.store.enqueue(payload, now)
        self.flush(now)

    def send_now(self, text: str) -> None:
        """Send directly, raising on failure; used to test the webhook."""
        self._post(self.webhook_url, {"text": text})

    def flush(self, now: dt.datetime) -> None:
        """Deliver queued messages in order; stop at the first failure and retry on the next call."""
        if not self.webhook_url:
            return
        for item_id, payload, created_at in self.store.outbox():
            if now - created_at > MAX_AGE:
                log.error("Dropping undelivered Google Chat message older than %s: %s", MAX_AGE, payload)
                self.store.outbox_done(item_id)
                continue
            url = self.webhook_url
            if "thread" in payload:
                url += ("&" if "?" in url else "?") + THREAD_OPTION
            try:
                self._post(url, payload)
            except Exception as exc:  # network errors, HTTP 4xx/5xx
                log.warning("Google Chat delivery failed, will retry: %s", exc)
                self.store.outbox_failed(item_id)
                return
            self.store.outbox_done(item_id)
