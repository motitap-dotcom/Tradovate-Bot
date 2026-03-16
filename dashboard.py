#!/usr/bin/env python3
"""
Multi-Bot Status Dashboard
============================
Monitors multiple trading bots by reading their status JSON files
and serving a live HTML dashboard on port 8080.

Usage:
    python dashboard.py

Access: http://<server-ip>:8080
"""

import json
import time
import threading
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime, timezone

# ── Command file path (shared with bot_commands.py) ──
COMMANDS_FILE = Path(__file__).parent / "bot_commands.json"
COMMANDS_RESULT_FILE = Path(__file__).parent / "bot_commands_result.json"

# ── Bot status file paths ──
BOT_FILES = [
    {"name": "MT5 Bot", "path": "/var/bots/mt5_status.json"},
    {"name": "HyroTrader", "path": "/var/bots/HyroTrader_status.json"},
    {"name": "Tradovate", "path": "/var/bots/Tradovate_status.json"},
]

# ── Cached bot data (refreshed every 5s by background thread) ──
_bots_cache = []
_cache_lock = threading.Lock()


def _read_bot_file(bot_def):
    """Read a single bot status file and return a normalized dict."""
    path = Path(bot_def["path"])
    result = {
        "name": bot_def["name"],
        "file": bot_def["path"],
        "available": False,
        "active": False,
        "balance": None,
        "last_trade": None,
        "updated": None,
        "stale": False,
        "raw": {},
    }
    if not path.exists():
        return result

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        result["available"] = True
        result["raw"] = data

        # Active status — try common field names
        for key in ("active", "running", "is_active", "status"):
            if key in data:
                val = data[key]
                if isinstance(val, bool):
                    result["active"] = val
                elif isinstance(val, str):
                    result["active"] = val.lower() in ("true", "running", "active", "on")
                break

        # Balance
        for key in ("balance", "equity", "account_balance", "total_balance"):
            if key in data and data[key] is not None:
                try:
                    result["balance"] = float(data[key])
                except (ValueError, TypeError):
                    pass
                break

        # Last trade
        for key in ("last_trade", "last_order", "last_signal", "last_trade_time"):
            if key in data and data[key]:
                result["last_trade"] = str(data[key])
                break

        # Update timestamp
        for key in ("timestamp", "updated", "last_update", "updated_at", "timestamp_utc"):
            if key in data and data[key]:
                result["updated"] = str(data[key])
                break

        # Check file modification time as fallback / staleness check
        mtime = path.stat().st_mtime
        age_seconds = time.time() - mtime
        if result["updated"] is None:
            result["updated"] = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            )
        result["stale"] = age_seconds > 120  # >2 minutes

    except Exception:
        pass

    return result


def _refresh_loop():
    """Background thread: re-read all bot files every 5 seconds."""
    global _bots_cache
    while True:
        bots = [_read_bot_file(b) for b in BOT_FILES]
        with _cache_lock:
            _bots_cache = bots
        time.sleep(5)


def _build_html():
    """Build the full HTML page from cached bot data."""
    with _cache_lock:
        bots = list(_bots_cache)

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    cards_html = ""
    for bot in bots:
        if not bot["available"]:
            status_class = "unavailable"
            status_text = "Not Available"
            border_color = "#6e7681"
        elif bot["stale"]:
            status_class = "stale"
            status_text = "STALE (>2 min)"
            border_color = "#f85149"
        elif bot["active"]:
            status_class = "active"
            status_text = "Active"
            border_color = "#3fb950"
        else:
            status_class = "inactive"
            status_text = "Inactive"
            border_color = "#f85149"

        balance_str = f"${bot['balance']:,.2f}" if bot["balance"] is not None else "N/A"
        last_trade_str = bot["last_trade"] or "N/A"
        updated_str = bot["updated"] or "N/A"

        stale_warning = ""
        if bot["stale"] and bot["available"]:
            stale_warning = '<div class="stale-warning">File not updated for over 2 minutes!</div>'

        # Open positions table
        raw = bot.get("raw", {})
        open_pos = raw.get("open_positions", [])
        # Normalize: ensure open_pos is a list of dicts
        if isinstance(open_pos, str):
            try:
                open_pos = json.loads(open_pos)
            except (json.JSONDecodeError, TypeError):
                open_pos = []
        if not isinstance(open_pos, list):
            open_pos = []
        open_count = raw.get("open_positions_count", len(open_pos))
        open_pos_html = ""
        if open_pos:
            open_pos_html = f'<div class="section-title">Open Positions ({open_count})</div>'
            open_pos_html += '<table class="trades-table"><tr><th>Symbol</th><th>Dir</th><th>Qty</th><th>P&L</th></tr>'
            for p in open_pos:
                if isinstance(p, str):
                    try:
                        p = json.loads(p)
                    except (json.JSONDecodeError, TypeError):
                        continue
                if not isinstance(p, dict):
                    continue
                direction = p.get("direction") or p.get("side") or ""
                pnl = p.get("pnl_dollars") or p.get("unrealised_pnl") or 0
                try:
                    pnl = float(pnl)
                except (ValueError, TypeError):
                    pnl = 0
                pnl_class = "green" if pnl >= 0 else "red"
                pnl_sign = "+" if pnl >= 0 else ""
                dir_class = "buy" if str(direction).lower() == "buy" else "sell"
                qty = p.get("qty") or p.get("size") or 0
                entry = p.get("entry_price")
                entry_html = f'<td>${float(entry):,.2f}</td>' if entry is not None else ""
                open_pos_html += (
                    f'<tr><td>{p.get("symbol", "")}</td>'
                    f'<td><span class="badge {dir_class}">{direction}</span></td>'
                    f'<td>{qty}</td>'
                    f'<td class="{pnl_class}">{pnl_sign}${pnl:,.2f}</td>{entry_html}</tr>'
                )
            open_pos_html += '</table>'
        elif bot["available"]:
            open_pos_html = '<div class="section-title">Open Positions (0)</div><div class="empty-msg">No open positions</div>'

        # Recent closed trades table
        closed_trades = raw.get("recent_closed_trades", [])
        # Normalize: ensure closed_trades is a list of dicts
        if isinstance(closed_trades, str):
            try:
                closed_trades = json.loads(closed_trades)
            except (json.JSONDecodeError, TypeError):
                closed_trades = []
        if not isinstance(closed_trades, list):
            closed_trades = []
        closed_html = ""
        if closed_trades:
            closed_html = '<div class="section-title">Recent Closed Trades</div>'
            closed_html += '<table class="trades-table"><tr><th>Symbol</th><th>Dir</th><th>P&L</th><th>Time</th></tr>'
            for ct in closed_trades:
                if isinstance(ct, str):
                    try:
                        ct = json.loads(ct)
                    except (json.JSONDecodeError, TypeError):
                        continue
                if not isinstance(ct, dict):
                    continue
                direction = ct.get("direction") or ct.get("side") or ""
                pnl = ct.get("pnl_dollars") or ct.get("closed_pnl") or 0
                try:
                    pnl = float(pnl)
                except (ValueError, TypeError):
                    pnl = 0
                pnl_class = "green" if pnl >= 0 else "red"
                pnl_sign = "+" if pnl >= 0 else ""
                dir_class = "buy" if str(direction).lower() == "buy" else "sell"
                closed_at = ct.get("closed_at", "")
                # Show just the time portion if it's an ISO timestamp
                if isinstance(closed_at, str) and "T" in closed_at:
                    closed_at = closed_at.split("T")[1][:8]
                closed_html += (
                    f'<tr><td>{ct.get("symbol", "")}</td>'
                    f'<td><span class="badge {dir_class}">{direction}</span></td>'
                    f'<td class="{pnl_class}">{pnl_sign}${pnl:,.2f}</td>'
                    f'<td>{closed_at}</td></tr>'
                )
            closed_html += '</table>'
        elif bot["available"]:
            closed_html = '<div class="section-title">Recent Closed Trades</div><div class="empty-msg">No closed trades</div>'

        cards_html += f"""
        <div class="card" style="border-top: 4px solid {border_color};">
            <div class="card-header">
                <span class="bot-name">{bot['name']}</span>
                <span class="status-badge {status_class}">{status_text}</span>
            </div>
            {stale_warning}
            <div class="row"><span class="label">Balance</span><span class="value">{balance_str}</span></div>
            <div class="row"><span class="label">Last Trade</span><span class="value">{last_trade_str}</span></div>
            <div class="row"><span class="label">Updated</span><span class="value">{updated_str}</span></div>
            <div class="row"><span class="label">File</span><span class="value file-path">{bot['file']}</span></div>
            {open_pos_html}
            {closed_html}
        </div>
        """

    return f"""<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="5">
<title>Bot Dashboard</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0d1117; color: #c9d1d9; min-height: 100vh; }}
.container {{ max-width: 1100px; margin: 0 auto; padding: 24px; }}
h1 {{ font-size: 1.6em; color: #58a6ff; margin-bottom: 8px; }}
.subtitle {{ color: #8b949e; font-size: 0.85em; margin-bottom: 24px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 20px; }}
.card {{ background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 20px; }}
.card-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }}
.bot-name {{ font-size: 1.2em; font-weight: 700; color: #e6edf3; }}
.status-badge {{ display: inline-block; padding: 4px 14px; border-radius: 14px; font-size: 0.8em; font-weight: 700; }}
.status-badge.active {{ background: #0e4429; color: #3fb950; border: 1px solid #238636; }}
.status-badge.inactive {{ background: #490d0d; color: #f85149; border: 1px solid #da3633; }}
.status-badge.stale {{ background: #4a1d00; color: #f85149; border: 1px solid #da3633; animation: pulse 1.5s infinite; }}
.status-badge.unavailable {{ background: #21262d; color: #6e7681; border: 1px solid #30363d; }}
@keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.6; }} }}
.stale-warning {{ background: #490d0d; color: #f85149; border: 1px solid #da3633; border-radius: 6px; padding: 8px 12px; margin-bottom: 12px; font-size: 0.85em; font-weight: 600; text-align: center; }}
.row {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #21262d; }}
.row:last-child {{ border-bottom: none; }}
.label {{ color: #8b949e; }}
.value {{ font-weight: 600; direction: ltr; text-align: left; }}
.file-path {{ font-family: monospace; font-size: 0.8em; color: #6e7681; word-break: break-all; }}
.section-title {{ color: #8b949e; font-size: 0.8em; text-transform: uppercase; letter-spacing: 1px; margin-top: 14px; margin-bottom: 8px; padding-top: 10px; border-top: 1px solid #21262d; }}
.trades-table {{ width: 100%; border-collapse: collapse; font-size: 0.82em; }}
.trades-table th {{ text-align: right; padding: 5px 6px; color: #8b949e; border-bottom: 1px solid #30363d; font-weight: 500; }}
.trades-table td {{ padding: 5px 6px; border-bottom: 1px solid #21262d; }}
.badge {{ display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 0.8em; font-weight: 600; }}
.badge.buy {{ background: #0e4429; color: #3fb950; }}
.badge.sell {{ background: #490d0d; color: #f85149; }}
.green {{ color: #3fb950; }}
.red {{ color: #f85149; }}
.empty-msg {{ color: #484f58; font-size: 0.85em; font-style: italic; }}
.command-panel {{ background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 20px; margin-top: 20px; }}
.cmd-buttons {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px; }}
.cmd-btn {{ padding: 8px 18px; border: none; border-radius: 6px; font-size: 0.9em; font-weight: 600; cursor: pointer; transition: opacity 0.2s; }}
.cmd-btn:hover {{ opacity: 0.85; }}
.btn-info {{ background: #1f6feb; color: #fff; }}
.btn-danger {{ background: #da3633; color: #fff; }}
.btn-warn {{ background: #d29922; color: #000; }}
.btn-ok {{ background: #238636; color: #fff; }}
.cmd-result {{ margin-top: 12px; padding: 8px 12px; border-radius: 6px; font-size: 0.85em; min-height: 20px; }}
.cmd-result.ok {{ background: #0e4429; color: #3fb950; border: 1px solid #238636; }}
.cmd-result.err {{ background: #490d0d; color: #f85149; border: 1px solid #da3633; }}
.cmd-result.pending {{ background: #21262d; color: #8b949e; }}
.footer {{ text-align: center; color: #484f58; font-size: 0.8em; margin-top: 24px; padding-top: 16px; border-top: 1px solid #21262d; }}
</style>
</head>
<body>
<div class="container">
<h1>Bot Dashboard</h1>
<div class="subtitle">Auto-refresh every 5 seconds | {now_str}</div>
<div class="grid">
{cards_html}
</div>
<div class="command-panel">
<div class="section-title" style="margin-top:0">Bot Commands</div>
<div class="cmd-buttons">
  <button onclick="sendCmd('status')" class="cmd-btn btn-info">Refresh Status</button>
  <button onclick="sendCmd('close_all')" class="cmd-btn btn-danger">Close All</button>
  <button onclick="sendCmd('lock', {{reason: prompt('Lock reason:') || 'manual'}})" class="cmd-btn btn-warn">Lock Trading</button>
  <button onclick="sendCmd('unlock')" class="cmd-btn btn-ok">Unlock Trading</button>
  <button onclick="sendCmd('restart_market_data')" class="cmd-btn btn-info">Restart Market Data</button>
</div>
<div id="cmd-result" class="cmd-result"></div>
</div>
<script>
async function sendCmd(command, params) {{
  const body = Object.assign({{command}}, params || {{}});
  const el = document.getElementById('cmd-result');
  el.textContent = 'Sending ' + command + '...';
  el.className = 'cmd-result pending';
  try {{
    const r = await fetch('/command', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(body)}});
    const j = await r.json();
    el.textContent = j.status === 'sent' ? 'Command sent: ' + command + ' (bot will execute within 30s)' : 'Error: ' + (j.message || j.error);
    el.className = 'cmd-result ' + (j.status === 'sent' ? 'ok' : 'err');
  }} catch(e) {{
    el.textContent = 'Network error: ' + e.message;
    el.className = 'cmd-result err';
  }}
}}
</script>
<div class="footer">Dashboard refreshes automatically every 5 seconds</div>
</div>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/command-result":
            # Return latest command result as JSON
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            try:
                if COMMANDS_RESULT_FILE.exists():
                    data = COMMANDS_RESULT_FILE.read_text(encoding="utf-8")
                else:
                    data = json.dumps({"status": "none", "message": "No commands executed yet"})
            except Exception:
                data = json.dumps({"status": "error", "message": "Failed to read result"})
            self.wfile.write(data.encode("utf-8"))
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_build_html().encode("utf-8"))

    def do_POST(self):
        """Handle command submissions from the dashboard."""
        if self.path != "/command":
            self.send_response(404)
            self.end_headers()
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")

        try:
            cmd = json.loads(body)
        except json.JSONDecodeError:
            # Try form-encoded
            params = urllib.parse.parse_qs(body)
            cmd = {k: v[0] for k, v in params.items()}

        if "command" not in cmd:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Missing 'command'"}).encode())
            return

        # Write command file for the bot to pick up
        cmd["timestamp"] = datetime.now(timezone.utc).isoformat()
        cmd["source"] = "dashboard"
        try:
            tmp = COMMANDS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(cmd, indent=2))
            tmp.replace(COMMANDS_FILE)
            response = {"status": "sent", "command": cmd["command"]}
        except Exception as e:
            response = {"status": "error", "message": str(e)}

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode("utf-8"))

    def log_message(self, format, *args):
        # Suppress default request logging
        pass


def main():
    port = 8080

    # Initial read before server starts
    global _bots_cache
    _bots_cache = [_read_bot_file(b) for b in BOT_FILES]

    # Start background refresh thread
    t = threading.Thread(target=_refresh_loop, daemon=True)
    t.start()

    server = HTTPServer(("0.0.0.0", port), DashboardHandler)
    print(f"Dashboard running on http://0.0.0.0:{port}")
    print(f"Monitoring {len(BOT_FILES)} bots. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
