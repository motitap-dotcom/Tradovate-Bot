#!/usr/bin/env python3
"""
Publish Dashboard to GitHub Pages
====================================
Generates a self-contained static HTML dashboard with all current bot data
embedded, and pushes it to the gh-pages branch for browser viewing.

Usage:
    python publish_dashboard.py            # One-time publish
    python publish_dashboard.py --loop     # Publish every 60 seconds
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BOT_DIR, "bot.log")
TOKEN_FILE = os.path.join(BOT_DIR, ".tradovate_token.json")
JOURNAL_FILE = os.path.join(BOT_DIR, "trade_journal.json")
TUNER_LOG = os.path.join(BOT_DIR, "tuner_log.json")
OUTPUT_DIR = os.path.join(BOT_DIR, ".gh-pages")


def collect_data() -> dict:
    """Collect all dashboard data into a single dict."""
    trading = _last_status()

    # Enrich trading data with live API balance if token is available
    api_balance = _api_balance()
    if api_balance:
        trading["balance"] = api_balance.get("totalCashValue", trading.get("balance", 50000))
        trading["day_pnl"] = api_balance.get("totalCashValue", 50000) - api_balance.get("totalCashValueSOD", 50000) + api_balance.get("openPnL", 0)
        # Distance to floor = equity - (peak - max_dd)
        equity = api_balance.get("netLiq", trading.get("balance", 50000))
        trading["to_floor"] = equity - (50000 - 2500)  # drawdown floor starts at account_size - max_dd
        trading["api_live"] = True

    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bot": _bot_status(),
        "token": _token_status(),
        "trading": trading,
        "journal": _journal_data(),
        "tuner": _tuner_data(),
        "log": _recent_log(),
        "activity": _recent_activity(),
    }
    return data


def _ensure_fresh_token():
    """Renew saved token if it's close to expiry or expired."""
    if not os.path.exists(TOKEN_FILE):
        return
    try:
        import requests
        with open(TOKEN_FILE) as f:
            t = json.load(f)
        token = t.get("accessToken", "")
        exp = t.get("expirationTime", "")
        if not token or not exp:
            return
        exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        remaining = (exp_dt - datetime.now(timezone.utc)).total_seconds()
        if remaining > 900:  # More than 15 min left — no need to renew
            return
        # Renew the token
        base_url = "https://demo.tradovateapi.com/v1"
        resp = requests.post(
            f"{base_url}/auth/renewaccesstoken",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/json",
                     "Content-Type": "application/json"},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            t["accessToken"] = data.get("accessToken", token)
            if data.get("expirationTime"):
                t["expirationTime"] = data["expirationTime"]
            with open(TOKEN_FILE, "w") as f:
                json.dump(t, f, indent=2)
    except Exception:
        pass


def _api_balance() -> dict:
    """Fetch live balance from Tradovate API using saved token."""
    if not os.path.exists(TOKEN_FILE):
        return {}
    try:
        import requests
        # Renew token if needed before using it
        _ensure_fresh_token()

        with open(TOKEN_FILE) as f:
            t = json.load(f)
        token = t.get("accessToken", "")
        account_id = t.get("accountId")
        if not token or not account_id:
            return {}

        # Check if token is expired (after renewal attempt)
        exp = t.get("expirationTime", "")
        if exp:
            exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) > exp_dt:
                return {}

        resp = requests.post(
            "https://demo.tradovateapi.com/v1/cashBalance/getcashbalancesnapshot",
            json={"accountId": account_id},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return {}


def _bot_status() -> dict:
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "bot.py"], text=True, stderr=subprocess.DEVNULL
        )
        pids = [int(p) for p in out.strip().split("\n") if p]
        return {"running": True, "pid": pids[0] if pids else 0}
    except subprocess.CalledProcessError:
        return {"running": False, "pid": 0}


def _token_status() -> dict:
    if not os.path.exists(TOKEN_FILE):
        return {"status": "missing", "minutes_remaining": 0}
    try:
        with open(TOKEN_FILE) as f:
            t = json.load(f)
        exp = t.get("expirationTime", "")
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


def _last_status() -> dict:
    if not os.path.exists(LOG_FILE):
        return {}
    try:
        out = subprocess.check_output(
            ["tail", "-50", LOG_FILE], text=True, stderr=subprocess.DEVNULL
        )
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


def _journal_data() -> dict:
    if not os.path.exists(JOURNAL_FILE):
        return {"summary": {}, "trades": [], "lessons": []}
    try:
        with open(JOURNAL_FILE) as f:
            jdata = json.load(f)
        summary = jdata.get("summary", {})
        trades = jdata.get("trades", [])
        closed = [t for t in trades if t.get("status") == "closed"]

        # Generate lessons
        lessons = []
        try:
            sys.path.insert(0, BOT_DIR)
            from trade_journal import TradeJournal
            tj = TradeJournal()
            lessons = tj.generate_lessons()
        except Exception:
            pass

        # By-symbol breakdown
        by_symbol = {}
        for t in closed:
            if t.get("pnl") is not None:
                sym = t["symbol"]
                by_symbol.setdefault(sym, {"trades": 0, "wins": 0, "pnl": 0})
                by_symbol[sym]["trades"] += 1
                by_symbol[sym]["pnl"] += t["pnl"]
                if t["pnl"] > 0:
                    by_symbol[sym]["wins"] += 1

        return {
            "summary": summary,
            "recent_trades": closed[-10:],
            "lessons": lessons,
            "by_symbol": by_symbol,
        }
    except Exception:
        return {"summary": {}, "trades": [], "lessons": []}


def _tuner_data() -> list:
    if not os.path.exists(TUNER_LOG):
        return []
    try:
        with open(TUNER_LOG) as f:
            return json.load(f)[-10:]
    except Exception:
        return []


def _recent_log() -> list:
    if not os.path.exists(LOG_FILE):
        return []
    try:
        out = subprocess.check_output(
            ["tail", "-40", LOG_FILE], text=True, stderr=subprocess.DEVNULL
        )
        return out.strip().split("\n")
    except subprocess.CalledProcessError:
        return []


def _recent_activity() -> list:
    if not os.path.exists(LOG_FILE):
        return []
    try:
        out = subprocess.check_output(
            ["tail", "-200", LOG_FILE], text=True, stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError:
        return []
    items = []
    for line in out.strip().split("\n"):
        if any(kw in line for kw in [
            "SIGNAL:", "LOCKED", "bracket order", "Force close",
            "Journal: ENTRY", "Journal: EXIT", "Auto-tune"
        ]):
            items.append(line.strip())
    return items[-12:]


def generate_html(data: dict) -> str:
    """Generate a complete self-contained HTML dashboard."""
    data_json = json.dumps(data, default=str, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="60">
<title>Tradovate Bot Dashboard</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0d1117; color: #c9d1d9; min-height: 100vh; }}
.container {{ max-width: 1200px; margin: 0 auto; padding: 16px; }}
.header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }}
h1 {{ font-size: 1.5em; color: #58a6ff; }}
.header-time {{ color: #8b949e; font-size: 0.85em; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; margin-bottom: 16px; }}
.card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }}
.card h2 {{ font-size: 0.9em; color: #8b949e; margin-bottom: 12px; text-transform: uppercase; letter-spacing: 1px; }}
.big-number {{ font-size: 2.2em; font-weight: 700; }}
.green {{ color: #3fb950; }}
.red {{ color: #f85149; }}
.yellow {{ color: #d29922; }}
.blue {{ color: #58a6ff; }}
.gray {{ color: #8b949e; }}
.row {{ display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid #21262d; }}
.row:last-child {{ border-bottom: none; }}
.label {{ color: #8b949e; }}
.value {{ font-weight: 600; }}
.status-badge {{ display: inline-block; padding: 4px 14px; border-radius: 16px; font-size: 0.85em; font-weight: 700; }}
.status-badge.running {{ background: #0e4429; color: #3fb950; border: 1px solid #238636; }}
.status-badge.stopped {{ background: #490d0d; color: #f85149; border: 1px solid #da3633; }}
.progress-bar {{ background: #21262d; border-radius: 6px; height: 12px; margin-top: 10px; overflow: hidden; }}
.progress-fill {{ height: 100%; border-radius: 6px; transition: width 0.3s; }}
.progress-label {{ display: flex; justify-content: space-between; margin-top: 6px; font-size: 0.85em; }}
.lessons {{ list-style: none; }}
.lessons li {{ padding: 10px 14px; margin-bottom: 8px; background: #1c2128; border-radius: 6px; border-right: 3px solid #58a6ff; font-size: 0.9em; line-height: 1.6; }}
.log {{ background: #0d1117; border-radius: 6px; padding: 12px; font-family: 'Fira Code', 'Courier New', monospace; font-size: 0.72em; max-height: 350px; overflow-y: auto; direction: ltr; text-align: left; line-height: 1.7; }}
.log .err {{ color: #f85149; }}
.log .warn {{ color: #d29922; }}
.log .info {{ color: #6e7681; }}
.log .signal {{ color: #3fb950; font-weight: 600; }}
.trades-table {{ width: 100%; border-collapse: collapse; font-size: 0.85em; direction: ltr; text-align: left; }}
.trades-table th {{ text-align: left; padding: 8px; color: #8b949e; border-bottom: 2px solid #30363d; font-weight: 600; }}
.trades-table td {{ padding: 8px; border-bottom: 1px solid #21262d; }}
.trades-table tr:hover {{ background: #1c2128; }}
.badge {{ display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 0.8em; font-weight: 600; }}
.badge.buy {{ background: #0e4429; color: #3fb950; }}
.badge.sell {{ background: #490d0d; color: #f85149; }}
.tuner-item {{ padding: 10px; margin-bottom: 6px; background: #1c2128; border-radius: 6px; font-size: 0.85em; }}
.tuner-item .param {{ color: #d29922; font-weight: 600; }}
.tuner-item .reason {{ color: #8b949e; font-size: 0.9em; margin-top: 4px; }}
.symbol-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; }}
.symbol-card {{ text-align: center; padding: 14px 10px; background: #0d1117; border-radius: 8px; border: 1px solid #21262d; }}
.symbol-card .sym {{ font-size: 0.85em; color: #8b949e; font-weight: 600; }}
.symbol-card .stat {{ font-size: 1.3em; font-weight: 700; margin: 4px 0; }}
.symbol-card .detail {{ font-size: 0.8em; color: #8b949e; }}
.activity-item {{ padding: 6px 0; border-bottom: 1px solid #21262d; font-size: 0.85em; direction: ltr; text-align: left; }}
.activity-item:last-child {{ border-bottom: none; }}
.act-signal {{ color: #3fb950; }}
.act-lock {{ color: #f85149; }}
.act-exit {{ color: #d29922; }}
.refresh-info {{ text-align: center; color: #484f58; font-size: 0.8em; margin-top: 16px; padding: 10px; }}
.refresh-info a {{ color: #58a6ff; text-decoration: none; }}
</style>
</head>
<body>
<div class="container">

<div class="header">
  <h1>Tradovate Bot Dashboard</h1>
  <div class="header-time" id="header-time"></div>
</div>

<!-- Top cards -->
<div class="grid">
  <div class="card" id="status-card"></div>
  <div class="card" id="balance-card"></div>
  <div class="card" id="challenge-card"></div>
</div>

<!-- Symbol Performance -->
<div class="card" style="margin-bottom:16px" id="symbols-card"></div>

<!-- Journal + Lessons -->
<div class="grid">
  <div class="card" id="journal-card"></div>
  <div class="card" id="lessons-card"></div>
</div>

<!-- Recent Trades -->
<div class="card" style="margin-bottom:16px" id="trades-card"></div>

<!-- Tuner + Activity -->
<div class="grid">
  <div class="card" id="tuner-card"></div>
  <div class="card" id="activity-card"></div>
</div>

<!-- Log -->
<div class="card" style="margin-bottom:16px" id="log-card"></div>

<div class="refresh-info" id="refresh-info"></div>
</div>

<script>
const DATA = {data_json};

function fmt(n, d=2) {{ return Number(n||0).toLocaleString('en-US', {{minimumFractionDigits:d, maximumFractionDigits:d}}); }}
function pnlC(v) {{ return (v||0) >= 0 ? 'green' : 'red'; }}
function pnlS(v) {{ return (v||0) >= 0 ? '+' : ''; }}

function render() {{
  const d = DATA;
  const bot = d.bot || {{}};
  const tok = d.token || {{}};
  const t = d.trading || {{}};
  const j = d.journal || {{}};
  const sum = j.summary || {{}};

  // Generated time
  const genTime = new Date(d.generated_at);
  document.getElementById('header-time').textContent =
    'Updated: ' + genTime.toLocaleString('he-IL', {{timeZone: 'Asia/Jerusalem'}});

  // Status card
  document.getElementById('status-card').innerHTML = `
    <h2>Bot Status</h2>
    <div style="text-align:center;margin:10px 0">
      <span class="status-badge ${{bot.running ? 'running' : 'stopped'}}">
        ${{bot.running ? '\\u25CF RUNNING' : '\\u25CF STOPPED'}}
      </span>
    </div>
    <div class="row"><span class="label">PID</span><span class="value">${{bot.pid || '-'}}</span></div>
    <div class="row"><span class="label">Token</span>
      <span class="value ${{tok.status === 'valid' ? 'green' : 'red'}}">${{tok.status}} (${{tok.minutes_remaining || 0}} min)</span></div>
    <div class="row"><span class="label">Trades Today</span><span class="value">${{t.trades_today || 0}}/${{t.max_trades || 12}}</span></div>
    <div class="row"><span class="label">Open Contracts</span><span class="value">${{t.open_contracts || 0}}/${{t.max_contracts || 10}}</span></div>
    ${{t.locked ? '<div style="text-align:center;margin-top:10px"><span class="status-badge stopped">TRADING LOCKED</span></div>' : ''}}
  `;

  // Balance card
  const bal = t.balance || 50000;
  const pnl = t.day_pnl || 0;
  const floor = t.to_floor || 0;
  document.getElementById('balance-card').innerHTML = `
    <h2>Account Balance</h2>
    <div class="big-number" style="text-align:center;margin:8px 0">$${{fmt(bal)}}</div>
    <div class="row"><span class="label">Day P&L</span>
      <span class="value ${{pnlC(pnl)}}">${{pnlS(pnl)}}$${{fmt(pnl)}}</span></div>
    <div class="row"><span class="label">To Drawdown Floor</span>
      <span class="value ${{floor < 500 ? 'red' : floor < 1000 ? 'yellow' : 'green'}}">$${{fmt(floor)}}</span></div>
    <div class="row"><span class="label">Last Update</span>
      <span class="value gray">${{t.timestamp || 'N/A'}}</span></div>
  `;

  // Challenge progress
  const profit = bal - 50000;
  const target = 5000;
  const pct = Math.max(0, Math.min(100, (profit / target) * 100));
  const barColor = profit >= 0 ? '#3fb950' : '#f85149';
  document.getElementById('challenge-card').innerHTML = `
    <h2>FundedNext Challenge</h2>
    <div class="row"><span class="label">Starting Balance</span><span class="value">$50,000</span></div>
    <div class="row"><span class="label">Profit Target</span><span class="value">$5,000</span></div>
    <div class="row"><span class="label">Current Profit</span>
      <span class="value ${{pnlC(profit)}}">${{pnlS(profit)}}$${{fmt(profit)}}</span></div>
    <div class="progress-bar"><div class="progress-fill" style="width:${{Math.max(0,pct)}}%;background:${{barColor}}"></div></div>
    <div class="progress-label"><span>${{fmt(pct,1)}}% complete</span><span>$${{fmt(Math.max(0,5000-profit),0)}} remaining</span></div>
    <div style="margin-top:10px">
      <div class="row"><span class="label">Max Trailing DD</span><span class="value">$2,500</span></div>
      <div class="row"><span class="label">Daily Loss Limit</span><span class="value">$2,500</span></div>
      <div class="row"><span class="label">Close By</span><span class="value">4:59 PM ET</span></div>
    </div>
  `;

  // Symbol performance
  const bySym = j.by_symbol || {{}};
  if (Object.keys(bySym).length > 0) {{
    let symHtml = '<h2>Performance by Symbol</h2><div class="symbol-grid">';
    for (const [sym, s] of Object.entries(bySym)) {{
      const wr = s.trades > 0 ? ((s.wins / s.trades) * 100).toFixed(0) : '0';
      symHtml += `<div class="symbol-card">
        <div class="sym">${{sym}}</div>
        <div class="stat ${{pnlC(s.pnl)}}">${{pnlS(s.pnl)}}$${{fmt(s.pnl)}}</div>
        <div class="detail">${{s.trades}} trades | ${{wr}}% WR</div>
      </div>`;
    }}
    symHtml += '</div>';
    document.getElementById('symbols-card').innerHTML = symHtml;
  }} else {{
    document.getElementById('symbols-card').innerHTML = '<h2>Performance by Symbol</h2><div class="gray">No completed trades yet</div>';
  }}

  // Journal summary
  if (sum.total_trades > 0) {{
    document.getElementById('journal-card').innerHTML = `
      <h2>Trade Journal Summary</h2>
      <div class="row"><span class="label">Total Trades</span><span class="value">${{sum.total_trades}}</span></div>
      <div class="row"><span class="label">Win Rate</span>
        <span class="value ${{(sum.win_rate||0) >= 0.5 ? 'green' : 'yellow'}}">${{((sum.win_rate||0)*100).toFixed(0)}}%</span></div>
      <div class="row"><span class="label">Wins / Losses</span>
        <span class="value"><span class="green">${{sum.wins||0}}W</span> / <span class="red">${{sum.losses||0}}L</span></span></div>
      <div class="row"><span class="label">Total P&L</span>
        <span class="value ${{pnlC(sum.total_pnl)}}">${{pnlS(sum.total_pnl)}}$${{fmt(sum.total_pnl)}}</span></div>
      <div class="row"><span class="label">Profit Factor</span><span class="value">${{fmt(sum.profit_factor||0)}}</span></div>
      <div class="row"><span class="label">Expectancy</span>
        <span class="value ${{pnlC(sum.expectancy)}}">${{pnlS(sum.expectancy)}}$${{fmt(sum.expectancy)}}/trade</span></div>
      <div class="row"><span class="label">Avg R-Multiple</span><span class="value">${{fmt(sum.avg_r_multiple||0)}}R</span></div>
      <div class="row"><span class="label">Best Trade</span><span class="value green">+$${{fmt(sum.best_trade||0)}}</span></div>
      <div class="row"><span class="label">Worst Trade</span><span class="value red">$${{fmt(sum.worst_trade||0)}}</span></div>
    `;
  }} else {{
    document.getElementById('journal-card').innerHTML = '<h2>Trade Journal Summary</h2><div class="gray" style="padding:20px;text-align:center">No closed trades yet. Waiting for market open...</div>';
  }}

  // Lessons
  const lessons = j.lessons || [];
  if (lessons.length > 0 && !lessons[0].includes('Not enough trades')) {{
    document.getElementById('lessons-card').innerHTML =
      '<h2>Lessons & Recommendations</h2><ul class="lessons">' +
      lessons.map(l => `<li>${{l}}</li>`).join('') + '</ul>';
  }} else {{
    document.getElementById('lessons-card').innerHTML = '<h2>Lessons & Recommendations</h2><div class="gray" style="padding:20px;text-align:center">Lessons will appear after enough trades are completed</div>';
  }}

  // Recent trades table
  const trades = j.recent_trades || [];
  if (trades.length > 0) {{
    let thtml = '<h2>Recent Trades</h2><table class="trades-table"><tr><th>Date</th><th>Symbol</th><th>Dir</th><th>Qty</th><th>Entry</th><th>Exit</th><th>P&L</th><th>R</th><th>Reason</th></tr>';
    for (const tr of [...trades].reverse()) {{
      const pnl = tr.pnl || 0;
      const dir = (tr.direction||'Buy') === 'Buy' ? 'buy' : 'sell';
      thtml += `<tr>
        <td>${{(tr.date||'').slice(5)}}</td>
        <td><strong>${{tr.symbol}}</strong></td>
        <td><span class="badge ${{dir}}">${{tr.direction||'?'}}</span></td>
        <td>${{tr.qty||1}}</td>
        <td>${{fmt(tr.entry_price||0)}}</td>
        <td>${{fmt(tr.exit_price||0)}}</td>
        <td class="${{pnlC(pnl)}}">${{pnlS(pnl)}}$${{fmt(pnl)}}</td>
        <td>${{fmt(tr.r_multiple||0,1)}}R</td>
        <td class="gray">${{tr.exit_reason||'-'}}</td>
      </tr>`;
    }}
    thtml += '</table>';
    document.getElementById('trades-card').innerHTML = thtml;
  }} else {{
    document.getElementById('trades-card').innerHTML = '<h2>Recent Trades</h2><div class="gray" style="padding:20px;text-align:center">No trades recorded yet</div>';
  }}

  // Auto-tuner
  const tuner = d.tuner || [];
  if (tuner.length > 0) {{
    let tHtml = '<h2>Auto-Tuner Adjustments</h2>';
    for (const a of [...tuner].reverse()) {{
      tHtml += `<div class="tuner-item">
        <span class="param">${{a.symbol}}.${{a.param}}</span>: ${{a.old_value}} \\u2192 <span class="${{a.applied ? 'green' : 'yellow'}}">${{a.new_value}}</span>
        <div class="reason">${{a.reason}}</div>
      </div>`;
    }}
    document.getElementById('tuner-card').innerHTML = tHtml;
  }} else {{
    document.getElementById('tuner-card').innerHTML = '<h2>Auto-Tuner Adjustments</h2><div class="gray" style="padding:20px;text-align:center">No adjustments made yet</div>';
  }}

  // Activity
  const activity = d.activity || [];
  if (activity.length > 0) {{
    let aHtml = '<h2>Recent Activity</h2>';
    for (const line of activity) {{
      const short = line.length > 20 ? line.slice(20) : line;
      let cls = '';
      if (line.includes('SIGNAL:') || line.includes('ENTRY')) cls = 'act-signal';
      else if (line.includes('LOCKED')) cls = 'act-lock';
      else if (line.includes('EXIT') || line.includes('Force')) cls = 'act-exit';
      aHtml += `<div class="activity-item ${{cls}}">${{short}}</div>`;
    }}
    document.getElementById('activity-card').innerHTML = aHtml;
  }} else {{
    document.getElementById('activity-card').innerHTML = '<h2>Recent Activity</h2><div class="gray" style="padding:20px;text-align:center">No trading activity yet</div>';
  }}

  // Log
  const logLines = d.log || [];
  if (logLines.length > 0) {{
    let lHtml = '<h2>Bot Log (Latest)</h2><div class="log">';
    for (const line of logLines) {{
      let cls = 'info';
      if (line.includes('[ERROR]')) cls = 'err';
      else if (line.includes('[WARNING]')) cls = 'warn';
      else if (line.includes('SIGNAL:')) cls = 'signal';
      lHtml += `<div class="${{cls}}">${{line.replace(/</g, '&lt;')}}</div>`;
    }}
    lHtml += '</div>';
    document.getElementById('log-card').innerHTML = lHtml;
    const logEl = document.querySelector('.log');
    if (logEl) logEl.scrollTop = logEl.scrollHeight;
  }} else {{
    document.getElementById('log-card').innerHTML = '<h2>Bot Log</h2><div class="gray" style="padding:20px;text-align:center">No log entries</div>';
  }}

  // Refresh info with countdown
  const refreshSeconds = 60;
  let countdown = refreshSeconds;
  function updateCountdown() {{
    const now = new Date();
    const age = Math.floor((now - genTime) / 1000);
    const stale = age > 120;
    document.getElementById('refresh-info').innerHTML =
      `<span style="color:${{stale ? '#f85149' : '#3fb950'}}">\\u25CF</span> ` +
      `Updated ${{age < 60 ? age + 's ago' : Math.floor(age/60) + 'm ago'}} ` +
      `(${{genTime.toLocaleString('he-IL', {{timeZone: 'Asia/Jerusalem'}})}}) ` +
      `| Auto-refresh in ${{countdown}}s`;
    countdown = Math.max(0, countdown - 1);
  }}
  updateCountdown();
  setInterval(updateCountdown, 1000);
}}

render();
</script>
</body>
</html>"""


def publish():
    """Generate HTML and push to gh-pages branch."""
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Collecting data...")
    data = collect_data()

    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Generating HTML...")
    html = generate_html(data)

    # Write to temp location
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    html_path = os.path.join(OUTPUT_DIR, "index.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Pushing to gh-pages...")

    # Use git worktree or create orphan branch approach
    try:
        _push_gh_pages(html_path)
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Published successfully!")
        return True
    except Exception as e:
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Push failed: {e}")
        return False


def _push_gh_pages(html_path: str):
    """Copy index.html to docs/ and push to current branch."""
    docs_dir = os.path.join(BOT_DIR, "docs")
    os.makedirs(docs_dir, exist_ok=True)

    import shutil
    shutil.copy2(html_path, os.path.join(docs_dir, "index.html"))

    # Create .nojekyll
    with open(os.path.join(docs_dir, ".nojekyll"), "w") as f:
        f.write("")

    # Auto-commit and push if in loop mode
    if "--loop" in sys.argv:
        try:
            subprocess.run(
                ["git", "add", "docs/index.html", "docs/.nojekyll"],
                check=True, capture_output=True, cwd=BOT_DIR
            )
            # Check if there are changes
            status = subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                capture_output=True, cwd=BOT_DIR
            )
            if status.returncode != 0:
                # There are staged changes
                ts = datetime.now(timezone.utc).strftime('%H:%M UTC')
                subprocess.run(
                    ["git", "-c", "commit.gpgsign=false",
                     "commit", "-m", f"Dashboard update {ts}"],
                    check=True, capture_output=True, cwd=BOT_DIR
                )
                # Get current branch
                branch = subprocess.check_output(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    text=True, cwd=BOT_DIR
                ).strip()
                for attempt in range(4):
                    result = subprocess.run(
                        ["git", "push", "-u", "origin", branch],
                        capture_output=True, text=True, cwd=BOT_DIR
                    )
                    if result.returncode == 0:
                        print(f"  Pushed to {branch}")
                        return
                    wait = 2 ** (attempt + 1)
                    print(f"  Push attempt {attempt+1} failed, retrying in {wait}s...")
                    time.sleep(wait)
                print(f"  Push failed after retries")
            else:
                print(f"  No changes to push")
        except Exception as e:
            print(f"  Git push error: {e}")
    else:
        print(f"  Dashboard written to docs/index.html")


def main():
    if "--loop" in sys.argv:
        interval = 60
        print(f"Publishing dashboard every {interval}s. Press Ctrl+C to stop.")
        try:
            while True:
                publish()
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        publish()


if __name__ == "__main__":
    main()
