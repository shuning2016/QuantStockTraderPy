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
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional

try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    _ET = _ZoneInfo("America/New_York")
except ImportError:
    # Python < 3.9: use fixed UTC-4 offset (EDT; close enough for date-boundary use)
    _ET = timezone(timedelta(hours=-4))

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

# FIX-1: Session "due-by" times in ET (hour, minute).
# = cron fire time + 45 min buffer for AI call + storage latency.
# Used to suppress false WARN when the daily review is run mid-day and
# future sessions simply haven't been scheduled yet.
#   premarket  cron 09:15 ET  → due by 10:00 ET
#   opening    cron 10:00 ET  → due by 10:45 ET
#   mid        cron 12:00 ET  → due by 12:45 ET
#   closing    cron 15:30 ET  → due by 16:15 ET
_SESSION_DUE_BY_ET: dict = {
    "premarket": (10,  0),
    "opening":   (10, 45),
    "mid":       (12, 45),
    "closing":   (16, 15),
}


def _sessions_due_by_now(date: str) -> list:
    """Return which sessions should already be in the logs for *date*.

    For any past date all 4 sessions are expected.
    For today only sessions whose cron window + buffer has elapsed in ET
    are expected — this prevents false WARN when the review is run
    mid-day before mid/closing have had a chance to fire.
    """
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if date < today_utc:
        return list(_ALL_SESSIONS)   # past date — all 4 expected
    now_et = datetime.now(_ET)
    return [
        sess for sess in _ALL_SESSIONS
        if (now_et.hour, now_et.minute) >= _SESSION_DUE_BY_ET[sess]
    ]

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
    """Count scored stock lines in the AI SCORE output section.

    FIX-2: single combined regex handles all bullet styles and bullet-less lines.
    FIX-3: second alternative catches markdown table rows — when the watchlist is
    large the AI sometimes switches to a table layout (| AAPL | ❌ | ... |) even
    though the prompt forbids it.  The table-row pattern `| SYM |` is safe because
    the header row uses Chinese (標的, not A-Z) and the separator row (|---|) has
    no uppercase letter.

    Matches:
      ▸ AAPL|↑|...    pipe-delimited (preferred — prompt template)
      - AAPL|↑|...    Grok bullet variant
      AMZN|↑|...      no bullet
      | AAPL | ...    markdown table row (fallback for large watchlists)
    """
    return len(re.findall(
        r'(?:'
        r'^\s*(?:[▸►▷>\-\*•–→]\s*)?[A-Z][A-Z0-9.]{0,5}\s*\|\s*[↑↓→=\-]'  # pipe-delimited SCORE
        r'|'
        r'^\s*\|\s*[A-Z][A-Z0-9.]{0,5}\s*\|'                               # markdown table row
        r')',
        ai_text, re.MULTILINE,
    ))


# ─── Individual Checks ───────────────────────────────────────────

def _chk1_session_completeness(session_logs: list, date: str) -> dict:
    """
    CHK-1: Did all *due* sessions fire today for each provider?

    BUG-4 fix: also flags sessions that fired but returned an AI [ERROR] response
    so a silent API failure doesn't pass as ✅ just because the log entry exists.

    FIX-1: time-aware check — when date is today, only sessions whose cron
    window + 45 min buffer has elapsed in ET are considered "expected".
    Running the review at 11:00 ET will never warn about mid/closing not
    having fired yet; those sessions simply haven't been scheduled.
    """
    # Determine which sessions are actually expected by now
    expected_sessions = _sessions_due_by_now(date)
    not_yet_due       = [s for s in _ALL_SESSIONS if s not in expected_sessions]

    fired:     dict = {p: set()  for p in _ALL_PROVIDERS}
    ai_errors: dict = {p: []     for p in _ALL_PROVIDERS}

    for entry in session_logs:
        prov = entry.get("ai_provider", "")
        sess = entry.get("session", "")
        if prov in fired and sess in _ALL_SESSIONS:
            fired[prov].add(sess)
            # A session that fired but the AI call failed has ai_analysis starting with [ERROR]
            ai_text = entry.get("ai_analysis", "")
            if ai_text.startswith("[ERROR]"):
                ai_errors[prov].append(sess)

    issues = []
    evidence = {}
    for prov in _ALL_PROVIDERS:
        missing = [s for s in expected_sessions if s not in fired[prov]]
        found   = sorted(fired[prov])
        errors  = ai_errors[prov]
        evidence[prov] = {
            "fired": found, "missing": missing, "ai_errors": errors,
            "not_yet_due": not_yet_due,
        }
        if missing:
            issues.append(f"{prov}: missing sessions {missing}")
        if errors:
            issues.append(f"{prov}: AI call failed in session(s) {errors}")

    pending_note = (f" (sessions not yet due: {not_yet_due})" if not_yet_due else "")

    if not issues:
        fired_count = len(expected_sessions)
        status  = _OK
        summary = (f"All {fired_count} due session(s) fired with no AI errors"
                   + pending_note)
    elif all(len(fired[p]) == 0 for p in _ALL_PROVIDERS) and expected_sessions:
        status  = _FAIL
        summary = f"No sessions fired at all on {date} — cron may be down{pending_note}"
    else:
        has_ai_errors = any(ai_errors[p] for p in _ALL_PROVIDERS)
        status  = _FAIL if has_ai_errors else _WARN
        summary = "; ".join(issues) + pending_note

    return {"id": "CHK-1", "name": "Session Completeness",
            "status": status, "summary": summary, "evidence": evidence}


def _chk2_watchlist_coverage(session_logs: list, expected_stocks: int) -> dict:
    """
    CHK-2: Did each AI score all watchlist stocks in its SCORE section?
    We count pipe-delimited SCORE lines in the AI text from the opening session.

    Only opening is checked here — the mid session prompt does NOT include
    the _SCORE instruction (it has 持仓评估 for current holdings instead),
    so checking mid would always produce false FAIL alerts.  If _SCORE is
    added to the mid prompt in the future, add "mid" back to the filter.
    """
    issues  = []
    evidence = {}
    # Only check the opening session which is the only session that includes _SCORE
    for entry in session_logs:
        sess = entry.get("session", "")
        prov = entry.get("ai_provider", "")
        if sess != "opening":
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
                "status": _WARN, "summary": "No opening session found to check",
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
    BUG-5 fix: now reads from decisions_raw (added to session log by app.py fix)
    which contains parse_mode for ALL decisions — both executed and blocked.
    Falls back to exec_log for older log entries that predate this fix.
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
        # Prefer decisions_raw (has parse_mode for every decision including executed ones).
        # Fall back to exec_log for log entries written before this fix was deployed.
        items = entry.get("decisions_raw") or entry.get("exec_log", [])
        for item in items:
            pm = item.get("parse_mode", "")
            if pm == "structured":
                counts[key]["structured"] += 1
            elif pm == "prose_fallback":
                counts[key]["prose_fallback"] += 1
                issues.append(f"{key}: prose_fallback detected")
            elif pm == "synthetic_hold":
                counts[key]["synthetic_hold"] += 1
                issues.append(f"{key}: synthetic_hold (no DECISION block found)")
            if pm:
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

    FIX-4: three distinct buckets now — previously prose_fallback BUYs were
    lumped into "no RR logged" (confusingly blaming the AI for omitting RR
    when the real problem was parse format).  Separating them gives a clearer
    signal about each failure mode:

      violations  — structured BUY executed with RR < 2.0 (gate bypass, FAIL)
      no_rr       — structured BUY executed but AI omitted RR from reason (WARN)
      prose_buys  — prose_fallback BUY executed without any verifiable RR (WARN;
                    after Fix-3 the gate now blocks these, so this bucket catches
                    only trades logged before that fix was deployed)

    Prose_fallback reasons always start with "[prose fallback]" (set by
    parse_ai_decisions pass-2), which is the reliable discriminator.
    """
    violations = []   # RR < 2.0  →  gate bypass  →  FAIL
    no_rr      = []   # structured, RR absent from reason  →  WARN
    prose_buys = []   # prose_fallback BUY  →  WARN (gate now blocks these)

    for t in trade_logs:
        if t.get("action") != "BUY":
            continue
        reason = t.get("reason") or ""
        entry_base = {
            "symbol":   t.get("symbol"),
            "provider": t.get("ai_provider"),
            "session":  t.get("session"),
            "reason":   reason[:100],
        }

        # prose_fallback BUYs: reason field always starts with "[prose fallback]"
        if reason.startswith("[prose fallback]"):
            prose_buys.append(entry_base)
            continue

        # Structured BUY — check for R:R in reason
        rr = _parse_rr_from_reason(reason)
        if rr is None:
            no_rr.append({**entry_base, "rr": None})
        elif rr < 2.0:
            violations.append({**entry_base, "rr": rr})

    issues = []
    if violations:
        issues.append(
            f"{len(violations)} BUY(s) with RR<2.0 executed (gate bypass): "
            + ", ".join(f"{v['provider']}/{v['symbol']} RR={v['rr']}" for v in violations)
        )
    if no_rr:
        issues.append(
            f"{len(no_rr)} BUY(s) with no RR in reason (AI omitted): "
            + ", ".join(f"{v['provider']}/{v['symbol']}" for v in no_rr)
        )
    if prose_buys:
        issues.append(
            f"{len(prose_buys)} prose_fallback BUY(s) executed without R:R verification "
            f"(now blocked by gate fix): "
            + ", ".join(f"{v['provider']}/{v['symbol']}" for v in prose_buys)
        )

    status  = (_FAIL if violations else _WARN if (no_rr or prose_buys) else _OK)
    summary = ("; ".join(issues) if issues
               else "All executed BUYs have RR ≥ 2.0 logged")

    return {"id": "CHK-7", "name": "R:R Gate Enforcement",
            "status": status, "summary": summary,
            "evidence": {"violations": violations, "missing_rr": no_rr,
                         "prose_fallback": prose_buys}}


def _chk8_same_day_reentry(trade_logs: list) -> dict:
    """
    CHK-8: Did a provider re-enter a symbol on the same day after exiting it?
    The 48h cooldown should block a BUY that follows a same-day SELL.

    BUG-7 fix: the old check flagged ANY 2+ BUYs for the same symbol, including
    legitimate scale-ins (adding to a winning position at a higher price, which
    check_position_rules allows).  Only a BUY that follows an intervening SELL
    in the day's timeline is a cooldown violation — that's the re-entry pattern.
    """
    # Build timeline per (provider, symbol), sorted chronologically
    timeline: dict = {}
    for t in sorted(trade_logs, key=lambda x: x.get("timestamp", "")):
        prov = t.get("ai_provider", "")
        sym  = t.get("symbol", "")
        act  = t.get("action", "")
        if not sym or act not in ("BUY", "SELL"):
            continue
        timeline.setdefault((prov, sym), []).append({
            "action": act, "session": t.get("session"), "price": t.get("price")
        })

    violations = []
    for (prov, sym), events in timeline.items():
        # Walk the sequence: a BUY that comes AFTER a SELL is a re-entry violation
        had_sell      = False
        reentry_buys  = 0
        for e in events:
            if e["action"] == "SELL":
                had_sell = True
            elif e["action"] == "BUY" and had_sell:
                reentry_buys += 1
        if reentry_buys >= 1:
            violations.append({
                "provider": prov,
                "symbol":   sym,
                "events":   events,
                "note":     f"BUY after same-day SELL ({reentry_buys} re-entry buy(s))",
            })

    status  = _FAIL if violations else _OK
    summary = ("; ".join(
                   f"{v['provider']}/{v['symbol']}: re-entered after same-day exit"
                   for v in violations
               ) if violations
               else "No same-day re-entries detected")

    return {"id": "CHK-8", "name": "Same-Day Re-entry (Cooldown)",
            "status": status, "summary": summary, "evidence": violations}


def _chk9_premarket_handoff(session_logs: list, states: dict) -> dict:
    """
    CHK-9: Did the premarket session run and extract a NEXT_ACTION focus note?

    BUG-2 fix: primary source is now session_logs (historical-date safe).
    The old implementation read live state["premarket_focus"] which always
    reflected TODAY's run regardless of the date being checked.
    Logic:
      1. Find premarket log entry per provider → confirms session ran.
      2. Check ai_analysis for a NEXT_ACTION line → confirms extraction worked.
      3. Live state["premarket_focus"] is included as supplementary evidence only.
    Note: ai_analysis is stored truncated to 2000 chars; NEXT_ACTION lines that
    appear beyond that point will be missed — this is a pre-existing log limit.
    """
    results  = {}
    warnings = []

    # Index premarket entries by provider from session_logs
    premarket_entries: dict = {}
    for entry in session_logs:
        if entry.get("session") == "premarket":
            prov = entry.get("ai_provider", "")
            if prov and prov not in premarket_entries:
                premarket_entries[prov] = entry

    for prov in _ALL_PROVIDERS:
        entry = premarket_entries.get(prov)
        live_focus = states.get(prov, {}).get("premarket_focus")

        if entry is None:
            results[prov] = {"premarket_ran": False, "next_action_found": None,
                             "premarket_focus": None}
            warnings.append(f"{prov}: no premarket session log found (session did not run)")
            continue

        ai_text = entry.get("ai_analysis", "")
        if ai_text.startswith("[ERROR]"):
            results[prov] = {"premarket_ran": True, "next_action_found": False,
                             "ai_error": True, "premarket_focus": None}
            warnings.append(f"{prov}: premarket ran but AI returned error — no focus note")
            continue

        na_match   = re.search(r'NEXT_ACTION\s*[：:]\s*(.+)', ai_text)
        focus_from_log = na_match.group(1).strip()[:80] if na_match else None

        results[prov] = {
            "premarket_ran":     True,
            "next_action_found": focus_from_log is not None,
            "premarket_focus":   focus_from_log or live_focus,
        }
        if focus_from_log is None:
            warnings.append(
                f"{prov}: premarket ran but no NEXT_ACTION line found "
                f"(opening session had no continuity note)"
            )

    status  = _WARN if warnings else _OK
    summary = ("; ".join(warnings) if warnings
               else "Premarket ran and NEXT_ACTION extracted for all providers")

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
        date                 : "YYYY-MM-DD" — the trading day to inspect
        read_log_fn          : read_log_range(prefix, from, to, provider=None) → list
        load_state_fn        : load_trade_state(provider) → dict
        watchlist            : current watchlist list (to know expected stock count)

    Returns a dict with:
        "date"        : date checked
        "checks"      : list of 10 check result dicts
        "ok_count" / "warn_count" / "fail_count"
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

    # Load live state for every provider (read-only — never modified here).
    # Note: live state is used only for CHK-5/CHK-6 (open positions) and as
    # supplementary evidence for CHK-9. CHK-9 now uses session_logs as primary.
    states: dict = {}
    for prov in _ALL_PROVIDERS:
        try:
            states[prov] = load_state_fn(prov)
        except Exception as e:
            logger.warning("Could not load state for %s: %s", prov, e)
            states[prov] = {}

    expected_stocks = len([s for s in watchlist if s.get("type") == "stock"])

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
        _chk9_premarket_handoff(session_logs, states),   # BUG-2: now takes session_logs
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
