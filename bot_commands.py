"""
Bot Commands — External instruction handler
=============================================
Allows sending commands to the running bot via a JSON file.
Any process (dashboard, CLI, cron) can write to COMMANDS_FILE
and the bot will pick it up on the next loop iteration.

Supported commands:
  - close_all: Close all open positions and cancel orders
  - close_symbol: Close position for a specific symbol (requires "symbol" param)
  - lock: Lock trading (requires "reason" param)
  - unlock: Unlock trading
  - status: Force write live_status.json immediately
  - restart_market_data: Restart market data stream

Usage (from any window/script):
    import json, pathlib
    pathlib.Path("bot_commands.json").write_text(json.dumps({
        "command": "close_all",
        "source": "dashboard",
        "timestamp": "2026-03-16T12:00:00Z"
    }))
"""

import json
import logging
import time
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger("bot.commands")

COMMANDS_FILE = Path(__file__).parent / "bot_commands.json"
COMMANDS_RESULT_FILE = Path(__file__).parent / "bot_commands_result.json"

# Maximum age of a command before it's considered stale and discarded (seconds)
MAX_COMMAND_AGE = 300  # 5 minutes


def read_pending_command() -> dict | None:
    """Read and consume a pending command from the commands file.

    Returns the command dict if valid, or None if no command pending.
    The file is deleted after reading to prevent re-execution.
    """
    if not COMMANDS_FILE.exists():
        return None

    try:
        raw = COMMANDS_FILE.read_text(encoding="utf-8").strip()
        if not raw:
            COMMANDS_FILE.unlink(missing_ok=True)
            return None

        cmd = json.loads(raw)

        # Delete immediately to prevent double-execution
        COMMANDS_FILE.unlink(missing_ok=True)

        if not isinstance(cmd, dict) or "command" not in cmd:
            logger.warning("Invalid command file (missing 'command' key): %s", raw[:200])
            return None

        # Check staleness
        ts = cmd.get("timestamp")
        if ts:
            try:
                cmd_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - cmd_time).total_seconds()
                if age > MAX_COMMAND_AGE:
                    logger.warning(
                        "Discarding stale command '%s' (age=%.0fs, max=%ds)",
                        cmd["command"], age, MAX_COMMAND_AGE,
                    )
                    _write_result(cmd, "discarded", f"Command too old ({age:.0f}s)")
                    return None
            except (ValueError, TypeError):
                pass  # No valid timestamp — execute anyway

        logger.info(
            "Received command: %s (source=%s)",
            cmd["command"], cmd.get("source", "unknown"),
        )
        return cmd

    except json.JSONDecodeError as e:
        logger.warning("Invalid JSON in commands file: %s", e)
        COMMANDS_FILE.unlink(missing_ok=True)
        return None
    except Exception as e:
        logger.warning("Error reading commands file: %s", e)
        return None


def _write_result(cmd: dict, status: str, message: str = ""):
    """Write command execution result for the caller to read."""
    try:
        result = {
            "command": cmd.get("command"),
            "status": status,
            "message": message,
            "executed_at": datetime.now(timezone.utc).isoformat(),
            "source": cmd.get("source", "unknown"),
        }
        tmp = COMMANDS_RESULT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, indent=2))
        tmp.replace(COMMANDS_RESULT_FILE)
    except Exception as e:
        logger.warning("Failed to write command result: %s", e)


def execute_command(cmd: dict, bot) -> bool:
    """Execute a command against the running bot instance.

    Returns True if the command was handled, False otherwise.
    """
    action = cmd.get("command", "").lower().strip()

    try:
        if action == "close_all":
            logger.info("COMMAND: Closing all positions and cancelling orders")
            if not bot.dry_run:
                bot.api.cancel_all_orders()
                bot.api.close_all_positions()
            _write_result(cmd, "ok", "All positions closed, orders cancelled")

        elif action == "close_symbol":
            symbol = cmd.get("symbol", "").upper()
            if not symbol:
                _write_result(cmd, "error", "Missing 'symbol' parameter")
                return False
            logger.info("COMMAND: Closing position for %s", symbol)
            if not bot.dry_run:
                contract_name = bot.contract_map.get(symbol)
                if contract_name:
                    contract = bot.api.find_contract(contract_name)
                    if contract:
                        bot.api.close_position(contract["id"])
                        _write_result(cmd, "ok", f"Closed position for {symbol}")
                    else:
                        _write_result(cmd, "error", f"Contract not found for {symbol}")
                else:
                    _write_result(cmd, "error", f"Unknown symbol: {symbol}")
                    return False

        elif action == "lock":
            reason = cmd.get("reason", "manual lock via command")
            logger.info("COMMAND: Locking trading — %s", reason)
            bot.risk.lock(reason)
            _write_result(cmd, "ok", f"Trading locked: {reason}")

        elif action == "unlock":
            logger.info("COMMAND: Unlocking trading")
            bot.risk.unlock()
            _write_result(cmd, "ok", "Trading unlocked")

        elif action == "status":
            logger.info("COMMAND: Force writing live status")
            bot._write_live_status()
            _write_result(cmd, "ok", "Status written to live_status.json")

        elif action == "restart_market_data":
            logger.info("COMMAND: Restarting market data stream")
            if bot.md_stream:
                bot.md_stream.stop()
            bot.md_stream = bot._start_market_data()
            if bot.md_stream:
                bot._subscribe_market_data()
            _write_result(cmd, "ok", "Market data stream restarted")

        else:
            logger.warning("Unknown command: %s", action)
            _write_result(cmd, "error", f"Unknown command: {action}")
            return False

        return True

    except Exception as e:
        logger.error("Command '%s' failed: %s", action, e, exc_info=True)
        _write_result(cmd, "error", str(e))
        return False


def send_command(command: str, **params) -> bool:
    """Convenience function to send a command to the bot from any process.

    Usage:
        from bot_commands import send_command
        send_command("close_all")
        send_command("close_symbol", symbol="NQ")
        send_command("lock", reason="manual stop")
    """
    payload = {
        "command": command,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "external",
        **params,
    }
    try:
        tmp = COMMANDS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(COMMANDS_FILE)
        return True
    except Exception as e:
        logger.error("Failed to send command: %s", e)
        return False
