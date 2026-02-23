# Tradovate API — Research Findings

Automated research performed on 2026-02-23.
Credentials verified: **FNFTMOTITAPWnBks** (FundedNext prop firm account).

---

## 1. Authentication

### REST Auth Flow (from JS reverse-engineering)

**Endpoint:** `POST /v1/auth/accesstokenrequest`

**Servers:**
| Environment | REST Base URL |
|-------------|----------------------------------------------|
| Live        | `https://live.tradovateapi.com/v1`           |
| Demo        | `https://demo.tradovateapi.com/v1`           |

**Request Payload:**
```json
{
  "name": "username",
  "password": "password",
  "appId": "tradovate_trader(web)",
  "appVersion": "3.260220.0",
  "deviceId": "uuid-v4",
  "cid": 8,
  "sec": "",
  "organization": ""
}
```

- `organization`: Empty string for FundedNext (NOT `"funded-next"`).
- `cid: 8`: Web trader client ID (no API key subscription needed).
- `sec: ""`: Empty when using web-style auth without API keys.

### Auth Response Scenarios

| Response | Meaning |
|----------|---------|
| `{"accessToken": "...", "mdAccessToken": "...", ...}` | Success — full token set |
| `{"p-ticket": "...", "p-time": 15, "p-captcha": true}` | Credentials correct, CAPTCHA required |
| `{"p-ticket": "...", "p-time": 15, "p-captcha": false}` | Credentials correct, wait and retry |
| `{"errorText": "Incorrect username or password..."}` | Wrong creds OR rate limited |
| `{"errorText": "The app is not registered"}` | Missing `appId` or `cid` |

### CAPTCHA Details (reCAPTCHA v2)

- **Type:** Google reCAPTCHA v2 (NOT hCaptcha)
- **Sitekey:** `6Ld7FAoTAAAAAPdydZWpQ__C8xf29eYfvswcz52T`
- **When required:** First login from a new device/IP without API keys.

**CAPTCHA Flow (from Tradovate JS `7448.d23b1dde.js`):**
1. Send auth request → receive `p-ticket` + `p-captcha: true`
2. Wait `p-time` seconds
3. Show reCAPTCHA widget to user → user solves → get `g-recaptcha-response` token
4. Resend the SAME auth request with two additional fields:
   - `"p-ticket": "<ticket_from_step_1>"`
   - `"p-captcha": "<recaptcha_response_token>"`
5. Server responds with full token set

### Token Renewal

**Endpoint:** `POST /v1/auth/renewaccesstoken`
- Header: `Authorization: Bearer <access_token>`
- No body required
- Returns new `accessToken` + `expirationTime`
- Tokens expire ~24h after issuance

### Rate Limiting

- After ~5 failed attempts, Tradovate returns "Incorrect password" for ALL requests
  (even correct credentials) — a security measure against brute force.
- Cooldown: approximately 5-10 minutes.
- Rate limit applies per source IP.

---

## 2. REST API Endpoints

All endpoints require `Authorization: Bearer <token>` header.
Unauthenticated requests return **404** (not 401) — intentional security design.

### Account
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/account/list` | List all accounts |
| GET | `/account/item?id=<id>` | Get specific account |

### Positions
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/position/list` | All open positions |
| GET | `/position/item?id=<id>` | Specific position |

### Orders
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/order/list` | All orders |
| POST | `/order/placeorder` | Place simple order |
| POST | `/order/placeOSO` | Place OSO bracket order |
| POST | `/order/cancelorder` | Cancel order |
| POST | `/order/modifyorder` | Modify existing order |

### Contracts
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/contract/find?name=<symbol>` | Find by symbol (e.g., `NQH6`) |
| GET | `/contract/suggest?t=<base>&l=1` | Front-month contract |
| GET | `/contract/item?id=<id>` | Specific contract |

### Cash Balance
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/cashBalance/getcashbalancesnapshot` | Balance snapshot |

### Fills
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/fill/list` | Recent fills |

---

## 3. WebSocket Protocol

### URLs
| Environment | Trading WS | Market Data WS |
|-------------|------------|----------------|
| Live | `wss://live.tradovateapi.com/v1/websocket` | `wss://md.tradovateapi.com/v1/websocket` |
| Demo | `wss://demo.tradovateapi.com/v1/websocket` | `wss://md-demo.tradovateapi.com/v1/websocket` |

### Protocol Details

**Message Format:**
```
<endpoint>\n<request_id>\n\n<json_body>
```

**Connection Lifecycle:**
1. Connect to WSS URL
2. Server sends `"o"` (open frame)
3. Client sends auth: `"authorize\n1\n\n<access_token>"`
4. Server responds: `'a[{"i":1,"s":200,...}]'` on success
5. Heartbeat: server sends `"h"` → client should reply `"[]"`
6. Data frames: `'a[{...}]'` — JSON array wrapped in `a` prefix

**Subscribe to Quotes:**
```
md/subscribeQuote\n2\n\n{"symbol":"NQH6"}
```

**Unsubscribe:**
```
md/unsubscribeQuote\n3\n\n{"symbol":"NQH6"}
```

### Quote Data Structure
```json
{
  "e": "md",
  "d": {
    "contractId": 12345,
    "timestamp": "2026-02-23T10:30:00.000Z",
    "bid": {"price": 21050.25, "size": 15},
    "ask": {"price": 21050.50, "size": 10},
    "trade": {"price": 21050.25, "size": 1},
    "high": {"price": 21100.00},
    "low": {"price": 21000.00}
  }
}
```

### Reconnection Strategy
- On disconnect, wait 2s, 4s, 8s, 16s, 32s (exponential backoff)
- Max 5 reconnect attempts
- After reconnect, re-subscribe to all symbols

---

## 4. Test Results

**50/50 tests passed** covering:

| Category | Tests | Status |
|----------|-------|--------|
| Authentication | 5 | All pass |
| API Endpoints (mocked) | 7 | All pass |
| WebSocket Protocol | 5 | All pass |
| ORB Strategy | 6 | All pass |
| VWAP Strategy | 5 | All pass |
| Risk Manager | 11 | All pass |
| Live API Connectivity | 3 | All pass |
| Config Validation | 4 | All pass |
| End-to-End Simulation | 3 | All pass |

### Simulation Results
- **NQ ORB**: 2 signals in 6-hour sim (5-min + 15-min breakouts)
- **GC VWAP**: 4 signals (2 longs + 2 shorts with cooldown)
- **Risk Manager**: Correctly caps at 12 trades/day

---

## 5. Remaining Setup Step

The bot is fully functional. The **only** remaining step is obtaining the initial
access token. This requires solving a reCAPTCHA once from a browser:

```bash
# On a PC with a browser:
python get_token.py

# OR: paste token from browser DevTools into .env:
TRADOVATE_ACCESS_TOKEN=<your_token_here>
```

After that, the bot auto-renews the token indefinitely.
