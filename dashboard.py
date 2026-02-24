#!/usr/bin/env python3
"""
Web Dashboard for Tradovate Bot
=================================
Lightweight Flask server providing real-time bot monitoring.

Usage:
    python dashboard.py              # Start on port 8080
    python dashboard.py --port 5000  # Custom port

Access from browser: http://<server-ip>:8080
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, date
from flask import Flask, jsonify, Response

import config
from trade_journal import TradeJournal
from tradovate_api import TradovateAPI

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BOT_DIR, "bot.log")
TOKEN_FILE = os.path.join(BOT_DIR, ".tradovate_token.json")
TUNER_LOG = os.path.join(BOT_DIR, "tuner_log.json")

app = Flask(__name__)


def _ensure_token():
    """Authenticate with Tradovate API if no valid token exists."""
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE) as f:
                data = json.load(f)
            exp = data.get("expirationTime", "")
            if exp:
                exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) < exp_dt:
                    return  # Token still valid
        except Exception:
            pass
    # No valid token — try to authenticate
    try:
        api = TradovateAPI()
        if api.authenticate():
            print("  Dashboard: Tradovate token acquired successfully")
        else:
            print("  Dashboard: Could not authenticate (CAPTCHA may be required)")
    except Exception as e:
        print(f"  Dashboard: Auth error: {e}")


# ─────────────────────────────────────────
# Data helpers
# ─────────────────────────────────────────

def _is_bot_running():
    try:
        out = subprocess.check_output(["pgrep", "-f", "bot.py"], text=True, stderr=subprocess.DEVNULL)
        pids = [int(p) for p in out.strip().split("\n") if p]
        return True, pids[0] if pids else 0
    except subprocess.CalledProcessError:
        return False, 0


def _get_last_status():
    if not os.path.exists(LOG_FILE):
        return {}
    try:
        out = subprocess.check_output(["tail", "-50", LOG_FILE], text=True, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        return {}

    pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*Status \| "
        r"balance=([\d.]+) \| day_pnl=([-\d.]+) \| to_floor=([-\d.]+) \| "
        r"contracts=(\d+)/(\d+) \| trades=(\d+)/(\d+) \| locked=(\w+)"
    )
    for line in reversed(out.strip().split("\n")):
        m = pattern.search(line)
        if m:
            return {
                "timestamp": m.group(1),
                "balance": float(m.group(2)),
                "day_pnl": float(m.group(3)),
                "to_floor": float(m.group(4)),
                "open_contracts": int(m.group(5)),
                "max_contracts": int(m.group(6)),
                "trades_today": int(m.group(7)),
                "max_trades": int(m.group(8)),
                "locked": m.group(9) == "True",
            }
    return {}


def _get_token_info():
    if not os.path.exists(TOKEN_FILE):
        return {"status": "missing", "minutes_remaining": 0}
    try:
        with open(TOKEN_FILE) as f:
            data = json.load(f)
        exp = data.get("expirationTime", "")
        if not exp:
            return {"status": "unknown", "minutes_remaining": 0}
        exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        remaining = (exp_dt - datetime.now(timezone.utc)).total_seconds() / 60
        return {
            "status": "valid" if remaining > 0 else "expired",
            "minutes_remaining": max(0, round(remaining)),
        }
    except Exception:
        return {"status": "error", "minutes_remaining": 0}


def _get_recent_log(lines=30):
    if not os.path.exists(LOG_FILE):
        return []
    try:
        out = subprocess.check_output(["tail", f"-{lines}", LOG_FILE], text=True, stderr=subprocess.DEVNULL)
        return out.strip().split("\n")
    except subprocess.CalledProcessError:
        return []


def _get_api_balance():
    """Fetch live balance from Tradovate API using saved token."""
    if not os.path.exists(TOKEN_FILE):
        return None
    try:
        import requests
        with open(TOKEN_FILE) as f:
            t = json.load(f)
        token = t.get("accessToken", "")
        account_id = t.get("accountId")
        if not token or not account_id:
            return None
        resp = requests.post(
            "https://demo.tradovateapi.com/v1/cashBalance/getcashbalancesnapshot",
            json={"accountId": account_id},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            if not data.get("errorText"):
                return data
    except Exception:
        pass
    return None


def _get_prices():
    """Get current futures prices from Yahoo Finance."""
    import requests
    symbols = {"NQ": "NQ=F", "ES": "ES=F", "GC": "GC=F", "CL": "CL=F"}
    prices = {}
    for name, sym in symbols.items():
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1m&range=1d"
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
            if resp.status_code == 200:
                meta = resp.json()["chart"]["result"][0]["meta"]
                prices[name] = {
                    "price": meta.get("regularMarketPrice", 0),
                    "high": meta.get("regularMarketDayHigh", 0),
                    "low": meta.get("regularMarketDayLow", 0),
                    "change": meta.get("regularMarketPrice", 0) - meta.get("chartPreviousClose", 0),
                }
        except Exception:
            pass
    return prices


def _get_tuner_log():
    if not os.path.exists(TUNER_LOG):
        return []
    try:
        with open(TUNER_LOG) as f:
            return json.load(f)[-20:]  # Last 20 adjustments
    except Exception:
        return []


# ─────────────────────────────────────────
# API endpoints
# ─────────────────────────────────────────

@app.route("/api/status")
def api_status():
    running, pid = _is_bot_running()
    status = _get_last_status()
    token = _get_token_info()

    # Enrich with live API balance when token is valid
    if token.get("status") == "valid":
        api_bal = _get_api_balance()
        if api_bal:
            status["balance"] = api_bal.get("totalCashValue", status.get("balance", 50000))
            sod = api_bal.get("totalCashValue", 50000)
            status["day_pnl"] = sod - 50000 + api_bal.get("openPnL", 0)
            equity = api_bal.get("netLiq", status.get("balance", 50000))
            status["to_floor"] = equity - (50000 - 2500)
            status["api_live"] = True

    return jsonify({
        "bot": {"running": running, "pid": pid},
        "token": token,
        "trading": status,
        "server_time": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/api/prices")
def api_prices():
    return jsonify(_get_prices())


@app.route("/api/journal")
def api_journal():
    journal = TradeJournal()
    summary = journal._compute_summary()
    by_symbol = journal.analyze_by_symbol()
    by_strategy = journal.analyze_by_strategy()
    by_hour = journal.analyze_by_hour()
    lessons = journal.generate_lessons()
    recent = journal._closed_trades()[-10:]  # Last 10 trades
    return jsonify({
        "summary": summary,
        "by_symbol": by_symbol,
        "by_strategy": by_strategy,
        "by_hour": by_hour,
        "lessons": lessons,
        "recent_trades": recent,
    })


@app.route("/api/tuner")
def api_tuner():
    return jsonify(_get_tuner_log())


@app.route("/api/log")
def api_log():
    return jsonify(_get_recent_log(50))


@app.route("/api/refresh-token", methods=["POST"])
def api_refresh_token():
    _ensure_token()
    return jsonify(_get_token_info())


@app.route("/api/config")
def api_config():
    challenge = config.ACTIVE_CHALLENGE
    specs = {}
    for sym, spec in config.CONTRACT_SPECS.items():
        if spec.get("enabled"):
            specs[sym] = {
                "name": spec["name"],
                "strategy": spec["strategy"],
                "stop_loss": spec["stop_loss_points"],
                "take_profit": spec["take_profit_points"],
                "point_value": spec["point_value"],
            }
    return jsonify({
        "prop_firm": config.PROP_FIRM,
        "environment": config.ENVIRONMENT,
        "challenge": challenge,
        "contracts": specs,
        "risk_per_trade": config.RISK_PER_TRADE_PCT,
        "max_daily_trades": config.MAX_DAILY_TRADES,
    })


# ─────────────────────────────────────────
# Main page
# ─────────────────────────────────────────

@app.route("/")
def index():
    return Response(HTML_PAGE, mimetype="text/html")


HTML_PAGE = """<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tradovate Bot Dashboard</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0d1117; color: #c9d1d9; min-height: 100vh; }
.container { max-width: 1200px; margin: 0 auto; padding: 16px; }
h1 { font-size: 1.5em; color: #58a6ff; margin-bottom: 16px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; margin-bottom: 16px; }
.card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }
.card h2 { font-size: 0.9em; color: #8b949e; margin-bottom: 12px; text-transform: uppercase; letter-spacing: 1px; }
.big-number { font-size: 2em; font-weight: 700; }
.green { color: #3fb950; }
.red { color: #f85149; }
.yellow { color: #d29922; }
.gray { color: #8b949e; }
.status-dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-left: 8px; }
.status-dot.on { background: #3fb950; box-shadow: 0 0 6px #3fb950; }
.status-dot.off { background: #f85149; }
.row { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid #21262d; }
.row:last-child { border-bottom: none; }
.label { color: #8b949e; }
.value { font-weight: 600; }
.prices-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
.price-card { text-align: center; padding: 12px 8px; background: #0d1117; border-radius: 6px; }
.price-card .sym { font-size: 0.85em; color: #8b949e; }
.price-card .px { font-size: 1.3em; font-weight: 700; margin: 4px 0; }
.price-card .chg { font-size: 0.85em; }
.lessons { list-style: none; }
.lessons li { padding: 8px 12px; margin-bottom: 6px; background: #1c2128; border-radius: 6px; border-right: 3px solid #58a6ff; font-size: 0.9em; line-height: 1.5; }
.log { background: #0d1117; border-radius: 6px; padding: 12px; font-family: 'Fira Code', monospace; font-size: 0.75em; max-height: 300px; overflow-y: auto; direction: ltr; text-align: left; line-height: 1.6; }
.log .err { color: #f85149; }
.log .warn { color: #d29922; }
.log .info { color: #8b949e; }
.trades-table { width: 100%; border-collapse: collapse; font-size: 0.85em; }
.trades-table th { text-align: right; padding: 8px; color: #8b949e; border-bottom: 1px solid #30363d; }
.trades-table td { padding: 8px; border-bottom: 1px solid #21262d; }
.tuner-item { padding: 8px; margin-bottom: 4px; background: #1c2128; border-radius: 4px; font-size: 0.85em; }
.refresh-info { text-align: center; color: #484f58; font-size: 0.8em; margin-top: 12px; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.8em; font-weight: 600; }
.badge.buy { background: #0e4429; color: #3fb950; }
.badge.sell { background: #490d0d; color: #f85149; }
</style>
</head>
<body>
<div class="container">
<h1>Tradovate Bot Dashboard</h1>

<div class="grid">
  <!-- Status Card -->
  <div class="card">
    <h2>Bot Status</h2>
    <div id="bot-status">Loading...</div>
  </div>

  <!-- Balance Card -->
  <div class="card">
    <h2>Balance</h2>
    <div id="balance-info">Loading...</div>
  </div>

  <!-- Challenge Progress -->
  <div class="card">
    <h2>Challenge Progress</h2>
    <div id="challenge-info">Loading...</div>
  </div>
</div>

<!-- Prices -->
<div class="card" style="margin-bottom:16px">
  <h2>Market Prices</h2>
  <div id="prices" class="prices-grid">Loading...</div>
</div>

<div class="grid">
  <!-- Journal Summary -->
  <div class="card">
    <h2>Trade Journal</h2>
    <div id="journal-summary">Loading...</div>
  </div>

  <!-- Lessons -->
  <div class="card">
    <h2>Lessons & Recommendations</h2>
    <ul id="lessons" class="lessons"><li>Loading...</li></ul>
  </div>
</div>

<!-- Recent Trades -->
<div class="card" style="margin-bottom:16px">
  <h2>Recent Trades</h2>
  <div id="recent-trades">No trades yet</div>
</div>

<div class="grid">
  <!-- Auto-Tuner -->
  <div class="card">
    <h2>Auto-Tuner Adjustments</h2>
    <div id="tuner">No adjustments yet</div>
  </div>

  <!-- Log -->
  <div class="card">
    <h2>Live Log</h2>
    <div id="log" class="log">Loading...</div>
  </div>
</div>

<div class="refresh-info" id="refresh-time"></div>
</div>

<script>
function fmt(n, d=2) { return Number(n).toLocaleString('en-US', {minimumFractionDigits:d, maximumFractionDigits:d}); }
function pnlClass(v) { return v >= 0 ? 'green' : 'red'; }
function pnlSign(v) { return v >= 0 ? '+' : ''; }

async function fetchJSON(url) {
  try { const r = await fetch(url); return await r.json(); } catch(e) { return null; }
}

async function updateStatus() {
  const d = await fetchJSON('/api/status');
  if (!d) return;

  const bot = d.bot;
  const t = d.trading || {};
  const tok = d.token;

  document.getElementById('bot-status').innerHTML = `
    <div class="row"><span class="label">Process</span>
      <span class="value">${bot.running ? 'RUNNING' : 'STOPPED'}
      <span class="status-dot ${bot.running ? 'on' : 'off'}"></span></span></div>
    <div class="row"><span class="label">PID</span><span class="value">${bot.pid || '-'}</span></div>
    <div class="row"><span class="label">Token</span>
      <span class="value ${tok.status === 'valid' ? 'green' : 'red'}">${tok.status} (${tok.minutes_remaining}m)</span></div>
    <div class="row"><span class="label">Trades Today</span><span class="value">${t.trades_today || 0}/${t.max_trades || 12}</span></div>
    <div class="row"><span class="label">Contracts</span><span class="value">${t.open_contracts || 0}/${t.max_contracts || 10}</span></div>
    ${t.locked ? '<div class="row"><span class="red" style="font-weight:700">TRADING LOCKED</span></div>' : ''}
  `;

  const bal = t.balance || 50000;
  const pnl = t.day_pnl || 0;
  const source = t.api_live ? '<span class="green" style="font-size:0.7em">LIVE API</span>' : '<span class="yellow" style="font-size:0.7em">FROM LOG</span>';
  document.getElementById('balance-info').innerHTML = `
    <div class="big-number">$${fmt(bal)}</div>
    <div style="text-align:center;margin:4px 0">${source}</div>
    <div style="margin-top:8px">
      <div class="row"><span class="label">Day P&L</span>
        <span class="value ${pnlClass(pnl)}">${pnlSign(pnl)}$${fmt(pnl)}</span></div>
      <div class="row"><span class="label">Distance to Floor</span>
        <span class="value ${(t.to_floor||0) < 500 ? 'red' : 'green'}">$${fmt(t.to_floor||0)}</span></div>
    </div>
  `;
}

async function updateChallenge() {
  const cfg = await fetchJSON('/api/config');
  if (!cfg) return;
  const c = cfg.challenge;
  const status = await fetchJSON('/api/status');
  const bal = status?.trading?.balance || 50000;
  const profit = bal - c.account_size;
  const pct = Math.max(0, (profit / c.profit_target) * 100);

  document.getElementById('challenge-info').innerHTML = `
    <div class="row"><span class="label">Prop Firm</span><span class="value">${cfg.prop_firm.toUpperCase()}</span></div>
    <div class="row"><span class="label">Profit Target</span><span class="value">$${fmt(c.profit_target,0)}</span></div>
    <div class="row"><span class="label">Current Profit</span>
      <span class="value ${pnlClass(profit)}">${pnlSign(profit)}$${fmt(profit)}</span></div>
    <div class="row"><span class="label">Progress</span><span class="value">${fmt(pct,1)}%</span></div>
    <div style="background:#21262d;border-radius:4px;height:8px;margin-top:8px">
      <div style="background:${pct>0?'#3fb950':'#f85149'};height:100%;border-radius:4px;width:${Math.min(100,Math.max(0,pct))}%"></div>
    </div>
    <div class="row" style="margin-top:8px"><span class="label">Max Drawdown</span><span class="value">$${fmt(c.max_trailing_drawdown,0)}</span></div>
    <div class="row"><span class="label">Daily Loss Limit</span><span class="value">${c.daily_loss_limit ? '$'+fmt(c.daily_loss_limit,0) : 'None'}</span></div>
  `;
}

async function updatePrices() {
  const p = await fetchJSON('/api/prices');
  if (!p) return;
  let html = '';
  for (const [sym, d] of Object.entries(p)) {
    const chgClass = d.change >= 0 ? 'green' : 'red';
    html += `<div class="price-card">
      <div class="sym">${sym}</div>
      <div class="px">$${fmt(d.price, sym==='CL'?2:2)}</div>
      <div class="chg ${chgClass}">${d.change>=0?'+':''}${fmt(d.change)}</div>
    </div>`;
  }
  document.getElementById('prices').innerHTML = html || '<div class="gray">No data</div>';
}

async function updateJournal() {
  const j = await fetchJSON('/api/journal');
  if (!j) return;
  const s = j.summary;

  if (!s || s.total_trades === 0) {
    document.getElementById('journal-summary').innerHTML = '<div class="gray">No closed trades yet</div>';
    document.getElementById('lessons').innerHTML = '<li>Waiting for trades...</li>';
    document.getElementById('recent-trades').innerHTML = '<div class="gray">No trades yet</div>';
    return;
  }

  document.getElementById('journal-summary').innerHTML = `
    <div class="row"><span class="label">Total Trades</span><span class="value">${s.total_trades}</span></div>
    <div class="row"><span class="label">Win Rate</span>
      <span class="value ${s.win_rate >= 0.5 ? 'green' : 'yellow'}">${(s.win_rate*100).toFixed(0)}%</span></div>
    <div class="row"><span class="label">Total P&L</span>
      <span class="value ${pnlClass(s.total_pnl)}">${pnlSign(s.total_pnl)}$${fmt(s.total_pnl)}</span></div>
    <div class="row"><span class="label">Profit Factor</span><span class="value">${fmt(s.profit_factor)}</span></div>
    <div class="row"><span class="label">Expectancy</span>
      <span class="value ${pnlClass(s.expectancy)}">${pnlSign(s.expectancy)}$${fmt(s.expectancy)}/trade</span></div>
    <div class="row"><span class="label">Avg R</span><span class="value">${fmt(s.avg_r_multiple)}R</span></div>
    <div class="row"><span class="label">Best/Worst</span>
      <span class="value"><span class="green">+$${fmt(s.best_trade)}</span> / <span class="red">$${fmt(s.worst_trade)}</span></span></div>
  `;

  // Lessons
  const lessonsHtml = j.lessons.map(l => `<li>${l}</li>`).join('');
  document.getElementById('lessons').innerHTML = lessonsHtml || '<li>No lessons yet</li>';

  // Recent trades
  if (j.recent_trades && j.recent_trades.length > 0) {
    let thtml = '<table class="trades-table"><tr><th>Date</th><th>Symbol</th><th>Dir</th><th>Qty</th><th>P&L</th><th>R</th><th>Exit</th></tr>';
    for (const t of j.recent_trades.reverse()) {
      const dir = t.direction === 'Buy' ? 'buy' : 'sell';
      const pnl = t.pnl || 0;
      thtml += `<tr>
        <td>${(t.date||'').slice(5)}</td>
        <td>${t.symbol}</td>
        <td><span class="badge ${dir}">${t.direction}</span></td>
        <td>${t.qty}</td>
        <td class="${pnlClass(pnl)}">${pnlSign(pnl)}$${fmt(pnl)}</td>
        <td>${fmt(t.r_multiple||0,1)}R</td>
        <td>${t.exit_reason||'-'}</td>
      </tr>`;
    }
    thtml += '</table>';
    document.getElementById('recent-trades').innerHTML = thtml;
  }
}

async function updateTuner() {
  const t = await fetchJSON('/api/tuner');
  if (!t || t.length === 0) {
    document.getElementById('tuner').innerHTML = '<div class="gray">No adjustments yet</div>';
    return;
  }
  let html = '';
  for (const a of t.reverse().slice(0, 10)) {
    html += `<div class="tuner-item">
      <strong>${a.symbol}</strong>.${a.param}: ${a.old_value} -> <span class="${a.applied?'green':'yellow'}">${a.new_value}</span>
      <br><span class="gray">${a.reason}</span>
    </div>`;
  }
  document.getElementById('tuner').innerHTML = html;
}

async function updateLog() {
  const lines = await fetchJSON('/api/log');
  if (!lines) return;
  let html = '';
  for (const line of lines) {
    let cls = 'info';
    if (line.includes('[ERROR]')) cls = 'err';
    else if (line.includes('[WARNING]')) cls = 'warn';
    html += `<div class="${cls}">${line.replace(/</g,'&lt;')}</div>`;
  }
  document.getElementById('log').innerHTML = html;
  const el = document.getElementById('log');
  el.scrollTop = el.scrollHeight;
}

async function refreshAll() {
  await Promise.all([updateStatus(), updatePrices(), updateJournal(), updateTuner(), updateLog(), updateChallenge()]);
  document.getElementById('refresh-time').textContent =
    'Last refresh: ' + new Date().toLocaleTimeString('he-IL') + ' | Auto-refresh every 15s';
}

refreshAll();
setInterval(refreshAll, 15000);
</script>
</body>
</html>
"""


def main():
    port = 8080
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    _ensure_token()

    print(f"\n  Dashboard running at: http://0.0.0.0:{port}")
    print(f"  Press Ctrl+C to stop\n")
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
