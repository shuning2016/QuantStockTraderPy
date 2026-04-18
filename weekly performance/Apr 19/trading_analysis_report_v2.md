# AI Trading System — Weekly Analysis & Improvement Report (v2)
> Period: 2026-04-13 to 2026-04-17
> AI Traders: Grok · DeepSeek · Claude
> Strategy: v6.0 (A01–A10 quantitative framework)
> Updated: 2026-04-18 — added priority tags and impact-if-not-fixed for every fix

---

## Priority Tag Legend

| Tag | Meaning | Fix within |
|-----|---------|------------|
| 🔴 P0 — Critical | Hard rule violation or active money loss. System cannot function correctly without this fix. | Before next session |
| 🟠 P1 — High | Directly reduces win rate or profit factor. Measurable weekly P&L impact. | This week |
| 🟡 P2 — Medium | Degrades feedback loop or increases noise in decision data. Harms strategy learning over time. | This month |
| 🟢 P3 — Low | Quality-of-life or optimisation. No immediate P&L impact but improves long-term edge. | Next review cycle |

---

## 1. Week at a Glance

| Metric | Grok | DeepSeek | Claude | Combined |
|--------|------|----------|--------|----------|
| Win rate | 40% | 0% | 0% | ~27% |
| Profitable trades | 2 of 5 | 0 of 2 | 0 of 2 | 2 of 9 |
| Actual losses | 0 | 0 | 0 | 0 |
| Best trade | SE +5.2% | — | — | SE +5.2% (Grok) |
| Execution quality score | 62/100 | 31/100 | 44/100 | 46/100 avg |

**Summary:** Zero losses across all three traders is a positive risk-control signal. However, two of three traders generated zero profit, and Grok left significant gains on the table through premature exits. The combined win rate of ~27% is below the 45–55% target for short-term momentum trading. The core problem is not entry quality — it is exit discipline.

---

## 2. Individual AI Trader Analysis

---

### 2.1 Grok — Best Performer, Two Fixable Flaws

**Overall verdict:** System working as designed on high-quality setups. Two execution bugs are costing real money.

#### Strengths
- Regime detection accurate: correctly identified Trend environment and acted accordingly
- SE trade (+5.2%): textbook execution — Trend Breakout, C:7, ATR stop at $87.00 (~1.4% risk), held long enough to capture the full +5.0% hard profit target
- NVDA second trade (+1.1%): C:9 with $197.44 stop (~1.0% risk) — correct confidence-to-risk sizing
- Zero stop failures: no stop was widened or removed

#### Errors & Fixes

---

**🔴 P0 | G1 — Hard-block C < 6 at code level**

*Error:* Executed a BUY on NVDA with confidence score of 0/10. The rule requires C ≥ 6 before any entry — this is a hard gate, not a guideline. A C:0 trade has no signal basis and is statistically equivalent to a random bet.

*Fix:* Pre-execution check in `execute_decisions()`: if confidence ≤ 5, log `"REJECTED: confidence gate"` and skip entirely. This must be a code-level block, not a prompt suggestion.

```python
if conf < CFG.SCORE_MIN_NORMAL:
    executed.append(f"⚠️ {sym} C:{conf} below minimum {CFG.SCORE_MIN_NORMAL}, skipped")
    continue
```

*Impact if not fixed:*
- Every future session risks trades with zero analytical basis entering the book
- C:0 trades contaminate the confidence calibration log — the A07 feedback system will begin treating random-outcome trades as signal data, slowly corrupting the entire learning loop
- Over 50 trades, even 2–3 C:0 entries per week will statistically drag win rate below breakeven
- Expected cost: ~$20–50 per occurrence in wasted capital exposure on a $10,000 account

---

**🟠 P1 | G2 — Two-tier trailing stop activation threshold by confidence**

*Error:* AAPL entered at C:8 (high conviction). Trailing stop triggered at 0.01R — effectively breakeven. Root cause: trailing stop was calibrated for a generic low-confidence trade, not a C:8 swing setup. High-confidence setups need room to develop.

*Fix:* Modify `check_auto_stop_rules()` to gate trailing stop activation on confidence tier:
- C:6–7 → trailing activates only after ≥ 0.3R profit
- C:8–9 → trailing activates only after ≥ 0.75R profit

```python
min_r_to_trail = 0.75 if conf >= 8 else 0.3
if unr < min_r_to_trail:
    continue  # hold stop at original level, don't trail yet
```

*Impact if not fixed:*
- Every C:8–9 trade — your highest-quality signals — will continue to be shaken out at near-zero profit
- The avg win / avg loss ratio stays suppressed below 1.5:1, making positive expectancy mathematically impossible regardless of win rate
- AAPL-type exits at 0.01R could represent $50–200 of missed profit per trade depending on position size
- This is the single biggest drag on Grok's profit factor right now

---

**🟡 P2 | G3 — Fix DECISION sell parser ("prose fallback")**

*Error:* One NVDA SELL produced an incomplete reason field ("prose fallback"), indicating the DECISION block format broke during generation. The trade was logged but the reason field is unparseable.

*Fix:* Every SELL must output a valid pipe-delimited line: `SELL|SYM|shares|reason|C:X`. Add a parser validation step: if the sell reason contains "prose" or fails regex match, flag as `PARSE_ERROR` and do not count as a valid trade log entry until manually reviewed.

*Impact if not fixed:*
- Corrupted trade log entries cannot be used in backtesting or strategy tuning
- The A07 feedback trigger (which fires after 20 valid trades) will be delayed or skewed by unparseable records
- Over time, the feedback loop that is supposed to self-improve the system will produce misleading signals
- Harder to audit or reconstruct what actually happened in each session

---

**🟢 P3 | G4 — Log R-value on every closed trade**

*Fix:* On every SELL, calculate and log: `r_value = realized_pnl / (risk_per_share × shares_sold)`. SE was approximately +3.5R; AAPL was +0.01R. Without R-values, the A07 expectancy formula runs on dollar P&L which is position-size-dependent and not comparable across trades.

*Impact if not fixed:*
- Expectancy calculation (A01 core formula) is less accurate — you can't distinguish a good small position from a bad large one
- Weekly review loses the ability to compare trade quality across different position sizes and symbols
- No blocking impact this week, but degrades strategy improvement speed over 4–8 weeks

---

### 2.2 DeepSeek — Structural Rebuild Required

**Overall verdict:** The technical foundation (ATR stops, regime awareness) exists but is undermined by three systematic execution errors: early entry, inadequate R:R, and emotional re-entry. 0% win rate across two trades of the same pattern is a structural problem, not bad luck.

#### What Worked
- Overnight discipline: correctly applied "no overnight if not profitable" on the first SE trade
- ATR-based stop placement: method is technically valid — the framework is understood
- No stop widening: once stop was set, it was respected

#### Errors & Fixes

---

**🔴 P0 | D1 — Wait for confirmed breakout price before entry**

*Error:* Signal defined breakout trigger at $92.00. Actual entry: $91.53 — bought in anticipation, not confirmation. A confirmation-based system must wait for the price to actually reach and hold the trigger level before entry. Early entry = predicting the breakout, not confirming it.

*Fix:* Add to prompt and engine: BUY only executes when `current_price ≥ stated_trigger_price`. Log both values on every trade. If price has not reached trigger, output HOLD with note "awaiting breakout confirmation at $X."

*Impact if not fixed:*
- Every breakout trade starts from a structurally weaker entry point — stop is the same distance away but the price hasn't confirmed direction
- Increases stop-out rate on otherwise valid setups: entry at $91.53 with stop at $88.25 vs entry at $92.00+ with the same stop means more downside exposure and less confirmation of momentum
- DeepSeek will continue producing 0% win rates on breakout strategies despite the underlying signals being valid
- Estimated ongoing cost: 1–2 unnecessary stop-outs per week = $60–150 in preventable losses

---

**🔴 P0 | D2 — Mandatory R:R ≥ 2.0 pre-entry calculation**

*Error:* Trade 1 R:R = 1.22:1 (Target +5% vs Stop -4.1%). Trade 2: C:6 confidence with 3.4% stop — both below the 2:1 minimum. At 1.22:1, even a 55% win rate produces negative expected value. These trades should never have been approved.

*Fix:* Before any DECISION:BUY is written, calculate and log: `RR = target_pct / stop_pct`. If RR < 2.0, the trade is blocked. Add RR to every DECISION line.

```
DECISION:
BUY|SE|15|Breakout C:6|Stop=$88.25(-4.1%)|Target=$96.60(+5%)|RR=1.22 → BLOCKED: RR < 2.0
```

*Impact if not fixed:*
- DeepSeek will continue taking trades where the math guarantees long-term losses even with good win rates
- At 1.22:1 R:R and 40% win rate: E = 0.4×(+5%) - 0.6×(-4.1%) = -0.46% per trade — negative expectancy by design
- This is the root cause of the 0% profitable week, not bad luck or market conditions
- Every week this remains unfixed is a week of guaranteed drift toward negative expectancy

---

**🔴 P0 | D3 — 48-hour same-symbol cooldown after any exit**

*Error:* After SE stop-out, immediately re-entered SE at $92.82 — a higher price with a wider stop, producing an even worse R:R. This is an emotional re-entry driven by the loss, not a new independent signal.

*Fix:* After any close (profit or loss) in SYM, lock that symbol for 48 trading hours. Store `cooldowns[sym] = exit_timestamp` in state. On any incoming BUY for a cooled-down symbol, output HOLD with "48h cooldown active since [time]."

*Impact if not fixed:*
- Revenge trading is the fastest way to convert a disciplined system into a losing one
- A stop-out followed by a same-day re-entry at a worse price doubles the loss exposure on a single thesis that has already been proven wrong that day
- Over a month: 2–3 revenge re-entries per week × average -$30 each = -$240–360 in avoidable losses
- Also corrupts the trade log with emotionally-triggered entries that skew all statistical analysis

---

**🟠 P1 | D4 — Confidence-to-risk sizing matrix (hard limits)**

*Error:* C:6 confidence trade carried a 3.4% stop — more than double the appropriate risk for that confidence tier. A C:6 signal is a moderate-quality setup; it should carry moderate risk. Assigning it maximum risk destroys R:R.

*Fix:* Enforce hard stop-size limits by confidence tier:
| Confidence | Max stop % |
|------------|------------|
| C:6 | 1.5% |
| C:7 | 2.0% |
| C:8 | 2.5% |
| C:9+ | 3.0% |

Any trade where the ATR-derived stop exceeds the tier limit must either reduce position size to bring risk within limit, or be rejected.

*Impact if not fixed:*
- C:6 trades will continue carrying C:9-level risk, producing structurally negative R:R every time
- The confidence score becomes meaningless as a risk-sizing tool — undermines the entire ATR position sizing framework in A04
- Long-run: inflated risk on moderate-confidence trades will cause drawdowns disproportionate to the signal quality

---

**🟠 P1 | D5 — Volume confirmation as a number, not a description**

*Error:* Both SE trades mentioned "放量" (volume expanding) in analysis text but no actual volume multiplier was logged or verified. Verbal descriptions of volume are unverifiable and therefore cannot block a bad entry.

*Fix:* Before any BUY, log: `Vol_today: Xm · 20d_avg: Ym · Ratio: Z×`. If Z < 1.5, trade is HOLD regardless of price pattern. The ratio must appear in the DECISION block.

*Impact if not fixed:*
- Breakout entries continue without verifying the most basic momentum confirmation
- Unconfirmed-volume breakouts have a significantly higher false-breakout rate — the move often reverses within 1–2 sessions
- Estimated impact: 30–40% of DeepSeek's breakout trades likely occur on insufficient volume, degrading win rate structurally

---

**🟢 P3 | D6 — Self-review check before same-day re-entry**

*Fix:* Add to prompt: "Before generating any DECISION:BUY, check if this symbol had a stop-out or exit today. If yes, explain in at least 2 sentences why this new signal is fundamentally different from the previous one — different timeframe, different catalyst, different technical level. If you cannot provide 2 distinct reasons, output HOLD."

*Impact if not fixed:*
- Marginal protection against D3 (cooldown) being bypassed by creative signal rationalization
- Without this self-check, the AI can construct post-hoc justifications for re-entries that bypass the intent of the cooldown rule
- Low immediate impact since D3 is a hard code block, but important for prompt robustness in edge cases

---

### 2.3 Claude — Sound Analysis, Broken Exit Execution

**Overall verdict:** Analysis quality is the strongest of the three — Regime judgment consistent, ATR stops mathematically correct, confidence calibrated honestly. The problem is entirely at the execution layer: correct entry decisions are being overridden by intraday noise sensitivity. This is a behavioral fix, not an analytical one.

#### What Worked
- ATR stop levels calculated correctly: $257.60 and $266.07 are technically valid placements
- Regime consistency: both trades in Trend regime — appropriate for the strategy
- Confidence calibration: C:7 is an honest, non-overconfident assessment
- No entry rule violations

#### Errors & Fixes

---

**🔴 P0 | C1 — Declare trade timeframe at entry and commit to it**

*Error:* AAPL was entered with ATR stop and overnight conditions checked — swing trade logic. Exit was triggered by a 0.2% intraday dip — day trade logic. Mixing timeframes within a single trade creates incoherent decisions: the entry criteria and exit criteria belong to two different strategies.

*Fix:* Every DECISION:BUY must include `[INTRADAY]` or `[SWING 2–5d]` tag. Once tagged, exit logic locks to that timeframe:
- `[INTRADAY]`: exits permitted intraday on price action
- `[SWING]`: exits only on (a) ATR stop hit, (b) Regime = Chop, or (c) pre-defined hold period end

```
DECISION:
BUY|AAPL|8|Trend pullback C:7|Stop=$257.60|[SWING 2-5d]
```

*Impact if not fixed:*
- Every swing entry will continue to be at risk of premature intraday exit — turning 2–5 day holds into same-day flat trades
- The system will consistently produce breakeven results on positions that should have generated +3–6% gains
- Claude's win rate will remain near 0% not because entries are wrong but because the holding logic is incoherent
- Estimated cost per occurrence: $100–300 in missed profit per AAPL/NVDA-type swing trade

---

**🔴 P0 | C2 — Stop = ATR stop price only, no other exit trigger for swing trades**

*Error:* AAPL swing trade exited on a 0.2% intraday dip, well before the ATR stop at $257.60 was hit. The stop was never triggered. Claude created an alternative exit reason mid-trade that overrode the pre-defined risk framework.

*Fix:* For any trade tagged `[SWING]`, the only valid exit conditions are:
1. Price closes at or below ATR stop price
2. Regime flips to Chop (A10 rule)
3. Pre-defined hold period expires at session close
4. Hard profit target (+5%) hit

A single intraday candle below a round number does not satisfy any of these conditions. Remove all subjective exit language from the SWING exit logic.

*Impact if not fixed:*
- Same as C1 — these two fixes address the same root failure from different angles
- Without C2, even if C1 is implemented (timeframe declared), the AI can still rationalize early exits by invoking undefined "technical weakness" criteria
- The ATR stop becomes decorative rather than functional — the system appears to have risk management but doesn't actually follow it

---

**🟠 P1 | C3 — Quantify "technical weakness" with objective criteria**

*Error:* "Price broke below 264 = technical weakness" is not a quantified rule. A single breach of a round number during market hours is normal price oscillation, especially for high-ATR stocks like AAPL where ATR > $4. Without an objective definition, "weakness" means whatever feels uncomfortable in the moment.

*Fix:* Define technical weakness as ALL of the following:
- Price closes (not trades) below ATR stop level, AND
- One of: (i) volume on the down day > 2× 20-day average, OR (ii) price closes below the 5-day low

Both conditions must be met. A single intraday dip with normal volume is explicitly not weakness.

*Impact if not fixed:*
- Claude will continue exiting valid trades early using subjective reasoning that cannot be backtested or replicated
- The feedback loop cannot improve exit timing if exit criteria are undefined — there is no objective measure to calibrate against
- Strategy learning (A07) is effectively blocked for Claude's exit decisions

---

**🟠 P1 | C4 — Increase trade frequency in Trend regime**

*Error:* 2 trades per week in a confirmed Trend regime with a focused watchlist is below the minimum for the feedback system to function. The A07 trigger requires 20 closed trades before it produces statistically valid signals.

*Fix:* In Trend regime with C ≥ 7, target 3–4 setups per week. Use the full watchlist to source candidates rather than limiting to 1–2 stocks. Do not skip setups that pass all entry criteria due to excess caution.

*Impact if not fixed:*
- At 2 trades/week, the A07 feedback loop takes 10 weeks to reach its first valid signal — the system cannot self-improve at this rate
- Small sample size means weekly win rate fluctuates wildly (0% or 100% in a 2-trade week), making performance assessment meaningless
- Missed opportunities in Trend regime are the lowest-risk trades in the system — being too selective during ideal conditions is a form of capital inefficiency

---

**🟢 P3 | C5 — Post-exit price tracking for stop calibration**

*Fix:* After every exit (especially early exits and stop-outs), track the stock's closing price for the next 2 trading days. Log: `"Exit: $X · Price 48h later: $Y · Exit correct: Y/N · Missed gain/avoided loss: $Z"`. Feed this data into the weekly review.

*Impact if not fixed:*
- No immediate P&L impact
- Over 4–6 weeks, exit calibration data becomes essential for determining whether ATR multiplier needs adjustment
- Without it, the team has no systematic way to know if stops are too tight, too wide, or correctly placed — improvement is left to intuition

---

## 3. Shared System Fixes — Apply to All 3 Traders

These fixes address failures that appeared across multiple traders. They must be implemented in both the v6 prompt template and `strategy_v6.py` engine before the next session.

---

**🔴 P0 | S1 — Mandatory R:R ≥ 2.0 pre-entry check**

*Affects:* All 3 (DeepSeek critical, Grok and Claude not documented)

*Fix:* No trade executes unless `Target% ÷ Stop% ≥ 2.0`. Calculate before DECISION is written. Block at engine level if below threshold. Log RR on every BUY line.

```python
if stop_pct > 0 and (target_pct / stop_pct) < 2.0:
    executed.append(f"⚠️ {sym} RR={target_pct/stop_pct:.2f} < 2.0 minimum, skipped")
    continue
```

*Impact if not fixed:*
- DeepSeek continues taking trades with guaranteed negative expected value at the current win rate
- Grok and Claude cannot verify their trades have adequate R:R without logging it — they may be taking sub-2:1 trades without knowing
- Profit factor (target ≥ 1.5) is mathematically unachievable if individual trade R:R averages below 2:1
- This is the most consequential unfixed bug in the entire system

---

**🔴 P0 | S2 — 48-hour same-symbol cooldown (system-wide)**

*Affects:* DeepSeek (confirmed), Grok and Claude (prevention)

*Fix:* State field `cooldowns: {SYM: exit_timestamp}`. On any BUY for a cooled symbol, auto-reject with log message. Cooldown clock runs on trading hours only (not calendar hours).

*Impact if not fixed:*
- DeepSeek will continue revenge-trading immediately after stop-outs
- Without a system-wide rule, Grok and Claude are also exposed to the same pattern in future high-volatility sessions
- A single revenge re-entry sequence can erase 3–5 normal winning trades — asymmetric downside

---

**🟠 P1 | S3 — Declare trade timeframe at every entry**

*Affects:* Claude (confirmed), Grok (partial — AAPL), DeepSeek (prevention)

*Fix:* Every DECISION:BUY must include `[INTRADAY]` or `[SWING 2–5d]`. Engine stores this tag in the holdings state. Exit logic references the tag to determine valid exit conditions.

*Impact if not fixed:*
- Swing trades continue being exited on day-trade logic — converting 3–6% potential gains into 0% results
- No way to distinguish intentional same-day exits from premature exits in the trade log
- Backtesting becomes unreliable because holding period is undefined per trade

---

**🟠 P1 | S4 — Trailing stop activates only at ≥ 0.5R for C:6–7, ≥ 0.75R for C:8–9**

*Affects:* Grok (confirmed AAPL), Claude (confirmed AAPL), DeepSeek (prevention)

*Fix:* Modify `check_auto_stop_rules()` to check R-value before moving the stop:

```python
conf = h.get("confidence", 6)
min_r_to_trail = 0.75 if conf >= 8 else 0.5
if unr < min_r_to_trail:
    continue  # don't activate trailing stop yet
```

*Impact if not fixed:*
- High-conviction trades (C:8–9) will continue being shaken out at near-zero profit
- Avg win remains suppressed — profit factor stays below 1.0 even with improving win rate
- The system's best setups are effectively penalized by the same stop logic applied to its weakest setups

---

**🟠 P1 | S5 — Volume confirmation as a logged number on every BUY**

*Affects:* DeepSeek (confirmed), Claude (not documented), Grok (prevention)

*Fix:* Require in DECISION block: `Vol: Xm / 20d_avg: Ym / Ratio: Z×`. Engine validates Z ≥ 1.5 before executing BUY. If ratio absent from DECISION, treat as missing confirmation and reject.

*Impact if not fixed:*
- Breakout trades without volume confirmation have a 40–60% higher false-breakout rate based on standard technical analysis research
- DeepSeek's SE trades had unverified volume — this may be a contributing factor to both stop-outs
- Win rate on breakout setups will remain structurally depressed

---

**🟡 P2 | S6 — Prompt pre-entry checklist block (all AI providers)**

*Affects:* All 3

*Fix:* Add the following block to the DECISION section of all AI provider prompts:

```
Pre-entry checklist (complete before any BUY):
① Confidence: C = X/10 — must be ≥ 6
② Breakout confirmed: Price ≥ trigger level — Y/N
③ Volume: Today Xm · 20d avg Ym · Ratio Z× — must be ≥ 1.5×
④ R:R: Target +X% ÷ Stop -Y% = Z:1 — must be ≥ 2.0
⑤ Timeframe: [INTRADAY] or [SWING 2-5d]
⑥ Cooldown: Last exit in this symbol: [date/none]. 48h elapsed — Y/N

All six must be Y/satisfied. Any N = HOLD.
```

*Impact if not fixed:*
- Without a structured checklist, AI providers will selectively apply rules depending on how confident the overall signal feels — strong setups get less scrutiny, which is the opposite of what the system needs
- The checklist also creates a parseable audit trail: if a trade goes wrong, reviewers can immediately see which checklist item was violated

---

## 4. Complete Priority-Ordered Fix List

All 15 fixes ranked from most to least urgent.

| Rank | ID | Scope | Priority | Fix summary |
|------|----|-------|----------|-------------|
| 1 | S1 | All 3 | 🔴 P0 | R:R ≥ 2.0 hard gate before every BUY |
| 2 | D2 | DeepSeek | 🔴 P0 | R:R pre-calculation and block (DeepSeek specific) |
| 3 | G1 | Grok | 🔴 P0 | C < 6 hard code block |
| 4 | D1 | DeepSeek | 🔴 P0 | Entry only at confirmed breakout price (≥ trigger) |
| 5 | D3 | DeepSeek | 🔴 P0 | 48h cooldown after exit (DeepSeek specific) |
| 6 | S2 | All 3 | 🔴 P0 | 48h same-symbol cooldown (system-wide) |
| 7 | C1 | Claude | 🔴 P0 | Declare [INTRADAY] or [SWING] at entry |
| 8 | C2 | Claude | 🔴 P0 | ATR stop = only valid exit trigger for [SWING] trades |
| 9 | G2 | Grok | 🟠 P1 | Two-tier trailing stop by confidence (≥ 0.3R / ≥ 0.75R) |
| 10 | S3 | All 3 | 🟠 P1 | Timeframe tag on all entries (system-wide) |
| 11 | S4 | All 3 | 🟠 P1 | Trailing stop activation threshold by confidence (system-wide) |
| 12 | D4 | DeepSeek | 🟠 P1 | Confidence-to-risk sizing matrix |
| 13 | D5 | DeepSeek | 🟠 P1 | Volume as a number on every BUY |
| 14 | C3 | Claude | 🟠 P1 | Objective definition of "technical weakness" |
| 15 | C4 | Claude | 🟠 P1 | Increase trade frequency in Trend regime to 3–4/week |
| 16 | S5 | All 3 | 🟠 P1 | Volume ratio logged and validated on all BUY decisions |
| 17 | S6 | All 3 | 🟡 P2 | Structured pre-entry checklist in all prompts |
| 18 | G3 | Grok | 🟡 P2 | Fix DECISION sell parser (prose fallback) |
| 19 | C5 | Claude | 🟢 P3 | Post-exit price tracking for stop calibration |
| 20 | G4 | Grok | 🟢 P3 | Log R-value on every closed trade |
| 21 | D6 | DeepSeek | 🟢 P3 | Self-review check before same-day re-entry |

---

## 5. Impact-If-Not-Fixed Summary Table

| Fix ID | If not fixed this week | If not fixed this month |
|--------|----------------------|------------------------|
| S1 / D2 | DeepSeek continues negative EV trades. Grok/Claude R:R unmonitored. | Profit factor stays below 1.0 — system cannot compound |
| G1 | Random C:0 trades pollute confidence log every session | A07 feedback loop corrupted by noise data within 3–4 weeks |
| D1 | DeepSeek stops out of valid breakouts before they confirm | 0% win rate on breakout strategy becomes permanent pattern |
| D3 / S2 | Revenge re-entries double losses on stop-out days | One bad volatile week can produce 5–6× normal drawdown |
| C1 / C2 | Swing trades exit intraday — 0% win rate continues | Claude never generates meaningful P&L data for A07 calibration |
| G2 / S4 | Best C:8–9 signals exit at 0.01–0.1R — avg win stays near zero | Profit factor mathematically capped below target 1.5 indefinitely |
| D4 | C:6 trades carry C:9 risk — destroys R:R on every moderate setup | Win rate needed to break even rises above 60% — unsustainable |
| D5 / S5 | Breakout entries without volume confirmation — high false-breakout rate | Win rate on breakout setups structurally 15–25% lower than it should be |
| C3 | Subjective exits override objective ATR stops unpredictably | Exit decisions cannot be backtested — A06 framework becomes non-functional for Claude |
| C4 | 2 trades/week → 10 weeks to reach A07 minimum sample | Strategy self-improvement is effectively disabled for Claude |
| S6 | AI providers skip checklist items on "obvious" setups | Rules applied inconsistently — auditing becomes impossible |
| G3 | Trade log has unparseable SELL entries | Backtesting skips or miscounts trades — performance metrics drift from reality |
| C5, G4, D6 | No R-value logging, no post-exit tracking | Slow degradation of signal quality data over 6–12 weeks |

---

## 6. Implementation Sequence — Next 5 Trading Days

### Before Monday open (P0 fixes — must complete)
- [ ] S1: Add R:R gate to `execute_decisions()` in `strategy_v6.py`
- [ ] G1: Add C < 6 hard block to `execute_decisions()`
- [ ] S2: Add cooldowns dict to state and check in BUY path
- [ ] D1: Add trigger-price confirmation check to DeepSeek prompt
- [ ] C1 + C2: Add timeframe tag to all prompts, add [SWING] exit logic gate

### By Wednesday (P1 fixes)
- [ ] G2 + S4: Update trailing stop activation logic in `check_auto_stop_rules()`
- [ ] D4: Add confidence-to-risk matrix check before position sizing
- [ ] S5 + D5: Add volume ratio field to DECISION template and validation
- [ ] C3: Define "technical weakness" criteria in Claude prompt
- [ ] C4: Increase Claude watchlist scan to source 3–4 candidates/week

### By end of week (P2 fixes)
- [ ] S6: Add pre-entry checklist block to all three AI provider prompts
- [ ] G3: Fix DECISION sell parser and add validation

### Next review cycle (P3 fixes)
- [ ] C5: Add post-exit tracking to session log
- [ ] G4: Add r_value field to `build_trade_log_entry()`
- [ ] D6: Add self-review paragraph to DeepSeek prompt

---

## 7. Performance Targets — Next Week

| Metric | Current | Target next week | Target 4-week |
|--------|---------|-----------------|---------------|
| Combined win rate | ~27% | ≥ 38% | ≥ 45% |
| Avg R per closed trade | ~0.3R | ≥ 0.8R | ≥ 1.5R |
| R:R violations (< 2.0) | 2 of 9 trades | 0 | 0 |
| C < 6 entries | 1 | 0 | 0 |
| Premature exits before stop | 3 of 9 | ≤ 1 | 0 |
| Revenge re-entries | 1 | 0 | 0 |
| Profit factor | < 1.0 | ≥ 1.1 | ≥ 1.5 |
| Trades with timeframe tag | 0 of 9 | 9 of 9 | 9 of 9 |
| Trades with volume ratio logged | 0 of 9 | 9 of 9 | 9 of 9 |

---

## 8. The Master Principle

> **Entry is 30% of the edge. Exit discipline is 70%.**

All three traders this week entered acceptable setups. All three failed at exit execution — leaving too early (Grok-AAPL, Claude-AAPL), entering before confirmation and stopping out (DeepSeek), or re-entering emotionally after a loss (DeepSeek). The entries were mostly fine. The exits and re-entries destroyed the P&L.

The 8 P0 fixes (S1, S2, D1, D2, D3, G1, C1, C2) address every critical failure from this week. Implementing them before Monday's open is the minimum viable action. Everything else improves the system — these 8 prevent it from actively losing money on rules it already knows it should follow.

---

*Report v2 generated: 2026-04-18 | Strategy: v6.0 | Next review: 2026-04-25*
