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
