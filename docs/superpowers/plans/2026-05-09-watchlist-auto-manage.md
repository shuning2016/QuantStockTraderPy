# Watchlist Auto-Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically add ARK fund buys to the watchlist and silently remove stale watchlist stocks (no fresh signal in 5 days, no open position).

**Architecture:** A single helper function `_apply_watchlist_automation(cache, watchlist)` in `app.py` encapsulates both features. It is called at both signal refresh points (daily cron and manual UI refresh) after `_refresh_signals()` completes. No changes to `signals.py`.

**Tech Stack:** Python 3.9, Flask, existing `load_trade_state` / `save_watchlist` / `load_watchlist` in `app.py`. Tests use `pytest` with `unittest.mock`.

---

## File Map

| File | Change |
|---|---|
| `app.py` | Add `_apply_watchlist_automation()` helper; call it at 2 signal refresh points |
| `tests/test_watchlist_automation.py` | New test file for both features |

---

## Task 1 — ARK Auto-Add

### Files
- Modify: `app.py`
- Create: `tests/test_watchlist_automation.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_watchlist_automation.py`:

```python
"""Tests for watchlist auto-management: ARK auto-add and staleness cleanup."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest
from unittest.mock import patch
import app as _app


# ── Shared fixtures ───────────────────────────────────────────────

def _empty_state():
    return {"holdings": {}, "cash": 100000, "log": []}


def _cache(untracked=None, watchlist_signals=None):
    return {
        "fetched_at": "2026-05-09T13:00:00",
        "partial": False,
        "watchlist_signals": watchlist_signals or {},
        "untracked_signals": untracked or [],
        "ark_holdings": {},
    }


# ── ARK auto-add ─────────────────────────────────────────────────

def test_ark_buy_is_added():
    cache = _cache(untracked=[
        {"type": "ark", "fund": "ARKK", "action": "buy",
         "shares": 100, "date": "2026-05-09", "sym": "TSLA"},
    ])
    with patch.object(_app, "load_trade_state", return_value=_empty_state()):
        result = _app._apply_watchlist_automation(cache, [])
    assert "TSLA" in result


def test_ark_sell_is_not_added():
    cache = _cache(untracked=[
        {"type": "ark", "fund": "ARKK", "action": "sell",
         "shares": 100, "date": "2026-05-09", "sym": "TSLA"},
    ])
    with patch.object(_app, "load_trade_state", return_value=_empty_state()):
        result = _app._apply_watchlist_automation(cache, [])
    assert "TSLA" not in result


def test_ark_buy_not_duplicated_if_already_on_watchlist():
    cache = _cache(untracked=[
        {"type": "ark", "fund": "ARKK", "action": "buy",
         "shares": 100, "date": "2026-05-09", "sym": "TSLA"},
    ])
    with patch.object(_app, "load_trade_state", return_value=_empty_state()):
        result = _app._apply_watchlist_automation(cache, ["TSLA"])
    assert result.count("TSLA") == 1


def test_non_ark_untracked_signal_not_added():
    cache = _cache(untracked=[
        {"type": "manager", "action": "buy",
         "date": "2026-05-09", "sym": "NVDA"},
    ])
    with patch.object(_app, "load_trade_state", return_value=_empty_state()):
        result = _app._apply_watchlist_automation(cache, [])
    assert "NVDA" not in result
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "/Users/shuning.wang/Library/CloudStorage/GoogleDrive-shuning2016@gmail.com/My Drive/My Projects/Python projects/StockTraderPy"
.venv/bin/python -m pytest tests/test_watchlist_automation.py -v
```

Expected: `AttributeError: module 'app' has no attribute '_apply_watchlist_automation'`

- [ ] **Step 3: Add `_apply_watchlist_automation` to `app.py`**

Find the line `def cron_signals():` (around line 2023). Add the helper function **above** it:

```python
def _apply_watchlist_automation(cache: dict, watchlist: list[str]) -> list[str]:
    """ARK auto-add + 5-day staleness cleanup. Returns updated watchlist list."""
    from datetime import date as _date, timedelta

    # ── Feature 1: ARK auto-add ───────────────────────────────────
    current_upper = {s.upper() for s in watchlist}
    updated = list(watchlist)
    for sig in cache.get("untracked_signals", []):
        if sig.get("type") == "ark" and sig.get("action") == "buy":
            sym = (sig.get("sym") or "").upper()
            if sym and sym not in current_upper:
                current_upper.add(sym)
                updated.append(sym)

    # ── Feature 2: 5-day staleness cleanup ───────────────────────
    cutoff = (_date.today() - timedelta(days=5)).strftime("%Y-%m-%d")

    # Build {sym_upper: latest_signal_date} from all signals in cache
    latest_dates: dict[str, str] = {}
    all_sigs = list(cache.get("untracked_signals", []))
    for sigs in cache.get("watchlist_signals", {}).values():
        all_sigs.extend(sigs)
    for sig in all_sigs:
        sym = (sig.get("sym") or "").upper()
        date_str = sig.get("date") or ""
        if sym and date_str:
            if sym not in latest_dates or date_str > latest_dates[sym]:
                latest_dates[sym] = date_str

    # Collect held symbols across all providers (safety override)
    held: set[str] = set()
    for provider in MODELS:
        for sym in load_trade_state(provider).get("holdings", {}):
            held.add(sym.upper())

    # Filter: keep if held, keep if fresh signal, keep if no signal history
    result = []
    for sym in updated:
        sym_upper = sym.upper()
        if sym_upper in held:
            result.append(sym)
        elif sym_upper in latest_dates:
            if latest_dates[sym_upper] >= cutoff:
                result.append(sym)
            # else: stale — drop silently
        else:
            result.append(sym)  # no signal history — keep
    return result
```

- [ ] **Step 4: Run ARK auto-add tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_watchlist_automation.py::test_ark_buy_is_added tests/test_watchlist_automation.py::test_ark_sell_is_not_added tests/test_watchlist_automation.py::test_ark_buy_not_duplicated_if_already_on_watchlist tests/test_watchlist_automation.py::test_non_ark_untracked_signal_not_added -v
```

Expected: all 4 PASS

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_watchlist_automation.py
git commit -m "feat: add _apply_watchlist_automation helper (ARK auto-add + staleness cleanup)"
```

---

## Task 2 — 5-Day Staleness Cleanup Tests

### Files
- Modify: `tests/test_watchlist_automation.py`

- [ ] **Step 1: Add staleness tests to the test file**

Append to `tests/test_watchlist_automation.py`:

```python
# ── Staleness cleanup ─────────────────────────────────────────────

def test_stale_stock_with_no_position_is_removed():
    """Stock with signal older than 5 days and no open position → removed."""
    cache = _cache(watchlist_signals={
        "AAPL": [{"type": "ark", "fund": "ARKK", "action": "buy",
                  "shares": 50, "date": "2026-04-01", "sym": "AAPL"}],
    })
    with patch.object(_app, "load_trade_state", return_value=_empty_state()):
        result = _app._apply_watchlist_automation(cache, ["AAPL"])
    assert "AAPL" not in result


def test_fresh_stock_is_kept():
    """Stock with signal within last 5 days → kept."""
    from datetime import date, timedelta
    fresh_date = (date.today() - timedelta(days=2)).strftime("%Y-%m-%d")
    cache = _cache(watchlist_signals={
        "NVDA": [{"type": "ark", "fund": "ARKK", "action": "buy",
                  "shares": 50, "date": fresh_date, "sym": "NVDA"}],
    })
    with patch.object(_app, "load_trade_state", return_value=_empty_state()):
        result = _app._apply_watchlist_automation(cache, ["NVDA"])
    assert "NVDA" in result


def test_held_stock_never_removed_even_if_stale():
    """Open position protects stock from staleness removal."""
    cache = _cache(watchlist_signals={
        "MSFT": [{"type": "ark", "fund": "ARKK", "action": "buy",
                  "shares": 50, "date": "2026-01-01", "sym": "MSFT"}],
    })
    state_with_holding = {"holdings": {"MSFT": {"shares": 10, "avgCost": 300}}}
    with patch.object(_app, "load_trade_state", return_value=state_with_holding):
        result = _app._apply_watchlist_automation(cache, ["MSFT"])
    assert "MSFT" in result


def test_manually_added_stock_with_no_signal_history_is_kept():
    """Stocks never seen in any signal are left alone (no signal history = keep)."""
    cache = _cache()  # empty signals
    with patch.object(_app, "load_trade_state", return_value=_empty_state()):
        result = _app._apply_watchlist_automation(cache, ["AMZN"])
    assert "AMZN" in result


def test_empty_watchlist_returns_empty():
    cache = _cache()
    with patch.object(_app, "load_trade_state", return_value=_empty_state()):
        result = _app._apply_watchlist_automation(cache, [])
    assert result == []
```

- [ ] **Step 2: Run all tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_watchlist_automation.py -v
```

Expected: all 9 tests PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_watchlist_automation.py
git commit -m "test: add staleness cleanup + edge case tests for watchlist automation"
```

---

## Task 3 — Wire Into Signal Refresh Call Sites

### Files
- Modify: `app.py` (2 locations)

- [ ] **Step 1: Update `cron_signals()` (around line 2023)**

Find this block:
```python
        cfg   = load_signal_config()
        prev  = load_signal_cache()
        cache = _refresh_signals(load_watchlist(), cfg, prev, QUIVER_KEY)
        save_signal_cache(cache)
```

Replace with:
```python
        cfg  = load_signal_config()
        prev = load_signal_cache()
        wl   = load_watchlist()
        cache = _refresh_signals(wl, cfg, prev, QUIVER_KEY)
        wl = _apply_watchlist_automation(cache, wl)
        save_watchlist(wl)
        save_signal_cache(cache)
```

- [ ] **Step 2: Update the `refreshSignals` action handler (around line 2265)**

Find this block:
```python
    if action == "refreshSignals":
        cfg   = load_signal_config()
        prev  = load_signal_cache()
        cache = _refresh_signals(load_watchlist(), cfg, prev, QUIVER_KEY)
        save_signal_cache(cache)
        return cache
```

Replace with:
```python
    if action == "refreshSignals":
        cfg  = load_signal_config()
        prev = load_signal_cache()
        wl   = load_watchlist()
        cache = _refresh_signals(wl, cfg, prev, QUIVER_KEY)
        wl = _apply_watchlist_automation(cache, wl)
        save_watchlist(wl)
        save_signal_cache(cache)
        return cache
```

- [ ] **Step 3: Run the full test suite to confirm nothing is broken**

```bash
.venv/bin/python -m pytest tests/ -v
```

Expected: all tests pass (no regressions)

- [ ] **Step 4: Commit**

```bash
git add app.py
git commit -m "feat: wire watchlist auto-management into signal refresh (ARK auto-add + staleness cleanup)"
```

---

## Self-Review Checklist

- [x] **Spec coverage:** ARK buy → add ✓ | ARK sell → not added ✓ | fund manager → not added ✓ | 5-day staleness ✓ | safety override (held stocks) ✓ | no signal history → keep ✓ | silent removal ✓ | both cron and manual refresh wired ✓
- [x] **Placeholder scan:** No TBDs or TODOs in plan steps
- [x] **Type consistency:** `_apply_watchlist_automation(cache: dict, watchlist: list[str]) -> list[str]` used consistently across all tasks
