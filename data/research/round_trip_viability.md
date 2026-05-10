# Phase 1.3 — Round-trip viability test

Generated: 2026-05-10T10:07:36.161860+00:00
Source DB: `/Users/openclaw/polymarket-arb/data/poly_mm_live.db`

## Sample
- Fills: 326
- Full snapshot coverage (mid+30s+2min+5min): 24/326
- Adverse-move thresholds: 30s≥1.0c, 2min≥2.0c, 5min≥3.0c

## Overall
- Mean half-spread captured: +0.27c
- Mean loss distance (informed-fill subset): +3.22c
- α_tolerance = 0.0844
- α_observed (informed share) = 0.0215
- α_observed × 1.5 margin = 0.0322
- **VERDICT: viable**

## By midpoint band
- 35-45: n=5, half_spread=+1.39c, loss_dist=4.04c, α_tol=0.345, α_obs=0.200, verdict=viable
- 45-55: n=85, half_spread=+0.91c, loss_dist=2.88c, α_tol=0.317, α_obs=0.059, verdict=viable
- 55-65: n=2, half_spread=+2.07c, loss_dist=4.11c, α_tol=0.503, α_obs=0.500, verdict=marginal

## By side
- yes_bid: n=209, half_spread=+0.30c, α_tol=0.085, α_obs=0.029, verdict=viable
- no_bid: n=117, half_spread=+0.22c, α_tol=0.190, α_obs=0.009, verdict=viable

## Informed classification breakdown
- Total informed (any window): 7 (2.1%)
- Only 30s: 1 | Only 2min: 2 | Only 5min: 1
- Multi-window (≥2 windows triggered): 3

## Decision interpretation
Round-trip strategy is structurally viable on its own. Hold-to-settle is additive, not necessary.