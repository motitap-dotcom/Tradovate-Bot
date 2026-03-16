# Test Coverage Analysis

## Current State

**Test file:** `test_all.py` (1,194 lines, 59 tests)
**Results:** 56/59 passing (3 failures are live API connectivity tests — expected in CI)

### Tested Modules

| Module | Tests | What's Covered |
|--------|-------|----------------|
| `tradovate_api.py` (auth) | 5 | Password encryption, HMAC, token persistence, injection, env var priority |
| `tradovate_api.py` (REST) | 7 | GET accounts/positions, suggest contract, bracket orders, cancel/close all, token renewal |
| `tradovate_api.py` (WS) | 5 | Auth message format, heartbeat, auth response, subscribe, request ID increment |
| `strategies.py` (ORB) | 6 | Range accumulation, long/short breakout, no double-fire, cooldown, max trades |
| `strategies.py` (VWAP) | 5 | VWAP calculation, long/short crossover, cooldown, reset |
| `strategies.py` (factory) | 1 | `create_strategy` returns correct types |
| `risk_manager.py` | 11 | Init, can_trade, drawdown lock, daily loss brake, max contracts, daily cap, position sizing, peak trailing, register open/close, new day reset |
| `config.py` | 4 | Contract specs, RR ratio, URLs, challenge settings |
| `bot.py` (rollover) | 9 | `_next_liquid_contract` for NQ/ES/GC/CL, date-based rollover trigger/no-trigger |
| E2E simulation | 3 | NQ ORB day sim, GC VWAP day sim, risk cap enforcement |

---

## Untested Modules (Prioritized)

### Priority 1 — Critical Business Logic (0% coverage)

#### 1. `trade_journal.py` (562 lines)
**Risk: HIGH** — Incorrect analytics feed into auto-tuner decisions and risk assessment.

Missing tests:
- `record_entry()` / `record_exit()` — trade lifecycle recording
- `record_exit_by_symbol()` — symbol-based exit matching
- R-multiple calculation (depends on stop_loss, entry_price, point_value)
- Duration calculation from entry/exit timestamps
- `_closed_trades(since=)` — date filtering
- `daily_pnl_breakdown()` — per-day P&L aggregation
- `highest_day_profit()` — used by consistency rule
- `compute_effective_target()` — consistency rule target adjustment (FundedNext-specific)
- `_compute_summary()` — win rate, profit factor, expectancy, avg R
- `analyze_by_symbol()` / `analyze_by_strategy()` / `analyze_by_hour()` / `analyze_by_exit_reason()`
- `generate_lessons()` — all lesson-generation branches (win rate, profit factor, R-multiple, symbol, time, exit, streak, duration)
- `_longest_losing_streak()` helper
- Persistence: `_load()` with corrupt file, `_save()` atomicity

#### 2. `auto_tuner.py` (277 lines)
**Risk: HIGH** — Bugs here silently shift strategy parameters, causing real money losses.

Missing tests:
- `_tune_stops()` — widening when SL rate > 70%, tightening when < 30%
- `_tune_targets()` — widening when avg R > 1.5, tightening when avg R < -0.5
- `_tune_symbol_allocation()` — flagging symbols with <30% WR and <-$500 P&L
- `_tune_daily_trade_cap()` — reducing cap when late trades lose >70%
- `_propose()` — ±20% cap, absolute bounds, tick-size rounding, too-small-change rejection
- `_apply_adjustments()` — actually modifies config.CONTRACT_SPECS
- `_log_adjustments()` — log file persistence, 200-entry cap
- `run()` — minimum trade threshold, full pipeline
- `_group_by()` helper

#### 3. `bot_state.py` (174 lines)
**Risk: MEDIUM-HIGH** — Bugs cause duplicate trades or lost state after restart.

Missing tests:
- `save_state()` / `load_state()` roundtrip
- `load_state()` returns None for stale date, corrupt file, missing file
- `build_state()` with ORB strategies (windows, breakout flags, trade times)
- `build_state()` with VWAP strategies (long/short counts, cooldown times)
- `restore_strategies()` — ORB restoration (trades_taken, breakout_fired, range)
- `restore_strategies()` — VWAP restoration (counts, cooldown times)
- Edge case: mismatched window count between saved and current state

### Priority 2 — Core Bot Logic (partially tested)

#### 4. `bot.py` — non-rollover code (1,095 lines, ~90% untested)
**Risk: HIGH** — The main orchestrator.

Missing tests:
- `_handle_signal()` — signal-to-order translation, dry-run vs live
- `_handle_fill()` — fill processing, journal recording, risk updates
- `_check_forced_exit()` — end-of-day position closing logic
- `_subscribe_market_data()` — contract resolution, subscription setup
- `_warm_up_strategies()` — historical data feeding
- `_trading_loop()` — market hours check, price dispatch
- `_status_report()` — status formatting
- Signal handling (SIGINT/SIGTERM graceful shutdown)
- Error recovery paths (API failures, WS disconnects)

### Priority 3 — Supporting Infrastructure

#### 5. `status_reporter.py` (70 lines)
Missing tests:
- `write_status()` — correct JSON structure, file path handling, error on missing dir

#### 6. `bot_health_check.py` (511 lines)
Missing tests:
- All check functions: process, token, account, market data WS, bot log, live status, system resources
- Health verdict logic (HEALTHY/DEGRADED/DOWN)

#### 7. `connection_check.py` (448 lines)
Missing tests:
- Check functions, ping mechanism, verdict logic

#### 8. `dashboard.py` (341 lines)
Missing tests:
- `_read_bot_file()`, `_build_html()`, HTTP handler

---

## Gaps Within Tested Modules

### `tradovate_api.py` — Missing Edge Cases
- **Network error handling**: No tests for retry logic, timeout behavior, connection drops
- **Malformed API responses**: No tests for unexpected JSON structures
- **Rate limiting**: No test for p-ticket/retry-after handling
- **`_post()` and `_get()` wrappers**: Error paths untested
- **`place_market_order()`**: Not tested directly (only `place_bracket_order`)
- **`get_account_balance()`**: Not tested
- **`_fetch_account_id()`**: Not tested
- **RestMarketDataPoller**: Zero tests for the REST-based market data fallback

### `strategies.py` — Missing Edge Cases
- **ORB outside market hours**: What happens with pre-market or after-hours timestamps?
- **ORB multi-window interaction**: 5-min fires but 15-min doesn't (or vice versa)
- **VWAP with zero volume**: Division by zero protection
- **VWAP whipsaw prevention**: Not tested
- **Strategy `reset()` on new day**: Tested for VWAP but not ORB
- **ORB with real contract specs**: Tests use "MNQ" (disabled micro) not "NQ" (enabled mini)

### `risk_manager.py` — Missing Edge Cases
- **Consistency rule / daily profit cap**: Not tested (FundedNext-specific)
- **`_sync_balance()` / `set_initial_balance()`**: Balance initialization from API
- **`end_of_day_update()`**: EOD trailing drawdown (Topstep mode)
- **`record_fill()`**: Not tested
- **Concurrent `update_balance()` with unrealized P&L swings**
- **`status()` dict**: Not verified for correct structure

---

## Structural Issues

1. **No pytest framework**: Tests use a custom `@test` decorator with manual invocation. This means:
   - No test discovery
   - No fixture support
   - No parametrize support
   - No proper assertion introspection
   - `pytest` collection fails due to top-level execution

2. **Live API tests in the same file**: The 3 connectivity tests always fail in CI and inflate failure count.

3. **No test isolation**: Tests share module-level imports and can have side effects on each other.

4. **No coverage measurement**: No `pytest-cov` integration to track actual line/branch coverage.

---

## Recommended Test Additions (by impact)

### Batch 1 — Highest ROI (trade_journal + auto_tuner)
These modules directly affect trading decisions and money:

```
test_trade_journal.py:
  - test_record_entry_creates_trade
  - test_record_exit_closes_trade
  - test_record_exit_calculates_r_multiple
  - test_record_exit_calculates_duration
  - test_record_exit_by_symbol
  - test_record_exit_unknown_id_warns
  - test_closed_trades_filters_by_date
  - test_daily_pnl_breakdown
  - test_highest_day_profit
  - test_compute_effective_target_no_consistency
  - test_compute_effective_target_with_consistency_adjustment
  - test_compute_summary_empty
  - test_compute_summary_with_trades
  - test_analyze_by_symbol
  - test_analyze_by_strategy
  - test_generate_lessons_low_win_rate
  - test_generate_lessons_high_sl_rate
  - test_longest_losing_streak

test_auto_tuner.py:
  - test_tune_stops_widens_on_high_sl_rate
  - test_tune_stops_tightens_on_low_sl_rate
  - test_tune_targets_widens_on_high_r
  - test_tune_targets_tightens_on_negative_r
  - test_propose_caps_at_20pct
  - test_propose_respects_absolute_bounds
  - test_propose_rounds_to_tick_size
  - test_tune_symbol_flags_bad_performer
  - test_tune_daily_cap_reduces_on_late_losses
  - test_run_skips_with_few_trades
  - test_apply_adjustments_modifies_config
```

### Batch 2 — State Persistence
Prevents duplicate trades and lost state:

```
test_bot_state.py:
  - test_save_load_roundtrip
  - test_load_returns_none_for_stale_date
  - test_load_returns_none_for_corrupt_file
  - test_build_state_orb
  - test_build_state_vwap
  - test_restore_orb_strategy
  - test_restore_vwap_strategy
```

### Batch 3 — Bot Core Signal Handling
The main trading loop:

```
test_bot.py:
  - test_handle_signal_dry_run
  - test_handle_signal_live_places_bracket
  - test_handle_fill_updates_journal_and_risk
  - test_forced_exit_closes_positions_at_cutoff
  - test_no_trading_outside_market_hours
```

### Batch 4 — API Error Handling
Network resilience:

```
test_tradovate_api_errors.py:
  - test_get_retries_on_network_error
  - test_post_handles_rate_limit
  - test_malformed_response_doesnt_crash
  - test_expired_token_triggers_renewal
  - test_place_bracket_rollback_on_oco_failure
```
