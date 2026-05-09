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


def test_ark_sell_does_not_reset_staleness_clock():
    """A newer sell signal does not extend the life of a stale buy signal."""
    cache = _cache(watchlist_signals={
        "TSLA": [
            {"type": "ark", "fund": "ARKK", "action": "buy",
             "shares": 50, "date": "2026-04-01", "sym": "TSLA"},
            {"type": "ark", "fund": "ARKK", "action": "sell",
             "shares": 50, "date": "2026-05-08", "sym": "TSLA"},
        ],
    })
    with patch.object(_app, "load_trade_state", return_value=_empty_state()):
        result = _app._apply_watchlist_automation(cache, ["TSLA"])
    assert "TSLA" not in result


def test_boundary_signal_exactly_5_days_old_is_kept():
    """Signal dated exactly 5 days ago is on the boundary — stock must be kept."""
    from datetime import date, timedelta
    boundary_date = (date.today() - timedelta(days=5)).strftime("%Y-%m-%d")
    cache = _cache(watchlist_signals={
        "AMZN": [{"type": "ark", "fund": "ARKK", "action": "buy",
                  "shares": 50, "date": boundary_date, "sym": "AMZN"}],
    })
    with patch.object(_app, "load_trade_state", return_value=_empty_state()):
        result = _app._apply_watchlist_automation(cache, ["AMZN"])
    assert "AMZN" in result
