# Phase 1.2 — Counterfactual settlement P&L on bot fills

Generated: 2026-05-10T10:03:47.994307+00:00
Source DB: `/Users/openclaw/polymarket-arb/data/poly_mm_live.db`

## Coverage
- Total fills: 326
- Distinct markets: 110
- Resolved markets: 110
- Fills in resolved markets: 326

## All-fills counterfactual (upper bound)
- Contracts: 640
- Wins: 298 | Losses: 342
- Win rate: 46.6%
- Avg win: 50.65c | Avg loss: 48.08c
- Frequency edge: -3.39c
- Magnitude edge: +1.29c
- Mean P&L per contract: **-2.11c**
- Total counterfactual: $-13.49

## Post-haircut mean (decision number)
- Haircut: 0.7 selection × 0.9 slippage × 0.8 hit-rate = 0.5
- Mean P&L per contract post-haircut: **-2.11c**

## Defensive-only subset (game-start unhedged inventory)
- Unhedged markets: 98
- Resolved unhedged: 98
- Contracts: 212
- Win rate: 39.6%
- Mean P&L per contract: -8.63c
- Total: $-18.29

## Aggregation: by side
- yes_bid: n_contracts=408, total=$-18.85, mean=-4.62c, win_rate=43.9%
- no_bid: n_contracts=232, total=$+5.36, mean=+2.31c, win_rate=51.3%

## Aggregation: by category
- sports: n_contracts=640, total=$-13.49, mean=-2.11c, win_rate=46.6%

## Comparison vs realized
- Stored realized pair_pnl total: +0.00c ($+0.00)
- Stored fees total: +279.17c

## Decision gate (per plan)
- **VERDICT: thesis fails (post-haircut < 0)**

## Caveats and known issues
- DB stores Kalshi-style positive maker fees (formula 0.0175*P*(1-P)*100). Polymarket actual rebate is NEGATIVE (formula 0.02*0.25*P*(1-P)*100 = 0.005*P*(1-P)*100). Counterfactual P&L computed without fees because: (a) entry fees are sunk, identical between strategies; (b) hold-to-settle has no exit fee. Cross-check vs realized requires same fees.
- Counterfactual is an upper bound; selection bias, slippage, and asymmetric
  hit-rate haircut applied to derive the decision number.
- Defensive subset uses volume-weighted entry price across net-side fills as a proxy.
- Markets that have not yet resolved (or whose resolution lookup failed) are excluded.