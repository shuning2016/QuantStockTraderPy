# Trading System Improvement Plan — 2026-05-01

This document records what we shipped today and what comes next. Reference for future sessions.

---

## 0. What was already shipped today (done, in `main`)

### Infrastructure (commit `7d12ff8`)
- Cron handlers return HTTP 200 on exception → kills Vercel cron auto-retry storm (the "10-min grok mid" symptom)
- `CRON_SECRET` is now mandatory (set it on Vercel)
- KV-based session lock around `run_trade_session` (cron + manual trigger can no longer race)
- `_api_post_with_retry` 429 cap lowered 30s → 10s to stay inside the 120s wall

### Strategy gates (commit `e00f262`, merged in `f607e5f`)
- **#4 Volume fabrication**: hedge tokens (预估/待确认/假设/约/≈/TBD/N/A/?) blocked in Vol:/Ratio: fields
- **#4 Missing volume**: BUY now requires a parseable numeric ratio (was silently allowed before)
- **#5 Stop tightening**: BUY rejected when (entry - stop) < 0.95 × 1.5×ATR
- **#6 Mid-session BUYs**: now require C≥8 AND vol_ratio ≥ 2.0×
- **#7 Chase entries**: prompt rule "当日已涨>5%标的不新入场"
- **#8 Position cap**: already enforced at 20% NAV (no change today)

These apply to all three providers identically.

---

## 1. Forward plan — Bracket A / B / C and provider mapping

Three brackets:
- **Bracket A** = profit-side changes (offensive)
- **Bracket B** = risk-side changes (defensive)
- **Bracket C** = process / structural changes (meta-fixes)

### Bracket B — Risk gates (DO FIRST)

| ID | Change | Grok | Claude | DeepSeek |
|---|---|---|---|---|
| B1 | Enforce `NO_TRADE_GAP_PCT = 3.0` server-side | ✓ | ✓ | ✓ |
| B2 | Earnings-day filter (skip BUY day-of and day-before earnings) | ✓ | ✓ | ✓ |
| B3 | Friday-afternoon reduce-overnight if position >0.5R | ✓ | aggressive (>0R) | ✓ |
| B4 | Per-provider position cap | 20% NAV | **15% NAV** | 20% NAV |
| B5 | Confidence-tier sizing (C:6 = 0.75×, C:7 = 1.0×, C:8 = 1.25×) | ✓ | ✓ | ✓ |
| B6 | Daily loss circuit-breaker | -3% | **-2%** | -3% |
| B7 | Account-wide max 5 open positions across providers | ✓ | ✓ | ✓ |
| B8 | Stale-signal kill (AI's quoted ATR/price differs >0.5% from current) | ✓ | ✓ | ✓ |

### Bracket A — Profit improvements

| ID | Change | Grok | Claude | DeepSeek |
|---|---|---|---|---|
| A1 | Scale-out: sell 50% at +1R, trail rest with ATR | 50% off | 50% off | **66% off** (rarely re-enters) |
| A2 | Trailing-stop multiplier | **1.0×ATR (tight)** | 1.5×ATR | 1.5×ATR |
| A3 | Lower entry friction: C≥6 normal, no bare HOLDs | – | – | ✓ DeepSeek-only prompt |
| A4 | Pre-screen watchlist: hide stocks gapping >3% from prompt | ✓ | ✓ | ✓ |
| A5 | Re-entry after 1 day if last exit was profitable + stop raised | ✓ | ✓ | ✓ |
| A6 | Sector concentration cap | 2 same-sector | **1 same-sector** | 2 same-sector |
| A7 | Min confidence floor | C≥6 normal, C≥7 Transition | **C≥7 normal, C≥8 Transition** | **C≥6 always** |

### Bracket C — Structural / process changes

| ID | Change | Effort | Provider impact |
|---|---|---|---|
| C1 | Test fixtures (`tests/fixtures/ai_outputs.jsonl` + pytest) | ½ day | none (test only) |
| C2 | Structured output (JSON tool calling) — replace free-text DECISION | 3-5 calendar days per provider, ~1.5 days work each | rolled out per-provider: **Claude first**, then DeepSeek, then Grok |
| C3 | `rules.yaml` single source of truth for prompt + gate definitions | 1.5 days | none (refactor) |
| C4 | Replay harness (`replay.py`) — re-run historical decisions through current gates | 1 day | none (tooling) |
| C5 | Per-provider prompt branches (`_DEC_CLAUDE`, `_DEC_GROK`, `_DEC_DEEPSEEK`) | ½ day | enables A3, A7 differentiation |

### Recommended order (no calendar phasing — ship as you can)

1. **C1** (½ day, test net) → ship before any Bracket A/B
2. **C5** (½ day) → prerequisite for per-provider prompts in A3/A7
3. **All of B + all of A** in one PR (1-2 days) → biggest behavior improvement
4. **C4** (1 day) → unlocks UX feature #6
5. **C2 Claude** (~5 calendar days) → biggest long-term win, then DeepSeek, then Grok
6. **C3** (1.5 days) → after C2 stabilizes; eliminates prompt/code drift
7. After C2 lands, ~30% of B/A gates can be deleted (schema replaces them)

### Logic per provider in one line

- **Grok = "let it cook"** — trust signal, normal size, tight trail to lock wins
- **Claude = "verify twice, size half"** — smaller positions, higher confidence floor, no mid-BUYs, tightest circuit breaker
- **DeepSeek = "lower the bar to act"** — concrete prompt, accept C≥6, must justify HOLDs, scale out faster

---

## 2. What you'll feel after these changes ship

### After Bracket B (risk gates)
- **Fewer trades overall**, especially fewer BUYs on:
  - stocks gapping > 3% premarket
  - stocks with earnings today/yesterday
  - mid-sessions on already-extended movers
- **Smaller Claude positions** (15% NAV instead of 20%) — Claude's "random" entries cost less when wrong
- **Daily loss caps fire**: a bad morning won't cascade — afternoon sessions skip after -2%/-3%
- **Friday afternoon**: more weekend-flat positions
- **Subjective feeling**: system feels "quieter" and more conservative; fewer surprises in the log

### After Bracket A (profit improvements)
- **Winners run further**: scale-out at +1R locks half the profit, the other half rides via trailing stop instead of getting stopped at +5% flat
- **Grok captures more**: tighter trail (1.0×ATR) means it gives back less from the high
- **DeepSeek trades more**: lower entry friction + concrete prompt → fewer "synthetic_hold" no-ops
- **Re-entry sooner**: winning patterns (you exited at +1R, stop raised) re-engage in 1 day instead of 2
- **Subjective feeling**: more profit per winning trade, more participation from DeepSeek

### After Bracket C (structural)
- **C1 fixture tests**: when something breaks, you'll know which change caused it within a minute (pytest output) instead of waiting for the next day's review
- **C2 JSON output (Claude first)**: parse failures and fabricated fields stop being a thing entirely. Daily review CHK-2/CHK-3/CHK-7 violations drop toward zero for Claude
- **C4 replay harness**: you can ask "would my proposed new gate have stopped GOOG?" before shipping it — answers come from real historical data, not speculation
- **Subjective feeling**: fewer fix-of-the-week PRs. The system stops drifting.

---

## 3. UX features and how to use them

| # | Feature | What it shows | How to use |
|---|---|---|---|
| 1 | **Trade detail panel** | Click a row in Trades table → side panel: parsed parameters, gate verdicts (✓/✗ per gate), AI raw reasoning (collapsible). | After any trade you find suspicious, click → see exactly what passed/failed. No more copy-pasting from the daily log. |
| 2 | **Per-provider P&L scoreboard** | Top of dashboard: 7d / 30d P&L, win rate, avg R, blocked-trade count for each provider. | Once a week, glance at it. Confirms (or refutes) "grok > claude > deepseek". If a provider drifts, you'll see it before the bad trade. |
| 3 | **Blocked-decisions log** | New tab: every BUY/SELL the gates rejected, plus *what the stock did since*. Red row = blocked stock that subsequently went up (gate over-blocked). Green row = blocked stock that went down (gate saved you). | Once a week. Tells you which gates are actually paying for themselves. Over-blocking gates get loosened; under-blocking gates get tightened. |
| 4 | **Session comparison view** | One row per session × date, three columns side-by-side (grok / claude / deepseek). | When investigating a bad trade, instantly see if the other two providers also took it. Disagreement is signal. |
| 5 | **CHK trends with sparklines** | Daily Review tab: each CHK metric gets a 14-day sparkline. | Catches slow drift (a parser degrading week over week) that single-day check misses. |
| 6 | **Replay button** | On any trade or blocked-decision row: "Replay against current gates". Re-runs the decision through today's code, shows new verdict. | Before shipping a new gate, click replay on 5-10 historical trades to verify the new gate doesn't over-block past winners. |
| 7 | **"Discuss this trade" button** | One click on trade detail → copies a structured payload (provider, decision, gate verdicts, market context) to clipboard. | Paste into chat to start an analysis conversation. Saves 5 min of context-setting at the start of every chat like today's. |

### Suggested UX rollout order (independent of A/B/C)

1. **#1 + #7** (½ day total) — immediate payoff in every analysis chat
2. **#2 + #3** (1.5 days) — start collecting ground-truth data on whether gates pay for themselves
3. **#4** (½ day) — useful once you have multi-provider runs to compare
4. **#5** (½ day) — once daily review history is rich enough (≥14 days)
5. **#6** (½ day, after C4 ships) — closes the loop: design new gates with historical replay before shipping

### Daily / weekly habit (recommended)

- **Daily**: glance at Daily Review (CHK-1..10). Click any row's trade detail (#1) if something looks off.
- **Weekly**: open Per-provider P&L (#2) and Blocked-decisions log (#3). Look for: provider underperforming, gates over- or under-blocking. Decide if any per-provider config should change.
- **Before shipping a new gate**: run Replay (#6) against the past 30 days of relevant decisions. If it would have blocked >2 winners for every loser caught, redesign before shipping.

---

## Open question still pending decision

- **Vercel plan tier** (Hobby vs Pro). Hobby has cron limits that may explain the 2026-04-30 opening session not auto-triggering. Confirm before assuming the cron/session-lock fixes are sufficient.
- **Set `CRON_SECRET` env var on Vercel** — required for cron to work after today's fix. Without it, all cron requests now 401.
