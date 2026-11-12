# Forward 24h Watchlist

- **General version:** `v4.26.6`
- **Scan finished (Israel Time):** `2026-11-12 13:58:55 IST`
- **Run:** `local-run-20261112T104037Z`
- **GitHub Actions run ID:** `local-b938a426b2b042b5a9dd668aa1e344fb`
- **GitHub Actions run URL:** unavailable
- **Status:** `ZERO_VERIFIED_OPPORTUNITIES`
- **Objective:** rank early Web3/DeFi breakout potential for the next 24 hours; source verification and execution readiness remain separate
- **NOT QUALIFIED research shortlist:** top 3 of 3 mature/unverified research candidates
- **Forecast horizon:** 24 hours
- **Ranked next-24h opportunities:** 106
- **Historical 24h comparable:** 38; not yet comparable: 68
- **Historical-age rule:** applies to past-return comparability only; it does not exclude young pools from the next-24h opportunity ranking.

- **Market regime:** MIXED_OR_UNVERIFIED; BTC breadth sample: 3236 assets; positive-alt breadth: 54.26%
- **Hyperliquid derivatives cross-check:** OK; matched candidates: 7 of 235 public perp markets

> Research-only reference plan, not an order. Upside scenarios are uncalibrated; the trade plan strictly enforces 1 single Take Profit (minimum +40.00%) with no stop loss and no trailing stop. Positions remain open for up to 24 hours until the single TP or 24h window mark. Neither hourly highs nor reference prices guarantee executable fills.

## Verified actionable opportunities

**ZERO VERIFIED OPPORTUNITIES** passed every mandatory final gate for this scan.


## Unverified research watchlist — NOT QUALIFIED

Research rankings and hypothetical upside do not satisfy the technical TP or verification gates. Only candidates with single-TP potential >= +40.00% are displayed.

| Pair | Chain | Current Price | Single TP | Potential (% till TP) | Pool Address | Token Contract | Pool Age | Status |
|---|---|---:|---:|---:|---|---|---:|---|
| AIB/SOL | solana | 0.000353 | 0.0009505251 | +169.27% | `5jN7YxKhncGwi3njP5Z8M6YYBKkuhL7p2axLLG2DaGK3` | `AJFnmpiF4VTKeqEWtwBUarib5zKKbXcs594UyrMHpump` | 9.51h | NOT QUALIFIED |
| Rocco / ETH | eth | 0.01814 | 0.0314538204 | +73.39% | `0x7641b8bc811f377720917d239e0fec414f4b90739db66346d2606a3d4a7d7782` | `0x10fdf1c42bcdb1de5da1393176dbd0fa3892d2dd` | 51.86h | NOT QUALIFIED |
| MTXUSDT | mexc | 1.4287 | 2.5239 | +76.65% | `CEX Order Book (MEXC)` | `CEX Custody (MTX)` | — | NOT QUALIFIED |

> *The 24h upside scenario is a volatility/momentum scenario from hourly candles, not a predicted return or calibrated probability. Initial downside is an ATR-based reference stop, not a guaranteed max loss. Scenario R:R is upside scenario divided by initial stop distance; it is a heuristic, not realized expectancy.

## Unverified research candidate details

### 1. AIB/SOL

**Qualification status: NOT QUALIFIED — research only.**

**AIB/SOL TP: +169.27%**

- **Next-24h opportunity rank:** 5
- **Next-24h opportunity score:** 35.64/100 (heuristic, not a probability)
- **Initial protective stop:** unavailable; candle ATR coverage is insufficient
- **24h upside scenario:** candle projection unavailable; candidate remains ranked using discovery momentum and research signals.
- **Tracked-token candle orientation:** base (contract matched)
- **Opportunity tier:** `EARLY_DISCOVERY`
- **Ranking model:** 70% early-breakout + 30% pre-age-guard research - risk penalties; projection unavailable
- **Late-chase / flow-risk penalty:** 14.00 points
- **Historical 24h return:** not comparable yet; this does not disqualify a new 24h forward opportunity.
- **Early-breakout score:** 69.54/100 — ACCELERATING_WATCHLIST
- **Early-breakout risk flags:** EXTREME_VOLUME_TO_LIQUIDITY_RATIO_CHECK_FOR_WASH_OR_ROTATION
- **Reference price:** `0.000353`
- **Single TP:** `0.0009505251`
- **TP method:** `OBSERVED_RECENT_RANGE_HIGH`
- **Quality gate:** `NOT_SHORTLISTED`
- **Execution ready:** `false`
- **Next-24h research rank:** `NOT QUALIFIED`
- **Historical age-guard disposition:** `NOT QUALIFIED`
- **Historical 24h return comparable:** `false`
- **Market-data timestamp (UTC):** `2026-11-12T11:06:28Z`
- **5m momentum:** +8.05%
- **1h momentum:** +36.08%
- **6h momentum:** +12.27%
- **24h momentum:** +1024.00%
- **24h volume (USD):** $1,758,605
- **Liquidity (USD):** $47,368
- **Pool age:** 9.51 hours
- **Evidence coverage:** 25.0%
- **Pool address:** `5jN7YxKhncGwi3njP5Z8M6YYBKkuhL7p2axLLG2DaGK3`
- **Market link:** https://dexscreener.com/solana/5jn7yxkhncgwi3njp5z8m6yybkkuhl7p2axllg2dagk3
- **Quality-gate reasons:**
  - DEX liquidity below $100k or unavailable
- **Entry blockers:**
  - price snapshot stale/unknown (> 45 min or timestamp missing)
  - independent DEX identity, safety, and two-way execution guard not PASS
  - executable buy AND sell route not verified
  - fresh trade-level spot CVD not verified (DEX swaps/CEX aggressor-side trades)
  - derivatives positioning audit incomplete/stale; check OI, funding, liquidations and long/short positioning
  - full token/contract safety checks not independently verified PASS
  - mandatory pre-pick source/timestamp evidence incomplete: spot_cvd: source/timestamp missing; derivatives: source/timestamp missing; route_quotes: source identity missing; safety: source/timestamp missing; single_take_profit: stale/future field timestamp
  - field-level provenance incomplete/stale: price: stale/future field timestamp; volume_15m: source/timestamp missing; volume_1h: stale/future field timestamp; spot_trades: source/timestamp missing; spot_cvd: source/timestamp missing; derivatives: source/timestamp missing; order_book: source/timestamp missing; route_quotes: source identity missing; safety: source/timestamp missing; single_take_profit: stale/future field timestamp
  - loss-pattern guard evidence missing
- **Risk flags:**
  - 24h move already >20%
  - contract/token safety is not independently verified PASS — speculative classification
  - 15m volume acceleration below 1.5x or missing
  - 1h volume acceleration below 1.5x or missing
  - spot buyer share below 53% or missing
  - spread above 30 bps or missing
  - two-sided 2% depth below $5000 research benchmark
  - high-risk scout: microcap/age/float/contract/liquidity risk
  - DEX_LOSS_PATTERN_GUARD_NOT_PASSED
  - LIQUIDITY_UNDER_100000_USD
  - NEW_POOL_UNDER_24H
  - PAID_PROMOTION_SIGNAL — not independent demand proof
  - 24H_RETURN_NOT_COMPARABLE_POOL_AGE_UNDER_24H_OR_UNKNOWN
- **Calibrated 24h probability:** unavailable (resolved observations: unknown)

### 2. Rocco / ETH

**Qualification status: NOT QUALIFIED — research only.**

**Rocco / ETH TP: +73.39%**

- **Next-24h opportunity rank:** 6
- **Next-24h opportunity score:** 34.94/100 (heuristic, not a probability)
- **1h ATR:** 10.28%
- **Initial protective stop:** `0.0138912009` (18.50% reference downside; 1.8X_1H_ATR_MIN_1PCT_MAX_25PCT_REFERENCE_RISK)
- **Scenario reward/risk:** 5.35x
- **24h upside scenario:** +98.94% (hourly volatility/momentum heuristic; not calibrated)
- **Scenario method:** 1H_ATR_RANGE_MOMENTUM_SCENARIO_NOT_CALIBRATED
- **Tracked-token candle orientation:** base (contract matched)
- **Opportunity tier:** `EARLY_DISCOVERY`
- **Ranking model:** 70% pre-age-guard research + 30% volatility/momentum upside projection - risk penalties
- **Late-chase / flow-risk penalty:** 0.00 points
- **Reference price:** `0.01814`
- **Single TP:** `0.0314538204`
- **TP method:** `OBSERVED_SWING_RESISTANCE`
- **Quality gate:** `FOLLOW_UP_SCREEN_PASSED_NOT_EXECUTION_READY`
- **Execution ready:** `false`
- **Next-24h research rank:** `NOT QUALIFIED`
- **Entry decision:** `NOT QUALIFIED`
- **Historical 24h return comparable:** `true`
- **Market-data timestamp (UTC):** `2026-11-12T11:33:40Z`
- **5m momentum:** +0.71%
- **1h momentum:** +7.42%
- **6h momentum:** +18.30%
- **24h momentum:** +149.95%
- **24h volume (USD):** $4,934,716
- **Liquidity (USD):** $381,340
- **Pool age:** 51.86 hours
- **Evidence coverage:** 45.5%
- **Pool address:** `0x7641b8bc811f377720917d239e0fec414f4b90739db66346d2606a3d4a7d7782`
- **Market link:** https://www.geckoterminal.com/eth/pools/0x7641b8bc811f377720917d239e0fec414f4b90739db66346d2606a3d4a7d7782
- **Entry blockers:**
  - independent DEX identity, safety, and two-way execution guard not PASS
  - executable buy AND sell route not verified
  - fresh trade-level spot CVD not verified (DEX swaps/CEX aggressor-side trades)
  - derivatives positioning audit incomplete/stale; check OI, funding, liquidations and long/short positioning
  - full token/contract safety checks not independently verified PASS
  - mandatory pre-pick source/timestamp evidence incomplete: spot_cvd: stale/future field timestamp; derivatives: source/timestamp missing; route_quotes: source identity missing
  - field-level provenance incomplete/stale: volume_15m: source/timestamp missing; spot_trades: stale/future field timestamp; spot_cvd: stale/future field timestamp; derivatives: source/timestamp missing; order_book: source/timestamp missing; route_quotes: source identity missing
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
- **Calibrated 24h probability:** `12.82%`

### 3. MTXUSDT

**Qualification status: NOT QUALIFIED — research only.**

**MTXUSDT TP: +76.65%**

- **Next-24h opportunity rank:** 16
- **Next-24h opportunity score:** 20.00/100 (heuristic, not a probability)
- **1h ATR:** 32.16%
- **Initial protective stop:** `1.0716` (25.00% reference downside; 1.8X_1H_ATR_MIN_1PCT_MAX_25PCT_REFERENCE_RISK)
- **Scenario reward/risk:** 10.00x
- **24h upside scenario:** +250.00% (hourly volatility/momentum heuristic; not calibrated)
- **Scenario method:** 1H_ATR_RANGE_MOMENTUM_SCENARIO_NOT_CALIBRATED
- **Opportunity tier:** `EARLY_DISCOVERY`
- **Ranking model:** 70% pre-age-guard research + 30% volatility/momentum upside projection - risk penalties
- **Late-chase / flow-risk penalty:** 10.00 points
- **Reference price:** `1.4287`
- **Single TP:** `2.5239`
- **TP method:** `OBSERVED_SWING_RESISTANCE`
- **Quality gate:** `NOT_SHORTLISTED`
- **Execution ready:** `false`
- **Next-24h research rank:** `NOT QUALIFIED`
- **Entry decision:** `NOT QUALIFIED`
- **Historical 24h return comparable:** `true`
- **Market-data timestamp (UTC):** `2026-11-12T10:49:33Z`
- **24h momentum:** +2098.08%
- **24h volume (USD):** $1,365,606
- **Evidence coverage:** 25.0%
- **Quality-gate reasons:**
  - No independent second-venue price confirmation
  - 24h return outside 2% to 300% screen
  - Cross-venue ticker collision or price divergence
- **Entry blockers:**
  - price snapshot stale/unknown (> 45 min or timestamp missing)
  - executable buy AND sell route not verified
  - fresh trade-level spot CVD not verified (DEX swaps/CEX aggressor-side trades)
  - derivatives positioning audit incomplete/stale; check OI, funding, liquidations and long/short positioning
  - full token/contract safety checks not independently verified PASS
  - mandatory pre-pick source/timestamp evidence incomplete: spot_cvd: source/timestamp missing; derivatives: source/timestamp missing; route_quotes: source/timestamp missing; safety: source/timestamp missing
  - field-level provenance incomplete/stale: price: stale/future field timestamp; volume_15m: source/timestamp missing; spot_trades: source/timestamp missing; spot_cvd: source/timestamp missing; derivatives: source/timestamp missing; order_book: source/timestamp missing; route_quotes: source/timestamp missing; safety: source/timestamp missing
- **Risk flags:**
  - 24h move already >20%
  - short-term momentum fading
  - contract/token safety is not independently verified PASS — speculative classification
  - 15m volume acceleration below 1.5x or missing
  - 1h volume acceleration below 1.5x or missing
  - spot buyer share below 53% or missing
  - spread above 30 bps or missing
  - two-sided 2% depth below $5000 research benchmark
  - no positive 1h/4h spot momentum
  - SINGLE_VENUE_ONLY — no cross-venue price confirmation
  - TICKER_COLLISION_OR_CROSS_VENUE_PRICE_DIVERGENCE
- **Calibrated 24h probability:** `12.76%`

## Interpretation

A `NOT_SHORTLISTED` quality gate or `execution_ready: false` means the candidate is a research lead, not a trade-ready pick. Missing evidence is not a pass. Probabilities are shown only when the candidate explicitly marks the value as calibrated.


## 24-hour reference-position monitoring

- **Status:** `OK`
- **Provider / resolution:** GeckoTerminal exact-token USD OHLCV for DEX; venue-specific spot OHLCV for CEX / 1h; full candles only; DEX USD denomination is selected by exact token contract/mint
- **Active references:** 2 / 12

| Asset / pair | Market | Pair address | Window closes (UTC) | MFE | Latest return | Protective stop | Status |
|---|---|---|---|---:|---:|---|---|
| chinese hat cat / CHAT/SOL | solana | `4p4UaFSzVfFaM8SyvqH1WzkG9GC89YoUuSL5pn6nY7FX` | 2026-11-12T22:50:51Z | +0.00% | +0.00% | initial: 0.0000195607 | OPEN — TARGET / PROTECTIVE STOP NOT YET OBSERVED |
| Shitcoin / SHITCOIN/SOL | solana | `9ovaDmtnzr164fJVBpypehiQ9a5FZMLaCaTTAprM8xxJ` | 2026-11-13T11:28:52Z | unavailable | unavailable | unavailable | OPEN — INITIAL REFERENCE |
| GREEN MORNING / GM / USDT | bsc | `0x4e3cc69020c4a43d7332f9998903e77d88d1098c` | 2026-11-08T00:24:02Z | +10349.08% | -100.00% | unavailable | 24H WINDOW CLOSED — TARGET UNVERIFIED |
| AKE / AKE/USDT | bsc | `0x20c0555ea3e2066673416423a17d64275fa43488` | 2026-11-08T01:27:20Z | +25.41% | +9.07% | unavailable | 24H WINDOW CLOSED — TARGET UNVERIFIED |
| Flap Coin / FLAP/USDT | bsc | `0x85c098721681a3af714ea2b1e8b7269b46f2b2dd` | 2026-11-08T12:11:39Z | +617275911.10% | +403.65% | unavailable | 24H WINDOW CLOSED — TARGET UNVERIFIED |
| MetaProSpaceToken / MPST/USDT | bsc | `0x7504e9e7dccb04c06e2df4fe91180c180216a307` | 2026-11-08T14:15:28Z | +3.19% | -3.56% | unavailable | 24H WINDOW CLOSED — TARGET UNVERIFIED |
| Cartoon Money / CBUCKS/USDC | solana | `B4Cp45PGifVM5FAXfh6AeV8QZ3eUNQnqGBw7KuC7MHnB` | 2026-11-08T17:06:17Z | +99.11% | +87.97% | unavailable | 24H WINDOW CLOSED — TP MILESTONE HIT; RUNNER EXITED AT TIME STOP |
| WANTED / WANTED/SOL | solana | `2qW4UmdbvQvsmxaepdfA5Bm6VbUuf1sR996J7JTVhbFL` | 2026-11-08T23:07:39Z | +0.00% | +0.00% | initial: 0.0000204634 | INITIAL PROTECTIVE STOP HIT — POSITION EXITED |
| Libra / Libra / WBNB | bsc | `0x5166508edf57c5218b0755acefd0fe25f253c084` | 2026-11-10T11:15:55Z | +0.00% | +0.00% | initial: 0.0067985115 | 24H WINDOW CLOSED — INCOMPLETE CANDLE COVERAGE |
| 中国人牛逼 / 中国人牛逼/SPCXB | bsc | `0xf6d7e50c8134f277c7daed5dba10f4918497c6be` | 2026-11-10T13:07:30Z | +0.00% | -79.07% | unavailable | 24H WINDOW CLOSED — TP NOT HIT |
| Elysium / ELYSIUM/PUMP | solana | `7LivQVeRtaajEiMF4fJKVnm8eqrPXVfTcUfwxeszBufg` | 2026-11-11T10:55:42Z | +56.20% | +13.39% | trail: 0.0001666585 | TRAILING STOP HIT — RUNNER EXITED |
| ARIA / ARIA_USDT | gate | `—` | 2026-11-11T14:46:35Z | +11.43% | +1.28% | trail: 0.21524 | TRAILING STOP HIT — RUNNER EXITED |
| CAP / CAPUSDT | bybit | `—` | 2026-11-11T14:46:34Z | +0.00% | +0.00% | initial: 0.114104 | EXIT AMBIGUOUS — TARGET AND STOP WITHIN SAME HOURLY CANDLE |
| MEMEOS / MEMEOS/SOL | solana | `Cxq6T1FqqwpwbnvKGP492qRUzvvLzxqAfKQupTMQd3L3` | 2026-11-11T18:22:32Z | +0.00% | -12.50% | initial: 0.0002447513 | INITIAL PROTECTIVE STOP HIT — POSITION EXITED |
| apeonx / X/SOL | solana | `CPWA5ufjXEyC7DTd7g5fVUxJYZ81kxvbC8f9R9XFx1Y8` | 2026-11-12T10:59:14Z | +0.00% | -11.70% | initial: 0.0003084037 | INITIAL PROTECTIVE STOP HIT — POSITION EXITED |
| ni hao cat / 哈基仙人/FXION | bsc | `0x599a0a927a2ad11d9ef07f2c155ba9a4de582003` | 2026-11-12T15:54:09Z | +37.12% | -3.50% | trail: 0.0000503835 | TRAILING STOP HIT — RUNNER EXITED |
| CAP / CAPUSDT | bybit | `—` | 2026-11-11T14:46:34Z | +0.00% | +0.00% | initial: 0.114104 | INITIAL PROTECTIVE STOP HIT — POSITION EXITED |
| MEMEOS / MEMEOS/SOL | solana | `Cxq6T1FqqwpwbnvKGP492qRUzvvLzxqAfKQupTMQd3L3` | 2026-11-11T18:22:32Z | +0.00% | -12.50% | initial: 0.0002447513 | INITIAL PROTECTIVE STOP HIT — POSITION EXITED |
| apeonx / X/SOL | solana | `CPWA5ufjXEyC7DTd7g5fVUxJYZ81kxvbC8f9R9XFx1Y8` | 2026-11-12T10:59:14Z | +0.00% | -11.70% | initial: 0.0003084037 | INITIAL PROTECTIVE STOP HIT — POSITION EXITED |
| 哈基仙人 / 哈基仙人/WBNB | bsc | `0xe93851207920fea5f1d8f569cdb45bf9d02207fc` | 2026-11-12T14:45:04Z | +21.25% | -20.13% | initial: 0.0000133599 | INITIAL PROTECTIVE STOP HIT — POSITION EXITED |
| ni hao cat / 哈基仙人/FXION | bsc | `0x599a0a927a2ad11d9ef07f2c155ba9a4de582003` | 2026-11-12T15:54:09Z | +37.12% | -3.50% | trail: 0.0000503835 | TRAILING STOP HIT — RUNNER EXITED |
| AudaCity / DACITYCOIN/TTWO | solana | `7Te1wXcfeQjKLGHXFvxSCnGZTqunoZ9fxBn7KKjrkNEr` | 2026-11-12T19:02:58Z | +42.74% | +7.77% | trail: 0.0073922513 | TRAILING STOP HIT — RUNNER EXITED |

> A TP touch is a milestone; the trailing runner remains open until its protective trail or the 24h time stop. Initial risk is ATR-based where candle coverage allows it. Hourly candle extrema are observed reference prices, not actual fills. A same-hour target/stop collision is marked ambiguous.