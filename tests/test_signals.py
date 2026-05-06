"""Tests for signals.py — insider, politician, and ARK signal fetching."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest
import signals


# ── Helpers ───────────────────────────────────────────────────────────────────

FORM4_XML_BUY = """\
<?xml version="1.0"?>
<ownershipDocument>
  <periodOfReport>2026-05-01</periodOfReport>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>Jensen Huang</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship>
      <isOfficer>true</isOfficer>
      <officerTitle>CEO</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <aff10b5One>false</aff10b5One>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>20000</value></transactionShares>
        <transactionPricePerShare><value>1084</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""

FORM4_XML_SELL = """\
<?xml version="1.0"?>
<ownershipDocument>
  <periodOfReport>2026-04-30</periodOfReport>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>Some CFO</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship>
      <isOfficer>true</isOfficer>
      <officerTitle>CFO</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <aff10b5One>true</aff10b5One>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>5000</value></transactionShares>
        <transactionPricePerShare><value>1090</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""

FORM4_XML_AWARD = """\
<?xml version="1.0"?>
<ownershipDocument>
  <periodOfReport>2026-04-29</periodOfReport>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>Some Dir</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>1</isDirector></reportingOwnerRelationship>
  </reportingOwner>
  <aff10b5One>false</aff10b5One>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionCoding><transactionCode>A</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>100</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""


def test_parse_form4_xml_filters_buy_sell():
    buys  = signals._parse_form4_xml(FORM4_XML_BUY, "2026-05-03")
    sells = signals._parse_form4_xml(FORM4_XML_SELL, "2026-05-02")
    award = signals._parse_form4_xml(FORM4_XML_AWARD, "2026-05-01")
    assert buys[0]["action"] == "buy"
    assert sells[0]["action"] == "sell"
    assert award == []  # award code 'A' excluded


def test_parse_form4_xml_fields():
    result = signals._parse_form4_xml(FORM4_XML_BUY, "2026-05-03")
    assert len(result) == 1
    s = result[0]
    assert s["who"] == "Jensen Huang"
    assert s["role"] == "CEO"
    assert s["amount"] == 20_000 * 1084
    assert s["shares"] == 20_000
    assert s["date"] == "2026-05-01"
    assert s["filing_date"] == "2026-05-03"
    assert s["type"] == "insider"
    assert s["is_plan"] is False


def test_parse_form4_xml_is_plan_flag():
    result = signals._parse_form4_xml(FORM4_XML_SELL, "2026-05-02")
    assert result[0]["is_plan"] is True


def test_parse_form4_xml_skips_tiny_trades():
    tiny_xml = """\
<?xml version="1.0"?>
<ownershipDocument>
  <periodOfReport>2026-05-01</periodOfReport>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>Bob</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><officerTitle>VP</officerTitle></reportingOwnerRelationship>
  </reportingOwner>
  <aff10b5One>false</aff10b5One>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>10</value></transactionShares>
        <transactionPricePerShare><value>100</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""
    result = signals._parse_form4_xml(tiny_xml, "2026-05-01")
    assert result == []  # $1,000 trade < $100K threshold


FAKE_TICKERS = {"0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA Corp"}}
FAKE_SUBMISSIONS = {
    "filings": {"recent": {
        "form":            ["4",                     "10-K"],
        "filingDate":      ["2026-05-03",            "2026-04-01"],
        "accessionNumber": ["0001234567-26-000001",  "0001234567-26-000000"],
    }}
}


def test_fetch_insider_trades_uses_edgar(monkeypatch):
    monkeypatch.setattr(signals.time, "sleep", lambda x: None)
    signals._ticker_cik_cache.clear()

    def fake_get(url, **kw):
        class R:
            def raise_for_status(self): pass
            def json(self_):
                if "company_tickers" in url:
                    return FAKE_TICKERS
                return FAKE_SUBMISSIONS
            text = FORM4_XML_BUY
        return R()

    monkeypatch.setattr(signals.requests, "get", fake_get)
    result = signals.fetch_insider_trades(["NVDA"])
    assert "NVDA" in result
    assert result["NVDA"][0]["who"] == "Jensen Huang"


def test_fetch_insider_trades_handles_http_error(monkeypatch):
    monkeypatch.setattr(signals.time, "sleep", lambda x: None)
    signals._ticker_cik_cache.clear()
    import requests as req
    monkeypatch.setattr(signals.requests, "get",
                        lambda *a, **kw: (_ for _ in ()).throw(req.RequestException("timeout")))
    result = signals.fetch_insider_trades(["NVDA"])
    assert result == {}  # graceful failure, no crash


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

ARK_TRADES_RESPONSE = {
    "symbol": "ARKK",
    "trades": [
        {"fund": "ARKK", "date": "2026-05-05", "ticker": "NVDA",
         "direction": "Buy",  "shares": 50_000},
        {"fund": "ARKK", "date": "2026-05-05", "ticker": "PLTR",
         "direction": "Buy",  "shares": 300_000},
        {"fund": "ARKK", "date": "2026-05-05", "ticker": "TSLA",
         "direction": "Sell", "shares": 10_000},
        {"fund": "ARKK", "date": "2026-05-05", "ticker": "",   # no ticker → skip
         "direction": "Buy",  "shares": 5_000},
    ]
}


def test_fetch_ark_trades_splits_watchlist_vs_untracked(monkeypatch):
    monkeypatch.setattr(signals.time, "sleep", lambda x: None)

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return ARK_TRADES_RESPONSE

    monkeypatch.setattr(signals.requests, "get", lambda *a, **kw: FakeResp())

    wl_matches, untracked, today_h = signals.fetch_ark_trades(
        funds=["ARKK"],
        watchlist=["NVDA"],
    )
    # NVDA in watchlist → watchlist_matches
    assert "NVDA" in wl_matches
    assert wl_matches["NVDA"][0]["action"] == "buy"
    assert wl_matches["NVDA"][0]["shares"] == 50_000
    # PLTR/TSLA not in watchlist → untracked
    assert any(s["sym"] == "PLTR" for s in untracked)
    assert any(s["sym"] == "TSLA" for s in untracked)
    # today_h is always empty (holdings diff no longer used)
    assert today_h == {}


def test_fetch_ark_trades_skips_unknown_fund(monkeypatch):
    monkeypatch.setattr(signals.time, "sleep", lambda x: None)
    wl, untracked, today_h = signals.fetch_ark_trades(
        funds=["FAKEFUND"],
        watchlist=["NVDA"],
    )
    assert wl == {}
    assert today_h == {}


# ── refresh_signals orchestrator tests ────────────────────────────────────────

def test_refresh_signals_merges_all_sources(monkeypatch):
    monkeypatch.setattr(signals, "fetch_insider_trades",
                        lambda syms: {"NVDA": [{"type": "insider", "date": "2026-05-01",
                                                 "action": "buy", "amount": 2_000_000,
                                                 "shares": 10_000, "who": "CEO", "role": "CEO",
                                                 "filing_date": "2026-05-03", "is_plan": False}]})
    monkeypatch.setattr(signals, "fetch_politician_trades",
                        lambda politicians, watchlist, quiver_key:
                            ({"NVDA": [{"type": "politician", "date": "2026-05-02",
                                         "action": "buy", "amount_range": "$500K-$1M",
                                         "who": "Pelosi", "role": "D-CA", "sym": "NVDA"}]},
                             [{"type": "politician", "sym": "TSM", "date": "2026-05-03",
                               "action": "buy", "amount_range": "$1M-$5M",
                               "who": "Pelosi", "role": "D-CA"}]))
    monkeypatch.setattr(signals, "fetch_ark_trades",
                        lambda funds, watchlist, prev_ark_holdings:
                            ({}, [], {}))

    result = signals.refresh_signals(
        watchlist=["NVDA", "AAPL"],
        config={"politicians": ["Nancy Pelosi"], "ark_funds": ["ARKK"]},
        prev_cache={},
        quiver_key="testkey",
    )

    assert "fetched_at" in result
    assert result["partial"] is False
    # NVDA should have both insider + politician signals merged
    assert len(result["watchlist_signals"]["NVDA"]) == 2
    # TSM is untracked
    assert any(s["sym"] == "TSM" for s in result["untracked_signals"])


def test_refresh_signals_partial_on_failure(monkeypatch):
    def bad_fetch(syms):
        raise RuntimeError("network error")
    monkeypatch.setattr(signals, "fetch_insider_trades", bad_fetch)
    monkeypatch.setattr(signals, "fetch_politician_trades",
                        lambda politicians, watchlist, quiver_key: ({}, []))
    monkeypatch.setattr(signals, "fetch_ark_trades",
                        lambda funds, watchlist, prev_ark_holdings: ({}, [], {}))

    result = signals.refresh_signals(
        watchlist=["NVDA"],
        config={},
        prev_cache={},
        quiver_key="",
    )
    assert result["partial"] is True


# ── Fund Manager 13F tests ────────────────────────────────────────────────────

INFOTABLE_XML = """\
<?xml version="1.0" ?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <infoTable>
    <nameOfIssuer>ALPHABET INC</nameOfIssuer>
    <titleOfClass>CAP STK CL C</titleOfClass>
    <cusip>02079K107</cusip>
    <value>1934222720</value>
    <shrsOrPrnAmt>
      <sshPrnamt>6163871</sshPrnamt>
      <sshPrnamtType>SH</sshPrnamtType>
    </shrsOrPrnAmt>
    <investmentDiscretion>SOLE</investmentDiscretion>
  </infoTable>
  <infoTable>
    <nameOfIssuer>AMAZON COM INC</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>023135106</cusip>
    <value>2217677936</value>
    <shrsOrPrnAmt>
      <sshPrnamt>9607824</sshPrnamt>
      <sshPrnamtType>SH</sshPrnamtType>
    </shrsOrPrnAmt>
    <investmentDiscretion>SOLE</investmentDiscretion>
  </infoTable>
</informationTable>"""


def test_parse_13f_infotable_returns_cusip_map():
    result = signals._parse_13f_infotable(INFOTABLE_XML)
    assert "02079K107" in result
    assert result["02079K107"]["issuer"] == "ALPHABET INC"
    assert result["02079K107"]["shares"] == 6_163_871
    assert result["02079K107"]["value"] == 1_934_222_720


def test_parse_13f_infotable_two_entries():
    result = signals._parse_13f_infotable(INFOTABLE_XML)
    assert len(result) == 2
    assert "023135106" in result


def test_parse_13f_infotable_empty_xml():
    xml = '<?xml version="1.0"?><informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable"></informationTable>'
    result = signals._parse_13f_infotable(xml)
    assert result == {}


def test_parse_13f_infotable_bad_xml_returns_empty():
    result = signals._parse_13f_infotable("not xml at all <><")
    assert result == {}


def test_fund_manager_registry_contains_known_managers():
    names = set(signals.FUND_MANAGER_REGISTRY.keys())
    assert "Michael Burry" in names
    assert "Bill Ackman" in names
    assert "Carl Icahn" in names
    assert "Stanley Druckenmiller" in names
    assert "Warren Buffett" in names
    assert "George Soros" in names


def test_fund_manager_registry_has_required_fields():
    for name, info in signals.FUND_MANAGER_REGISTRY.items():
        assert "cik" in info, f"{name} missing cik"
        assert "fund" in info, f"{name} missing fund"
        assert len(info["cik"]) == 10 and info["cik"].isdigit(), f"{name} cik must be exactly 10 digits, got '{info['cik']}'"
