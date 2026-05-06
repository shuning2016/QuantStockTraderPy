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


QUIVER_RESPONSE = [
    {"Date": "2026-05-03", "Ticker": "NVDA", "Representative": "Nancy Pelosi",
     "Transaction": "Purchase", "Range": "$500,001-$1,000,000",
     "Party": "D", "State": "CA"},
    {"Date": "2026-05-01", "Ticker": "TSM", "Representative": "Nancy Pelosi",
     "Transaction": "Purchase", "Range": "$250,001-$500,000",
     "Party": "D", "State": "CA"},
    {"Date": "2026-04-28", "Ticker": "AAPL", "Representative": "Other Person",
     "Transaction": "Sale", "Range": "$1,000,001-$5,000,000",
     "Party": "R", "State": "TX"},
    # Old trade — should be excluded (set date far in past)
    {"Date": "2025-01-01", "Ticker": "MSFT", "Representative": "Nancy Pelosi",
     "Transaction": "Purchase", "Range": "$100,001-$250,000",
     "Party": "D", "State": "CA"},
]


def test_fetch_politician_trades_splits_watchlist_vs_untracked(monkeypatch):
    monkeypatch.setattr(signals.time, "sleep", lambda x: None)

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return QUIVER_RESPONSE

    monkeypatch.setattr(signals.requests, "get", lambda *a, **kw: FakeResp())

    wl_matches, untracked = signals.fetch_politician_trades(
        politicians=["Nancy Pelosi"],
        watchlist=["NVDA", "AAPL"],
        quiver_key="testkey",
    )
    # NVDA is in watchlist → watchlist_matches
    assert "NVDA" in wl_matches
    assert wl_matches["NVDA"][0]["action"] == "buy"
    # TSM is NOT in watchlist → untracked
    assert any(s["sym"] == "TSM" for s in untracked)
    # Other Person is not in politicians list → excluded
    assert "AAPL" not in wl_matches
    # Old trade (2025) → excluded
    assert not any(s["sym"] == "MSFT" for s in untracked)


def test_fetch_politician_trades_no_key_returns_empty(monkeypatch):
    wl, untracked = signals.fetch_politician_trades(
        politicians=["Nancy Pelosi"],
        watchlist=["NVDA"],
        quiver_key="",
    )
    assert wl == {}
    assert untracked == []


# ── ARK tests ─────────────────────────────────────────────────────────────────

ARK_CSV_TODAY = """\
date,fund,company,ticker,cusip,shares,market value ($),weight (%)
05/06/2026,ARKK,NVIDIA Corp,NVDA,67066G104,"200000","$216000000",7.0%
05/06/2026,ARKK,Tesla Inc,TSLA,88160R101,"500000","$120000000",4.0%
05/06/2026,ARKK,Palantir,PLTR,69608A108,"300000","$15000000",0.5%
"""

ARK_CSV_PREV = {
    "ARKK": {
        "NVDA": 150_000,   # bought 50K more → buy signal
        "TSLA": 510_000,   # sold 10K → exactly at threshold, included as sell
        # PLTR not present yesterday → new position of 300K → buy signal
    }
}


def test_fetch_ark_trades_detects_net_changes(monkeypatch):
    monkeypatch.setattr(signals.time, "sleep", lambda x: None)

    class FakeResp:
        text = ARK_CSV_TODAY
        def raise_for_status(self): pass

    monkeypatch.setattr(signals.requests, "get", lambda *a, **kw: FakeResp())

    wl_matches, untracked, today_h = signals.fetch_ark_trades(
        funds=["ARKK"],
        watchlist=["NVDA"],
        prev_ark_holdings=ARK_CSV_PREV,
    )
    # NVDA: in watchlist, net +50K → buy in watchlist_matches
    assert "NVDA" in wl_matches
    assert wl_matches["NVDA"][0]["action"] == "buy"
    assert wl_matches["NVDA"][0]["shares"] == 50_000
    # PLTR: not in watchlist, net +300K → untracked
    assert any(s["sym"] == "PLTR" for s in untracked)
    # today_holdings should reflect parsed CSV
    assert today_h["ARKK"]["NVDA"] == 200_000


def test_parse_ark_csv_handles_comma_shares():
    result = signals._parse_ark_csv(ARK_CSV_TODAY)
    assert result["NVDA"] == 200_000
    assert result["TSLA"] == 500_000


def test_fetch_ark_trades_skips_unknown_fund(monkeypatch):
    monkeypatch.setattr(signals.time, "sleep", lambda x: None)
    wl, untracked, today_h = signals.fetch_ark_trades(
        funds=["FAKEFUND"],
        watchlist=["NVDA"],
        prev_ark_holdings={},
    )
    assert wl == {}
    assert today_h == {}
