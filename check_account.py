#!/usr/bin/env python3
"""
Quick account status & recent transactions checker.
Connects to Tradovate API, prints balance, positions, and last 24h activity.
"""

import json
import sys
from datetime import datetime, timezone, timedelta
from tradovate_api import TradovateAPI

def main():
    api = TradovateAPI()
    print("Authenticating...")
    if not api.authenticate():
        print("ERROR: Authentication failed")
        sys.exit(1)
    print(f"Authenticated as {api.account_spec} (userId={api.user_id}, accountId={api.account_id})\n")

    # --- Account info ---
    accounts = api.get_accounts()
    if accounts:
        for acc in accounts:
            print(f"Account: {acc.get('name')} | id={acc.get('id')} | active={acc.get('active')}")
    print()

    # --- Cash balance ---
    balance = api.get_cash_balance()
    if balance:
        print("=== Cash Balance ===")
        for key in ["totalCashValue", "netLiq", "openPnL", "realizedPnL",
                     "initialMargin", "maintenanceMargin", "totalUsedMargin"]:
            val = balance.get(key)
            if val is not None:
                print(f"  {key}: ${val:,.2f}")
        print()

    # --- Open positions ---
    positions = api.get_positions()
    print(f"=== Open Positions ({len(positions)}) ===")
    for p in positions:
        net = p.get("netPos", 0)
        if net != 0:
            contract_id = p.get("contractId")
            contract = api._get(f"/contract/item?id={contract_id}")
            name = contract.get("name", f"cid={contract_id}") if contract else f"cid={contract_id}"
            print(f"  {name}: {net:+d} contracts | avgPrice={p.get('netPrice')} | timestamp={p.get('timestamp')}")
    if not any(p.get("netPos", 0) != 0 for p in positions):
        print("  (no open positions)")
    print()

    # --- Orders (working) ---
    orders = api._get("/order/list") or []
    working = [o for o in orders if o.get("ordStatus") in ("Working", "Accepted")]
    print(f"=== Working Orders ({len(working)}) ===")
    for o in working:
        print(f"  {o.get('action')} {o.get('orderQty')} {o.get('symbol')} | type={o.get('orderType')} | status={o.get('ordStatus')}")
    if not working:
        print("  (no working orders)")
    print()

    # --- Recent fills (last 24h) ---
    fills = api.get_fills()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    recent_fills = []
    for f in fills:
        ts = f.get("timestamp", "")
        try:
            fill_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if fill_time >= cutoff:
                recent_fills.append(f)
        except (ValueError, TypeError):
            recent_fills.append(f)  # include if we can't parse

    print(f"=== Fills in Last 24h ({len(recent_fills)}) ===")
    for f in recent_fills:
        contract_id = f.get("contractId")
        contract = api._get(f"/contract/item?id={contract_id}")
        name = contract.get("name", f"cid={contract_id}") if contract else f"cid={contract_id}"
        print(f"  {f.get('timestamp')} | {f.get('action')} {f.get('qty', f.get('contractQuantity', '?'))} {name} @ {f.get('price', f.get('fillPrice', '?'))}")
    if not recent_fills:
        print("  (no fills in last 24h)")
    print()

    # --- All orders from last 24h ---
    all_orders = orders
    recent_orders = []
    for o in all_orders:
        ts = o.get("timestamp", "")
        try:
            order_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if order_time >= cutoff:
                recent_orders.append(o)
        except (ValueError, TypeError):
            recent_orders.append(o)

    print(f"=== All Orders in Last 24h ({len(recent_orders)}) ===")
    for o in recent_orders:
        print(f"  {o.get('timestamp')} | {o.get('action')} {o.get('orderQty')} {o.get('symbol')} | type={o.get('orderType')} | status={o.get('ordStatus')} | fillPrice={o.get('avgFillPrice', '-')}")
    if not recent_orders:
        print("  (no orders in last 24h)")

if __name__ == "__main__":
    main()
