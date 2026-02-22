# Tradovate API — Order System Research

> Research date: 2026-02-22
> Sources: Tradovate API docs, community forums, GitHub examples

---

## 1. Order Types Available via API

| Type | Description | Key Fields |
|------|-------------|------------|
| **Market** | Execute immediately at best price | `orderType: "Market"` |
| **Limit** | Execute at specified price or better | `orderType: "Limit"`, `price` |
| **Stop** | Becomes market when stop price hit | `orderType: "Stop"`, `stopPrice` |
| **StopLimit** | Becomes limit when stop price hit | `orderType: "StopLimit"`, `stopPrice`, `price` |
| **TrailingStop** | Stop follows price at fixed distance | `orderType: "TrailingStop"`, `stopPrice` |
| **TrailingStopLimit** | Trailing stop → limit order | `orderType: "TrailingStopLimit"`, `stopPrice`, `price` |

---

## 2. API Endpoints for Orders

### Base URLs
- **Demo**: `https://demo.tradovateapi.com/v1`
- **Live**: `https://live.tradovateapi.com/v1`

### Endpoints

| Endpoint | Purpose | When to Use |
|----------|---------|-------------|
| `POST /order/placeOrder` | Single order, no brackets | Simple market/limit entry |
| `POST /order/placeOSO` | Bracket order (entry + SL + TP) | **Main endpoint — what our bot uses** |
| `POST /order/placeOCO` | SL + TP pair on existing position | Add brackets after position is open |
| `POST /orderStrategy/startOrderStrategy` | Advanced strategy with trailing stops | Auto-trail, breakeven, complex brackets |
| `POST /orderStrategy/modifyOrderStrategy` | Modify active strategy | Convert stop → trailing stop mid-trade |
| `POST /order/cancelorder` | Cancel a working order | `{ "orderId": 12345 }` |

---

## 3. placeOSO — Bracket Order (Our Main Method)

This is what our bot uses. Entry order + 2 exit brackets (SL + TP) as an OCO pair.

### Structure
```json
{
  "accountSpec": "your_account_name",
  "accountId": 12345,
  "action": "Buy",
  "symbol": "NQM5",
  "orderQty": 1,
  "orderType": "Market",
  "timeInForce": "Day",
  "isAutomated": true,
  "bracket1": {
    "action": "Sell",
    "orderType": "Stop",
    "stopPrice": 18000.00
  },
  "bracket2": {
    "action": "Sell",
    "orderType": "Limit",
    "price": 18200.00
  }
}
```

### How it Works
1. **Entry order** fills (Market or Limit)
2. **bracket1** and **bracket2** are sent as an OCO pair
3. When one bracket fills → the other is **automatically cancelled**
4. bracket1 = Stop Loss, bracket2 = Take Profit (convention)

### Key Rules
- `isAutomated: true` — **required** for bot/algorithmic orders
- Opposite action: if entry is `Buy`, brackets must be `Sell` (and vice versa)
- Stop price must not be too close to current market price (causes rejection)
- `timeInForce` options: `"Day"`, `"GTC"` (Good Till Cancel), `"GTD"` (Good Till Date)

### Our Bot's Implementation (tradovate_api.py:185-243)
```python
def place_bracket_order(self, symbol, action, qty, entry_price,
                        stop_price, take_profit_price, order_type="Market"):
    opposite_action = "Sell" if action == "Buy" else "Buy"
    payload = {
        "accountSpec": self.account_spec,
        "accountId": self.account_id,
        "action": action,
        "symbol": symbol,
        "orderQty": qty,
        "orderType": order_type,
        "timeInForce": "Day",
        "isAutomated": True,
        "bracket1": {
            "action": opposite_action,
            "orderType": "Stop",
            "stopPrice": stop_price,
        },
        "bracket2": {
            "action": opposite_action,
            "orderType": "Limit",
            "price": take_profit_price,
        },
    }
    return self._post("/order/placeOSO", payload)
```
**Status: Already correctly implemented.**

---

## 4. placeOCO — Add SL/TP to Existing Position

When you already have an open position (e.g., from a market order) and want to add SL + TP after the fact.

### Structure
```json
{
  "accountSpec": "your_account_name",
  "accountId": 12345,
  "action": "Sell",
  "symbol": "NQM5",
  "orderQty": 1,
  "orderType": "Stop",
  "stopPrice": 18000.00,
  "other": {
    "action": "Sell",
    "orderType": "Limit",
    "price": 18200.00
  }
}
```

### When to Use
- Position was opened without brackets
- Need to add/modify SL/TP after entry
- Partial close scenarios

---

## 5. startOrderStrategy — Advanced Brackets with Trailing Stop

### Structure
```json
{
  "accountId": 12345,
  "accountSpec": "your_account_name",
  "symbol": "ESM5",
  "action": "Buy",
  "orderStrategyTypeId": 2,
  "params": "{\"entryVersion\":{\"orderQty\":1,\"orderType\":\"Market\"},\"brackets\":[{\"qty\":1,\"profitTarget\":50,\"stopLoss\":-25,\"trailingStop\":false}]}"
}
```

### Key Points
- `orderStrategyTypeId: 2` — the only supported type (Brackets)
- `params` must be **JSON-stringified** (string, not object)
- `profitTarget` and `stopLoss` are in **price distance** (not absolute prices)
- **WebSocket recommended** — REST endpoint may return 404 errors

### With Auto-Trail (Trailing Stop that activates after profit)
```json
{
  "brackets": [{
    "qty": 1,
    "profitTarget": 50,
    "stopLoss": -25,
    "trailingStop": true,
    "autoTrail": {
      "stopLoss": 0.25,
      "trigger": 0.25,
      "freq": 0.25
    }
  }]
}
```

| autoTrail Field | Description |
|-----------------|-------------|
| `stopLoss` | Trailing distance from current price (raw value, not ticks) |
| `trigger` | Profit threshold before trail activates |
| `freq` | Minimum price movement before stop adjusts |

### Working JavaScript Example (from Tradovate forums)
```javascript
const longBracket = {
  qty: orderQuantity,
  profitTarget: takeProfitThreshold,
  stopLoss: -(Math.ceil(takeProfitThreshold / 5)),
  trailingStop: true
}

const body = {
  accountId: parseInt(process.env.ID, 10),
  accountSpec: process.env.SPEC,
  symbol: contract.name,
  action: "Buy",
  orderStrategyTypeId: 2,
  params: JSON.stringify({
    entryVersion: {
      orderQty: orderQuantity,
      orderType: "Market",
    },
    brackets: [longBracket]
  })
}
```

---

## 6. placeOSO with Trailing Stop (Simpler Method)

You can also use placeOSO with TrailingStop as a bracket type:

```json
{
  "accountSpec": "yourUserName",
  "accountId": 12345,
  "action": "Buy",
  "symbol": "ESH6",
  "orderQty": 1,
  "orderType": "Limit",
  "price": 4500,
  "isAutomated": true,
  "bracket1": {
    "action": "Sell",
    "orderType": "TrailingStop",
    "stopPrice": 4480.00
  },
  "bracket2": {
    "action": "Sell",
    "orderType": "Limit",
    "price": 4510.00
  }
}
```

This creates: **Entry → (Trailing Stop SL + Limit TP) as OCO**

---

## 7. Order Rejection — Common Causes

| Cause | Solution |
|-------|----------|
| Stop price too close to market | Ensure minimum distance (varies by contract) |
| Invalid price (not on tick) | Round to tick_size: `round(price / tick_size) * tick_size` |
| Insufficient margin | Reduce position size |
| Market closed | Check trading hours |
| Account not authorized | Verify API key permissions |
| Wrong environment | Demo credentials on demo URL, live on live URL |

---

## 8. Time In Force Options

| Value | Meaning | Use Case |
|-------|---------|----------|
| `"Day"` | Cancelled at end of trading session | Default for day trading |
| `"GTC"` | Good Till Cancelled — stays active | Swing trades |
| `"GTD"` | Good Till specific Date | Specific expiry needed |

---

## 9. What Our Bot Already Has vs. What We Can Add

### Already Implemented
- Authentication + token renewal (`tradovate_api.py`)
- `placeOSO` bracket orders with SL + TP
- Market data WebSocket streaming
- Contract resolution (front-month lookup)
- Risk management (trailing drawdown, daily loss, position sizing)
- Force close at session end

### Can Be Added (Future Enhancements)
| Feature | API Endpoint | Priority |
|---------|-------------|----------|
| Trailing stop brackets | `placeOSO` with `TrailingStop` type | High |
| Auto-trail (breakeven → trail) | `startOrderStrategy` with `autoTrail` | Medium |
| Add SL/TP to existing position | `placeOCO` | Medium |
| Modify stop mid-trade | `modifyOrderStrategy` | Medium |
| Order status tracking via WebSocket | Trading WebSocket events | High |
| Partial position close (runner) | Two OSOs with different quantities | Low |

---

## 10. Sources

### Official
- [Tradovate API Documentation](https://api.tradovate.com/)
- [Tradovate API FAQ Examples (GitHub)](https://github.com/tradovate/example-api-faq)
- [Tradovate Trading Strategy Example (GitHub)](https://github.com/tradovate/example-api-trading-strategy)

### Community Forum Threads
- [Place TP+SL via API](https://community.tradovate.com/t/place-a-tp-sl-order-via-api/8537)
- [OSO/OCO/Bracket Orders](https://community.tradovate.com/t/oso-oco-bracket-orders/10272)
- [Starting Strategies through API](https://community.tradovate.com/t/starting-strategies-through-api/2625)
- [Bracket Order with Trailing Stop](https://community.tradovate.com/t/creating-an-bracket-order-with-trailing-stop/11356)
- [Convert Stop to Trailing Stop](https://community.tradovate.com/t/convert-stop-to-trailing-stop-after-profit/4805)
- [Auto BreakEven via placeOSO](https://community.tradovate.com/t/auto-breakeven-thru-api-placeoso/12332)
- [Order Types and Limitations](https://community.tradovate.com/t/understanding-order-types-and-their-limitations/2161)
- [OSO Order in API](https://community.tradovate.com/t/oso-order-in-api/3192)
- [OSO Order Rejection Issues](https://community.tradovate.com/t/use-api-send-oso-order-some-time-be-reject-one-osoid/4479)
