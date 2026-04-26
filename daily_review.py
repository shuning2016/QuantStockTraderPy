"""
daily_review.py — Daily Execution Health Check (bug detection only).

PURPOSE
-------
Catch code and system failures within ONE trading day, before they
compound across multiple sessions.  This is NOT a strategy review —
no win rates, no P&L conclusions, no parameter recommendations.

SCOPE (10 deterministic checks — all yes/no, no statistical inference)
------
CHK-1   Session completeness   All 4 sessions (premarket/opening/mid/closing)
                               fired today for each provider?
CHK-2   Watchlist coverage     Did the AI score all 6 watchlist stocks in its
                               SCORE section, or skip some?
CHK-3   DECISION parse quality Were DECISION blocks pipe-delimited (structured)?
                               Any prose_fallback or synthetic_hold?
CHK-4   BUY confidence logging Any executed BUY logged with confidence = 0?
                               (indicates parse_confidence_score failed)
CHK-5   ATR plausibility       Is each open position's entryAtr in the
                               0.3%–8% band relative to its avg cost?
CHK-6   Stop above entry       Any LONG position where stopPrice ≥ avgCost?
                               (structural bug — guarantees instant stop-out)
CHK-7   R:R gate enforcement   Any executed BUY whose logged reason shows RR < 2.0?
                               (R:R gate should have blocked it)
CHK-8   Same-day re-entry      Same symbol bought twice today by the same provider
                               without the 48h cooldown blocking it?
CHK-9   Premarket→opening      Was premarket_focus set in state?  If not, the
         handoff                opening session had no continuity from premarket.
CHK-10  Prose SELL entries     Any SELL trade log entry with parse_error = True?
                               (DECISION block failed to parse on closing/mid sell)

HOW TO USE
----------
Via the Flask API (no code change needed):
    GET /api/daily-review          →  today's report
    GET /api/daily-review/2026-04-26  →  specific date

The response is both machine-readable JSON and a human-readable plain-text
summary field ("report_text") you can read directly.

DESIGN CONSTRAINTS
------------------
• Pure functions — no Flask, no global state, no side effects.
• All callbacks injected (same pattern as weekly_review.py).
• Does NOT load or modify trade state — read-only.
• Does NOT change any CFG constant or strategy parameter.
• Findings are labelled ok / warn / fail — NEVER "increase threshold" or
  "adjust stop".  Execution bugs only.
"""

import re
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

logger = logging.getLogger("quant.daily")

# ─── Status constants ─────────────────────────────────────────────
_OK   = "ok"
_WARN = "warn"
_FAIL = "fail"

_ALL_SESSIONS  = ["premarket", "opening", "mid", "closing"]
_ALL_PROVIDERS = ["grok", "deepseek", "claude"]

# Expected ATR as fraction of stock price (0.3% – 8%)
_ATR_MIN_RATIO = 0.003
_ATR_MAX_RATIO = 0.08

# ─── Helpers ─────────────────────────────────────────────────────

def _badge(status: str) -> str:
    return {"ok": "✅", "warn": "⚠️ ", "fail": "❌"}.get(status, "❓")


def _parse_rr_from_reason(reason: str) -> Optional[float]:
    """Extract RR=X from a trade log reason string."""
    m = re.search(r'RR\s*[=:]\s*(\d+\.?\d*)', reason, re.IGNORECASE)
    if m:
        return float(m.group(1))
    # Fallback: compute from Target+X% and Stop-Y%
    t_m = re.search(r'[Tt]arget[^|]{0,40}?\+(\d+\.?\d*)%', reason)
    s_m = re.search(r'[Ss]top[^|]{0,40}?-(\d+\.?\d*)%',   reason)
    if t_m and s_m:
        t, s = float(t_m.group(1)), float(s_m.group(1))
        if s > 0:
            return round(t / s, 2)
    return None


def _count_score_lines(ai_text: str) -> int:
    """Count ▸ SYM lines in the AI SCORE output section."""
    return len(re.findall(r'^\s*[▸►▷>]\s*[A-Z]{1,6}', ai_text, re.MULTILINE))


# ─── Individual Checks ───────────────────────────────────────────

def _chk1_session_completeness(session_logs: list, date: str) -> dict:
    """
    CHK-1: Did all 4 sessions fire today for each provider?
    Source: session log entries (one entry per session × provider run).
    """
    fired: dict = {p: set() for p in _ALL_PROVIDERS}
    for entry in session_logs:
        prov = entry.get("ai_provider", "")
        sess = entry.get("session", "")
        if prov in fired and sess in _ALL_SESSIONS:
            fired[prov].add(sess)

    issues = []
    evidence = {}
    for prov in _ALL_PROVIDERS:
        missing = [s for s in _ALL_SESSIONS if s not in fired[prov]]
        found   = sorted(fired[prov])
        evidence[prov] = {"fired": found, "missing": missing}
        if missing:
            issues.append(f"{prov}: missing {missing}")

    if not issues:
        status  = _OK
        summary = f"All 4 sessions fired for all providers"
    elif len(issues) == len(_ALL_PROVIDERS) and all(
        len(fired[p]) == 0 for p in _ALL_PROVIDERS
    ):
        status  = _FAIL
        summary = f"No sessions fired at all on {date} — cron may be down"
    else:
        status  = _WARN
        summary = "; ".join(issues)

    return {"id": "CHK-1", "name": "Session Completeness",
            "status": status, "summary": summary, "evidence": evidence}


def _chk2_watchlist_coverage(session_logs: list, expected_stocks: int) -> dict:
    """
    CHK-2: Did each AI score all watchlist stocks in its SCORE section?
    We count ▸ SYM lines in the AI text from opening and mid sessions.
    A count below expected_stocks means the AI skipped some candidates.
    """
    issues  = []
    evidence = {}
    # Only check sessions where the AI is expected to produce a SCORE section
    for entry in session_logs:
        sess = entry.get("session", "")
        prov = entry.get("ai_provider", "")
        if sess not in ("opening", "mid"):
            continue
        ai_text = entry.get("ai_analysis", "")
        scored  = _count_score_lines(ai_text)
        key     = f"{prov}/{sess}"
        evidence[key] = {"scored": scored, "expected": expected_stocks}
        if scored == 0:
            issues.append(f"{key}: no SCORE lines found (parse failure or AI skipped all)")
        elif scored < expected_stocks:
            issues.append(f"{key}: scored {scored}/{expected_stocks} stocks")

    if not evidence:
        return {"id": "CHK-2", "name": "Watchlist Coverage",
                "status": _WARN, "summary": "No opening/mid sessions found to check",
                "evidence": {}}

    status  = _FAIL if any("no SCORE" in i for i in issues) else (
              _WARN if issues else _OK)
    summary = ("; ".join(issues) if issues
               else f"All sessions scored {expected_stocks}/{expected_stocks} stocks")

    return {"id": "CHK-2", "name": "Watchlist Coverage",
            "status": status, "summary": summary, "evidence": evidence}


def _chk3_decision_parse_quality(session_logs: list) -> dict:
    """
    CHK-3: Were DECISION blocks parsed as structured (pipe-delimited)?
    Prose fallback and synthetic_hold are warning signals.
    """
    counts: dict = {}   # "prov/sess" → {structured, prose_fallback, synthetic_hold, total}
    issues  = []

    for entry in session_logs:
        sess = entry.get("session", "")
        prov = entry.get("ai_provider", "")
        if sess == "premarket":
            continue   # premarket is analysis-only, no DECISION expected
        key  = f"{prov}/{sess}"
        counts.setdefault(key, {"structured": 0, "prose_fallback": 0,
                                "synthetic_hold": 0, "total": 0})
        for item in entry.get("exec_log", []):
            pm = item.get("parse_mode", "")
            if pm == "structured":
                counts[key]["structured"] += 1
            elif pm == "prose_fallback":
                counts[key]["prose_fallback"] += 1
                issues.append(f"{key}: prose_fallback detected")
            elif pm == "synthetic_hold":
                counts[key]["synthetic_hold"] += 1
                issues.append(f"{key}: synthetic_hold (no DECISION block found)")
            counts[key]["total"] += 1

    unique_issues = list(dict.fromkeys(issues))   # deduplicate, preserve order
    status  = (_FAIL if any("prose_fallback" in i for i in unique_issues)
               else _WARN if unique_issues else _OK)
    summary = ("; ".join(unique_issues) if unique_issues
               else "All DECISION blocks parsed as structured")

    return {"id": "CHK-3", "name": "DECISION Parse Quality",
            "status": status, "summary": summary, "evidence": counts}


def _chk4_buy_confidence(trade_logs: list) -> dict:
    """
    CHK-4: Any executed BUY with confidence = 0?
    After our fix, this should never happen for structured BUYs (defaults to 6).
    A zero here means parse_confidence_score failed AND the fallback didn't fire.
    """
    zero_conf = []
    for t in trade_logs:
        if t.get("action") == "BUY" and (t.get("confidence") or 0) == 0:
            zero_conf.append({
                "symbol":   t.get("symbol"),
                "provider": t.get("ai_provider"),
                "session":  t.get("session"),
                "price":    t.get("price"),
                "reason":   (t.get("reason") or "")[:80],
            })

    status  = _FAIL if zero_conf else _OK
    summary = (f"{len(zero_conf)} BUY(s) logged with confidence=0: "
               + ", ".join(f"{z['provider']}/{z['symbol']}" for z in zero_conf)
               if zero_conf else "All BUY entries have non-zero confidence")

    return {"id": "CHK-4", "name": "BUY Confidence Logging",
            "status": status, "summary": summary, "evidence": zero_conf}


def _chk5_atr_plausibility(states: dict) -> dict:
    """
    CHK-5: Is each open position's entryAtr within 0.3%–8% of avgCost?
    An out-of-range ATR means position sizing was computed from a bad value.
    """
    issues   = []
    evidence = {}
    for prov, state in states.items():
        for sym, h in state.get("holdings", {}).items():
            avg   = h.get("avgCost", 0)
            atr   = h.get("entryAtr", None)
            key   = f"{prov}/{sym}"
            if atr is None or avg <= 0:
                evidence[key] = {"atr": atr, "avgCost": avg, "ratio_pct": None}
                issues.append(f"{key}: entryAtr missing")
                continue
            ratio = atr / avg
            evidence[key] = {
                "atr": round(atr, 4), "avgCost": round(avg, 2),
                "ratio_pct": round(ratio * 100, 2),
            }
            if not (_ATR_MIN_RATIO <= ratio <= _ATR_MAX_RATIO):
                issues.append(
                    f"{key}: ATR=${atr:.4f} is {ratio*100:.1f}% of price "
                    f"(expected {_ATR_MIN_RATIO*100:.1f}%–{_ATR_MAX_RATIO*100:.1f}%)"
                )

    status  = _FAIL if issues else _OK
    summary = ("; ".join(issues) if issues
               else "All open positions have plausible ATR values"
               if evidence else "No open positions to check")

    return {"id": "CHK-5", "name": "ATR Plausibility",
            "status": status, "summary": summary, "evidence": evidence}


def _chk6_stop_above_entry(states: dict) -> dict:
    """
    CHK-6: Any LONG position where stopPrice >= avgCost?
    For long positions the stop must always sit BELOW the entry price.
    A stop at or above entry means any downward tick triggers an immediate exit.
    This is a structural bug — almost always caused by a bad ATR estimate.
    """
    bugs     = []
    evidence = {}
    for prov, state in states.items():
        for sym, h in state.get("holdings", {}).items():
            avg  = h.get("avgCost", 0)
            stop = h.get("stopPrice", None)
            key  = f"{prov}/{sym}"
            evidence[key] = {"avgCost": round(avg, 4) if avg else None,
                             "stopPrice": round(stop, 4) if stop is not None else None}
            if stop is not None and avg and stop >= avg:
                gap_pct = (stop - avg) / avg * 100
                bugs.append(
                    f"{key}: stopPrice=${stop:.2f} is ${stop - avg:+.2f} "
                    f"({gap_pct:+.2f}%) ABOVE avgCost=${avg:.2f}"
                )

    status  = _FAIL if bugs else _OK
    summary = ("; ".join(bugs) if bugs
               else "All stop prices are below entry (correct for LONG)"
               if evidence else "No open positions to check")

    return {"id": "CHK-6", "name": "Stop Price vs Entry",
            "status": status, "summary": summary, "evidence": evidence}


def _chk7_rr_gate_enforcement(trade_logs: list) -> dict:
    """
    CHK-7: Any executed BUY whose reason shows RR < 2.0?
    The R:R gate in execute_decisions should have blocked these.
    If they appear in the log it means either the gate was bypassed
    or the AI didn't include RR in the reason string.
    """
    violations = []
    no_rr      = []
    for t in trade_logs:
        if t.get("action") != "BUY":
            continue
        reason = t.get("reason") or ""
        rr = _parse_rr_from_reason(reason)
        entry = {
            "symbol":   t.get("symbol"),
            "provider": t.get("ai_provider"),
            "session":  t.get("session"),
            "rr":       rr,
            "reason":   reason[:100],
        }
        if rr is None:
            no_rr.append(entry)
        elif rr < 2.0:
            violations.append(entry)

    issues = []
    if violations:
        issues.append(
            f"{len(violations)} BUY(s) with RR<2.0 executed: "
            + ", ".join(f"{v['provider']}/{v['symbol']} RR={v['rr']}" for v in violations)
        )
    if no_rr:
        issues.append(
            f"{len(no_rr)} BUY(s) with no RR logged (AI omitted from reason): "
            + ", ".join(f"{v['provider']}/{v['symbol']}" for v in no_rr)
        )

    status = (_FAIL if violations else _WARN if no_rr else _OK)
    summary = ("; ".join(issues) if issues
               else "All executed BUYs have RR ≥ 2.0 logged")

    return {"id": "CHK-7", "name": "R:R Gate Enforcement",
            "status": status, "summary": summary,
            "evidence": {"violations": violations, "missing_rr": no_rr}}


def _chk8_same_day_reentry(trade_logs: list) -> dict:
    """
    CHK-8: Same symbol bought twice in one day by the same provider?
    The 48h cooldown should prevent same-day re-entry after any exit.
    Two BUYs on the same symbol without an intervening SELL is also flagged
    (averaging down is blocked by check_position_rules).
    """
    # Build timeline per provider: track buy/sell events for each symbol
    timeline: dict = {}   # (prov, sym) → list of {action, session, price}
    for t in sorted(trade_logs, key=lambda x: x.get("timestamp", "")):
        prov = t.get("ai_provider", "")
        sym  = t.get("symbol", "")
        act  = t.get("action", "")
        if not sym or act not in ("BUY", "SELL"):
            continue
        key = (prov, sym)
        timeline.setdefault(key, []).append({
            "action": act, "session": t.get("session"), "price": t.get("price")
        })

    violations = []
    for (prov, sym), events in timeline.items():
        buys = [e for e in events if e["action"] == "BUY"]
        if len(buys) >= 2:
            violations.append({
                "provider": prov,
                "symbol":   sym,
                "events":   events,
                "note":     "same symbol bought 2+ times today by same provider",
            })

    status  = _FAIL if violations else _OK
    summary = ("; ".join(
                   f"{v['provider']}/{v['symbol']}: {len([e for e in v['events'] if e['action']=='BUY'])} BUYs"
                   for v in violations
               ) if violations
               else "No same-day re-entries detected")

    return {"id": "CHK-8", "name": "Same-Day Re-entry (Cooldown)",
            "status": status, "summary": summary, "evidence": violations}


def _chk9_premarket_handoff(states: dict) -> dict:
    """
    CHK-9: Was premarket_focus set in state after premarket ran?
    An empty string means the premarket AI either didn't produce a NEXT_ACTION
    line or the extraction regex failed — the opening session then ran blind.
    """
    results  = {}
    warnings = []
    for prov, state in states.items():
        focus = state.get("premarket_focus", None)
        results[prov] = {"premarket_focus": focus}
        if focus is None:
            warnings.append(f"{prov}: premarket_focus key missing (premarket may not have run)")
        elif focus.strip() == "":
            warnings.append(f"{prov}: premarket_focus is empty (NEXT_ACTION not extracted)")

    status  = _WARN if warnings else _OK
    summary = ("; ".join(warnings) if warnings
               else "Premarket focus note populated for all providers: "
                    + " | ".join(f"{p}: '{v['premarket_focus'][:40]}…'" for p, v in results.items()))

    return {"id": "CHK-9", "name": "Premarket→Opening Handoff",
            "status": status, "summary": summary, "evidence": results}


def _chk10_prose_sell_entries(trade_logs: list) -> dict:
    """
    CHK-10: Any SELL trade log entry with parse_error = True?
    This means the AI produced its SELL decision in prose instead of
    pipe-delimited format, so the reason field is unreliable for A07 analysis.
    """
    bad_sells = []
    for t in trade_logs:
        if t.get("action") == "SELL" and t.get("parse_error"):
            bad_sells.append({
                "symbol":   t.get("symbol"),
                "provider": t.get("ai_provider"),
                "session":  t.get("session"),
                "reason":   (t.get("reason") or "")[:100],
            })

    status  = _WARN if bad_sells else _OK
    summary = (f"{len(bad_sells)} SELL(s) with prose_fallback parse: "
               + ", ".join(f"{b['provider']}/{b['symbol']}" for b in bad_sells)
               if bad_sells
               else "All SELL entries have clean structured parse")

    return {"id": "CHK-10", "name": "Prose SELL Entries",
            "status": status, "summary": summary, "evidence": bad_sells}


# ─── Report Builder ──────────────────────────────────────────────

def _build_report_text(date: str, checks: list, pnl_summary: dict) -> str:
    """Render the 10 checks as a human-readable plain-text report."""

    ok_count   = sum(1 for c in checks if c["status"] == _OK)
    warn_count = sum(1 for c in checks if c["status"] == _WARN)
    fail_count = sum(1 for c in checks if c["status"] == _FAIL)

    header = (
        f"╔══════════════════════════════════════════════════════════════╗\n"
        f"║  Daily Execution Health Check — {date:<29}║\n"
        f"╠══════════════════════════════════════════════════════════════╣\n"
        f"║  ✅ {ok_count} OK  │  ⚠️  {warn_count} WARN  │  ❌ {fail_count} FAIL"
        f"{'':>{33 - len(str(ok_count)) - len(str(warn_count)) - len(str(fail_count))}}║\n"
        f"╚══════════════════════════════════════════════════════════════╝\n"
    )

    lines = [header]
    for chk in checks:
        badge = _badge(chk["status"])
        lines.append(f"{badge} {chk['id']:6s} {chk['name']}")
        lines.append(f"         {chk['summary']}")
        lines.append("")

    # P&L section — factual totals only, no conclusions
    lines.append("─" * 64)
    lines.append("📊 Today's P&L (factual summary — no strategy conclusions)")
    lines.append("")
    if not pnl_summary:
        lines.append("   No trades executed today.")
    else:
        for prov, info in pnl_summary.items():
            trades_str = f"{info['trades']} trade(s)"
            pnl_str    = f"${info['realized_pnl']:+.2f}"
            lines.append(f"   {prov:<12} {trades_str:<14} Realized P&L: {pnl_str}")
        total_pnl = sum(v["realized_pnl"] for v in pnl_summary.values())
        lines.append(f"   {'COMBINED':<12} {'':14} Realized P&L: ${total_pnl:+.2f}")
    lines.append("")

    # Action summary
    if fail_count > 0 or warn_count > 0:
        lines.append("─" * 64)
        lines.append("🔧 Action Items (execution bugs only — fix before next session)")
        lines.append("")
        for chk in checks:
            if chk["status"] in (_FAIL, _WARN):
                badge = _badge(chk["status"])
                lines.append(f"  {badge} [{chk['id']}] {chk['summary']}")
        lines.append("")

    lines.append(
        "⚠️  This report covers execution health only.\n"
        "   Strategy quality, win rates, and parameter changes\n"
        "   are evaluated in the weekly review (min 10 trades)."
    )

    return "\n".join(lines)


# ─── Main Entry Point ────────────────────────────────────────────

def run_daily_review(
    date: str,
    read_log_fn: Callable,
    load_state_fn: Callable,
    watchlist: list,
) -> dict:
    """
    Run all 10 execution health checks for a given trading date.

    Args:
        date          : "YYYY-MM-DD" — the trading day to inspect
        read_log_fn   : read_log_range(prefix, from, to, provider=None) → list
                        (same signature as the one in app.py)
        load_state_fn : load_trade_state(provider) → dict
        watchlist     : current watchlist list (to know expected stock count)

    Returns a dict with:
        "date"        : date checked
        "checks"      : list of 10 check result dicts
        "ok" / "warn" / "fail" counts
        "pnl_summary" : per-provider factual P&L totals (no conclusions)
        "report_text" : human-readable plain-text report
        "generated_at": UTC timestamp
    """
    logger.info("Running daily execution review for %s", date)

    # ── Load data sources ────────────────────────────────────────
    session_logs = read_log_fn("sessions", date, date)
    trade_logs   = read_log_fn("trades",   date, date)
    logger.info("Loaded %d session entries and %d trade entries for %s",
                len(session_logs), len(trade_logs), date)

    # Load live state for every provider (read-only — never modified here)
    states: dict = {}
    for prov in _ALL_PROVIDERS:
        try:
            states[prov] = load_state_fn(prov)
        except Exception as e:
            logger.warning("Could not load state for %s: %s", prov, e)
            states[prov] = {}

    # Expected number of stocks (capped by MAX_WATCHLIST_STOCKS)
    from strategy_v6 import CFG  # import here to avoid circular at module level
    stock_count = len([s for s in watchlist if s.get("type") == "stock"])
    expected_stocks = min(stock_count, CFG.MAX_WATCHLIST_STOCKS)

    # ── Run all 10 checks ────────────────────────────────────────
    checks = [
        _chk1_session_completeness(session_logs, date),
        _chk2_watchlist_coverage(session_logs, expected_stocks),
        _chk3_decision_parse_quality(session_logs),
        _chk4_buy_confidence(trade_logs),
        _chk5_atr_plausibility(states),
        _chk6_stop_above_entry(states),
        _chk7_rr_gate_enforcement(trade_logs),
        _chk8_same_day_reentry(trade_logs),
        _chk9_premarket_handoff(states),
        _chk10_prose_sell_entries(trade_logs),
    ]

    # ── Factual P&L summary ──────────────────────────────────────
    # Numbers only — NO quality judgements attached.
    pnl_summary: dict = {}
    for t in trade_logs:
        prov = t.get("ai_provider", "unknown")
        pnl  = t.get("realized_pnl") or 0
        pnl_summary.setdefault(prov, {"trades": 0, "realized_pnl": 0.0})
        pnl_summary[prov]["trades"]       += 1
        pnl_summary[prov]["realized_pnl"] += pnl
    for prov in pnl_summary:
        pnl_summary[prov]["realized_pnl"] = round(
            pnl_summary[prov]["realized_pnl"], 2)

    # ── Status counts ────────────────────────────────────────────
    ok_count   = sum(1 for c in checks if c["status"] == _OK)
    warn_count = sum(1 for c in checks if c["status"] == _WARN)
    fail_count = sum(1 for c in checks if c["status"] == _FAIL)

    report_text = _build_report_text(date, checks, pnl_summary)

    logger.info(
        "Daily review complete for %s: %d ok / %d warn / %d fail",
        date, ok_count, warn_count, fail_count,
    )

    return {
        "date":          date,
        "checks":        checks,
        "ok_count":      ok_count,
        "warn_count":    warn_count,
        "fail_count":    fail_count,
        "pnl_summary":   pnl_summary,
        "report_text":   report_text,
        "generated_at":  datetime.now(timezone.utc).isoformat(),
    }
