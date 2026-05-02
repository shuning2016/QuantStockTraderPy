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


def test_batch_fetch_prices_handles_none_quote(monkeypatch):
    monkeypatch.setattr(app, "get_stock_quote", lambda sym: None)
    monkeypatch.setattr(app.time, "sleep", lambda x: None)

    result = app._batch_fetch_prices(["AAPL"])

    assert result == {}


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


def _make_state_with_holding(sym="AAPL", avg_cost=100.0, stop_price=97.0, shares=10):
    state = S.new_trade_state()
    state["holdings"][sym] = _make_holding(avg_cost=avg_cost, stop_price=stop_price, shares=shares)
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
    # shares=1 bypasses SCALE_OUT_1R (requires shares >= 2), letting
    # execution reach the HARD_PROFIT check at +5%.
    state = _make_state_with_holding("AAPL", avg_cost=100.0, stop_price=97.0, shares=1)
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


def test_execute_guardian_sell_updates_state_fields():
    state = _make_state_for_sell()
    sell  = {"sym": "AAPL", "shares": 10, "reason": "追踪止损$97.00", "tag": "STOP_LOSS"}

    app._execute_guardian_sell(state, sell, price=96.5,
                               today="2026-05-02", now_et="11:00")

    assert state["dailyPnL"]["2026-05-02"] == pytest.approx((96.5 - 100.0) * 10, rel=1e-6)
    assert state["cooldowns"]["AAPL"] == "2026-05-02"
    assert state["post_exit_watch"]["AAPL"]["exit_price"] == 96.5
    assert state["post_exit_watch"]["AAPL"]["exit_date"] == "2026-05-02"
    assert state["post_exit_watch"]["AAPL"]["avg_cost"] == 100.0
    assert state["post_exit_watch"]["AAPL"]["pnl_pct"] == pytest.approx(-3.5, rel=1e-4)
    assert "log_id" in state["post_exit_watch"]["AAPL"]


def test_execute_guardian_sell_zero_avg_cost_safe():
    state = _make_state_for_sell()
    state["holdings"]["AAPL"]["avgCost"] = 0.0
    sell  = {"sym": "AAPL", "shares": 10, "reason": "追踪止损$0.00", "tag": "STOP_LOSS"}

    app._execute_guardian_sell(state, sell, price=5.0,
                               today="2026-05-02", now_et="11:00")

    assert state["post_exit_watch"]["AAPL"]["pnl_pct"] == 0
