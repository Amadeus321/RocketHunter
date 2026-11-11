# Forward 24h Watchlist

- **General version:** `v4.26.6`
- **Scan finished (Israel Time):** `2026-11-12 01:16:57 IST`
- **Run:** `local-run-20261111T220023Z`
- **GitHub Actions run ID:** `local-654d1425d6fb41e4a84cdaf8b5df3cc4`
- **GitHub Actions run URL:** unavailable
- **Status:** `ZERO_VERIFIED_OPPORTUNITIES`
- **Objective:** rank early Web3/DeFi breakout potential for the next 24 hours; source verification and execution readiness remain separate
- **NOT QUALIFIED research shortlist:** top 3 of 3 mature/unverified research candidates
- **Forecast horizon:** 24 hours
- **Ranked next-24h opportunities:** 110
- **Historical 24h comparable:** 42; not yet comparable: 68
- **Historical-age rule:** applies to past-return comparability only; it does not exclude young pools from the next-24h opportunity ranking.

- **Market regime:** RISK_OFF; BTC breadth sample: 3299 assets; positive-alt breadth: 23.7%
- **Hyperliquid derivatives cross-check:** OK; matched candidates: 5 of 235 public perp markets

> Research-only reference plan, not an order. Upside scenarios are uncalibrated; the trade plan strictly enforces 1 single Take Profit (minimum +40.00%) with no stop loss and no trailing stop. Positions remain open for up to 24 hours until the single TP or 24h window mark. Neither hourly highs nor reference prices guarantee executable fills.

## Verified actionable opportunities

Only candidates passing the final price, identity, +40% single-TP, safety and two-way quote gates appear here.

| Pair | Chain | Current Price | Single TP | Potential (% till TP) | Pool Address | Token Contract | Verification | Safety | Execution |
|---|---|---:|---:|---:|---|---|---|---|---|
| BURNIE / SOL | solana | 0.0008039 | 0.0026474622 | +229.33% | `5tYFviFWQRKV9BJSTHGitbdqEYC1BGUgRUDnSADUXqJP` | `CGEDT9QZDvvH5GmVkWJH2BXiMJqMJySC9ihWyr7Spump` | QUALIFIED | PASS | PASS |
| ARIA / USDT 0.01% | bsc | 0.2294 | 0.3259987026 | +42.11% | `0xa5dbeaf16fc031eae92175974f8d0a439be4ad17` | `0x5d3a12c42e5372b2cc3264ab3cdcf660a1555238` | QUALIFIED | PASS | PASS |
| mihao / ETH | eth | 0.005156 | 0.0085271582 | +65.38% | `0xed6c13aa281124eeb8cd9195bba31a3a78b1d6a84665fb3da0ad86cd130f6900` | `0xb3a6add33f777ac6ea0ec88bdf533573c8bbbc03` | QUALIFIED | PASS | PASS |
| PAYDAY / SOL | solana | 0.0001787 | 0.0002505569 | +40.21% | `9rQAUw4uNyfsqsJ9F3BPo3hBFY1V6ptaEtPkRhhH3zpL` | `uHMWNFjUNxbPy1q43WrdbzygmJwdeh6AiQm1NKdxPAY` | QUALIFIED | PASS | PASS |

## Unverified research watchlist — NOT QUALIFIED

Research rankings and hypothetical upside do not satisfy the technical TP or verification gates. Only candidates with single-TP potential >= +40.00% are displayed.

| Pair | Chain | Current Price | Single TP | Potential (% till TP) | Pool Address | Token Contract | Pool Age | Status |
|---|---|---:|---:|---:|---|---|---:|---|
| MTXUSDT | mexc | 2.6046 | 3.904 | +49.89% | `—` | `—` | — | NOT QUALIFIED |
| MTX-USDT | kucoin | 3.0001 | 4.4887 | +49.62% | `—` | `—` | — | NOT QUALIFIED |
| Tilly/SPCX | solana | 0.0000444 | 0.000065083 | +46.58% | `8EgzFk4NyHRPZPFtkCJPWzEg4maciht7xvk5YGfi9Ybd` | `BFfrW2h5CuTMHbDmXeJL5sdKAZVQDHrShwCPf2Jrpump` | 54.50h | NOT QUALIFIED |

> *The 24h upside scenario is a volatility/momentum scenario from hourly candles, not a predicted return or calibrated probability. Initial downside is an ATR-based reference stop, not a guaranteed max loss. Scenario R:R is upside scenario divided by initial stop distance; it is a heuristic, not realized expectancy.

## Unverified research candidate details

### 1. MTXUSDT

**Qualification status: NOT QUALIFIED — research only.**

**MTXUSDT TP: +49.89%**

- **Next-24h opportunity rank:** 57
- **Next-24h opportunity score:** 0.00/100 (heuristic, not a probability)
- **Initial protective stop:** unavailable; candle ATR coverage is insufficient
- **24h upside scenario:** candle projection unavailable; candidate remains ranked using discovery momentum and research signals.
- **Opportunity tier:** `EARLY_DISCOVERY`
- **Ranking model:** pre-age-guard research score; breakout/projection signal unavailable; risk penalties applied
- **Late-chase / flow-risk penalty:** 10.00 points
- **Reference price:** `2.6046`
- **Single TP:** `3.904`
- **TP method:** `OBSERVED_SWING_RESISTANCE`
- **Quality gate:** `NOT_SHORTLISTED`
- **Execution ready:** `false`
- **Next-24h research rank:** `NOT QUALIFIED`
- **Entry decision:** `NOT QUALIFIED`
- **Historical 24h return comparable:** `true`
- **Market-data timestamp (UTC):** `2026-11-11T22:11:23Z`
- **24h momentum:** +4053.83%
- **24h volume (USD):** $1,281,293
- **Evidence coverage:** 35.0%
- **Market link:** https://api.mexc.com/api/v3/depth?symbol=MTXUSDT&limit=100
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
  - field-level provenance incomplete/stale: price: stale/future field timestamp; volume_15m: source/timestamp missing; spot_trades: source/timestamp missing; spot_cvd: source/timestamp missing; derivatives: source/timestamp missing; order_book: stale/future field timestamp; route_quotes: source/timestamp missing; safety: source/timestamp missing
- **Risk flags:**
  - 24h move already >20%
  - 7d move already >50%
  - BTC/market risk-off regime — stricter long-entry bar applied
  - contract/token safety is not independently verified PASS — speculative classification
  - 15m volume acceleration below 2.0x or missing
  - 1h volume acceleration below 2.0x or missing
  - spot buyer share below 58% or missing
  - spread above 20 bps or missing
  - two-sided 2% depth below $5000 research benchmark
  - BTC risk-off: relative strength below +2pp or unavailable
  - SINGLE_VENUE_ONLY — no cross-venue price confirmation
  - TICKER_COLLISION_OR_CROSS_VENUE_PRICE_DIVERGENCE
- **Calibrated 24h probability:** `12.63%`

### 2. MTX-USDT

**Qualification status: NOT QUALIFIED — research only.**

**MTX-USDT TP: +49.62%**

- **Next-24h opportunity rank:** 59
- **Next-24h opportunity score:** 0.00/100 (heuristic, not a probability)
- **Initial protective stop:** unavailable; candle ATR coverage is insufficient
- **24h upside scenario:** candle projection unavailable; candidate remains ranked using discovery momentum and research signals.
- **Opportunity tier:** `EARLY_DISCOVERY`
- **Ranking model:** pre-age-guard research score; breakout/projection signal unavailable; risk penalties applied
- **Late-chase / flow-risk penalty:** 10.00 points
- **Reference price:** `3.0001`
- **Single TP:** `4.4887`
- **TP method:** `OBSERVED_SWING_RESISTANCE`
- **Quality gate:** `NOT_SHORTLISTED`
- **Execution ready:** `false`
- **Next-24h research rank:** `NOT QUALIFIED`
- **Entry decision:** `NOT QUALIFIED`
- **Historical 24h return comparable:** `true`
- **Market-data timestamp (UTC):** `2026-11-11T22:09:17Z`
- **24h momentum:** +1100.04%
- **24h volume (USD):** $1,042,601
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
  - field-level provenance incomplete/stale: price: stale/future field timestamp; volume_15m: source/timestamp missing; spot_trades: source/timestamp missing; spot_cvd: source/timestamp missing; derivatives: source/timestamp missing; order_book: stale/future field timestamp; route_quotes: source/timestamp missing; safety: source/timestamp missing
- **Risk flags:**
  - 24h move already >20%
  - 7d move already >50%
  - BTC/market risk-off regime — stricter long-entry bar applied
  - contract/token safety is not independently verified PASS — speculative classification
  - 15m volume acceleration below 2.0x or missing
  - 1h volume acceleration below 2.0x or missing
  - spot buyer share below 58% or missing
  - spread above 20 bps or missing
  - two-sided 2% depth below $5000 research benchmark
  - BTC risk-off: relative strength below +2pp or unavailable
  - SINGLE_VENUE_ONLY — no cross-venue price confirmation
  - TICKER_COLLISION_OR_CROSS_VENUE_PRICE_DIVERGENCE
- **Calibrated 24h probability:** `12.63%`

### 3. Tilly/SPCX

**Qualification status: NOT QUALIFIED — research only.**

**Tilly/SPCX TP: +46.58%**

- **Next-24h opportunity rank:** 58
- **Next-24h opportunity score:** 0.00/100 (heuristic, not a probability)
- **Initial protective stop:** unavailable; candle ATR coverage is insufficient
- **24h upside scenario:** candle projection unavailable; candidate remains ranked using discovery momentum and research signals.
- **Tracked-token candle orientation:** base (contract matched)
- **Opportunity tier:** `EARLY_DISCOVERY`
- **Ranking model:** pre-age-guard research score; breakout/projection signal unavailable; risk penalties applied
- **Late-chase / flow-risk penalty:** 25.00 points
- **Reference price:** `0.0000444`
- **Single TP:** `0.000065083`
- **TP method:** `ATR_RANGE_PROJECTION`
- **Quality gate:** `NOT_SHORTLISTED`
- **Execution ready:** `false`
- **Next-24h research rank:** `NOT QUALIFIED`
- **Entry decision:** `NOT QUALIFIED`
- **Historical 24h return comparable:** `true`
- **Market-data timestamp (UTC):** `2026-11-11T22:26:51Z`
- **5m momentum:** +13.01%
- **1h momentum:** +200.00%
- **6h momentum:** +592.00%
- **24h momentum:** +568.00%
- **24h volume (USD):** $141,923
- **Pool age:** 54.50 hours
- **Evidence coverage:** 25.0%
- **Pool address:** `8EgzFk4NyHRPZPFtkCJPWzEg4maciht7xvk5YGfi9Ybd`
- **Market link:** https://dexscreener.com/solana/8egzfk4nyhrpzpftkcjpwzeg4maciht7xvk5ygfi9ybd
- **Quality-gate reasons:**
  - DEX liquidity below $100k or unavailable
  - DEX 24h volume below $250k or unavailable
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
  - 7d move already >50%
  - BTC/market risk-off regime — stricter long-entry bar applied
  - contract/token safety is not independently verified PASS — speculative classification
  - 15m volume acceleration below 2.0x or missing
  - 1h volume acceleration below 2.0x or missing
  - spot buyer share below 58% or missing
  - spread above 20 bps or missing
  - two-sided 2% depth below $5000 research benchmark
  - BTC risk-off: relative strength below +2pp or unavailable
  - 24h spot turnover below $250k or missing
  - DEX_LOSS_PATTERN_GUARD_NOT_PASSED
  - NEW_POOL_UNDER_14D
  - NO_LIQUIDITY_DATA
- **Calibrated 24h probability:** `12.63%`

## Interpretation

A `NOT_SHORTLISTED` quality gate or `execution_ready: false` means the candidate is a research lead, not a trade-ready pick. Missing evidence is not a pass. Probabilities are shown only when the candidate explicitly marks the value as calibrated.


## 24-hour reference-position monitoring

- **Status:** `OK`
- **Provider / resolution:** GeckoTerminal exact-token USD OHLCV for DEX; venue-specific spot OHLCV for CEX / 1h; full candles only; DEX USD denomination is selected by exact token contract/mint
- **Active references:** 3 / 12

| Asset / pair | Market | Pair address | Window closes (UTC) | MFE | Latest return | Protective stop | Status |
|---|---|---|---|---:|---:|---|---|
| 哈基仙人 / 哈基仙人/WBNB | bsc | `0xe93851207920fea5f1d8f569cdb45bf9d02207fc` | 2026-11-12T14:45:04Z | +21.25% | -21.65% | initial: 0.0000133599 | POSSIBLE TP TOUCH — HOURLY CANDLE OVERLAPS WINDOW EDGE |
| AudaCity / DACITYCOIN/TTWO | solana | `7Te1wXcfeQjKLGHXFvxSCnGZTqunoZ9fxBn7KKjrkNEr` | 2026-11-12T19:02:58Z | +23.34% | -5.02% | initial: 0.0048056317 | OPEN — TARGET / PROTECTIVE STOP NOT YET OBSERVED |
| chinese hat cat / CHAT/SOL | solana | `4p4UaFSzVfFaM8SyvqH1WzkG9GC89YoUuSL5pn6nY7FX` | 2026-11-12T22:50:51Z | unavailable | unavailable | initial: 0.0000195607 | OPEN — INITIAL REFERENCE |
| Libra / Libra / WBNB | bsc | `0x5166508edf57c5218b0755acefd0fe25f253c084` | 2026-11-10T11:15:55Z | +0.00% | +0.00% | initial: 0.0067985115 | INITIAL PROTECTIVE STOP HIT — POSITION EXITED |
| 中国人牛逼 / 中国人牛逼/SPCXB | bsc | `0xf6d7e50c8134f277c7daed5dba10f4918497c6be` | 2026-11-10T13:07:30Z | +0.00% | -72.67% | unavailable | 24H WINDOW CLOSED — INCOMPLETE CANDLE COVERAGE |
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
| CAP / CAPUSDT | bybit | `—` | 2026-11-11T14:46:34Z | +0.00% | +0.00% | initial: 0.114104 | EXIT AMBIGUOUS — TARGET AND STOP WITHIN SAME HOURLY CANDLE |
| MEMEOS / MEMEOS/SOL | solana | `Cxq6T1FqqwpwbnvKGP492qRUzvvLzxqAfKQupTMQd3L3` | 2026-11-11T18:22:32Z | +0.00% | -12.50% | initial: 0.0002447513 | INITIAL PROTECTIVE STOP HIT — POSITION EXITED |
| apeonx / X/SOL | solana | `CPWA5ufjXEyC7DTd7g5fVUxJYZ81kxvbC8f9R9XFx1Y8` | 2026-11-12T10:59:14Z | +0.00% | -11.70% | initial: 0.0003084037 | INITIAL PROTECTIVE STOP HIT — POSITION EXITED |
| ni hao cat / 哈基仙人/FXION | bsc | `0x599a0a927a2ad11d9ef07f2c155ba9a4de582003` | 2026-11-12T15:54:09Z | +37.12% | -3.50% | trail: 0.0000503835 | TRAILING STOP HIT — RUNNER EXITED |

> A TP touch is a milestone; the trailing runner remains open until its protective trail or the 24h time stop. Initial risk is ATR-based where candle coverage allows it. Hourly candle extrema are observed reference prices, not actual fills. A same-hour target/stop collision is marked ambiguous.