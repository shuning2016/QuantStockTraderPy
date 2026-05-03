# Guardian Cron Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `/api/cron/guardian` endpoint that checks stop-loss and take-profit for all held stocks every 5 minutes during market hours (triggered by cron-job.org, zero AI tokens).

**Architecture:** A pure price-comparison guardian runs outside the normal AI trading sessions. It batches Finnhub price fetches (20/batch, 1s gap) to respect the 30 calls/sec free-tier limit, then applies Approach C: immediate exit on 5% take-profit (HARD_PROFIT), 30-second confirmation before executing any stop-loss to filter out price wicks. All sell execution reuses the existing `build_trade_log_entry` + state mutation pattern from `execute_decisions`.

**Tech Stack:** Python/Flask (existing), Finnhub REST API (existing `get_stock_quote`), Upstash Redis KV (existing `_kv`), cron-job.org (external HTTP trigger, no code change needed)

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `app.py` | Modify | Add 3 helpers + 1 endpoint (guardian logic lives here, close to its dependencies) |
| `tests/test_guardian.py` | Create | Unit tests for the 3 helpers (no HTTP, no Redis) |

---

### Task 1: `_batch_fetch_prices()` — batched Finnhub fetcher

**Files:**
- Modify: `app.py` (add function after `get_single_quote`, around line 565)
- Create: `tests/test_guardian.py`

- [ ] **Step 1: Create test file with failing test for batch fetch**

```python
# tests/test_guardian.py
"""Tests for guardian cron helpers."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest          # noqa: E402
import app             # noqa: E402
import strategy_v6 as S  # noqa: E402


def test_batch_fetch_prices_returns_all_symbols(monkeypatch):
    monkeypatch.setattr(app, "get_stock_quote", lambda sym: {"c": 100.0 + ord(sym[0])})
    monkeypatch.setattr(app.time, "sleep", lambda x: None)

    result = app._batch_fetch_prices(["AAPL", "MSFT", "NVDA"])

    assert set(result.keys()) == {"AAPL", "MSFT", "NVDA"}
    assert all(v > 0 for v in result.values())


def test_batch_fetch_prices_skips_zero_price(monkeypatch):
    monkeypatch.setattr(app, "get_stock_quote", lambda sym: {"c": 0.0})
    monkeypatch.setattr(app.time, "sleep", lambda x: None)

    result = app._batch_fetch_prices(["AAPL"])

    assert result == {}


def test_batch_fetch_prices_sleeps_between_batches(monkeypatch):
    sleep_calls = []
    monkeypatch.setattr(app, "get_stock_quote", lambda sym: {"c": 50.0})
    monkeypatch.setattr(app.time, "sleep", lambda x: sleep_calls.append(x))

    # 25 symbols → 2 batches of 20 and 5 → 1 sleep between them
    symbols = [f"S{i:02d}" for i in range(25)]
    app._batch_fetch_prices(symbols)

    assert len(sleep_calls) == 1
    assert sleep_calls[0] == 1.0


def test_batch_fetch_prices_no_sleep_for_single_batch(monkeypatch):
    sleep_calls = []
    monkeypatch.setattr(app, "get_stock_quote", lambda sym: {"c": 50.0})
    monkeypatch.setattr(app.time, "sleep", lambda x: sleep_calls.append(x))

    app._batch_fetch_prices(["AAPL", "MSFT"])

    assert len(sleep_calls) == 0
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd "$(git rev-parse --show-toplevel)"
pytest tests/test_guardian.py -v 2>&1 | head -30
```
Expected: `AttributeError: module 'app' has no attribute '_batch_fetch_prices'`

- [ ] **Step 3: Add `_batch_fetch_prices` to `app.py` after `get_single_quote` (~line 565)**

```python
def _batch_fetch_prices(symbols: list, batch_size: int = 20) -> dict:
    """Fetch current prices for all symbols in batches.

    Sends at most `batch_size` requests per second to stay under Finnhub's
    30 calls/sec free-tier burst limit.  Returns {sym: price} for symbols
    with a valid (>0) last price; silently omits symbols with no price data
    (market closed, bad ticker, or Finnhub glitch).
    """
    prices = {}
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i : i + batch_size]
        for sym in batch:
            q = get_stock_quote(sym)
            if q.get("c", 0) > 0:
                prices[sym] = q["c"]
        if i + batch_size < len(symbols):
            time.sleep(1.0)
    return prices
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
pytest tests/test_guardian.py::test_batch_fetch_prices_returns_all_symbols \
       tests/test_guardian.py::test_batch_fetch_prices_skips_zero_price \
       tests/test_guardian.py::test_batch_fetch_prices_sleeps_between_batches \
       tests/test_guardian.py::test_batch_fetch_prices_no_sleep_for_single_batch \
       -v
```
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add tests/test_guardian.py app.py
git commit -m "$(cat <<'EOF'
feat: add _batch_fetch_prices helper with Finnhub rate-limit batching

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `_check_guardian_exits()` — detect stop-loss and take-profit breaches

**Files:**
- Modify: `app.py` (add after `_batch_fetch_prices`)
- Modify: `tests/test_guardian.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_guardian.py` (imports already at top from Task 1):

```python
def _make_holding(avg_cost=100.0, stop_price=97.0, shares=10,
                  confidence=7, entry_time="09:35"):
    return {
        "shares":       shares,
        "avgCost":      avg_cost,
        "stopPrice":    stop_price,
        "highPrice":    avg_cost,
        "entryAtr":     2.0,
        "riskPerShare": avg_cost - stop_price,
        "confidence":   confidence,
        "entryTime":    entry_time,
    }


def _make_state_with_holding(sym="AAPL", avg_cost=100.0, stop_price=97.0):
    state = S.new_trade_state()
    state["holdings"][sym] = _make_holding(avg_cost=avg_cost, stop_price=stop_price)
    state["currentRegime"]  = "Trend"
    return state


def test_check_guardian_exits_detects_stop_loss():
    state = _make_state_with_holding("AAPL", avg_cost=100.0, stop_price=97.0)
    prices = {"AAPL": 96.0}  # below stopPrice

    result = app._check_guardian_exits(state, prices, "grok")

    assert len(result["stop_losses"]) == 1
    assert result["stop_losses"][0]["sym"] == "AAPL"
    assert len(result["take_profits"]) == 0


def test_check_guardian_exits_detects_take_profit():
    state = _make_state_with_holding("AAPL", avg_cost=100.0, stop_price=97.0)
    prices = {"AAPL": 106.0}  # above HARD_PROFIT_PCT=5%

    result = app._check_guardian_exits(state, prices, "grok")

    assert len(result["take_profits"]) == 1
    assert result["take_profits"][0]["sym"] == "AAPL"
    assert len(result["stop_losses"]) == 0


def test_check_guardian_exits_no_breach():
    state = _make_state_with_holding("AAPL", avg_cost=100.0, stop_price=97.0)
    prices = {"AAPL": 101.0}  # between stop and profit

    result = app._check_guardian_exits(state, prices, "grok")

    assert result["stop_losses"] == []
    assert result["take_profits"] == []


def test_check_guardian_exits_updates_high_price():
    state = _make_state_with_holding("AAPL", avg_cost=100.0, stop_price=97.0)
    state["holdings"]["AAPL"]["highPrice"] = 100.0
    prices = {"AAPL": 103.0}

    app._check_guardian_exits(state, prices, "grok")

    assert state["holdings"]["AAPL"]["highPrice"] == 103.0


def test_check_guardian_exits_skips_missing_price():
    state = _make_state_with_holding("AAPL", avg_cost=100.0, stop_price=97.0)
    prices = {}  # no price for AAPL

    result = app._check_guardian_exits(state, prices, "grok")

    assert result["stop_losses"] == []
    assert result["take_profits"] == []
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_guardian.py -k "check_guardian_exits" -v 2>&1 | head -20
```
Expected: `AttributeError: module 'app' has no attribute '_check_guardian_exits'`

- [ ] **Step 3: Add `_check_guardian_exits` to `app.py` after `_batch_fetch_prices`**

```python
def _check_guardian_exits(state: dict, prices: dict, provider: str) -> dict:
    """Detect stop-loss and take-profit breaches for one provider's holdings.

    Calls check_auto_stop_rules with the freshly fetched prices and splits
    results by urgency:
      stop_losses  — need 30-second confirmation before executing
      take_profits — execute immediately (5% gain is not a wick)

    Side-effects: updates highPrice and stopPrice on holdings in state
    (legitimate tracking mutations, same as a normal session would do).
    """
    state["lastPrices"] = prices
    state["_nowET"] = datetime.now(_ET).strftime("%H:%M")

    sells = S.check_auto_stop_rules(state, "guardian", provider=provider)

    STOP_TAGS   = {"STOP_LOSS", "TRAIL_STOP_PROFIT", "HARD_STOP"}
    PROFIT_TAGS = {"HARD_PROFIT"}

    return {
        "stop_losses":  [s for s in sells if s.get("tag") in STOP_TAGS],
        "take_profits": [s for s in sells if s.get("tag") in PROFIT_TAGS],
    }
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
pytest tests/test_guardian.py -k "check_guardian_exits" -v
```
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add tests/test_guardian.py app.py
git commit -m "$(cat <<'EOF'
feat: add _check_guardian_exits helper using existing auto-stop rules

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: `_execute_guardian_sell()` — execute a confirmed sell

**Files:**
- Modify: `app.py` (add after `_check_guardian_exits`)
- Modify: `tests/test_guardian.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_guardian.py`:

```python
def _make_state_for_sell():
    state = S.new_trade_state()
    state["cash"]    = 10_000.0
    state["_today"]  = "2026-05-02"
    state["_nowET"]  = "11:00"
    state["holdings"]["AAPL"] = _make_holding(avg_cost=100.0, stop_price=97.0, shares=10)
    return state


def test_execute_guardian_sell_removes_holding():
    state = _make_state_for_sell()
    sell  = {"sym": "AAPL", "shares": 10, "reason": "追踪止损$97.00", "tag": "STOP_LOSS"}

    app._execute_guardian_sell(state, sell, price=96.5,
                               today="2026-05-02", now_et="11:00")

    assert "AAPL" not in state["holdings"]


def test_execute_guardian_sell_adds_cash():
    state = _make_state_for_sell()
    sell  = {"sym": "AAPL", "shares": 10, "reason": "追踪止损$97.00", "tag": "STOP_LOSS"}

    app._execute_guardian_sell(state, sell, price=96.5,
                               today="2026-05-02", now_et="11:00")

    expected_cash = 10_000.0 + 96.5 * 10 * (1 - S.CFG.EXEC_SLIPPAGE)
    assert state["cash"] == pytest.approx(expected_cash, rel=1e-6)


def test_execute_guardian_sell_logs_guardian_tag():
    state = _make_state_for_sell()
    sell  = {"sym": "AAPL", "shares": 10, "reason": "追踪止损$97.00", "tag": "STOP_LOSS"}

    app._execute_guardian_sell(state, sell, price=96.5,
                               today="2026-05-02", now_et="11:00")

    assert len(state["log"]) == 1
    assert state["log"][0]["exit_tag"] == "GUARDIAN_STOP_LOSS"
    assert state["log"][0]["session"] == "guardian"


def test_execute_guardian_sell_profit_tag():
    state = _make_state_for_sell()
    sell  = {"sym": "AAPL", "shares": 10, "reason": "硬止盈+5%", "tag": "HARD_PROFIT"}

    app._execute_guardian_sell(state, sell, price=106.0,
                               today="2026-05-02", now_et="11:00")

    assert state["log"][0]["exit_tag"] == "GUARDIAN_HARD_PROFIT"


def test_execute_guardian_sell_skips_if_already_sold():
    state = _make_state_for_sell()
    del state["holdings"]["AAPL"]  # already gone
    sell  = {"sym": "AAPL", "shares": 10, "reason": "追踪止损$97.00", "tag": "STOP_LOSS"}

    app._execute_guardian_sell(state, sell, price=96.5,
                               today="2026-05-02", now_et="11:00")

    assert state["log"] == []
    assert state["cash"] == 10_000.0


def test_execute_guardian_sell_partial_reduces_shares():
    state = _make_state_for_sell()
    sell  = {"sym": "AAPL", "shares": 5, "reason": "分批止盈+1R", "tag": "SCALE_OUT_1R"}

    app._execute_guardian_sell(state, sell, price=103.0,
                               today="2026-05-02", now_et="11:00")

    assert state["holdings"]["AAPL"]["shares"] == 5
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_guardian.py -k "execute_guardian_sell" -v 2>&1 | head -20
```
Expected: `AttributeError: module 'app' has no attribute '_execute_guardian_sell'`

- [ ] **Step 3: Add `_execute_guardian_sell` to `app.py` after `_check_guardian_exits`**

```python
def _execute_guardian_sell(state: dict, sell: dict, price: float,
                           today: str, now_et: str) -> None:
    """Execute one guardian-triggered sell. Mutates state in place.

    Reuses the same state-mutation pattern as execute_decisions so guardian
    sells appear in trade logs, dailyPnL, cooldowns, and post_exit_watch
    identically to session-triggered sells.  Tag is prefixed with GUARDIAN_
    so guardian exits are distinguishable in the UI and A07 feedback loop.
    """
    sym      = sell["sym"]
    holdings = state.setdefault("holdings", {})
    if sym not in holdings:
        return

    h        = holdings[sym]
    avg_cost = h["avgCost"]
    shares   = sell["shares"]
    real     = (price - avg_cost) * shares
    tag      = "GUARDIAN_" + sell.get("tag", "STOP")

    state["_today"] = today
    state["_nowET"] = now_et

    entry = S.build_trade_log_entry("sell", {
        "sym":         sym,
        "shares":      shares,
        "price":       price,
        "realizedPnl": real,
        "reason":      sell["reason"],
        "session":     "guardian",
        "confidence":  h.get("confidence", 0),
    }, state, tag)

    state.setdefault("log", []).append(entry)
    state["cash"] = state.get("cash", 0.0) + price * shares * (1 - S.CFG.EXEC_SLIPPAGE)
    state.setdefault("dailyPnL", {})[today] = (
        state["dailyPnL"].get(today, 0) + real
    )

    if shares >= h["shares"]:
        del holdings[sym]
    else:
        h["shares"] -= shares

    state.setdefault("cooldowns", {})[sym]       = today
    state.setdefault("post_exit_watch", {})[sym] = {
        "exit_price": price,
        "exit_date":  today,
        "avg_cost":   avg_cost,
        "pnl_pct":    round((price - avg_cost) / avg_cost * 100, 2) if avg_cost else 0,
        "log_id":     entry["id"],
    }
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
pytest tests/test_guardian.py -k "execute_guardian_sell" -v
```
Expected: 6 passed

- [ ] **Step 5: Run full test suite to confirm no regressions**

```bash
pytest tests/ -v
```
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add tests/test_guardian.py app.py
git commit -m "$(cat <<'EOF'
feat: add _execute_guardian_sell reusing existing sell state-mutation pattern

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: `/api/cron/guardian` endpoint

**Files:**
- Modify: `app.py` (add endpoint after `cron_run`, around line 1400)

No unit test — this integrates Redis, Finnhub, and Flask routing. Test manually via `curl` after deploy.

- [ ] **Step 1: Add the guardian endpoint to `app.py` after the `cron_run` function (~line 1400)**

```python
_GUARDIAN_LOCK_KEY = "guardian_lock"
_GUARDIAN_LOCK_TTL = 90  # seconds: covers 30s confirmation + execution headroom


@app.route("/api/cron/guardian", methods=["GET"])
def cron_guardian():
    """Guardian cron: intra-session stop-loss and take-profit enforcement.

    Triggered by cron-job.org (not Vercel cron — every-5-min frequency
    exceeds the Hobby plan's once-per-day limit).

    Schedule on cron-job.org:
      Job 1 — market hours:  */5 13-20 * * 1-5  (UTC)
      Job 2 — off-hours:     0 * * * *
    Both jobs call: GET https://<app>.vercel.app/api/cron/guardian
    with header:    Authorization: Bearer <CRON_SECRET>
    """
    if not _verify_cron(request):
        return jsonify({"error": "Unauthorized"}), 401

    log = _logging.getLogger("quant.guardian")

    # Acquire guardian lock (atomic SET NX) — skip if a prior run is mid-flight
    if _USE_KV:
        token  = f"{int(time.time() * 1000)}-{os.getpid()}"
        result = _kv(["SET", _GUARDIAN_LOCK_KEY, token, "NX", "EX",
                       str(_GUARDIAN_LOCK_TTL)])
        if result != "OK":
            log.info("Guardian skipped — lock held (prior run still active)")
            return jsonify({"ok": True, "status": "skipped",
                            "reason": "guardian lock held"}), 200

    try:
        # Skip if any trading session is currently running — avoids race where
        # both guardian and a session attempt to sell the same holding.
        for p in _VALID_PROVIDERS:
            for s in _VALID_SESSIONS:
                if _USE_KV and _kv(["GET", f"session_lock:{p}:{s}"]):
                    log.info("Guardian skipped — session lock held: %s/%s", p, s)
                    return jsonify({"ok": True, "status": "skipped",
                                    "reason": f"session lock: {p}/{s}"}), 200

        today   = today_et()
        now_et  = datetime.now(_ET).strftime("%H:%M")

        # Load all three providers' states
        states = {p: load_trade_state(p) for p in _VALID_PROVIDERS}

        # Collect unique symbols currently held across all providers
        all_symbols = list({
            sym
            for state in states.values()
            for sym in state.get("holdings", {})
        })

        if not all_symbols:
            log.info("Guardian: no holdings to check")
            return jsonify({"ok": True, "status": "no_holdings", "checked": 0}), 200

        # Batch-fetch prices (20/batch, 1s gap → ≤20 calls/sec, safe under 30/sec)
        prices = _batch_fetch_prices(all_symbols)
        log.info("Guardian: fetched %d/%d prices for %s",
                 len(prices), len(all_symbols), all_symbols)

        # First pass: detect exits per provider
        suspected_stops  = {}   # {provider: [sell_dict, ...]}
        take_profit_hits = {}   # {provider: [sell_dict, ...]}

        for provider, state in states.items():
            if not state.get("holdings"):
                continue
            exits = _check_guardian_exits(state, prices, provider)
            if exits["stop_losses"]:
                suspected_stops[provider]  = exits["stop_losses"]
            if exits["take_profits"]:
                take_profit_hits[provider] = exits["take_profits"]

        # Execute take-profits immediately — 5% gain is not a wick
        profit_executed = []
        for provider, sells in take_profit_hits.items():
            state = states[provider]
            for sell in sells:
                sym   = sell["sym"]
                price = prices.get(sym, 0)
                if price <= 0 or sym not in state.get("holdings", {}):
                    continue
                _execute_guardian_sell(state, sell, price, today, now_et)
                profit_executed.append(f"{provider}/{sym}")
                log.info("Guardian PROFIT: %s/%s @$%.2f tag=%s",
                         provider, sym, price, sell.get("tag"))

        # Confirm stop-losses: sleep 30s, re-fetch, verify breach persists
        stop_executed = []
        if suspected_stops:
            stop_syms = list({
                s["sym"]
                for sells in suspected_stops.values()
                for s in sells
            })
            log.info("Guardian: suspected stop breach on %s — confirming in 30s",
                     stop_syms)
            time.sleep(30)
            confirm_prices = _batch_fetch_prices(stop_syms)

            for provider, sells in suspected_stops.items():
                state = states[provider]
                for sell in sells:
                    sym        = sell["sym"]
                    conf_price = confirm_prices.get(sym, 0)
                    holding    = state.get("holdings", {}).get(sym)
                    if not holding or conf_price <= 0:
                        continue

                    stop_p      = holding.get("stopPrice", float("inf"))
                    avg_cost    = holding["avgCost"]
                    risk_per    = holding.get("riskPerShare", avg_cost * 0.02)
                    pnl_pct     = (conf_price - avg_cost) / avg_cost * 100
                    hard_stop   = max(S.CFG.HARD_STOP_PCT,
                                      risk_per / avg_cost * 100 if avg_cost > 0
                                      else S.CFG.HARD_STOP_PCT)

                    still_breached = (conf_price <= stop_p or pnl_pct <= -hard_stop)

                    if still_breached:
                        _execute_guardian_sell(state, sell, conf_price, today, now_et)
                        stop_executed.append(f"{provider}/{sym}")
                        log.info("Guardian STOP confirmed: %s/%s @$%.2f",
                                 provider, sym, conf_price)
                    else:
                        log.info("Guardian STOP wick filtered: %s/%s recovered $%.2f",
                                 provider, sym, conf_price)

        # Persist updated states and write guardian trades to persistent log
        changed_providers = {p.split("/")[0] for p in profit_executed + stop_executed}
        for provider in changed_providers:
            save_trade_state(provider, states[provider])
            for entry in states[provider].get("log", []):
                if (entry.get("session") == "guardian"
                        and entry.get("date") == today
                        and not entry.get("_logged")):
                    append_log("trades", entry, today)
                    entry["_logged"] = True

        log.info("Guardian complete: checked=%d stop=%s profit=%s",
                 len(all_symbols), stop_executed, profit_executed)
        return jsonify({
            "ok":             True,
            "status":         "checked",
            "checked":        len(all_symbols),
            "prices_fetched": len(prices),
            "stop_executed":  stop_executed,
            "profit_executed": profit_executed,
        }), 200

    except Exception as e:
        log.error("Guardian error: %s", e, exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 200
    finally:
        if _USE_KV:
            _kv(["DEL", _GUARDIAN_LOCK_KEY])
```

- [ ] **Step 2: Run full test suite — confirm no regressions**

```bash
pytest tests/ -v
```
Expected: all existing tests still pass

- [ ] **Step 3: Smoke test locally with `CRON_ALLOW_UNAUTH=1`**

```bash
CRON_ALLOW_UNAUTH=1 python app.py &
sleep 2
curl -s http://localhost:5001/api/cron/guardian | python -m json.tool
kill %1
```
Expected JSON: `{"ok": true, "status": "no_holdings", "checked": 0}` (no holdings in local dev state)

- [ ] **Step 4: Commit**

```bash
git add app.py
git commit -m "$(cat <<'EOF'
feat: add /api/cron/guardian endpoint with 30s stop-loss confirmation

Approach C: immediate exit on HARD_PROFIT (5%), 30s wick-filter before
executing stop-loss. Zero AI tokens. Triggered by cron-job.org.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: cron-job.org configuration

**Files:** None — web UI only. No code changes.

- [ ] **Step 1: Create Job 1 — market hours (every 5 min)**

1. Go to [cron-job.org](https://cron-job.org) → Sign up (free, no credit card)
2. Click **Create cronjob**
3. Settings:
   - Title: `StockTrader Guardian — market hours`
   - URL: `https://<your-app>.vercel.app/api/cron/guardian`
   - Schedule: Custom → `*/5 13-20 * * 1-5`
   - Request method: `GET`
   - Add header: `Authorization` = `Bearer <your CRON_SECRET value>`
4. Save

- [ ] **Step 2: Create Job 2 — off-hours check (every 1 hr)**

1. Click **Create cronjob**
2. Settings:
   - Title: `StockTrader Guardian — off-hours`
   - URL: `https://<your-app>.vercel.app/api/cron/guardian`
   - Schedule: Custom → `0 * * * *`
   - Request method: `GET`
   - Add header: `Authorization` = `Bearer <your CRON_SECRET value>`
3. Save

Note: Job 1 and Job 2 overlap during market hours (13:00–20:00 UTC) — the guardian lock ensures only one runs at a time. The hourly job fires first for the :00 minute, then the 5-minute job takes over for :05, :10, etc. This is correct behaviour.

- [ ] **Step 3: Verify first run**

After the next `:00` or `:05` minute boundary, check the cron-job.org execution log:
- HTTP response code should be `200`
- Response body should contain `"ok": true`

Also check Vercel function logs (Vercel dashboard → Functions tab) for `quant.guardian` log lines.

---

## Cost Summary (100 stocks, confirmed setup)

| Service | Usage | Cost |
|---|---|---|
| cron-job.org | 96 HTTP triggers/day | **$0** |
| Finnhub | ~9,600 price calls/day (avg 6.7/min, burst 20/run) | **$0** |
| Vercel functions | ~96 invocations/day × ~37s max | **$0** |
| AI tokens | 0 (guardian is pure price comparison) | **$0** |
