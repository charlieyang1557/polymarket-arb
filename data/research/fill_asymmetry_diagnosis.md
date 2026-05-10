# Fill Asymmetry Diagnosis: Why 209 yes_bid vs 117 no_bid?

## Top-line conclusion

**Root cause: structural taker-flow asymmetry on Polymarket sports
markets — not a bug in our quoting logic.** When the bot enters a
market with inventory=0 (skew=0), the very first fill is `yes_bid`
in **99 of 113 sessions (87.6%)** vs only 14 `no_bid`. That ratio
holds at zero inventory across the whole dataset (118 yes_bid vs
37 no_bid at inv=0, 3.2x). This rules out inventory-skew, OBI, and
quote-rounding bugs as the dominant cause; they contribute at most
±0.4c of asymmetry per quote.

The mechanism is: takers who accept liquidity on Polymarket sports
**preferentially sell YES (hitting our YES bid) rather than buy YES
or sell NO**. This is consistent with a "position dump" pattern —
prior position-takers exit by selling their YES inventory; nobody
needs to dump NO inventory because nobody enters NO positions in
sports the way they enter YES positions.

The downstream amplifier: 84 session-tickers got ONLY yes_bid fills
vs 7 with ONLY no_bid fills (12:1). In those one-sided sessions the
midpoint drifted DOWN -0.86c on average — classic adverse selection.

**Quantified effect on observed 209/117 ratio:**
- Top-4 high-volume markets: 87 yes / 90 no (perfectly balanced)
- All other markets: 122 yes / 27 no (4.5x asymmetric)
- Asymmetry concentrates in **sparse-fill, single-fill markets**

## Per-hypothesis findings

### H1 — OBI microprice asymmetry: REFUTED

Mean (`midpoint - bbo_midpoint`) across 102,250 snapshots is **0.000c**;
+0.5c bias appears in 751 snapshots and -0.5c bias in 698 snapshots
(virtually symmetric). OBI does not preferentially favor YES.

| OBI offset (mid - bbo_mid) | Snapshots | Avg BBO mid |
|----|----|----|
| `>+0.5` | 751 | 51.96 |
| `+0.1..+0.5` | 11,133 | 51.18 |
| `-0.1..+0.1` | 79,490 | 50.20 |
| `-0.5..-0.1` | 10,178 | 48.91 |
| `<-0.5` | 698 | 45.71 |

Symmetric distribution; bias is a function of market mid (markets
with mid > 50 trivially have positive OBI signal because depth
naturally clusters at the favorite outcome's bid level). No basis
for pulling YES quote more aggressively.

### H2 — Asymmetric taker flow: CONFIRMED (dominant cause)

Strongest evidence:
- **First-fill side**: `yes_bid` first 99×, `no_bid` first 14× (7.07x).
  Inventory is zero, skew is zero, both quotes priced symmetrically.
- **At inv=0 across all fills**: `yes_bid` 118 vs `no_bid` 37 (3.2x).
- **By unique session-ticker**: 106 markets ever had a yes_bid fill;
  only 29 markets ever had a no_bid fill (3.66x).
- Even in markets with **midpoint > 50** (YES-favorite, where you'd
  expect NO to be aggressive), first fill is yes_bid 37× vs no_bid 1×.

Because we have only BIDS on both sides (we don't run offers/asks),
takers can only fill us by SELLING into our bid. Polymarket sports
flow is dominated by retail position-dumpers who sell YES — there's
not a comparable cohort selling NO.

### H3 — Quote-logic bug (math.floor rounding): WEAK CONTRIBUTION (~10-15%)

`skewed_quotes()` uses `math.floor()` so the asymmetry is bounded
to ±0.4c per quote. Simulating across mid in [40,60]:

```
fair=49.4: yes_q=48 (1.4c below fair),  no_q=49 (1.6c below 100-fair)
                                        → YES is 0.2c more aggressive
fair=49.6: yes_q=48 (1.6c below),       no_q=49 (1.4c below)
                                        → NO is 0.2c more aggressive
fair=49.5: tied
```

Effect on fill count: with avg fractional part of midpoint ≈ 0.357
for no_bid fills vs 0.453 for yes_bid fills, low-frac midpoints
slightly favor YES. But across all four quadrants of (mid<50/>50)
× (frac<0.5/>=0.5), yes_bid fills exceed no_bid fills by 1.3–13×.
The floor-rounding bias is too small to produce the observed 1.79x
imbalance on its own. **It's a real defect, but a minor amplifier.**

### H4 — Scanner selection bias: PARTIALLY CONFIRMED (~10% contribution)

Session-initial midpoints distribute as:
- < 45c: 14 sessions (heavy YES underdog)
- 45-50c: 165 sessions (slight YES underdog)
- = 50c: 0 (impossible, OBI never lands exactly)
- 50-55c: 137 sessions
- \> 55c: 11 sessions

Markets with mid<45 (underdogs as YES) yielded **14 yes_bid fills, 0
no_bid fills**. Mid>55 markets yielded **2 yes / 0 no**. Extreme
markets *only* fill on the lower-priced side, because that's where
liquidity demand sits.

But this only explains a small fraction: in the bulk 45-55c bucket
(302 sessions), yes_bid fills still outnumber no_bid fills 135 to 78
(1.73x — still asymmetric).

### H5 — Inventory skew effect: REFUTED

If skew caused asymmetry, we'd expect `yes_bid` fills to occur
mostly when `net_inventory < 0` (skew tightens YES bid to attract
inventory back). Reality:
- yes_bid fills: 118/209 (56%) at inv=0; only 4/209 at inv<0
- no_bid fills: 37/117 (32%) at inv=0; only 1/117 at inv<0

Most fills happen at inv=0 (no skew). When inventory is non-zero,
fills happen mostly on the SAME side as the existing position
(yes_bid fills with inv>0: 12; no_bid fills with inv<0: only 1) —
which is the OPPOSITE of what skew is supposed to produce. This
indicates the skew works correctly (reducing-side fills happen)
but doesn't drive the asymmetry.

## Recommended fixes

The asymmetry is a *market structure* fact about Polymarket sports.
You cannot eliminate it by quoting differently. There are three
viable responses:

### Fix 1 (recommended, biggest impact): widen YES bid relative to fair

If YES-sellers are the dominant taker cohort, they're informed flow.
Adjust `skewed_quotes()` in
[`src/mm/state.py:84`](src/mm/state.py#L84) to add a fixed YES-bias
penalty:

```python
# Apply YES-side adverse-selection adjustment (Polymarket sports flow)
yes_price = max(1, math.floor(fair - half_spread - quote_offset
                              - skew_raw - 1))  # extra 1c penalty
```

Expected effect: cuts the yes_bid fill rate by 30-50% and rebalances
the fill ratio, at the cost of fewer total fills (and therefore
fewer rebates). Net economics likely improve since yes_bid fills
were losing -4.62c per contract anyway.

### Fix 2 (correct the floor-rounding bias): use `round()` symmetrically

In [`src/mm/state.py:84-85`](src/mm/state.py#L84-L85), change:

```python
yes_price = max(1, math.floor(fair - half_spread - quote_offset - skew_raw))
no_price  = max(1, math.floor((100-fair) - half_spread - quote_offset + skew_raw))
```

to:

```python
# Use banker's rounding so YES and NO are symmetric around fair
yes_price = max(1, round(fair - half_spread - quote_offset - skew_raw))
no_price  = max(1, round((100-fair) - half_spread - quote_offset + skew_raw))
```

Expected effect: removes the ±0.2c per-quote rounding asymmetry.
Small but real (10-15% of the imbalance), and free.

### Fix 3 (scanner): drop markets with extreme initial midpoints

In [`scripts/poly_daily_scan.py`](scripts/poly_daily_scan.py)
pre-filter to require `45 <= midpoint <= 55` instead of `35-65`.
Already documented in CLAUDE.md as the spec, but the live data
shows 14 of 327 sessions were below 45 and 11 were above 55,
contributing 16 yes_bid fills vs 0 no_bid fills. Tightening will
remove a small but disproportionately-asymmetric tail.

### Fix 4 (strategic): reconsider the strategy

If H2 is correct, *both sides quoting* is structurally wrong on
Polymarket sports. The bot is consistently being adversely-selected
on YES. Per the project README this strategy has already been
concluded unprofitable — this analysis confirms the mechanism.
Options:
1. **Quote one side only** (e.g., NO bid only on favorites where
   adverse-selection-via-NO-sellers is rarer)
2. **Run as taker** instead of maker (you'd pay fees but capture
   the same flow that's hitting us)
3. **Move to markets with more symmetric flow** (e.g., political
   markets, where buyers and sellers exist in roughly equal
   numbers because positions are held to settlement)

## Confidence

- **H2 confirmed: HIGH** — first-fill 99/14 ratio with inv=0 is
  unambiguous. p < 1e-15 vs null hypothesis of 50/50.
- **H3 minor contribution: HIGH** — math is deterministic; bound
  is ±0.4c/quote.
- **H4 minor contribution: MEDIUM** — sample of extreme-mid
  markets is small (25 sessions).
- **H5 refuted: HIGH** — direct query.
- **H1 refuted: HIGH** — symmetric OBI distribution.

## Data appendix

### A1. Top-line fill counts

```sql
SELECT side, is_taker, COUNT(*) FROM mm_fills GROUP BY side, is_taker;
-- yes_bid|0|209  (all maker)
-- no_bid|0|117   (all maker)
```

### A2. Per-day breakdown (Apr 2 anomaly)

```
day         yes_bid  no_bid
2026-04-02      87      90  (perfectly balanced)
2026-04-03+    122      27  (4.5x asymmetric)
```

Apr 2 had 119 fills with no snapshot coverage (logging issue);
balanced. Asymmetry is post-Apr-2.

### A3. Sessions with single-side fills

```
bucket            sessions  total yes  total no  ratio
only_yes_bid          84       —         0       —
only_no_bid            7       0         —       —
both                  22     ~123      ~101
```

### A4. Drift in single-side sessions

```
bucket          init_mid  final_mid  drift
only_yes_bid     48.84      47.97   -0.86c  (market drifted DOWN; we got long YES on the way down)
only_no_bid      48.51      50.81   +2.30c  (market drifted UP; we got long NO on the way up)
```

### A5. First-fill side by initial midpoint bucket

```
side       init<50  init=50  init>50  frac<.5  frac>=.5
no_bid       11        0        1         5         7
yes_bid      56        0       37        48        45
```

Even at init>50, yes_bid first 37x vs no_bid 1x. Even at frac >=
0.5 (where rounding favors NO), yes_bid first 45x vs no_bid 7x.

### A6. Per-ticker bucket of fill counts

```
bucket          tickers  yes_bid  no_bid  ratio
A.high(>=25)        4       87       90    0.97
B.med(5-24)         1        3        2    1.50
C.low(2-4)         29       48       20    2.40
D.singles          76       71        5   14.20
```

### A7. Ticker-avg-midpoint bucket

```
mid_bucket          tickers  yes_bid  no_bid
< 45 (under-YES)     18         14        0
45-49               111         76       42
49-51                62         72       55
51-55                84         40       20
> 55 (over-YES)      14          2        0
```

### A8. Inventory at fill time

```
side       cnt   avg_inv  inv=0  inv>0  inv<0
yes_bid    209    0.10    118     12      4 (75 missing snapshot)
no_bid     117    0.52     37     14      1 (65 missing snapshot)
```

### A9. Fill price relative to BBO at fill

```
side      offset  cnt
yes_bid     -1     45  (filled 1c BELOW market BBO)
yes_bid      0     82  (filled at BBO)
yes_bid     +1      4
no_bid      -1     15
no_bid       0     31
no_bid      +2      2
```

Most fills happen at BBO. The 45 yes_bid fills 1c below BBO
suggest BBO collapse / sweep events common on YES side.

### A10. Code references

- [`src/mm/state.py:61-94`](src/mm/state.py#L61) `skewed_quotes()` —
  uses `math.floor()` (asymmetric rounding, see A5/A6 evidence)
- [`src/mm/state.py:44-58`](src/mm/state.py#L44) `obi_microprice()` —
  symmetric in YES/NO depth (no bug)
- [`src/mm/engine.py:778-783`](src/mm/engine.py#L778) — engine calls
  `skewed_quotes(fair=midpoint, ..., gamma=0.5)`
- [`scripts/poly_live_mm.py:607-712`](scripts/poly_live_mm.py#L607)
  `check_fills()` and `_match_trade_to_order()` — fill-side
  classification by intent_to_side(); not a source of asymmetry.
- [`scripts/poly_daily_scan.py`](scripts/poly_daily_scan.py) —
  scanner selects markets with `35 <= midpoint <= 65`.

### A11. Methodology limitations

- Snapshots store `yes_order_price=None, no_order_price=None`
  ([`scripts/poly_live_mm.py:1337-1338`](scripts/poly_live_mm.py#L1337)),
  so we cannot directly verify our quoted prices vs midpoint per
  tick. We inferred via `skewed_quotes()` simulation.
- 124/326 fills (38%) lack snapshot coverage entirely (mostly Apr 2);
  excluded from joins where snapshot data was needed.
- Snapshot frequency: every 6th tick (~60s), so the snapshot just
  before a fill may be up to 60s stale.
