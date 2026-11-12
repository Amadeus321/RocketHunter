# Forward 24h Watchlist

- **General version:** `v4.26.7`
- **Scan finished (Israel Time):** `2026-11-12 22:15:07 IST`
- **Run:** `local-run-20261112T185036Z`
- **GitHub Actions run ID:** `local-eab753d93ac044d7a80d60bd59ff2831`
- **GitHub Actions run URL:** unavailable
- **Status:** `ZERO_VERIFIED_OPPORTUNITIES`
- **Objective:** rank early Web3/DeFi breakout potential for the next 24 hours; source verification and execution readiness remain separate
- **NOT QUALIFIED research shortlist:** top 2 of 2 mature/unverified research candidates
- **Forecast horizon:** 24 hours
- **Ranked next-24h opportunities:** 101
- **Historical 24h comparable:** 32; not yet comparable: 69
- **Historical-age rule:** applies to past-return comparability only; it does not exclude young pools from the next-24h opportunity ranking.

- **Market regime:** MIXED_OR_UNVERIFIED; BTC breadth sample: 3229 assets; positive-alt breadth: 42.92%
- **Hyperliquid derivatives cross-check:** OK; matched candidates: 4 of 235 public perp markets

> Research-only reference plan, not an order. Upside scenarios are uncalibrated; the trade plan strictly enforces 1 single Take Profit (minimum +40.00%) with no stop loss and no trailing stop. Positions remain open for up to 24 hours until the single TP or 24h window mark. Neither hourly highs nor reference prices guarantee executable fills.

## Verified actionable opportunities

**ZERO VERIFIED OPPORTUNITIES** passed every mandatory final gate for this scan.


## Unverified research watchlist — NOT QUALIFIED

Research rankings and hypothetical upside do not satisfy the technical TP or verification gates. Only candidates with single-TP potential >= +40.00% are displayed.

| Pair | Chain | Current Price | Single TP | Potential (% till TP) | Pool Address | Token Contract | Pool Age | Status |
|---|---|---:|---:|---:|---|---|---:|---|
| HUNG / NVDAx | solana | 0.0004247 | 0.0009921849 | +133.62% | `2ULaeaUQWkid31cVKyFEujx9ReF7cdGepPxuoptz57m5` | `4xgqefa5WcdHWKXzqvM9MRJqWYGaHVEUnpUL4Vq5pump` | 86.79h | NOT QUALIFIED |
| NOBUSHIBA/SOL | solana | 0.0007663 | 0.0015233828 | +98.80% | `Dagau7G32pmkjf8ng2372xo2hyGfgenx5Yn7mWMkHfGF` | `8AAVuQpJcqpvCuutxyBTVvgBEXZJSkZoNv1sExpBpump` | 41.55h | NOT QUALIFIED |

> *The 24h upside scenario is a volatility/momentum scenario from hourly candles, not a predicted return or calibrated probability. Initial downside is an ATR-based reference stop, not a guaranteed max loss. Scenario R:R is upside scenario divided by initial stop distance; it is a heuristic, not realized expectancy.

## Unverified research candidate details

### 1. HUNG / NVDAx

**Qualification status: NOT QUALIFIED — research only.**

**HUNG / NVDAx TP: +133.62%** *(AURA Front-Run Safe Target: +128.94%)*

- **Next-24h opportunity rank:** 8
- **Next-24h opportunity score:** 24.90/100 (heuristic, not a probability)
- **1h ATR:** 54.61%
- **Initial protective stop:** `0.0002125886` (25.00% reference downside; 1.8X_1H_ATR_MIN_1PCT_MAX_25PCT_REFERENCE_RISK)
- **Scenario reward/risk:** 10.00x
- **24h upside scenario:** +250.00% (hourly volatility/momentum heuristic; not calibrated)
- **Scenario method:** 1H_ATR_RANGE_MOMENTUM_SCENARIO_NOT_CALIBRATED
- **Tracked-token candle orientation:** base (contract matched)
- **Opportunity tier:** `EARLY_DISCOVERY`
- **Ranking model:** 70% pre-age-guard research + 30% volatility/momentum upside projection - risk penalties
- **Late-chase / flow-risk penalty:** 10.00 points
- **Reference price:** `0.0004247`
- **Single TP:** `0.0009921849`
- **TP method:** `OBSERVED_SWING_RESISTANCE`
- **Quality gate:** `NOT_SHORTLISTED`
- **Execution ready:** `false`
- **Next-24h research rank:** `NOT QUALIFIED`
- **Entry decision:** `NOT QUALIFIED`
- **Historical 24h return comparable:** `true`
- **Market-data timestamp (UTC):** `2026-11-12T19:16:43Z`
- **5m momentum:** +0.57%
- **1h momentum:** -5.09%
- **6h momentum:** +23.86%
- **24h momentum:** +3130.82%
- **24h volume (USD):** $6,409,646
- **Liquidity (USD):** $71,837
- **Pool age:** 86.79 hours
- **Evidence coverage:** 38.0%
- **Pool address:** `2ULaeaUQWkid31cVKyFEujx9ReF7cdGepPxuoptz57m5`
- **Market link:** https://www.geckoterminal.com/solana/pools/2ULaeaUQWkid31cVKyFEujx9ReF7cdGepPxuoptz57m5
- **Quality-gate reasons:**
  - DEX liquidity below $100k or unavailable
  - 24h return outside 0% to 2,500% screen
  - VOLUME_TO_LIQUIDITY_EXTREME — verify wash/turnover quality
- **Entry blockers:**
  - price snapshot stale/unknown (> 45 min or timestamp missing)
  - independent DEX identity, safety, and two-way execution guard not PASS
  - executable buy AND sell route not verified
  - fresh trade-level spot CVD not verified (DEX swaps/CEX aggressor-side trades)
  - derivatives positioning audit incomplete/stale; check OI, funding, liquidations and long/short positioning
  - full token/contract safety checks not independently verified PASS
  - mandatory pre-pick source/timestamp evidence incomplete: spot_cvd: source/timestamp missing; derivatives: source/timestamp missing; route_quotes: source/timestamp missing; safety: source/timestamp missing; single_take_profit: stale/future field timestamp
  - field-level provenance incomplete/stale: price: stale/future field timestamp; volume_15m: source/timestamp missing; volume_1h: stale/future field timestamp; spot_trades: source/timestamp missing; spot_cvd: source/timestamp missing; derivatives: source/timestamp missing; order_book: source/timestamp missing; route_quotes: source/timestamp missing; safety: source/timestamp missing; single_take_profit: stale/future field timestamp
  - loss-pattern guard evidence missing
- **Risk flags:**
  - 24h move already >20%
  - 7d move already >50%
  - short-term momentum fading
  - contract/token safety is not independently verified PASS — speculative classification
  - 15m volume acceleration below 1.5x or missing
  - spot buyer share below 53% or missing
  - spread above 30 bps or missing
  - two-sided 2% depth below $5000 research benchmark
  - no positive 1h/4h spot momentum
  - high-risk scout: microcap/age/float/contract/liquidity risk
  - DEX_LOSS_PATTERN_GUARD_NOT_PASSED
  - LIQUIDITY_UNDER_100000_USD
  - NEW_POOL_UNDER_14D
  - VOLUME_TO_LIQUIDITY_EXTREME — verify wash/turnover quality
- **Calibrated 24h probability:** `12.70%`

### 2. NOBUSHIBA/SOL

**Qualification status: NOT QUALIFIED — research only.**

**NOBUSHIBA/SOL TP: +98.80%** *(AURA Front-Run Safe Target: +95.34%)*

- **Next-24h opportunity rank:** 10
- **Next-24h opportunity score:** 23.15/100 (heuristic, not a probability)
- **1h ATR:** 67.10%
- **Initial protective stop:** `0.0005808939` (25.00% reference downside; 1.8X_1H_ATR_MIN_1PCT_MAX_25PCT_REFERENCE_RISK)
- **Scenario reward/risk:** 10.00x
- **24h upside scenario:** +250.00% (hourly volatility/momentum heuristic; not calibrated)
- **Scenario method:** 1H_ATR_RANGE_MOMENTUM_SCENARIO_NOT_CALIBRATED
- **Tracked-token candle orientation:** base (contract matched)
- **Opportunity tier:** `EARLY_DISCOVERY`
- **Ranking model:** 70% pre-age-guard research + 30% volatility/momentum upside projection - risk penalties
- **Late-chase / flow-risk penalty:** 10.00 points
- **Reference price:** `0.0007663`
- **Single TP:** `0.0015233828`
- **TP method:** `OBSERVED_SWING_RESISTANCE`
- **Quality gate:** `NOT_SHORTLISTED`
- **Execution ready:** `false`
- **Next-24h research rank:** `NOT QUALIFIED`
- **Entry decision:** `NOT QUALIFIED`
- **Historical 24h return comparable:** `true`
- **Market-data timestamp (UTC):** `2026-11-12T19:45:55Z`
- **5m momentum:** +1.23%
- **1h momentum:** +3.27%
- **6h momentum:** -26.93%
- **24h momentum:** +227.00%
- **24h volume (USD):** $2,046,104
- **Liquidity (USD):** $71,508
- **Pool age:** 41.55 hours
- **Evidence coverage:** 38.0%
- **Pool address:** `Dagau7G32pmkjf8ng2372xo2hyGfgenx5Yn7mWMkHfGF`
- **Market link:** https://api.dexscreener.com/latest/dex/tokens/npz2FhoufQ7pKqhGpaipYWacrAkidyW2yDpVWSvpump,AWuUCEXwrCvbixzMY5sQfNEY7hf6j8aeiDubv5Hhpump,4FP2GRQS3WYPsZSE2dwaVzqkajrBpHcXyVSTVt83pump,DtcicPCKTKeAh7TDEzTym3hXeoHAoMiBdYJhMYxZpump,B82PFKmT4aM38EUUqVoZs6sZdAfxDHHrB6DSXxCKpump,0x766F575A1aB2eAe9014B639761F80365fa0C94Fe,7TmeeJxBF3PFsbgzuy23XpWJzQsqVXjvgLTb8S2zpump,2hyMwC4c11L32VsJwWznCmvfERBhaz19CFpGQyHMpump,13Cb7SGfxqrtMDU1JNANyTnTmQtbmH55QWXXzdwBmoon,KMifrkYmDdAKtVXAVBEPEQxJ7qP2iLgUcfZjUZipump,4BmPe8LGK72S1yULdD5HG7aUYHHi8okboS2Nkufrpump,0xfaB7c96661531B944cb787AD28D2Ab4d9f9d4444,9L9XBcrRaMWLb18T837fB1JP73MCD7ANdRrdkdSqpump,wbcE7iMmWN1XGEa6mtgRS3FoLgc936rMJYjeujapump,BWJfBNeXpapG7uoqByp5KSwWgCQba8QcDmA2DQiEpump,HrvF3KLc2Q5P6pHbsJBNn6pPoUvb3jGBBKzVkSripump,8Gug6wcw7FacmrSNBGcquKi84ZrAvzJRzap9WuBqbkwA,GNeL8vepuzWCdormGWDV3EQc8h9s7yNPjoFjTB23iUYN,BmAt6895v6PczX3g8jguJheWCj4hYQK5MmnBYr8Yc948,CLRScyywR3CQSZBTrQpvyy3U5FwajnR4Xa3h9Wisdoge,8p7GCo3fWJ55LFQbhkBd8hCpw3ACk98PgK7Lsd6Upump,0x2Fc6dE9C65a620f918cA7d1191D3AFA495ca2898,0x944307de478Dbf305575D87F1493473c292771e2,9ry9Dx1sygydxypdrkrjwhPASjokePeo5Lyo8QYopump,8AAVuQpJcqpvCuutxyBTVvgBEXZJSkZoNv1sExpBpump,AJFnmpiF4VTKeqEWtwBUarib5zKKbXcs594UyrMHpump,HsprHxBRgiVaQdRf3TnKNuZZtaX9JUSPgMFdAkk6pump,0x6511c233790Ec10f7d673c64DC82C3F941D57828,CM65K38bqGePYkNQBjGRRDYMhyxgh9xTQg2HPQTFpump,EQDhnXQgjkymx2DgbcLZqvLClyBEsFAAr-RotkrXhf1OD3Gp
- **Quality-gate reasons:**
  - DEX liquidity below $100k or unavailable
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
  - 24h move already >20%
  - 7d move already >50%
  - contract/token safety is not independently verified PASS — speculative classification
  - 15m volume acceleration below 1.5x or missing
  - 1h volume acceleration below 1.5x or missing
  - spot buyer share below 53% or missing
  - spread above 30 bps or missing
  - two-sided 2% depth below $5000 research benchmark
  - DEX_LOSS_PATTERN_GUARD_NOT_PASSED
  - LIQUIDITY_UNDER_100000_USD
  - NEW_POOL_UNDER_14D
  - PAID_PROMOTION_SIGNAL — not independent demand proof
- **Calibrated 24h probability:** `12.70%`

## Interpretation

A `NOT_SHORTLISTED` quality gate or `execution_ready: false` means the candidate is a research lead, not a trade-ready pick. Missing evidence is not a pass. Probabilities are shown only when the candidate explicitly marks the value as calibrated.


## 24-hour reference-position monitoring

- **Status:** `OK`
- **Provider / resolution:** GeckoTerminal exact-token USD OHLCV for DEX; venue-specific spot OHLCV for CEX / 1h; full candles only; DEX USD denomination is selected by exact token contract/mint
- **Active references:** 2 / 12

| Asset / pair | Market | Pair address | Window closes (UTC) | MFE | Latest return | Protective stop | Status |
|---|---|---|---|---:|---:|---|---|
| Shitcoin / SHITCOIN/SOL | solana | `9ovaDmtnzr164fJVBpypehiQ9a5FZMLaCaTTAprM8xxJ` | 2026-11-13T11:28:52Z | +0.00% | +0.00% | unavailable | TARGET FORMING — MFE TRACKED |
| Forest / FOREST/WBNB | bsc | `0xf0601427e3bfc416bc7838fb9e15c6f0f56f4aa0` | 2026-11-13T19:36:43Z | unavailable | unavailable | initial: 0.0358815687 | OPEN — INITIAL REFERENCE |

> A TP touch is a milestone; the trailing runner remains open until its protective trail or the 24h time stop. Initial risk is ATR-based where candle coverage allows it. Hourly candle extrema are observed reference prices, not actual fills. A same-hour target/stop collision is marked ambiguous.