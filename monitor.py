#!/usr/bin/env python3
"""
Server Monitor — Check bot status from any environment via GitHub API.

This script reads the server management results from GitHub (pushed by
the server-manage workflow) and displays them in a readable format.

Usage:
    python monitor.py              # Check latest status
    python monitor.py --watch      # Refresh every 30 seconds
    python monitor.py --trigger    # Trigger a new status check (needs GH token)
"""

import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timezone


REPO = "motitap-dotcom/Tradovate-Bot"
STATUS_FILES = ["server_manage_result.json", "system_status.json"]

# ANSI colors
G = "\033[32m"
R = "\033[31m"
Y = "\033[33m"
B = "\033[34m"
C = "\033[36m"
DIM = "\033[2m"
BOLD = "\033[1m"
X = "\033[0m"
W = 60


def _get_proxies():
    """Auto-detect egress proxy from JAVA_TOOL_OPTIONS (Claude Code env)."""
    java_opts = os.environ.get("JAVA_TOOL_OPTIONS", "")
    match = re.search(r'-Dhttps\.proxyPassword=(jwt_\S+)', java_opts)
    if not match:
        return {}

    proxy_pass = match.group(1)
    match_user = re.search(r'-Dhttps\.proxyUser=(\S+)', java_opts)
    proxy_user = match_user.group(1) if match_user else ""
    match_host = re.search(r'-Dhttps\.proxyHost=(\S+)', java_opts)
    proxy_host = match_host.group(1) if match_host else "21.0.0.81"
    match_port = re.search(r'-Dhttps\.proxyPort=(\S+)', java_opts)
    proxy_port = match_port.group(1) if match_port else "15004"

    return {
        "https": f"http://{proxy_user}:{proxy_pass}@{proxy_host}:{proxy_port}"
    }


def _gh_api(path, method="GET", json_data=None):
    """Make a GitHub API request (handles proxy auto-detection)."""
    import requests

    url = f"https://api.github.com{path}"
    proxies = _get_proxies()
    headers = {"Accept": "application/vnd.github.v3+json"}

    # Try with GH token if available
    gh_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if gh_token:
        headers["Authorization"] = f"Bearer {gh_token}"

    kwargs = {"headers": headers, "timeout": 15}
    if proxies:
        kwargs["proxies"] = proxies
    if json_data:
        kwargs["json"] = json_data

    if method == "POST":
        r = requests.post(url, **kwargs)
    else:
        r = requests.get(url, **kwargs)

    return r


def get_file_from_main(filename):
    """Read a file from the main branch via GitHub API."""
    r = _gh_api(f"/repos/{REPO}/contents/{filename}?ref=main")
    if r.status_code == 200:
        content = r.json().get("content", "")
        decoded = base64.b64decode(content).decode()
        return json.loads(decoded)
    return None


def get_recent_commits(n=5):
    """Get recent commit messages from main."""
    r = _gh_api(f"/repos/{REPO}/commits?sha=main&per_page={n}")
    if r.status_code == 200:
        return [
            {
                "sha": c["sha"][:7],
                "message": c["commit"]["message"].split("\n")[0],
                "date": c["commit"]["author"]["date"][:19],
            }
            for c in r.json()
        ]
    return []


def get_workflow_runs(n=5):
    """Get recent workflow runs."""
    r = _gh_api(f"/repos/{REPO}/actions/runs?per_page={n}")
    if r.status_code == 200:
        return [
            {
                "id": run["id"],
                "name": run["name"],
                "status": run["status"],
                "conclusion": run.get("conclusion", "..."),
                "created_at": run["created_at"][:19],
                "branch": run.get("head_branch", "?"),
            }
            for run in r.json().get("workflow_runs", [])
        ]
    return []


def trigger_workflow(workflow="server-manage.yml", command="full-diagnostic"):
    """Trigger a workflow_dispatch (needs GH_TOKEN with repo scope)."""
    r = _gh_api(
        f"/repos/{REPO}/actions/workflows/{workflow}/dispatches",
        method="POST",
        json_data={"ref": "main", "inputs": {"command": command}},
    )
    return r.status_code == 204


def display():
    """Show the monitoring dashboard."""
    now_utc = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    print(f"\033[2J\033[H", end="")  # clear screen
    print(f"{BOLD}{'=' * W}{X}")
    print(f"{BOLD}  TRADOVATE BOT — REMOTE MONITOR{X}")
    print(f"{'=' * W}")
    print(f"  {DIM}{now_utc}{X}")

    # ── Server Management Result ──
    print(f"\n{BOLD}  Server Status{X}")
    print(f"{'-' * W}")

    manage_result = get_file_from_main("server_manage_result.json")
    if manage_result:
        ts = manage_result.get("timestamp", "?")
        ssh_ok = manage_result.get("ssh_connected", False)
        srv = manage_result.get("server", {})

        if not manage_result.get("ssh_available"):
            print(f"  {R}SSH not configured{X}")
            print(f"  Add SERVER_HOST + SERVER_SSH_KEY in GitHub Settings")
        elif not ssh_ok:
            print(f"  {R}SSH connection failed{X}")
        else:
            bot_active = srv.get("bot_active", False)
            if bot_active:
                print(f"  Bot:     {G}RUNNING{X} (PID {srv.get('bot_pid', '?')})")
            else:
                print(f"  Bot:     {R}STOPPED{X}")

            print(f"  Uptime:  {srv.get('uptime_since', '?')}")
            print(f"  Memory:  {srv.get('memory', '?')}")
            print(f"  Disk:    {srv.get('disk', '?')}")
            print(f"  Commit:  {srv.get('git_commit', '?')}")

            token_valid = srv.get("token_valid")
            if token_valid:
                print(f"  Token:   {G}Valid{X} ({srv.get('token_remaining_min', '?')}m remaining)")
            else:
                print(f"  Token:   {R}Expired/Missing{X}")

            cron = srv.get("cron_active", False)
            print(f"  Cron:    {G if cron else R}{'Active' if cron else 'Inactive'}{X}")

            # Bot logs
            logs = srv.get("bot_logs", "")
            if logs:
                print(f"\n{BOLD}  Recent Bot Logs{X}")
                print(f"{'-' * W}")
                for line in logs.split("\n")[-10:]:
                    if "ERROR" in line or "FAIL" in line:
                        print(f"  {R}{line[:75]}{X}")
                    elif "SIGNAL" in line or "ORDER" in line or "ENTRY" in line:
                        print(f"  {G}{line[:75]}{X}")
                    elif "WARNING" in line:
                        print(f"  {Y}{line[:75]}{X}")
                    else:
                        print(f"  {DIM}{line[:75]}{X}")

        print(f"\n  {DIM}Last check: {ts}{X}")
    else:
        print(f"  {Y}No server management data yet{X}")
        print(f"  Run the server-manage workflow first")

    # ── System Status ──
    print(f"\n{BOLD}  API Status{X}")
    print(f"{'-' * W}")

    sys_status = get_file_from_main("system_status.json")
    if sys_status:
        api_ok = sys_status.get("api_reachable", False)
        auth = sys_status.get("auth_method")
        print(f"  API:     {G if api_ok else R}{'Reachable' if api_ok else 'Down'}{X}")
        print(f"  Auth:    {G if auth else R}{auth or 'Not authenticated'}{X}")

        bal = sys_status.get("balance", {})
        if bal:
            total = bal.get("totalCashValue", "?")
            print(f"  Balance: ${total}")

        positions = sys_status.get("positions", [])
        orders = sys_status.get("orders", [])
        print(f"  Positions: {len(positions)} | Orders: {len(orders)}")

        errors = sys_status.get("errors", [])
        if errors:
            print(f"\n  {R}Errors:{X}")
            for e in errors:
                print(f"    {R}- {e[:65]}{X}")

        print(f"  {DIM}Last check: {sys_status.get('timestamp', '?')}{X}")
    else:
        print(f"  {Y}No system status data{X}")

    # ── Recent Workflow Runs ──
    print(f"\n{BOLD}  Recent Workflows{X}")
    print(f"{'-' * W}")
    runs = get_workflow_runs(5)
    for run in runs:
        status = run["conclusion"] or run["status"]
        color = G if status == "success" else R if status == "failure" else Y
        print(f"  {color}{status:>8}{X} | {run['name'][:30]:<30} | {run['created_at'][11:]}")

    # ── Recent Commits ──
    print(f"\n{BOLD}  Recent Commits (main){X}")
    print(f"{'-' * W}")
    commits = get_recent_commits(5)
    for c in commits:
        print(f"  {DIM}{c['sha']}{X} {c['message'][:50]}")

    print(f"\n{'=' * W}")
    print(f"  {DIM}Refresh: python monitor.py --watch{X}")


def main():
    try:
        import requests  # noqa: F401
    except ImportError:
        print("pip install requests")
        sys.exit(1)

    if "--trigger" in sys.argv:
        cmd = sys.argv[sys.argv.index("--trigger") + 1] if len(sys.argv) > sys.argv.index("--trigger") + 1 else "full-diagnostic"
        if trigger_workflow(command=cmd):
            print(f"Triggered server-manage workflow with command: {cmd}")
        else:
            print("Failed to trigger workflow. Set GH_TOKEN env var with repo scope.")
        return

    if "--watch" in sys.argv:
        try:
            while True:
                display()
                time.sleep(30)
        except KeyboardInterrupt:
            pass
    else:
        display()


if __name__ == "__main__":
    main()
