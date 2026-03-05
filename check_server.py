#!/usr/bin/env python3
"""
Server Health Check
====================
Quick script to verify the bot setup on the server.
Run this directly on the VPS to diagnose issues.

Usage:
    python3 check_server.py          # Full check
    python3 check_server.py --fix    # Auto-fix common issues
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BOT_DIR = Path(__file__).parent
SERVICE = "tradovate-bot"

CHECKS = []


def check(name):
    """Decorator to register a check function."""
    def decorator(fn):
        CHECKS.append((name, fn))
        return fn
    return decorator


def run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)


def ok(msg):
    print(f"  \033[32m[OK]\033[0m {msg}")


def warn(msg):
    print(f"  \033[33m[WARN]\033[0m {msg}")


def fail(msg):
    print(f"  \033[31m[FAIL]\033[0m {msg}")


@check("Environment files")
def check_env():
    issues = []
    env_file = BOT_DIR / ".env"
    if env_file.exists():
        ok(".env exists")
        # Check for required vars
        content = env_file.read_text()
        for var in ["TRADOVATE_USERNAME", "TRADOVATE_PASSWORD"]:
            if var in content and "your_" not in content.split(var + "=")[1].split("\n")[0]:
                ok(f"  {var} is set")
            else:
                fail(f"  {var} is missing or placeholder")
                issues.append(var)
    else:
        fail(".env missing — copy from .env.example and fill in credentials")
        issues.append(".env")

    token_file = BOT_DIR / ".tradovate_token.json"
    if token_file.exists():
        try:
            data = json.loads(token_file.read_text())
            exp = data.get("expirationTime", "")
            if exp:
                exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
                remaining = (exp_dt - datetime.now(timezone.utc)).total_seconds()
                if remaining > 0:
                    ok(f"Token valid ({remaining / 60:.0f} min remaining)")
                else:
                    warn(f"Token EXPIRED ({-remaining / 60:.0f} min ago) — will auto-renew on bot start")
            else:
                warn("Token file has no expiration — may need renewal")
        except Exception as e:
            warn(f"Token file parse error: {e}")
    else:
        warn("No saved token — bot will authenticate on start")

    return len(issues) == 0


@check("Python environment")
def check_python():
    venv = BOT_DIR / "venv" / "bin" / "python"
    if venv.exists():
        ok(f"venv exists: {venv}")
        code, out, err = run(f"{venv} -c 'import requests, websocket; print(\"deps ok\")'")
        if code == 0:
            ok("Dependencies installed")
        else:
            fail(f"Missing dependencies: {err}")
            return False
    else:
        # Check system Python
        code, out, err = run("python3 -c 'import requests, websocket; print(\"deps ok\")'")
        if code == 0:
            ok("System Python has required dependencies")
        else:
            fail("Missing dependencies. Run: pip install -r requirements.txt")
            return False
    return True


@check("Systemd service")
def check_service():
    code, out, _ = run(f"systemctl is-active {SERVICE}")
    if out == "active":
        ok(f"Service {SERVICE} is RUNNING")
        _, pid, _ = run(f"systemctl show {SERVICE} --property=MainPID --value")
        _, uptime, _ = run(f"systemctl show {SERVICE} --property=ActiveEnterTimestamp --value")
        ok(f"  PID: {pid}, since: {uptime}")
        return True
    elif out == "inactive":
        warn(f"Service {SERVICE} is STOPPED")
        _, logs, _ = run(f"journalctl -u {SERVICE} --no-pager -n 5")
        if logs:
            warn(f"  Last logs: {logs[:200]}")
        return False
    elif out == "failed":
        fail(f"Service {SERVICE} has FAILED")
        _, logs, _ = run(f"journalctl -u {SERVICE} --no-pager -n 10")
        if logs:
            fail(f"  Error logs:\n{logs}")
        return False
    else:
        # Service not installed
        service_file = BOT_DIR / "tradovate-bot.service"
        if service_file.exists():
            warn(f"Service not installed. Run: sudo cp {service_file} /etc/systemd/system/ && sudo systemctl enable {SERVICE}")
        else:
            warn("Service file not found")
        return False


@check("Cron job")
def check_cron():
    code, out, _ = run("crontab -l 2>/dev/null")
    if "server_cron" in out:
        ok("Cron job active")
        return True
    else:
        warn("Cron job not installed. Run: see setup_vps.sh")
        return False


@check("Git repository")
def check_git():
    code, out, _ = run(f"cd {BOT_DIR} && git log -1 --format='%h %s (%ar)'")
    if code == 0:
        ok(f"Latest commit: {out}")
    else:
        fail("Not a git repository")
        return False

    code, branch, _ = run(f"cd {BOT_DIR} && git branch --show-current")
    ok(f"Branch: {branch}")

    # Check if behind remote
    run(f"cd {BOT_DIR} && git fetch origin main 2>/dev/null")
    code, out, _ = run(f"cd {BOT_DIR} && git rev-list HEAD..origin/main --count 2>/dev/null")
    if code == 0 and out and int(out) > 0:
        warn(f"Behind remote by {out} commit(s). Run: git pull origin main")
    elif code == 0:
        ok("Up to date with remote")

    return True


@check("Tradovate API connectivity")
def check_api():
    try:
        import requests
    except ImportError:
        fail("requests not installed")
        return False

    for label, url in [("Demo", "https://demo.tradovateapi.com/v1"), ("Live", "https://live.tradovateapi.com/v1")]:
        try:
            r = requests.post(f"{url}/auth/accesstokenrequest", json={"name": "test"}, timeout=10)
            if r.status_code in (200, 401, 403):
                ok(f"{label} API reachable (status={r.status_code})")
            else:
                warn(f"{label} API returned {r.status_code}")
        except Exception as e:
            fail(f"{label} API unreachable: {e}")
            return False
    return True


@check("System resources")
def check_resources():
    _, disk, _ = run("df -h / | tail -1 | awk '{print $5}'")
    _, mem, _ = run("free -m | awk '/^Mem:/{printf \"%dMB/%dMB (%.0f%%)\", $3, $2, $3/$2*100}'")
    ok(f"Disk: {disk}, Memory: {mem}")

    if disk and int(disk.replace("%", "")) > 90:
        warn("Disk usage > 90%!")
        return False
    return True


def main():
    fix_mode = "--fix" in sys.argv

    print()
    print("=" * 50)
    print("  Tradovate Bot — Server Health Check")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    results = {}
    for name, fn in CHECKS:
        print(f"\n--- {name} ---")
        try:
            results[name] = fn()
        except Exception as e:
            fail(f"Check error: {e}")
            results[name] = False

    # Summary
    total = len(results)
    passed = sum(1 for v in results.values() if v)
    print(f"\n{'=' * 50}")
    print(f"  Results: {passed}/{total} checks passed")

    if passed == total:
        print("  \033[32mAll checks passed!\033[0m")
    else:
        failed_checks = [k for k, v in results.items() if not v]
        print(f"  \033[31mFailed: {', '.join(failed_checks)}\033[0m")

    if fix_mode and passed < total:
        print(f"\n--- Auto-fix ---")
        if not results.get("Systemd service"):
            print("  Restarting service...")
            run(f"systemctl restart {SERVICE}")
            code, out, _ = run(f"systemctl is-active {SERVICE}")
            if out == "active":
                ok("Service restarted successfully")
            else:
                fail("Service restart failed")

    print()
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
