# Forward 24h Watchlist

- **General version:** `v4.26.7`
- **Scan finished (Israel Time):** `2026-11-14 12:47:00 IST`
- **Run:** `local-run-20261114T092115Z`
- **GitHub Actions run ID:** `local-d569d41060d84b7d8c2cd1762abc0a5f`
- **GitHub Actions run URL:** unavailable
- **Status:** `ZERO_VERIFIED_OPPORTUNITIES`
- **Objective:** rank early Web3/DeFi breakout potential for the next 24 hours; source verification and execution readiness remain separate
- **NOT QUALIFIED research shortlist:** top 2 of 2 mature/unverified research candidates
- **Forecast horizon:** 24 hours
- **Ranked next-24h opportunities:** 84
- **Historical 24h comparable:** 38; not yet comparable: 46
- **Historical-age rule:** applies to past-return comparability only; it does not exclude young pools from the next-24h opportunity ranking.

- **Market regime:** MIXED_OR_UNVERIFIED; BTC breadth sample: 3277 assets; positive-alt breadth: 49.25%
- **Hyperliquid derivatives cross-check:** OK; matched candidates: 13 of 235 public perp markets

> Research-only reference plan, not an order. Upside scenarios are uncalibrated; the trade plan strictly enforces 1 single Take Profit (minimum +40.00%) with no stop loss and no trailing stop. Positions remain open for up to 24 hours until the single TP or 24h window mark. Neither hourly highs nor reference prices guarantee executable fills.

## Verified actionable opportunities

**ZERO VERIFIED OPPORTUNITIES** passed every mandatory final gate for this scan.


## Unverified research watchlist — NOT QUALIFIED

Research rankings and hypothetical upside do not satisfy the technical TP or verification gates. Only candidates with single-TP potential >= +40.00% are displayed.

| Pair | Chain | Current Price | Single TP | Potential (% till TP) | Pool Address | Token Contract | Pool Age | Status |
|---|---|---:|---:|---:|---|---|---:|---|
| FRONG / WETH 0.25% | robinhood | 0.002566 | 0.0040020834 | +55.97% | `0xacea8920877840033f0275c37f9b61550b5326917e948bcf8339714d96f9521a` | `0x6245e67affa44a23077f0ea7f981a8dc743a0c47` | 2557.22h | NOT QUALIFIED |
| PONS / WPRL | base | 0.1229347878 | 0.4652381278 | +278.44% | `0xc4a328359592211d4e415915c6c6f4e8807f6b16` | `0x304b826a47a5e6cb2766ad9069f670651c9f847d` | 47.50h | NOT QUALIFIED |

> *The 24h upside scenario is a volatility/momentum scenario from hourly candles, not a predicted return or calibrated probability. The trade plan has exactly one single TP (+40.00% minimum), with no stop loss and no trailing stop. Candle excursions are research observations, not executable fills.

## Unverified research candidate details

### 1. FRONG / WETH 0.25%

**Qualification status: NOT QUALIFIED — research only.**

**FRONG / WETH 0.25% TP: +55.97%**

- **Next-24h opportunity rank:** 1
- **Next-24h opportunity score:** 40.25/100 (heuristic, not a probability)
- **1h ATR:** 7.14%
- **24h upside scenario:** +74.16% (hourly volatility/momentum heuristic; not calibrated)
- **Scenario method:** 1H_ATR_RANGE_MOMENTUM_SCENARIO_NOT_CALIBRATED
- **Tracked-token candle orientation:** base (contract matched)
- **Opportunity tier:** `EARLY_DISCOVERY`
- **Ranking model:** 70% pre-age-guard research + 30% volatility/momentum upside projection - risk penalties
- **Late-chase / flow-risk penalty:** 0.00 points
- **Reference price:** `0.002566`
- **Single TP:** `0.0040020834`
- **TP method:** `OBSERVED_SWING_RESISTANCE`
- **Quality gate:** `FOLLOW_UP_SCREEN_PASSED_NOT_EXECUTION_READY`
- **Execution ready:** `false`
- **Next-24h research rank:** `NOT QUALIFIED`
- **Entry decision:** `NOT QUALIFIED`
- **Historical 24h return comparable:** `true`
- **Market-data timestamp (UTC):** `2026-11-14T10:16:52Z`
- **5m momentum:** -1.11%
- **1h momentum:** +6.43%
- **6h momentum:** +11.74%
- **24h momentum:** +65.39%
- **24h volume (USD):** $1,534,594
- **Liquidity (USD):** $401,041
- **Pool age:** 2557.22 hours
- **Evidence coverage:** 41.0%
- **Pool address:** `0xacea8920877840033f0275c37f9b61550b5326917e948bcf8339714d96f9521a`
- **Market link:** https://www.geckoterminal.com/robinhood/pools/0xacea8920877840033f0275c37f9b61550b5326917e948bcf8339714d96f9521a
- **Entry blockers:**
  - independent DEX identity, safety, and two-way execution guard not PASS
  - executable buy AND sell route not verified
  - fresh trade-level spot CVD not verified (DEX swaps/CEX aggressor-side trades)
  - derivatives positioning audit incomplete/stale; check OI, funding, liquidations and long/short positioning
  - full token/contract safety checks not independently verified PASS
  - mandatory pre-pick source/timestamp evidence incomplete: spot_cvd: source/timestamp missing; derivatives: source/timestamp missing; route_quotes: source identity missing
  - field-level provenance incomplete/stale: volume_15m: source/timestamp missing; spot_trades: source/timestamp missing; spot_cvd: source/timestamp missing; derivatives: source/timestamp missing; order_book: source/timestamp missing; route_quotes: source identity missing
  - independent token-to-pool identity verification missing
  - launch-allocation and connected-wallet analysis missing
  - exact LP-position ownership/withdrawal audit missing
  - token-security fields incomplete: cannot_sell_all
  - fresh two-way buy and sell route quotes missing
- **Risk flags:**
  - 24h move already >20%
  - contract/token safety is not independently verified PASS — speculative classification
  - 15m volume acceleration below 1.5x or missing
  - spot buyer share below 53% or missing
  - spread above 30 bps or missing
  - two-sided 2% depth below $5000 research benchmark
  - DEX_LOSS_PATTERN_GUARD_NOT_PASSED
- **Calibrated 24h probability:** `12.80%`

### 2. PONS / WPRL

**Qualification status: NOT QUALIFIED — research only.**

**PONS / WPRL TP: +278.44%**

- **Next-24h opportunity rank:** 38
- **Next-24h opportunity score:** 3.00/100 (heuristic, not a probability)
- **Hyperliquid perp cross-check:** SYMBOL_AND_PRICE_CROSSCHECK_WITHIN_10PCT; mark $0.1291; hourly funding +0.0013%
- **24h upside scenario:** candle projection unavailable; candidate remains ranked using discovery momentum and research signals.
- **Tracked-token candle orientation:** base (contract matched)
- **Opportunity tier:** `EARLY_DISCOVERY`
- **Ranking model:** pre-age-guard research score; breakout/projection signal unavailable; risk penalties applied
- **Late-chase / flow-risk penalty:** 0.00 points
- **Reference price:** `0.1229347878`
- **Single TP:** `0.4652381278`
- **TP method:** `OBSERVED_SWING_RESISTANCE`
- **Quality gate:** `QUALITY STATUS UNAVAILABLE`
- **Execution ready:** `unknown`
- **Next-24h research rank:** `NOT QUALIFIED`
- **Entry decision:** `NOT QUALIFIED`
- **Historical 24h return comparable:** `true`
- **Market-data timestamp (UTC):** `2026-11-14T10:14:52Z`
- **5m momentum:** +0.00%
- **1h momentum:** +0.00%
- **6h momentum:** -13.20%
- **24h momentum:** -4.73%
- **24h volume (USD):** $157,378,481
- **Liquidity (USD):** $23,086,652
- **Pool age:** 47.50 hours
- **Evidence coverage:** 30.0%
- **Pool address:** `0xc4a328359592211d4e415915c6c6f4e8807f6b16`
- **Market link:** https://www.geckoterminal.com/base/pools/0xc4a328359592211d4e415915c6c6f4e8807f6b16
- **Entry blockers:**
  - independent DEX identity, safety, and two-way execution guard not PASS
  - executable buy AND sell route not verified
  - fresh trade-level spot CVD not verified (DEX swaps/CEX aggressor-side trades)
  - derivatives positioning audit incomplete/stale; check OI, funding, liquidations and long/short positioning
  - full token/contract safety checks not independently verified PASS
  - mandatory pre-pick source/timestamp evidence incomplete: spot_cvd: source/timestamp missing; derivatives: source/timestamp missing; route_quotes: source/timestamp missing; safety: source/timestamp missing
  - field-level provenance incomplete/stale: volume_15m: source/timestamp missing; spot_trades: source/timestamp missing; spot_cvd: source/timestamp missing; derivatives: source/timestamp missing; order_book: source/timestamp missing; route_quotes: source/timestamp missing; safety: source/timestamp missing
  - loss-pattern guard evidence missing
- **Risk flags:**
  - short-term momentum fading
  - contract/token safety is not independently verified PASS — speculative classification
  - 15m volume acceleration below 1.5x or missing
  - 1h volume acceleration below 1.5x or missing
  - spot buyer share below 53% or missing
  - spread above 30 bps or missing
  - two-sided 2% depth below $5000 research benchmark
  - no positive 1h/4h spot momentum
  - DEX_LOSS_PATTERN_GUARD_NOT_PASSED
- **Calibrated 24h probability:** `12.75%`

## Interpretation

A `NOT_SHORTLISTED` quality gate or `execution_ready: false` means the candidate is a research lead, not a trade-ready pick. Missing evidence is not a pass. Probabilities are shown only when the candidate explicitly marks the value as calibrated.


## 24-hour reference-position monitoring

- **Status:** `OK`
- **Provider / resolution:** GeckoTerminal exact-token USD OHLCV for DEX; venue-specific spot OHLCV for CEX / 1h; full candles only; DEX USD denomination is selected by exact token contract/mint
- **Active references:** 2 / 12

| Asset / pair | Market | Pair address | Window closes (UTC) | MFE | Latest return | Status |
|---|---|---|---|---:|---:|---|
| Microsoft / MSFT/MSFTX | solana | `8n8cKHmF5RJEDGMcuVudpdYddfmPdKZT776jo6G1CrEj` | 2026-11-14T11:42:34Z | +18.44% | -38.60% | OPEN — SINGLE TP NOT YET OBSERVED |
| frong / FRONG / WETH 0.25% | robinhood | `0xacea8920877840033f0275c37f9b61550b5326917e948bcf8339714d96f9521a` | 2026-11-15T10:16:52Z | unavailable | unavailable | OPEN — INITIAL REFERENCE |

> Exactly one single TP of at least +40.00% is used; there is no stop loss or trailing stop. This read-only reference window continues until the single TP touch or its 24-hour close. Hourly candle extrema are observations, not executable fills.