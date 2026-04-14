"""
Push alerts
============
Best-effort webhook notifier for bot-critical events.

- Transport: Telegram (default) or Discord.
- Configure via environment:
    ALERT_WEBHOOK_URL   — full webhook URL (required to enable alerts)
    ALERT_TRANSPORT     — "telegram" (default) or "discord"
    TELEGRAM_CHAT_ID    — required for telegram transport
- Every call is rate-limited: the same (level, title) key only fires
  once per ``DEDUPE_WINDOW_SECONDS`` seconds to avoid spamming during
  a flapping WS.
- All network failures are swallowed — alerts must never crash the
  trading loop. Failures are logged at WARNING only.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

try:
    import requests  # type: ignore
except Exception:  # pragma: no cover — requests is in requirements.txt
    requests = None  # type: ignore

logger = logging.getLogger(__name__)

DEDUPE_WINDOW_SECONDS = 300  # 5 minutes per key

# Internal dedupe state — key -> timestamp of last successful send.
_last_sent: dict[str, float] = {}
_lock = threading.Lock()

# Level emojis for readability in Telegram / Discord
_LEVEL_PREFIX = {
    "INFO": "ℹ️",
    "GOOD": "✅",
    "WARNING": "⚠️",
    "CRITICAL": "🚨",
}


def _dedupe_key(level: str, title: str) -> str:
    return f"{level}::{title}"


def _should_send(key: str, now: float) -> bool:
    """True if we haven't sent this key within the dedupe window."""
    with _lock:
        last = _last_sent.get(key, 0.0)
        if now - last < DEDUPE_WINDOW_SECONDS:
            return False
        _last_sent[key] = now
        return True


def _format_text(level: str, title: str, body: str) -> str:
    prefix = _LEVEL_PREFIX.get(level.upper(), "")
    header = f"{prefix} *{level.upper()}* — {title}".strip()
    return f"{header}\n{body}" if body else header


def _send_telegram(url: str, chat_id: str, text: str) -> bool:
    if requests is None:
        return False
    resp = requests.post(
        url,
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
        timeout=5,
    )
    return 200 <= resp.status_code < 300


def _send_discord(url: str, text: str) -> bool:
    if requests is None:
        return False
    resp = requests.post(url, json={"content": text}, timeout=5)
    return 200 <= resp.status_code < 300


def send(level: str, title: str, body: str = "",
         *, force: bool = False) -> bool:
    """Fire-and-forget alert.

    Args:
        level: one of INFO / GOOD / WARNING / CRITICAL.
        title: short category — used as dedupe key along with level.
        body: freeform context (multi-line allowed).
        force: bypass the dedupe window.

    Returns True if the webhook accepted the message, False on any
    failure (missing config, network error, HTTP non-2xx). Never
    raises.
    """
    webhook = os.getenv("ALERT_WEBHOOK_URL", "").strip()
    if not webhook:
        return False

    key = _dedupe_key(level, title)
    now = time.time()
    if not force and not _should_send(key, now):
        return False

    transport = os.getenv("ALERT_TRANSPORT", "telegram").strip().lower()
    text = _format_text(level, title, body)

    try:
        if transport == "discord":
            ok = _send_discord(webhook, text)
        else:
            chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
            if not chat_id:
                logger.warning("alerts: TELEGRAM_CHAT_ID not set, skipping")
                return False
            ok = _send_telegram(webhook, chat_id, text)
    except Exception as e:
        logger.warning("alerts: webhook failed (%s): %s", transport, e)
        return False

    if not ok:
        logger.warning("alerts: webhook returned non-2xx for %s", key)
    return ok


def reset_dedupe() -> None:
    """Testing helper — clear the dedupe map."""
    with _lock:
        _last_sent.clear()
