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
