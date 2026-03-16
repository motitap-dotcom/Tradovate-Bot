# Test Coverage Analysis — Tradovate Bot

**Date:** 2026-03-16
**Current tests:** 56 tests in `test_all.py` across 9 categories

---

## Current Coverage Summary

| Category | Tests | Module(s) |
|----------|-------|-----------|
| Authentication | 5 | `tradovate_api.py` |
| API Endpoints | 7 | `tradovate_api.py` |
| WebSocket Protocol | 5 | `tradovate_api.py` |
| Strategy (ORB) | 6 | `strategies.py` |
| Strategy (VWAP) | 5 | `strategies.py` |
| Risk Manager | 10 | `risk_manager.py` |
| Live Connectivity | 3 | `tradovate_api.py` (network) |
| Config Validation | 4 | `config.py` |
| E2E Simulation | 3 | `strategies.py` + `risk_manager.py` |
| Contract Rollover | 8 | `bot.py` |

---

## Modules with ZERO Test Coverage

| Module | Key Testable Functions | Priority |
|--------|----------------------|----------|
| `trade_journal.py` | `record_entry/exit()`, `compute_effective_target()`, `_compute_summary()`, `analyze_by_*()`, `generate_lessons()`, `_longest_losing_streak()` | **HIGH** — P&L accuracy and consistency-rule math directly affect challenge compliance |
| `auto_tuner.py` | `_tune_stops()`, `_tune_targets()`, `_propose()`, `_tune_daily_trade_cap()`, `_apply_adjustments()` | **MEDIUM** — incorrect tuning could silently widen stops or disable profitable symbols |
| `bot_state.py` | `save_state()`, `load_state()`, `build_state()`, `restore_strategies()` | **MEDIUM** — corrupt/stale state restore could cause double-trades or missed cooldowns |
| `status_reporter.py` | `write_status()` | LOW — monitoring only |
| `bot_health_check.py` | `check_token()`, `check_bot_log()`, verdict logic | LOW — server-side tooling |

---

## Critical Gaps in Already-Tested Modules

### risk_manager.py

| Missing Test | Risk | Why It Matters |
|-------------|------|----------------|
| Daily profit cap (`_check_daily_profit_cap()`) | **Challenge-failing** | The consistency rule (max 40% of cumulative profit in one day) is never tested |
| `set_initial_balance()` regression | **Challenge-failing** | The false-lock bug where `day_start_balance` defaults to $50K (common issue #5) has no regression test |
| `update_balance()` NaN/Inf guard | Data corruption | No test that corrupted balance values are rejected |
| `end_of_day_update()` | Incorrect drawdown floor | EOD trailing drawdown (Topstep mode) is untested |
| Position sizing edge cases | Over-sizing | Zero-point stop loss or tiny account balance not tested |

### strategies.py

| Missing Test | Risk | Why It Matters |
|-------------|------|----------------|
| ORB fresh-cross validation | False signals | Gap-open above range (price never inside) shouldn't trigger a breakout |
| ORB dual-window interaction | Missed trades or double-trades | Actual config uses 3-min + 5-min windows with separate caps — only single window tested |
| VWAP whipsaw protection | Over-trading | Rapid long→short reversal should be blocked by cross-direction cooldown |
| VWAP reversed OHLC guard | Corrupted VWAP | `update_vwap()` handles reversed high/low — no test for this |
| Strategy `reset()` across day boundaries | Stale state | No test that strategies properly reset at market open |

### tradovate_api.py

| Missing Test | Risk | Why It Matters |
|-------------|------|----------------|
| Auth cascade fallback chain | Auth failure | Only env-var priority tested; saved-token → web → API-key → browser chain untested |
| `_handle_p_ticket()` | Auth failure | Device verification flow completely untested |
| Rate limiting / retry | Connection loss | No test for 429 or p-ticket throttling behavior |
| `get_cash_balance()` | Wrong balance | Balance retrieval (critical for risk manager seeding) untested |
| `RestMarketDataPoller` | No market data | WebSocket fallback path has zero tests |

### bot.py

| Missing Test | Risk | Why It Matters |
|-------------|------|----------------|
| `_process_tick()` | Core logic gap | The tick → signal → order pipeline is never unit-tested |
| `_check_force_close_time()` | Open positions overnight | Time-based force-close untested |
| `_sync_balance()` | False lock | Recovery path for failed initial balance fetch untested |
| `_init_balance_from_api()` | False lock | The fix for common issue #5 has no regression test |

---

## Recommended Test Additions (Ranked by Impact)

### Tier 1 — Challenge Compliance (add first)

1. **Daily profit cap enforcement** — test trading locks when single-day P&L exceeds 40% of cumulative profit
2. **`set_initial_balance()` regression** — verify `day_start_balance` uses API balance, not $50K default
3. **Trade journal `record_exit()` R-multiple** — verify R = PnL / risk is computed correctly
4. **Trade journal `compute_effective_target()`** — verify consistency-rule target adjustment math
5. **Trade journal `_compute_summary()`** — verify win rate, profit factor, expectancy calculations

### Tier 2 — Strategy Correctness

6. **ORB gap-open rejection** — price opens above range without crossing from inside → no signal
7. **ORB dual-window** — 3-min and 5-min windows fire independently with separate caps
8. **VWAP whipsaw protection** — rapid long→short reversal blocked by cross-direction cooldown
9. **VWAP reversed OHLC guard** — feed high < low, verify VWAP not corrupted

### Tier 3 — Resilience & Recovery

10. **`bot_state.py` save/load roundtrip** — save state, load, verify strategies restored correctly
11. **`bot_state.py` staleness rejection** — state from yesterday returns `None`
12. **`update_balance()` NaN guard** — feed NaN balance, verify rejection without state corruption
13. **Auth cascade fallback** — mock each auth method failing, verify next one is tried
14. **Force-close time check** — verify `_check_force_close_time()` returns True after 16:59 ET

### Tier 4 — Analytics Accuracy

15. **`auto_tuner._propose()` clamping** — verify ±20% cap and absolute bounds
16. **`auto_tuner._tune_stops()`** — >70% SL hit rate widens stops by 10%
17. **`trade_journal.analyze_by_symbol()`** — verify per-symbol P&L aggregation
18. **`trade_journal.generate_lessons()`** — verify heuristic triggers (worst symbol flagged when win rate < 30%)

---

## Structural Improvements

| Improvement | Benefit |
|------------|---------|
| Migrate to native pytest (remove custom decorator framework) | Better fixtures, parametrize, failure reporting |
| Add `conftest.py` with shared fixtures | Mock API, mock risk manager, sample trade data reusable across tests |
| Separate unit vs integration tests (`@pytest.mark.network`) | CI can skip live connectivity tests, faster feedback loop |
| Add `pytest-cov` coverage reporting | Track coverage %, catch regressions in CI |
| Parametrize contract-specific tests | One test definition covers all 4 contracts (NQ, ES, GC, CL) instead of duplicating |
