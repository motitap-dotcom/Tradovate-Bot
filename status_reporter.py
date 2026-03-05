"""
Status Reporter
================
Writes periodic bot status to a JSON file for external monitoring.
File path: /var/bots/{bot_name}_status.json

Called from the main loop every 30 seconds. Errors here never stop the bot.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("status_reporter")

STATUS_DIR = Path("/var/bots")


def write_status(
    bot_name: str,
    is_active: bool,
    balance: float,
    last_trade: dict | None,
    extra: dict | None = None,
) -> None:
    """
    Write bot status JSON to /var/bots/{bot_name}_status.json.

    Parameters
    ----------
    bot_name : str
        Identifier for the bot (used in the file name and JSON).
    is_active : bool
        Whether the bot is currently running.
    balance : float
        Current account balance.
    last_trade : dict or None
        Info about the most recent trade, or None if no trades today.
    extra : dict or None
        Optional extra fields to include in the JSON.
    """
    try:
        # Ensure directory exists
        STATUS_DIR.mkdir(parents=True, exist_ok=True)

        payload = {
            "bot_name": bot_name,
            "is_active": is_active,
            "balance": balance,
            "last_trade": last_trade,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        if extra:
            payload.update(extra)

        status_file = STATUS_DIR / f"{bot_name}_status.json"
        tmp = status_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        tmp.replace(status_file)

    except Exception as e:
        # Never let status reporting crash the bot
        logger.warning("Status report failed: %s", e)
