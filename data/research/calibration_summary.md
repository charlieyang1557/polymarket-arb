# Phase 1.1 — Polymarket sports calibration vs realized settlement

Generated: 2026-05-10T09:56:27.880040+00:00
Data: `data/historical/price_history.json`

## Sample size precheck
- Total resolved sports markets in file: **307**
- In main band [0.30, 0.70): **39**
- In wider band [0.25, 0.75): **52**
- Precheck ok: **False** | Fallback: **aggregate_only**
- Band used for analysis: **(0.3, 0.7)**

## Aggregate calibration (in-band, equal-weighted)
- n = 39
- Mean implied price: 0.450
- Realized YES-settle rate: 0.179
- 95% bootstrap CI on settle rate: [0.051, 0.308]
- Aggregate gap (realized − implied): **-27.03pp**

## Per-bucket (size = 5c)

| Bucket | n | Wins | Settle YES rate | Mean price | 95% CI | Gap (pp) | CI excludes implied? |
|---|---|---|---|---|---|---|---|
| 0.30-0.35 | 8 | 2 | 0.250 | 0.338 | [0.000, 0.625] | -8.82 |  |
| 0.35-0.40 | 3 | 0 | 0.000 | 0.383 | [0.000, 0.000] | -38.33 | ✓ |
| 0.40-0.45 | 6 | 1 | 0.167 | 0.427 | [0.000, 0.500] | -26.07 |  |
| 0.45-0.50 | 13 | 2 | 0.154 | 0.481 | [0.000, 0.385] | -32.73 | ✓ |
| 0.50-0.55 | 7 | 1 | 0.143 | 0.519 | [0.000, 0.429] | -37.64 | ✓ |
| 0.60-0.65 | 2 | 1 | 0.500 | 0.615 | [0.000, 1.000] | -11.50 |  |

## Decision gate (per plan, derived from break-even economics ~2-3c)
- Max miscalibration in band: **38.33pp** (signed: -38.33pp)
- **VERDICT: insufficient data — aggregate-only result, treat as preliminary**

## Caveats
- Sports is NOT 'single-name' per paper Appendix B — expected miscalibration is smaller than paper's headline 25pp.
- Equal-weighted bucket statistics; volume weighting may differ.
- Pre-game price defined as `price_24h_before`; midlife fallback used when null.
- This measures Polymarket-vs-realized-settlement, NOT Polymarket-vs-Pinnacle (different question from strategy_a).