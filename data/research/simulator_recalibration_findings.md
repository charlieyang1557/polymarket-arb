# Simulator Re-Calibration with Trade Tape — Findings

**Generated:** 2026-05-19.
**Script:** [scripts/research/roundtrip_simulator.py](../../scripts/research/roundtrip_simulator.py)
**Inputs:** 326 historical fills (Mar-Apr) + 1,810 trade tape rows (May 11-19)

## TL;DR

Path 1 (re-calibrate simulator survival rates with current trade tape data) is **complete and inconclusive**. The trade tape's directional signal, applied retrospectively to historical fills, gives net P&L **worse than baseline by up to $9.37**. Even under optimal threshold tuning, the auto-calibrated configuration only matches baseline. No static penalty configuration the simulator can express beats baseline +$2.19 by more than ~$0.30.

**Strategic implication:** The MM strategy is at-best break-even in the simulator (and slightly negative in realized live, −$2.92), and the trade tape doesn't provide a stable signal to tilt the edge positive. Path 2 (adaptive in real-time) is plausible but data shows the underlying signal is unstable enough to make adaptive learning hard.

## Full result table

Ran the round-trip simulator on 326 historical live fills under each configuration:

| Configuration | ALL net P&L | Δ vs baseline | Notes |
|---|---|---|---|
| **baseline** (no penalty) | **+$2.19** | — | reference; corresponds to realized live −$2.92 (simulator over-optimistic by $5) |
| flat 1c YES (current Path C) | +$0.63 | −$1.56 | confirms penalty is mildly negative aggregate |
| differential `tsc:base` | +$2.48 | **+$0.29** | best static config; relies on historical drift correlation |
| differential `tsc:pessimistic` | +$2.54 | +$0.35 | marginally better but within trial variance |
| auto[base, threshold 0.45/0.55] | **−$7.18** | **−$9.37** | catastrophic; trade tape signal mis-applied |
| auto[base, threshold 0.40] | +$2.26 | +$0.07 | conservative threshold → essentially no-op |
| auto[base, threshold 0.30+] | +$2.26 | +$0.07 | same as above (no prefix triggers) |

## Why the aggressive auto-calibration fails

The trade tape (May 11-19) tagged 7 prefixes for `no_penalty` (current flow is NO_BID drain heavy) and 1 for `yes_penalty` (`astatc-mlb`, 88% YES_BID share). Applied to historical fills:

| Prefix | Trade tape signal | Historical fill profile | Why it hurt |
|---|---|---|---|
| `asc-nba` | no_penalty (44.4% — barely under 0.45) | yes:no = 47:35 (~balanced) | Dropped ~36% of historical no_bid fills, which had been the PROFITABLE side (+$46 hold-to-settle in this slice). |
| `aec-mlb` | no_penalty | yes:no = 7:0 | Historical data too thin to matter. |
| `aec-wta` | no_penalty | yes:no = 1:0 | Historical too thin. |
| `aec-cs2` | no_penalty | — | Not in historical fills. |
| `tsc-nba` | no_penalty (19.4%) | yes:no = 20:6 | Only 6 no_bid fills historically, drop has small effect. |
| `astatc-mlb` | yes_penalty (88%) | — | Not in historical fills (this prefix appeared after Apr). |

**The asc-nba "no_penalty" tag did almost all the damage** ($−9.37). The threshold mechanism flagged it because 44.4% is barely under 0.45, and the effect was massive because asc-nba had a lot of historical no_bid fills that were favorable.

This is the textbook **temporal mismatch problem**:
- Calibration period (May 11-19): asc markets drifted NO → fewer YES_BID hits → tagged "no_penalty"
- Historical period (Mar-Apr): asc markets had favorable NO outcomes → no_bid fills were profitable → cutting them hurts

The current signal does NOT reliably predict that historical fills' direction.

## What this tells us about Path 2 (adaptive)

Path 2 was supposed to address temporal variance by refitting in real time. But this re-calibration result raises a concern:

**If the directional signal is unstable enough that current-week data is harmful when applied to fills 2 months prior, can we expect a few-hour-old signal to reliably predict the next few-hour fills?**

The empirical evidence for that prediction horizon doesn't exist in our data. To answer it, we'd need:
1. Split trade tape into early (e.g., May 11-14) and late (May 15-19) halves
2. Calibrate on the early half
3. Apply to the late half (treated as "future")
4. See if the calibration delivers positive net P&L on the late-half fills

If yes → Path 2 has signal worth chasing.
If no → the signal decays too fast to be exploitable.

**This in-sample test is the critical gate before any Path 2 implementation work.**

## Threshold sensitivity

Lowering `--auto-threshold-low` from 0.45 to 0.40 excludes `asc-nba` (44.4%) from the no_penalty group. With that exclusion the only remaining no_penalty prefixes have thin historical data, so the auto-calibration becomes essentially a no-op (+$2.26 ≈ baseline +$2.19).

This implies: **the catastrophic −$7.18 result was driven by ONE prefix (asc-nba) near the threshold boundary**, not a broad signal. The trade tape signal is too unstable to use without per-prefix sanity checks.

## My adjudication: Path 2 is now LOWER priority

Earlier (before running the re-calibration), I leaned Path 1 first then Path 2 if Path 1 showed positive signal. Path 1's result is:

- No static penalty configuration the simulator can express improves on baseline by more than ~$0.30
- The trade-tape-derived calibration is catastrophic under realistic thresholds
- Best simulator config (tsc:base differential, +$2.48) is sample-specific to the historical period and may not generalize

Given this, Path 2's expected ceiling is low. Even if a perfectly adaptive system captured the full theoretical gain in EVERY market, the simulator says that's <$0.50 per 326-fill sample.

**Recommended next direction (in order of expected value):**

1. **Drop the YES penalty entirely.** Revert Path C's flat 1c to 0. Predicted P&L: baseline +$2.19 (simulator), realized −$2.92 + ~$1.56 recovery = −$1.36. Still negative but $1.56 better than current.

2. **Run the early/late split test** before committing to Path 2 build. If trade tape signal stable over days, Path 2 may add small value; if not, abandon Path 2.

3. **Pivot to a Path B option:**
   - Kalshi politics (different market structure, potentially symmetric flow)
   - Same-platform taker (capture the flow that's been adversely-selecting us — interesting because the trade tape shows clear directional flow we could ride instead of fight)
   - Single-side quoting (only quote the YES_ASK / NO_BID side per prefix based on trade tape signal, skipping the symmetric MM mechanism)

4. **Investigate adverse selection at the order level** with the trade tape + snapshots. The current simulator approximates this — with aggressor-aware data we could measure actual VPIN per Bartlett & O'Hara Section 6 and decide whether a toxicity gate beats penalty tilting.

5. **Continue trade tape collection** regardless. More data per prefix improves all downstream analyses.

## What I'm NOT recommending

- **Implement `{"tsc": 1}` differential live.** Earlier recommendation. Now invalidated — the +$0.29 gain is too small to justify the implementation/approval cost, and tsc's signal isn't stable.
- **Implement Path 2 immediately.** Too low expected value given Path 1's result.
- **Continue running the bot in paper mode and expect strategy validation.** It's an operational shakedown; the P&L numbers are not strategy evidence (per [memory/feedback_live_is_ground_truth.md](file:///Users/openclaw/.claude/projects/-Users-openclaw-polymarket-arb/memory/feedback_live_is_ground_truth.md)).

## Decision point for the user

Three forks, in order of my lean:

**Fork A (most actionable): Revert flat 1c YES penalty, keep collecting trade tape, run early/late split test.**
- Bot config returns to "no penalty" baseline. Predicted small improvement on realized P&L.
- Cheap to do, no live risk.
- Buys time to gather more trade tape data + run validation tests.
- If split test is positive, build Path 2; if negative, escalate to Fork B.

**Fork B: Pivot to Path B alternatives.**
- The MM thesis is weakened enough that exploring different strategy structures is warranted.
- Specifically: same-platform taker (run alongside trade tape — capture the directional flow we observe) is the most data-grounded option.

**Fork C: Accept break-even and continue current Path C.**
- Realized −$2.92, expected to stay roughly there.
- Safest in terms of capital but doesn't make progress.

I lean **Fork A** (revert penalty, gather more data, validate before bigger moves). Specifically the revert is a small, well-scoped change that aligns with the simulator's recommendation and avoids the bigger commitment of either Fork B or Path 2.
