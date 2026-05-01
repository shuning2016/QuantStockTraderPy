"""Regression tests for execute_decisions gates (C1).

Each fixture is a parsed AI decision plus the expected outcome marker.
We exercise execute_decisions directly so the gate logic is covered without
re-running the AI parser on every test.

Run with: pytest tests/ -v
"""
import json
import os
import sys
from pathlib import Path

# Make repo root importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import strategy_v6 as S  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "ai_outputs.jsonl"


def _make_state(regime="Trend", cash=100_000.0):
    st = S.new_trade_state()
    st["cash"] = cash
    st["currentRegime"] = regime
    st["_today"] = "2026-05-01"
    st["_nowET"] = "10:15"
    return st


def _load_fixtures():
    out = []
    with open(FIXTURES) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def test_fixtures_load():
    assert _load_fixtures(), "fixtures file empty"


def _run_one(fx):
    state    = _make_state(regime=fx.get("regime", "Trend"))
    session  = fx.get("session", "opening")
    provider = fx.get("provider", "grok")
    sym      = fx["decision"].get("symbol") or "AAPL"
    prices   = {sym: fx["price"]} if fx["price"] else {}
    atr_est  = {sym: fx["atr"]} if fx["atr"] else {}
    return S.execute_decisions(
        [fx["decision"]], state, session, prices, atr_est,
        provider=provider, account_ctx={"total_open_positions": 0},
    )


def test_each_fixture():
    failures = []
    for fx in _load_fixtures():
        executed = _run_one(fx)
        joined = "\n".join(executed)
        if fx["expect_marker"] not in joined:
            failures.append(
                f"[{fx['id']}] expected marker '{fx['expect_marker']}' "
                f"not found in:\n  {joined}"
            )
    assert not failures, "\n".join(failures)


def test_provider_overrides_present():
    for p in ("grok", "claude", "deepseek"):
        assert p in S.PROVIDER_OVERRIDES
    # Claude must be tighter than the others
    assert S.PROVIDER_OVERRIDES["claude"]["MAX_SINGLE_RATIO"] < \
           S.PROVIDER_OVERRIDES["grok"]["MAX_SINGLE_RATIO"]
    assert S.PROVIDER_OVERRIDES["claude"]["DAILY_LOSS_CIRCUIT_PCT"] < \
           S.PROVIDER_OVERRIDES["grok"]["DAILY_LOSS_CIRCUIT_PCT"]
    # DeepSeek scales out more aggressively
    assert S.PROVIDER_OVERRIDES["deepseek"]["SCALE_OUT_FRACTION"] > \
           S.PROVIDER_OVERRIDES["grok"]["SCALE_OUT_FRACTION"]
    # Grok trail tightest
    assert S.PROVIDER_OVERRIDES["grok"]["TRAIL_1R_MULT"] < \
           S.PROVIDER_OVERRIDES["claude"]["TRAIL_1R_MULT"]


def test_get_provider_cfg_fallthrough():
    # unknown key falls through to CFG default
    assert S.get_provider_cfg("grok", "MAX_HOLDINGS", -1) == S.CFG.MAX_HOLDINGS
    # unknown provider also falls through
    assert S.get_provider_cfg("nonesuch", "MAX_SINGLE_RATIO", -1) == S.CFG.MAX_SINGLE_RATIO


def test_b6_circuit_breaker_blocks_buy():
    state = _make_state()
    nav = S.calc_nav(state)
    # Set today P&L to -3.5% so Grok's -3% breaker fires
    state["dailyPnL"] = {"2026-05-01": -nav * 0.035}
    decisions = [{"action": "BUY", "symbol": "AAPL", "shares": 0,
                  "reason": "Vol:50M Ratio:2.5× ATR:3.50 Stop:194.75 RR:2.5 C:8/10",
                  "confidence": 8, "parse_mode": "structured"}]
    out = S.execute_decisions(decisions, state, "opening",
                               {"AAPL": 200.0}, {"AAPL": 3.5},
                               provider="grok",
                               account_ctx={"total_open_positions": 0})
    assert any("熔断" in line for line in out), out


def test_b7_account_max_positions_blocks_buy():
    state = _make_state()
    decisions = [{"action": "BUY", "symbol": "AAPL", "shares": 0,
                  "reason": "Vol:50M Ratio:2.5× ATR:3.50 Stop:194.75 RR:2.5 C:8/10",
                  "confidence": 8, "parse_mode": "structured"}]
    out = S.execute_decisions(decisions, state, "opening",
                               {"AAPL": 200.0}, {"AAPL": 3.5},
                               provider="grok",
                               account_ctx={"total_open_positions": 5})
    assert any("账户级总持仓" in line for line in out), out


def test_b5_confidence_size_mult():
    # C:6 should be smaller than C:7, C:8 should be larger
    s6 = S.calc_position_size(100_000, 100, 2.0, "Trend", "grok", 6)
    s7 = S.calc_position_size(100_000, 100, 2.0, "Trend", "grok", 7)
    s8 = S.calc_position_size(100_000, 100, 2.0, "Trend", "grok", 8)
    assert s6["shares"] < s7["shares"] <= s8["shares"]


def test_b4_claude_smaller_position():
    # Claude's 15% cap should produce ≤ Grok's 20% cap
    s_claude = S.calc_position_size(100_000, 100, 2.0, "Trend", "claude", 7)
    s_grok   = S.calc_position_size(100_000, 100, 2.0, "Trend", "grok", 7)
    # at $100/share with 15% cap = 150 shares vs 200 shares; risk-based may be smaller
    # but the cap should never let Claude exceed Grok
    assert s_claude["shares"] <= s_grok["shares"]


def test_a1_scale_out_at_1r():
    """+1R triggers a partial exit, runner remains."""
    state = _make_state()
    state["holdings"]["AAPL"] = {
        "shares": 10, "avgCost": 100.0, "stopPrice": 97.0,
        "entryAtr": 2.0, "riskPerShare": 3.0,
        "highPrice": 103.5, "entryTime": "10:00",
        "confidence": 7, "timeframe": "SWING",
    }
    state["lastPrices"] = {"AAPL": 103.5}  # +1.16R
    sells = S.check_auto_stop_rules(state, "opening", provider="grok")
    assert any(s.get("tag") == "SCALE_OUT_1R" for s in sells), sells
    scale = next(s for s in sells if s["tag"] == "SCALE_OUT_1R")
    assert 1 <= scale["shares"] < 10  # partial, not full


def test_a2_grok_tighter_trail():
    """Grok's 1.0× trail should produce a higher stop than Claude's 2.0× trail."""
    def _make(provider):
        st = _make_state()
        st["holdings"]["AAPL"] = {
            "shares": 10, "avgCost": 100.0, "stopPrice": 97.0,
            "entryAtr": 2.0, "riskPerShare": 3.0,
            "highPrice": 110.0, "entryTime": "10:00",
            "confidence": 7, "timeframe": "SWING",
            # Pre-mark partial_taken so scale-out doesn't fire and we observe trail only
            "partial_taken": "2026-05-01",
        }
        st["lastPrices"] = {"AAPL": 110.0}
        S.check_auto_stop_rules(st, "opening", provider=provider)
        return st["holdings"]["AAPL"]["stopPrice"]
    assert _make("grok") > _make("claude")


def test_a5_profitable_reentry_skips_cooldown():
    state = _make_state()
    state["cooldowns"] = {"AAPL": "2026-04-30"}  # 1 business day ago
    state["post_exit_watch"] = {"AAPL": {"pnl_pct": 3.5}}
    decisions = [{"action": "BUY", "symbol": "AAPL", "shares": 0,
                  "reason": "Vol:50M Ratio:2.5× ATR:3.50 Stop:194.75 RR:2.5 C:7/10",
                  "confidence": 7, "parse_mode": "structured"}]
    out = S.execute_decisions(decisions, state, "opening",
                               {"AAPL": 200.0}, {"AAPL": 3.5},
                               provider="grok",
                               account_ctx={"total_open_positions": 0})
    # A5 should let it pass (we expect a buy executed, or at least the A5 note)
    joined = "\n".join(out)
    assert "A5再入场" in joined or "✅ 买入" in joined, out
