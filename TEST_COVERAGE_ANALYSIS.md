# Test Coverage Analysis — Tradovate Bot

**Date:** 2026-04-22
**Current tests:** 143 in `test_all.py` (all passing)
**Coverage baseline:** 49% (enforced floor in CI: 48% no-regression)

## Coverage by Module (2026-04-22)

| Module | Coverage | Notes |
|---|---|---|
| `risk_manager.py` | 95% | Near-complete |
| `strategies.py` | 91% | |
| `status_reporter.py` | 90% | |
| `config.py` | 83% | |
| `bot_state.py` | 80% | |
| `trade_journal.py` | 71% | |
| `auto_tuner.py` | 61% | MAE/MFE paths partially covered |
| `continuous_learner.py` | 58% | Per-parameter heuristics not individually tested |
| `bot_commands.py` | 57% | Added in this pass |
| `tradovate_api.py` | 40% | Biggest gap — p-ticket, auth cascade, REST poller, YahooFinance all untested |
| `bot.py` | 31% | Biggest gap — main loop, warm-up, sync paths |
| `bot_health_check.py` | 25% | Most branches untested |

Excluded from coverage measurement (CLI/operator tools): `test_all.py`, `check_*.py`, `get_token.py`, `verify_bot.py`, `monitor.py`, `publish_dashboard.py`, `browser_bot.py`, `dashboard.py`, `connection_check.py`.

---

## Current Coverage Summary

| Category | Tests | Module(s) |
|----------|-------|-----------|
| Authentication | 5 | `tradovate_api.py` |
| API Endpoints | 7 | `tradovate_api.py` |
| WebSocket Protocol | 5 | `tradovate_api.py` |
| **WebSocket Resilience** | **6** | `tradovate_api.py` (reconnect / backoff / fallback) |
| Strategy (ORB) | 6 + 2 edge | `strategies.py` |
| Strategy (VWAP) | 5 + 2 edge | `strategies.py` |
| Strategy reset | 2 | `strategies.py` |
| Risk Manager (core) | 10 | `risk_manager.py` |
| Risk Manager (EOD / profit cap) | 4 | `risk_manager.py` |
| Live Connectivity | 3 | network |
| Config Validation | 4 | `config.py` |
| E2E Simulation | 3 | `strategies.py` + `risk_manager.py` |
| Contract Rollover | 9 | `bot.py` |
| Trade Journal | 13 | `trade_journal.py` |
| Auto-Tuner | 9 | `auto_tuner.py` |
| Bot State | 7 | `bot_state.py` |
| Status Reporter | 3 | `status_reporter.py` |
| Health Check | 5 | `bot_health_check.py` |
| Continuous Learner | 3 | `continuous_learner.py` |
| Bot executor | 6 | `bot.py` (_execute_signal, _process_price) |
| **Bot sync / EOD** | **3** | `bot.py` (_sync_balance) |
| **Bot commands** | **7** | `bot_commands.py` |
| API edge cases | 6 | `tradovate_api.py` (NaN/Inf guards) |

---

## Modules with ZERO Test Coverage

| Module | Key Testable Functions | Priority |
|--------|----------------------|----------|
| `browser_bot.py` | `harvest()`, `_auto_login()`, `_select_organization()` | MEDIUM — Playwright auth fallback; hard to test but high blast radius if silently broken |
| `connection_check.py` | `check_bot_process()`, `check_token()`, `check_account()`, `run_health_check()` | LOW — diagnostic tool |
| `dashboard.py` / `publish_dashboard.py` | `_build_html()`, `_read_bot_file()` | LOW — reporting only |
| `monitor.py` | monitor utilities | LOW |
| `check_server.py` / `verify_bot.py` / `get_token.py` | CLI scripts | LOW — operator tooling |

---

## Remaining Gaps in Already-Tested Modules

### tradovate_api.py

| Missing Test | Risk | Notes |
|-------------|------|-------|
| `_handle_p_ticket()` | Auth failure | Device verification / captcha flow — common issue #4; still untested |
| Auth cascade fallback chain | Auth failure | env → saved → web → API-key → browser — only env-var priority tested |
| `_try_browser_auth` | Auth failure | Playwright entry point; untested |
| `RestMarketDataPoller` | No market data | WebSocket fallback via Yahoo — untested |
| `YahooFinanceSession` crumb init | Market data | Indirectly used; untested directly |
| Rate limiting / 429 retry | Connection loss | `_post`/`_get` retry path — untested |

### bot.py

| Missing Test | Risk | Notes |
|-------------|------|-------|
| `_check_force_close_time()` behavior in `_main_loop` | Positions left overnight | The main-loop force-close branch (bot.py:733-754) is not integration-tested |
| `_warm_up_strategies()` | Stale/missing strategy state on start | Historical-bar warm-up is untested |
| `_sync_fills()` | Journal drift | Fill reconciliation path untested |
| Auto-recovery (3 consecutive API failures → re-auth) | Silent outage | bot.py:774-789 re-auth recovery is untested |

### strategies.py

| Missing Test | Risk | Notes |
|-------------|------|-------|
| ORB dual-window independence | Missed trades or double-trades | 5-min + 15-min windows with separate caps — only single window fires tested |
| VWAP whipsaw protection (cross-direction) | Over-trading | Same-direction cooldown covered; cross-direction gap not yet tested |
| VWAP stale-bars rejection | Bad signals | `_vwap_stale_bars >= 3` guard not exercised |

---

## Tests Added in This Pass (2026-04-22)

**Tier 1 — Live-trading safety**
1. WS graceful close (1000) reconnects with 1s delay, no failure counter
2. WS abnormal close triggers exponential backoff (2 * 2^(n-1))
3. WS backoff capped at 60s
4. WS fallback to REST after FALLBACK_THRESHOLD consecutive failures
5. WS `_on_error` with "403" sets `_got_403` flag for full re-auth
6. WS `_on_close` respects `_should_run=False` (no reconnect during shutdown)
7. `end_of_day_update()` advances peak + drawdown floor on winning day (Topstep)
8. `end_of_day_update()` does NOT lower floor on losing day
9. `end_of_day_update()` is a no-op when `trails_unrealized=True` (Apex)
10. `_sync_balance` seeds balance via `set_initial_balance` on first successful call (guards common issue #5)
11. `_sync_balance` skips when API returns `errorText`
12. `_sync_balance` prefers `netLiq` over `totalCashValue`

**Tier 2 — Strategy correctness**
13. ORB: stale price above range (post-restart warmup) does NOT fire — guards fresh-cross protection
14. ORB: fresh cross from inside range → above fires long breakout (control)
15. VWAP: reversed OHLC (high<low) auto-swapped without corruption
16. VWAP: zero-volume bar skipped, does not mutate VWAP
17. ORB reset clears range, breakout flag, prices, and last price across all windows
18. VWAP reset clears accumulator and cross-direction cooldown state

**Tier 3 — Command interface**
19. `send_command` → `read_pending_command` roundtrip; file consumed after read
20. Stale command (>5min) discarded with result write
21. Invalid JSON cleaned up without crash
22. Missing `command` key rejected
23. `execute_command` `close_all` in dry-run does NOT call API
24. `execute_command` `close_all` in live mode calls cancel+close
25. Unknown command returns False

---

## Recommended Next Additions (Remaining Gaps)

### Tier 1 — Live-trading safety (still open)

1. **`_handle_p_ticket` device-verification** — mock p-ticket response; assert 15s+ wait and retry
2. **Auth cascade fallback** — mock each auth method failing; assert next is tried in order
3. **Auto-recovery re-auth** (`bot.py:774-789`) — simulate 3 consecutive API failures; assert `_re_authenticate()` is called and md_stream restarted
4. **Force-close main-loop branch** — feed `now_et()` ≥ `FORCE_CLOSE_ET`; assert `cancel_all_orders`, `close_all_positions`, `end_of_day_update`, and auto-tuner all fire

### Tier 2 — Strategy correctness

5. **ORB dual-window independence** — 5-min and 15-min windows fire independently with separate caps
6. **VWAP whipsaw (cross-direction cooldown)** — long fires, short attempted within `min_trade_gap_minutes` → blocked
7. **VWAP stale-bars rejection** — feed 3+ zero-volume bars; assert next valid bar still suppressed

### Tier 3 — Missing modules

8. **`RestMarketDataPoller`** — mock `YahooFinanceSession.fetch_chart`; assert poll loop dispatches quotes to callbacks
9. **`_warm_up_strategies()`** — mock historical bars; assert ORB range seeded and VWAP accumulator populated

### Tier 4 — Analytics

10. **`continuous_learner._analyze_stop_loss` / `_analyze_take_profit` / `_analyze_cooldown`** — per-parameter heuristics not individually tested

---

## Structural Improvements

| Improvement | Benefit |
|------------|---------|
| Migrate to native pytest (remove custom `@test` decorator at `test_all.py:38`) | Fixtures, parametrize, `-k` filtering, `-x` fast-fail |
| Split `test_all.py` (~3 000 lines) into `tests/test_<module>.py` | Navigation, parallel execution |
| Add `conftest.py` with shared fixtures (mock API, seeded risk manager, sample trade data) | Stop duplicating tempfile + MagicMock setup in every test |
| Separate unit vs integration with `@pytest.mark.network` | CI can skip 3 live-connectivity tests for fast feedback |
| Add `pytest-cov` to CI with a coverage floor (e.g., 70%) | Catch regressions; currently no measurement |
| Parametrize contract-specific tests | One test definition covers all 4 contracts (NQ, ES, GC, CL) |
| Fix 8 pre-existing failing tests (see below) | Currently broken before any changes in this branch |

### Pre-existing failing tests (from `main`, not this branch)

1. `ORB: max trades cap respected`
2. `Credentials are correct (p-ticket received)` — network dependent
3. `Full trading day simulation with NQ ORB`
4. `AutoTuner: widening stops when SL hit rate > 70%`
5. `AutoTuner: tightening stops when SL hit rate < 30%`
6. `AutoTuner: widening TP when avg R > 1.5`
7. `AutoTuner: tightening TP when avg R < -0.5`
8. `Bot: _execute_signal in dry run mode logs but doesn't place orders`

These should be triaged and fixed separately — they indicate real regressions in either the tests or the modules they cover.
