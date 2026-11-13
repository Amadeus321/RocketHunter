# Forward 24h Watchlist

- **General version:** `v4.26.7`
- **Scan finished (Israel Time):** `2026-11-13 12:44:46 IST`
- **Run:** `local-run-20261113T091729Z`
- **GitHub Actions run ID:** `local-run-20261113T091729Z`
- **GitHub Actions run URL:** unavailable
- **Status:** `COMPLETED`
- **Objective:** rank early Web3/DeFi breakout potential for the next 24 hours; source verification and execution readiness remain separate
- **NOT QUALIFIED research shortlist:** top 1 of 1 mature/unverified research candidates
- **Forecast horizon:** 24 hours
- **Ranked next-24h opportunities:** 146
- **Historical 24h comparable:** 71; not yet comparable: 75
- **Historical-age rule:** applies to past-return comparability only; it does not exclude young pools from the next-24h opportunity ranking.

- **Market regime:** MIXED_OR_UNVERIFIED; BTC breadth sample: 3321 assets; positive-alt breadth: 48.81%
- **Hyperliquid derivatives cross-check:** OK; matched candidates: 11 of 235 public perp markets

> Research-only reference plan, not an order. Upside scenarios are uncalibrated; the trade plan strictly enforces 1 single Take Profit (minimum +40.00%) with no stop loss and no trailing stop. Positions remain open for up to 24 hours until the single TP or 24h window mark. Neither hourly highs nor reference prices guarantee executable fills.

## Verified actionable opportunities

**ZERO VERIFIED OPPORTUNITIES** passed every mandatory final gate for this scan.


## Unverified research watchlist — NOT QUALIFIED

Research rankings and hypothetical upside do not satisfy the technical TP or verification gates. Only candidates with single-TP potential >= +40.00% are displayed.

| Pair | Chain | Current Price | Single TP | Potential (% till TP) | Pool Address | Token Contract | Pool Age | Status |
|---|---|---:|---:|---:|---|---|---:|---|
| DNA_USDT | gate | 0.17242 | 0.24488625 | +42.03% | `CEX Order Book (GATE)` | `CEX Custody (DNA)` | — | NOT QUALIFIED |

> *The 24h upside scenario is a volatility/momentum scenario from hourly candles, not a predicted return or calibrated probability. Initial downside is an ATR-based reference stop, not a guaranteed max loss. Scenario R:R is upside scenario divided by initial stop distance; it is a heuristic, not realized expectancy.

## Unverified research candidate details

### 1. DNA_USDT

**Qualification status: NOT QUALIFIED — research only.**

**DNA_USDT TP: +42.03%**

- **Next-24h opportunity rank:** 6
- **Next-24h opportunity score:** 30.52/100 (heuristic, not a probability)
- **1h ATR:** 1.79%
- **Initial protective stop:** unavailable; candle ATR coverage is insufficient
- **24h upside scenario:** +34.54% (hourly volatility/momentum heuristic; not calibrated)
- **Scenario method:** 1H_ATR_RANGE_MOMENTUM_SCENARIO_NOT_CALIBRATED
- **Opportunity tier:** `EARLY_DISCOVERY`
- **Ranking model:** 70% pre-age-guard research + 30% volatility/momentum upside projection - risk penalties
- **Late-chase / flow-risk penalty:** 0.00 points
- **Reference price:** `0.17242`
- **Single TP:** `0.24488625`
- **TP method:** `OBSERVED_SWING_RESISTANCE`
- **Quality gate:** `FOLLOW_UP_SCREEN_PASSED_NOT_EXECUTION_READY`
- **Execution ready:** `false`
- **Next-24h research rank:** `NOT QUALIFIED`
- **Entry decision:** `NOT QUALIFIED`
- **Historical 24h return comparable:** `true`
- **Market-data timestamp (UTC):** `2026-11-13T09:26:24Z`
- **24h momentum:** +10.89%
- **24h volume (USD):** $48,303
- **Evidence coverage:** 38.0%
- **Entry blockers:**
  - executable buy AND sell route not verified
  - fresh trade-level spot CVD not verified (DEX swaps/CEX aggressor-side trades)
  - derivatives positioning audit incomplete/stale; check OI, funding, liquidations and long/short positioning
  - full token/contract safety checks not independently verified PASS
  - mandatory pre-pick source/timestamp evidence incomplete: spot_cvd: source/timestamp missing; derivatives: source/timestamp missing; route_quotes: source/timestamp missing; safety: source/timestamp missing
  - field-level provenance incomplete/stale: volume_15m: source/timestamp missing; spot_trades: source/timestamp missing; spot_cvd: source/timestamp missing; derivatives: source/timestamp missing; order_book: source/timestamp missing; route_quotes: source/timestamp missing; safety: source/timestamp missing
- **Risk flags:**
  - short-term momentum fading
  - contract/token safety is not independently verified PASS — speculative classification
  - 15m volume acceleration below 1.5x or missing
  - spot buyer share below 53% or missing
  - spread above 30 bps or missing
  - two-sided 2% depth below $5000 research benchmark
  - no positive 1h/4h spot momentum
- **Calibrated 24h probability:** `12.70%`

## Interpretation

A `NOT_SHORTLISTED` quality gate or `execution_ready: false` means the candidate is a research lead, not a trade-ready pick. Missing evidence is not a pass. Probabilities are shown only when the candidate explicitly marks the value as calibrated.


## 24-hour reference-position monitoring

- **Status:** `OK`
- **Provider / resolution:** GeckoTerminal exact-token USD OHLCV for DEX; venue-specific spot OHLCV for CEX / 1h; full candles only; DEX USD denomination is selected by exact token contract/mint
- **Active references:** 3 / 12

| Asset / pair | Market | Pair address | Window closes (UTC) | MFE | Latest return | Protective stop | Status |
|---|---|---|---|---:|---:|---|---|
| Shitcoin / SHITCOIN/SOL | solana | `9ovaDmtnzr164fJVBpypehiQ9a5FZMLaCaTTAprM8xxJ` | 2026-11-13T11:28:52Z | +0.00% | +0.00% | unavailable | TARGET FORMING — MFE TRACKED |
| Forest / FOREST/WBNB | bsc | `0xf0601427e3bfc416bc7838fb9e15c6f0f56f4aa0` | 2026-11-13T19:36:43Z | +0.00% | +0.00% | unavailable | OPEN — SINGLE TP NOT YET OBSERVED |
| DNA / DNA_USDT | gate | `—` | 2026-11-14T09:26:24Z | unavailable | unavailable | unavailable | OPEN — INITIAL REFERENCE |

> A TP touch is a milestone; the trailing runner remains open until its protective trail or the 24h time stop. Initial risk is ATR-based where candle coverage allows it. Hourly candle extrema are observed reference prices, not actual fills. A same-hour target/stop collision is marked ambiguous.