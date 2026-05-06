"""Tests for signals.py — insider, politician, and ARK signal fetching."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest
import signals


# ── Helpers ───────────────────────────────────────────────────────────────────

INSIDER_CSV = """\
X,Filing Date,Trade Date,Ticker,Company Name,Insider Name,Title,Trade Type,Price,Qty,Owned,ΔOwn,Value
1,2026-05-03,2026-05-01,NVDA,NVIDIA Corp,Jensen Huang,CEO,P - Purchase,$1084.00,"20,000","1,000,000",+2%,"$21,680,000"
2,2026-05-02,2026-04-30,NVDA,NVIDIA Corp,Some CFO,CFO,S - Sale,$1090.00,"5,000","500,000",-1%,"$5,450,000"
3,2026-05-01,2026-04-29,NVDA,NVIDIA Corp,Some Dir,Director,A - Award,$100.00,"1,000","200,000",+0.5%,"$100,000"
"""


def test_parse_insider_csv_filters_buy_sell():
    result = signals._parse_insider_csv("NVDA", INSIDER_CSV)
    actions = [s["action"] for s in result]
    assert "buy" in actions
    assert "sell" in actions
    # Award type should be excluded
    assert len(result) == 2


def test_parse_insider_csv_fields():
    result = signals._parse_insider_csv("NVDA", INSIDER_CSV)
    buy = next(s for s in result if s["action"] == "buy")
    assert buy["who"] == "Jensen Huang"
    assert buy["role"] == "CEO"
    assert buy["amount"] == 21_680_000
    assert buy["shares"] == 20_000
    assert buy["date"] == "2026-05-01"
    assert buy["type"] == "insider"
    assert buy["is_plan"] is False
    assert buy["filing_date"] == "2026-05-03"


def test_fetch_insider_trades_uses_sleep(monkeypatch):
    sleep_calls = []
    monkeypatch.setattr(signals.time, "sleep", lambda x: sleep_calls.append(x))

    class FakeResp:
        text = INSIDER_CSV
        def raise_for_status(self): pass

    monkeypatch.setattr(signals.requests, "get", lambda *a, **kw: FakeResp())
    result = signals.fetch_insider_trades(["NVDA", "AAPL"])
    assert "NVDA" in result
    assert len(sleep_calls) == 2  # one per symbol


def test_fetch_insider_trades_handles_http_error(monkeypatch):
    monkeypatch.setattr(signals.time, "sleep", lambda x: None)
    import requests as req
    def bad_get(*a, **kw):
        raise req.RequestException("timeout")
    monkeypatch.setattr(signals.requests, "get", bad_get)
    result = signals.fetch_insider_trades(["NVDA"])
    assert result == {}  # graceful failure, no crash


def test_parse_insider_csv_empty_returns_empty_list():
    empty_csv = "X,Filing Date,Trade Date,Ticker,Company Name,Insider Name,Title,Trade Type,Price,Qty,Owned,ΔOwn,Value\n"
    result = signals._parse_insider_csv("NVDA", empty_csv)
    assert result == []
