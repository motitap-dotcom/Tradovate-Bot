"""
Status Reporter
================
Writes bot status to /var/bots/Tradovate_status.json for external monitoring.
"""

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
STATUS_PATH = Path("/var/bots/Tradovate_status.json")


def _config_fingerprint() -> dict:
    """Return a short hash of the tunable params so external monitors can
    detect when the VPS is running a different config than the repo.
    """
    payload = {
        "prop_firm": config.PROP_FIRM,
        "env": config.ENVIRONMENT,
        "brake_pct": config.DAILY_LOSS_BRAKE_PCT,
        "max_daily_trades": config.MAX_DAILY_TRADES,
        "risk_per_trade_pct": config.RISK_PER_TRADE_PCT,
        "max_contracts": config.ACTIVE_CHALLENGE.get("max_contracts"),
        "trading_cutoff_et": config.TRADING_CUTOFF_ET,
        "enabled_symbols": sorted(
            sym for sym, spec in config.CONTRACT_SPECS.items()
            if spec.get("enabled")
        ),
        "contract_params": {
            sym: {
                k: spec.get(k) for k in (
                    "stop_loss_points", "take_profit_points",
                    "risk_reward_ratio", "max_orb_trades",
                    "orb_cooldown_minutes", "vwap_cooldown_minutes",
                    "max_vwap_trades_per_direction",
                )
            }
            for sym, spec in config.CONTRACT_SPECS.items()
            if spec.get("enabled")
        },
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
    return {"hash": digest, "summary": payload}


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
            "losing_streak": risk_status.get("losing_streak"),
            "streak_pause_until": risk_status.get("streak_pause_until"),
            "active_symbols": list(contract_map.keys()) if contract_map else [],
            "open_positions": open_pos,
            "open_positions_count": len(open_pos),
            "recent_closed_trades": closed,
            "config": _config_fingerprint(),
        }

        tmp = STATUS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(STATUS_PATH)
    except Exception as e:
        logger.warning("Failed to write %s: %s", STATUS_PATH, e)
