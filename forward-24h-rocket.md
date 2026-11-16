# Forward 24h Watchlist

- **General version:** `v4.26.7`
- **Scan finished (Israel Time):** `2026-11-15 18:55:12 IST`
- **Run:** `local-run-20261115T152715Z`
- **GitHub Actions run ID:** `local-87782d8097fe4569a864d31a4c16d3dc`
- **GitHub Actions run URL:** unavailable
- **Status:** `ZERO_VERIFIED_OPPORTUNITIES`
- **Objective:** rank early Web3/DeFi breakout potential for the next 24 hours; source verification and execution readiness remain separate
- **NOT QUALIFIED research shortlist:** top 1 of 1 mature/unverified research candidates
- **Forecast horizon:** 24 hours
- **Ranked next-24h opportunities:** 100
- **Historical 24h comparable:** 30; not yet comparable: 70
- **Historical-age rule:** applies to past-return comparability only; it does not exclude young pools from the next-24h opportunity ranking.

- **Market regime:** MIXED_OR_UNVERIFIED; BTC breadth sample: 3234 assets; positive-alt breadth: 56.65%
- **Hyperliquid derivatives cross-check:** OK; matched candidates: 12 of 235 public perp markets

> Research-only reference plan, not an order. Upside scenarios are uncalibrated; the trade plan strictly enforces 1 single Take Profit (minimum +40.00%) with no stop loss and no trailing stop. Positions remain open for up to 24 hours until the single TP or 24h window mark. Neither hourly highs nor reference prices guarantee executable fills.

## Verified actionable opportunities

**ZERO VERIFIED OPPORTUNITIES** passed every mandatory final gate for this scan.


## Unverified research watchlist — NOT QUALIFIED

Research rankings and hypothetical upside do not satisfy the technical TP or verification gates. Only candidates with single-TP potential >= +40.00% are displayed.

| Pair | Chain | Current Price | Single TP | Potential (% till TP) | Pool Address | Token Contract | Pool Age | Status |
|---|---|---:|---:|---:|---|---|---:|---|
| mihao / ETH | eth | 0.01526 | 0.0225061584 | +47.48% | `0xed6c13aa281124eeb8cd9195bba31a3a78b1d6a84665fb3da0ad86cd130f6900` | `0xb3a6add33f777ac6ea0ec88bdf533573c8bbbc03` | 105.81h | NOT QUALIFIED |

> *The 24h upside scenario is a volatility/momentum scenario from hourly candles, not a predicted return or calibrated probability. The trade plan has exactly one single TP (+40.00% minimum), with no stop loss and no trailing stop. Candle excursions are research observations, not executable fills.

## Unverified research candidate details

### 1. mihao / ETH

**Qualification status: NOT QUALIFIED — research only.**

**mihao / ETH TP: +47.48%**

- **Next-24h opportunity rank:** 11
- **Next-24h opportunity score:** 20.38/100 (heuristic, not a probability)
- **1h ATR:** 10.93%
- **24h upside scenario:** +221.72% (hourly volatility/momentum heuristic; not calibrated)
- **Scenario method:** 1H_ATR_RANGE_MOMENTUM_SCENARIO_NOT_CALIBRATED
- **Tracked-token candle orientation:** base (contract matched)
- **Opportunity tier:** `EARLY_DISCOVERY`
- **Ranking model:** 70% pre-age-guard research + 30% volatility/momentum upside projection - risk penalties
- **Late-chase / flow-risk penalty:** 18.00 points
- **Reference price:** `0.01526`
- **Single TP:** `0.0225061584`
- **TP method:** `OBSERVED_SWING_RESISTANCE`
- **Quality gate:** `FOLLOW_UP_SCREEN_PASSED_NOT_EXECUTION_READY`
- **Execution ready:** `false`
- **Next-24h research rank:** `NOT QUALIFIED`
- **Entry decision:** `NOT QUALIFIED`
- **Historical 24h return comparable:** `true`
- **Market-data timestamp (UTC):** `2026-11-15T15:53:25Z`
- **5m momentum:** +5.59%
- **1h momentum:** +68.47%
- **6h momentum:** +615.30%
- **24h momentum:** +242.29%
- **24h volume (USD):** $1,508,950
- **Liquidity (USD):** $330,558
- **Pool age:** 105.81 hours
- **Evidence coverage:** 38.0%
- **Pool address:** `0xed6c13aa281124eeb8cd9195bba31a3a78b1d6a84665fb3da0ad86cd130f6900`
- **Market link:** https://www.geckoterminal.com/eth/pools/0xed6c13aa281124eeb8cd9195bba31a3a78b1d6a84665fb3da0ad86cd130f6900
- **Entry blockers:**
  - price snapshot stale/unknown (> 45 min or timestamp missing)
  - independent DEX identity, safety, and two-way execution guard not PASS
  - executable buy AND sell route not verified
  - fresh trade-level spot CVD not verified (DEX swaps/CEX aggressor-side trades)
  - derivatives positioning audit incomplete/stale; check OI, funding, liquidations and long/short positioning
  - full token/contract safety checks not independently verified PASS
  - mandatory pre-pick source/timestamp evidence incomplete: spot_cvd: source/timestamp missing; derivatives: source/timestamp missing; route_quotes: source identity missing; single_take_profit: stale/future field timestamp
  - field-level provenance incomplete/stale: price: stale/future field timestamp; volume_15m: source/timestamp missing; volume_1h: stale/future field timestamp; spot_trades: source/timestamp missing; spot_cvd: source/timestamp missing; derivatives: source/timestamp missing; order_book: source/timestamp missing; route_quotes: source identity missing; single_take_profit: stale/future field timestamp
  - independent token-to-pool identity verification missing
  - launch-allocation and connected-wallet analysis missing
  - exact LP-position ownership/withdrawal audit missing
  - independent token-security result has no structured fields
- **Risk flags:**
  - 24h move already >20%
  - 7d move already >50%
  - contract/token safety is not independently verified PASS — speculative classification
  - 15m volume acceleration below 1.5x or missing
  - 1h volume acceleration below 1.5x or missing
  - spot buyer share below 53% or missing
  - spread above 30 bps or missing
  - two-sided 2% depth below $5000 research benchmark
  - DEX_LOSS_PATTERN_GUARD_NOT_PASSED
  - NEW_POOL_UNDER_14D
- **Calibrated 24h probability:** `12.77%`

## Interpretation

A `NOT_SHORTLISTED` quality gate or `execution_ready: false` means the candidate is a research lead, not a trade-ready pick. Missing evidence is not a pass. Probabilities are shown only when the candidate explicitly marks the value as calibrated.


## 24-hour reference-position monitoring

- **Status:** `OK`
- **Provider / resolution:** GeckoTerminal exact-token USD OHLCV for DEX; venue-specific spot OHLCV for CEX / 1h; full candles only; DEX USD denomination is selected by exact token contract/mint
- **Active references:** 1 / 12

| Asset / pair | Market | Pair address | Window closes (UTC) | MFE | Latest return | Status |
|---|---|---|---|---:|---:|---|
| 牛逼 / 牛逼/USDT | bsc | `0x3b3c4548154c7b07b4dbb0a620f2b44c56340347` | 2026-11-16T15:54:13Z | unavailable | unavailable | OPEN — INITIAL REFERENCE |

> Exactly one single TP of at least +40.00% is used; there is no stop loss or trailing stop. This read-only reference window continues until the single TP touch or its 24-hour close. Hourly candle extrema are observations, not executable fills.