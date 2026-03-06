"""
Status Reporter
================
Writes bot status to /var/bots/Tradovate_status.json for external monitoring.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
STATUS_PATH = Path("/var/bots/Tradovate_status.json")


def write_status(
    risk_status: dict,
    *,
    contract_map: dict | None = None,
    dry_run: bool = False,
    open_positions: list | None = None,
    recent_closed_trades: list | None = None,
):
    """Write current bot status to the shared status file.

    Args:
        risk_status: dict from RiskManager.status()
        contract_map: symbol -> contract name mapping
        dry_run: whether the bot is in dry-run mode
        open_positions: list of open position dicts (symbol, direction, qty, entry_price, pnl_dollars)
        recent_closed_trades: list of recently closed trade dicts (symbol, direction, pnl_dollars, closed_at)
    """
    try:
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)

        open_pos = open_positions or []
        closed = recent_closed_trades or []

        payload = {
            "bot": "Tradovate",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "timestamp_et": datetime.now(ET).isoformat(),
            "environment": config.ENVIRONMENT,
            "dry_run": dry_run,
            "balance": risk_status.get("balance"),
            "equity": risk_status.get("equity"),
            "day_pnl": risk_status.get("day_pnl"),
            "peak_balance": risk_status.get("peak_balance"),
            "drawdown_floor": risk_status.get("drawdown_floor"),
            "distance_to_floor": risk_status.get("distance_to_floor"),
            "open_contracts": risk_status.get("open_contracts"),
            "trades_today": risk_status.get("trades_today"),
            "locked": risk_status.get("locked"),
            "lock_reason": risk_status.get("lock_reason"),
            "active_symbols": list(contract_map.keys()) if contract_map else [],
            "open_positions": open_pos,
            "open_positions_count": len(open_pos),
            "recent_closed_trades": closed,
        }

        tmp = STATUS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(STATUS_PATH)
    except Exception as e:
        logger.warning("Failed to write %s: %s", STATUS_PATH, e)
