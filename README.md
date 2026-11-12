# Crypto Pre-Breakout — v4.28.0 strict qualification and bounded local scan

**9-Engine Imminent Breakout & 24h Probabilistic Returns:** v4.28.0  
**AURA Machine-Learning Feature-Attribution Upgrade:** v4.28.0  
**Unified multi-provider discovery & 180m budget manager:** v4.27.0  
**DexPaprika all-network early-breakout discovery / 24h MFE monitor:** v4.25.0  
**Birdeye free multichain rotation:** v4.24.0  
**Multi-provider discovery / coverage ledger:** v4.23.0  
**Paginated discovery / durable all-market ledger:** v4.22.0  
**Forward watchlist / memory manifest overlay:** v4.20.0  
**Collector / full-universe overlay:** v4.19.0  
**Underlying archived collector core:** v4.15.3  
**Forward-age qualification guard:** v4.18.1  
**Mode:** read-only market research; no wallet signing, order submission or trade execution  
**External data budget:** $0 — public endpoints only
**Actionable shortlist:** final candidates must pass the canonical single-TP +40.00% gate and independent identity, price, safety, and buy/sell quote checks. Discovery leads remain separate; an empty qualified list is reported as zero.
**Scan duration:** local manual execution is the primary path; each full local run has a 170-minute default deadline and a hard maximum of 180 minutes. Runs use isolated output folders and merge learning memory safely. GitHub Actions is optional.

### v4.28.0 — Phase 2 & Phase 3: 9-Engine Imminent Breakout Model, 24h Probabilistic Returns & AURA ML Feature Attribution

- **9-Engine Imminent Breakout Acceleration Model** (`scripts/breakout_signal_engines.py`):
  Evaluates behavioral acceleration and rate-of-change anomalies rather than raw historical gains across 9 independent signal engines:
  1. *Momentum Acceleration*: detects price velocity acceleration before major breakouts.
  2. *Volume Intelligence*: evaluates 5m-to-1h turnover burst ratios and organic volume expansion.
  3. *Buy/Sell Pressure*: tracks taker buy volume dominance and positive CVD proxy.
  4. *Liquidity Intelligence*: measures exit liquidity depth cushions and liquidity-to-FDV ratios.
  5. *Accumulation Detection*: identifies compression coils, higher lows, and supply absorption.
  6. *Smart-Money Research*: analyzes transaction volume clusters and community takeover dynamics.
  7. *Breakout Prediction*: measures proximity to resistance and volatility expansion.
  8. *Ecosystem Flow*: integrates DefiLlama chain flow multipliers and multi-provider corroboration.
  9. *Risk Intelligence*: applies hard penalties for thin liquidity rugs, wash trading, and late exhaustion.
- **24-Hour Probabilistic Expected Return Engine** (`scripts/expected_return_engine.py`):
  - Strictly enforces the mandatory technical TP minimum (+40.00%).
  - Estimates $P(\text{TP}_{40\%} \text{ within 24h})$ probability using logit-scaled confluence and liquidity factors.
  - Computes Expected Net 24-Hour Return ($E[R_{24h}]$) factoring win probability, downside drawdown, and DEX execution slippage drag.
  - Projects Maximum Favorable Excursion (MFE) and Maximum Adverse Excursion (MAE).
  - Estimates hours to target and reversal/front-run risk.
  - Generates a realistic opportunity rank prioritizing assets with high win odds and positive net return over speculative low-probability setups.
- **AURA Genuine Machine-Learning Feature-Attribution Upgrade** (`scripts/user_trade_learning.py`):
  - Upgraded internal AI learning engine from simple win/loss counters to empirical feature-attribution learning.
  - Reconstructs/extracts 0-100 feature vectors for user-taken trades across all 9 breakout dimensions.
  - Computes empirical covariance and attribution between feature values and actual user-reported trade outcomes (Final P/L %).
  - Trains exclusively on user-confirmed taken positions (`trained_on_user_positions_only: True`), preserving historical forecasts without fabricating labels.
  - Retains permanent negative regression safeguards (FAIR/SOL near-miss front-run buffer, USOE/SOL liquidity cushion, CORLA/WBNB honeypot).
  - Preserves software version provenance for every position record (`software_version`).
  - Automatically updates active model version to `AURA-v4.28.0`.

### v4.27.0 — Phase 1: Unified Network Registry, Multi-Lane DEX Screener & DefiLlama Flow Intelligence

- **Unified Network Registry**: Normalizes cross-provider identities across Ethereum, Solana, BSC, Base, Arbitrum, Polygon, Avalanche, Optimism, Sui, Blast, Linea, Scroll, Fantom/Sonic, Mantle, zkSync, Aptos, Sei, Mode, PulseChain, TON, Tron, Cronos, Celo, and NEAR. Tracks per-provider query health, supported features, and pool counts.
- **Upgraded Multi-Lane DEX Screener Discovery**: Expands discovery across fresh token profiles (`/token-profiles/latest/v1`), momentum discovery (`/token-boosts/latest/v1`, `/token-boosts/top/v1`, `/community-takeovers/latest/v1`, `/ads/latest/v1`), and intelligent 30-token batch lookups (`/latest/dex/tokens/{addresses}`), increasing throughput by up to 30x while respecting API pacing.
- **DefiLlama Ecosystem Flow & Volume Allocation Engine**: Evaluates global 24h DEX volumes and blockchain TVL dynamics to compute ecosystem acceleration scores, automatically routing more discovery budget to expanding ecosystems.
- **Cross-Provider Evidence Corroboration**: Corroborates discovery evidence across DEX Screener, GeckoTerminal, DexPaprika, and DEXTools, marking multi-provider corroborated candidates with provenance tracking.
- **Unified 180-Minute Scanning Budget Manager**: Enforces phase budgets (Preflight 5m, Discovery 100m, Enrichment 20m, Verification 30m, Finalization 15m, Safety Reserve 10m) and dynamically rolls forward unused time to prevent run timeouts.

### v4.26.7 — fix user position journal refresh/deletion glitch & AURA learning state sync

- **Fix User Position Journal Input Wiping**: Eliminates aggressive client-side polling DOM destruction and removes the 30-second `<meta http-equiv="refresh">` tag in Section 3 "User Position Journal & AI Follow-Up (Real Trades) — AURA Learning Engine". Field edits (e.g., TP hit dropdown selection, Final P/L %, and follow-up notes) now preserve focus, reconcile non-destructively, auto-save smoothly on blur/change/debounce without deleting user typing, and provide instant visual save confirmations.
- **Clean Field Defaults**: Eliminates the synthetic `"Unknown"` placeholder from input values so fields display clean user values or helpful placeholders.
- **AURA Version Provenance**: Automatically tags newly recorded positions and the trained AURA model version with `v4.26.7` (`AURA-v4.26.7`) while preserving historical records with their original version provenance.

### v4.26.6 — strict technical TP qualification and separated result lanes

- A candidate is `QUALIFIED` only when its **observed technical single TP** is at least +40.00% above the fresh reference price and every exact-pool identity, safety/loss-pattern and fresh two-way quote gate passes. A hypothetical 24-hour upside scenario can never substitute for technical TP.
- TP resistance now uses only structurally valid, completed hourly candles for the same pool and tracked-token orientation. Candle coverage, duplicate/malformed bars, retrieval-time alignment, extreme upper wicks, target provenance, pool liquidity and volatility-distance limits are recorded. The maximum permitted target distance is the greater of +40% or three hourly ATRs; liquidity below the existing $100,000 research screen is not eligible.
- Final JSON and Markdown separate **verified actionable opportunities**, **NOT QUALIFIED unverified research watchlist**, and **NOT QUALIFIED young/unknown-age discovery-only leads**. `top_opportunities` contains qualified rows only; an empty qualified list stays empty.
- Late-chase scoring now penalizes excessive 5m/1h/6h/24h moves, abnormal short-window acceleration and strong run-ups at 24h highs.
- A later zero-scope/no-candidate audit pass preserves same-run CVD and order-book evidence instead of overwriting it with empty results; a fresh non-empty attempt still replaces the earlier snapshot.
- Existing learning-memory history, calibration and reference-position records are not reset, rewritten or replaced. Historical outcome updates remain mark-to-market research observations, not executed-trade results.

### v4.26.5 — local API-key prompting and Birdeye launcher support

- Adds an opt-in launcher prompt for supported API credentials. Secret inputs are hidden and supplied only to the launched scan processes; they are not written to disk or persisted in the Windows environment.
- Adds `-Birdeye` to the visible Windows launcher so its local runner can enable the already-supported rotating Birdeye discovery lane when `BIRDEYE_API_KEY` is supplied.
- The launcher prompt intentionally omits `BITQUERY_API_KEY` and `HELIUS_API_KEY`; this version does not contain integrations for those providers.

### v4.26.4 — fix isolated runtime package imports

- Adds the isolated runtime's project root to Python's import path when `run_full_scan.py` is launched as a script. This fixes `ModuleNotFoundError: No module named 'scripts'` during local scan startup.
- Adds a regression test that executes the bounded subprocess helper with a working directory outside the project root, matching the local runtime launch layout.

v4.19.0 is installed after the v4.15.3 runtime archives and the v4.18.1 research/forward-age guard. The v4.20.0 overlay is applied on top of that stack. The archived collector core remains v4.15.3; version fields are kept separate rather than relabelling the core.

### v4.26.0 — automatic chain-aware loss-pattern evidence

- Adds a read-only RugCheck/Solana RPC/Raydium evidence path and Blockscout/EVM RPC path before the DEX entry guard.
- Adds dynamic Blockscout PRO API support for the current 100+ EVM chain registry when BLOCKSCOUT_PRO_API_KEY is configured.
- Confirms exact token-to-pool identity, launch allocation evidence and LP token custody where supported.
- Distinguishes an LP share held at an unspendable address (**locked**) from tokens actually burned.
- Uses a conservative EVM first-inbound native-coin funder graph when complete Blockscout histories are available; token-transfer edges are not treated as common ownership.
- Unsupported concentrated-liquidity/LP models, truncated graph history and missing provider fields remain unverified.

### v4.25.1 correction — separate historical age from the forward holding window

- The v4.18 guard's 24-hour age check applies only to whether the **past/source-reported 24-hour return** is historically comparable. It must not prevent a newly launched pool from being ranked for a fresh 24-hour holding window.
- The final post-scan step now restores the separate **next-24h opportunity ranking** after the historical-age guard. Every identity-bearing DEX candidate in the forward cohort remains ranked; historical comparability, execution readiness, and the forward opportunity rank are shown as separate fields.
- v4.25.1 fixes the v4.19 compact-memory integration under both supported Python module names, so early-breakout score, signal components, quote-token symbol and pair orientation survive the all-market/discovery export.
- The renderer now displays the next-24h opportunity rank and score separately from historical 24-hour comparability. No model score is presented as a calibrated probability.
- Regression tests cover a three-hour-old pool remaining ranked for the next 24 hours, provider-signal preservation in compact rows, and choosing the strongest family-level breakout signal.


### v4.26.3 — bounded local execution, accurate pacing and live progress

- Enforces subprocess deadlines across the complete process tree, including child scanners, using Windows Job Objects and POSIX process groups. Timed-out local scans cannot leave scanner descendants running in the background.
- Corrects GeckoTerminal pacing estimates to count global pages, every per-network family/page, and network-registry requests.
- Uses a bounded local default of one GeckoTerminal page over 12 rotating networks (about 10.2 minutes of scheduled pacing, before endpoint latency); allows up to three pages and 24 networks while retaining the 170-minute default / 180-minute hard runtime cap.
- Opens a visible PowerShell monitor window for every launched run. It displays the elapsed timer, current source progress, latest activity and final scan status; an unexpectedly exited runner is marked INTERRUPTED and any partial actionable output is cleared.

### v4.26.2 — unrestricted local launch and concurrent run support

- Adds `scripts/run_local_scan.py` as the full local alternative to hosted Actions, including the existing learning, calibration, provider rotation, identity, safety, quote, TP and final report stages.
- Gives each local run its own output and memory snapshot, then merges historical discovery, forecast and outcome records into the persistent local memory under a cross-process lock.
- Makes the Birdeye CU and DexPaprika request reservations shared and locked across overlapping local scans so the scanner's internal free-tier ceilings remain effective.
- Uses an independent maximum runtime of 180 minutes per run and keeps incomplete or interrupted runs out of the completed/action-ready state.

### v4.26.1 — upside-first top-10 opportunity board

- Expands the source-backed forward DEX candidate cohort from 40 to 80 pools per full production scan.
- Merges pool identities across universe, short-window momentum, volume, new-pool and high-risk scout boards; the strongest early-breakout family signal is retained rather than being hidden by an earlier lower-scoring duplicate.
- Makes the next-24h ranking prioritize the early-breakout score (75%) over the pre-age-guard research score (25%), with explicit penalties for late-chase, short-term reversal and sell-side-flow risk. Scores remain relative heuristics, not return forecasts or probabilities.
- Publishes a concise top-10 Markdown board with opportunity tier, 5m/1h/6h/24h momentum, volume, liquidity, pool age, source-backed TP potential and separate execution/safety blockers. The full evaluated candidate set remains in JSON for audit and reference-position monitoring.
- High-upside research leads remain visible even when safety, LP, sellability or route checks are incomplete; those gates are labelled UNKNOWN/NOT VERIFIED rather than treated as PASS. A missing TP does not remove an early candidate from the opportunity ranking.

## What v4.25.0 adds — keyless all-network early-breakout discovery and 24h peak tracking

- **New independent discovery provider:** adds DexPaprika's keyless public REST API. No API key, account, payment method, paid host or plan upgrade is used.
- **Cross-network pool discovery:** queries the provider's global `/pools/search` endpoint without a `chains` filter. The provider's current agent documentation describes 36 supported blockchains and broad DEX indexing. The scanner requests the first 100 results from six separate ranked families: 5-minute momentum, 1-hour momentum, 6-hour momentum, 24-hour momentum, 24-hour pool volume and pools created within 24 hours. It records `has_next_page`; this version does not page beyond the first 100 results per family.
- **Short-horizon breakout score:** transparently scores 5m/1h/6h momentum, acceleration of the 1h trend versus the 6h pace, transaction activity, volume/liquidity turnover, liquidity depth, pool novelty, alignment across windows and 1h buy pressure when supplied. It is a relative heuristic, not a probability or expected-return forecast. Thin liquidity, very young pools, extreme turnover and late-chase behaviour are explicit risk flags, not silent exclusions.
- **Ranking policy:** merges the new provider's early-breakout score into the existing forward research ranking (75% early-breakout score / 25% existing research score in v4.26.1 when the new signal is available). The expanded candidate pool includes v4.19 all-market universe and identity-bearing discovery rows. Missing OHLCV or a not-yet-formed TP no longer removes a DEX pool from the research ranking; those gaps lower verification/evidence status. A separate best-immediate-entry field remains subject to the existing evidence and execution checks.
- **Corrected 24-hour outcome measurement:** the scanner keeps up to 12 concurrent research reference positions, opens at most one new reference per full scan, and monitors the reference for up to 24 hours. It records maximum favorable excursion (MFE), minimum price, latest return, retracement from peak, first observed TP-touch candle and final 24-hour status. A later pullback never erases an earlier target touch. A candle overlapping the exact entry or expiry boundary is marked possible/unconfirmed rather than falsely confirmed.
- **USD-safe OHLCV:** DexPaprika pool candles are quoted in the other token of the pair, not universally USD. The tracker only treats stablecoin-quoted pools as directly comparable with USD entry/TP levels and applies the provider's `inversed` orientation flag when the token order was normalized. Non-stablecoin pairs remain eligible for the upside ranking but are not misreported as dollar-denominated TP hits by this monitor.
- **Free-tier protection:** uses the keyless API at a 4.2-second request interval (below the documented 15 requests/minute keyless limit), stores a rolling 30-day request ledger, hard-stops at 7,000 scanner request reservations, and also honors the provider's `x-credits-remaining` response header by stopping when the observed remaining quota reaches 3,000. The local request count is an estimate, not a guarantee of all account/IP usage; the provider's header is authoritative when present. Requests from unrelated users on a shared runner IP can still affect keyless capacity.
- **Persisted monitoring:** `dexpaprika-request-ledger.json`, cached `provider-registries/dexpaprika-networks.json`, `active-24h-watchlist.json` and `completed-24h-watchlist.json` are stored in persistent scanner memory.
- **Honest coverage boundary:** these six first-page rankings are a new high-value sample across the provider's supported network catalog, not a census of every Web3 asset, DEX, pool, chain or token. A pool still requires a usable identity and quote orientation for certain monitoring tasks.

## What v4.24.0 adds — Birdeye Standard (Free) multichain discovery

- **Dynamic network registry:** fetches `GET /defi/networks` and caches the supported-chain registry for seven days. The registry endpoint costs 1 CU and is refreshed only when the cache expires or is unavailable.
- **Three free discovery families:** `GET /defi/token_trending` (40 CU, up to 50 rows), `GET /defi/v2/tokens/new_listing` (30 CU, up to 20 rows; not supported on Sui), and `GET /defi/tokenlist` (30 CU, up to 50 rows). Each request uses its provider-listed `x-chain` identifier.
- **Rotating breadth, not a false census:** exactly one Birdeye discovery-family request is made per full scan. The persisted rotation cycles through supported network × family slots (excluding Sui/new-listing); it does not imply that Birdeye's trending/listing endpoints enumerate every token or pool.
- **Free-tier cost guard:** Standard is currently $0 with 30,000 CUs/month and a 1 request/second limit. The scanner reserves the estimated cost before each attempted API request and hard-stops at 20,000 estimated CUs/month to leave a 10,000-CU buffer. At 12 full scans/day for 30 days, even if all 360 calls were the highest-cost 40-CU trending endpoint, planned discovery is 14,400 CUs plus the infrequent 1-CU registry refresh. A shared key used by other applications can consume the remaining account budget outside this scanner's local ledger.
- **No overage path:** the integration never selects or upgrades to a paid plan. Configure the Birdeye account on Standard (Free) only, keep any max-usage/auto-renew setting disabled, and use a dedicated key for this scanner where possible. If the scanner's 20,000-CU ceiling is reached, its Birdeye discovery requests are skipped and reported rather than upgraded.
- **Persisted audit:** `birdeye-rotation-state.json`, `birdeye-cu-ledger.json`, the cached `provider-registries/birdeye-networks.json`, per-source request rows and the provider-network coverage ledger are stored in persistent scanner memory.

### Optional zero-cost Birdeye setup

Create a Birdeye Data account on **Standard (Free)** and set `BIRDEYE_API_KEY` in the environment before starting Rocket Hunter. For the current PowerShell session: `$env:BIRDEYE_API_KEY = "your-key"`. Keep the key out of source files, reports, and commits; keep paid upgrades and auto-renew disabled. The scanner's internal cap cannot see usage from other applications sharing the key.

Without this secret, the scanner logs Birdeye as `SKIPPED`; DefiLlama, DEX Screener, GeckoTerminal, DEXTools (if separately configured), and the existing public aggregators still run.

## What v4.23.0 adds — free multi-provider coverage

- **DefiLlama public API (no key):** reads `/v2/chains` and `/overview/dexs` to record the chain TVL registry, DEX-volume chain inventory and DEX protocol counts. These are chain/protocol context and volume-prioritization data, not individual-pool feeds.
- **DEX Screener public API:** retains the existing latest token-profile, community-takeover, boost, broad search and token-pair discovery routes. The public API reference does not document a complete all-chain registry endpoint, so this scanner records only chain IDs actually observed in returned DEX Screener data; it never represents that sample as complete.
- **DEXTools optional free API:** when `DEXTOOLS_API_KEY` is configured, fetches the blockchain registry (persisted with a seven-day cache) and requests one rotating chain's hot-pair feed per full scan. Both token sides of each discovered pool are retained as discovery-only rows when the hot-pair endpoint does not supply a price or 24-hour return. The integration is capped at one hot-pairs request per full scan and never upgrades a plan.
- **GeckoTerminal:** continues the rotating registry sweep, with three pages per global list and per-network trending/new-pool/top-volume families by default.
- **Provider-specific coverage ledger:** persists each provider's own network identifiers and cumulative observed coverage in `provider-network-state.json`. Provider identifiers are kept separate because names such as `ether`, `Ethereum`, `eth` and `ethereum` cannot safely be assumed to be identical without an explicit mapping.
- **Honest completeness boundaries:** a provider chain registry is not a pool census. DefiLlama DEX volume summaries are not pair records. DEX Screener's observed chain IDs are not its full platform network list. The combined sources broaden discovery but do not establish literal coverage of every Web3 token, pool, NFT or chain.

### Optional zero-cost DEXTools setup

Create a DEXTools API account on its Free tier, then set `DEXTOOLS_API_KEY` in the environment before starting Rocket Hunter. For the current PowerShell session: `$env:DEXTOOLS_API_KEY = "your-key"`. Keep the key out of chat, source files, reports, and commits. Do not select a paid plan.

Without this secret, DEXTools is reported as `SKIPPED`; DefiLlama, DEX Screener, GeckoTerminal and the existing public aggregators continue working.

## What v4.20.0 changes

- **Always-on DEX forward watchlist.** Forward discovery includes up to 40 exact DEX pool markets across the execution, new-launch, new-pool, short-horizon mover and full-universe lanes. Distinct pools for the same token remain distinct candidates. A missing target no longer removes a candidate from the watchlist.
- **Correct age semantics.** The 24-hour pool-age guard applies only to historical 24-hour return comparison. Young or unknown-age pools remain eligible for forward 24-hour breakout monitoring; their source-reported since-creation move is never mislabeled as a completed 24-hour return.
- **Shorter-history TP research.** A single TP may be derived from at least 8 valid hourly candles using observed swing resistance, recent range high or an explicitly labelled ATR/range projection. When history is insufficient, the candidate remains visible with `TP forming`; no target, price or address is invented.
- **Expanded-universe second pass.** After the v4.19.0 collector exports the detailed universe, the workflow rebuilds the forward watchlist from those rows, then reapplies the age guard and calibration. This prevents the early forward stage from seeing only a small report-board sample.
- **Compact report presentation.** The leading summary uses pair-first lines such as `Reboot/ETH TP: +16.28%` and `HIGH/SOL TP: +98.68%` (example format), with reference price, target, pair address and verification details below. TP upside is calculated from the current source-backed spot reference to the selected single TP; it is not a promise of realized PnL.
- **Version-aware shared memory.** Full-scan and fast-refresh pointers are tracked separately as `last_full_scan` and `last_fast_refresh`. A fast refresh cannot overwrite the latest full-scan pointer, and the next full scan refreshes that pointer even when an older one already exists.

## What v4.19.0 changes

- **Rotating GeckoTerminal discovery.** Twelve preferred anchor networks remain in the selection when present in the live registry. The remaining slots rotate through the discovered non-testnet network registry. Each full scan requests 32 networks and paginates three pages for each selected network's trending, new-pool and top-volume pool families by default (configurable from 1 to 10). Registry discovery reads up to 12 pages and stops on an empty page; reports distinguish a confirmed exhausted registry from a page-cap or request failure. Network coverage is reported as unknown when the registry size could not be established.
- **Expanded collector export.** The collector hook exports detailed candidate rows before the top-100 report-board truncation. The memory stage snapshots eligible priced rows with a source-reported 24-hour return, then reconciles duplicate market keys. This improves coverage beyond the report lanes, but it is not a census of every asset, pool, chain or market. Rows without a usable price or 24-hour source return are not included in this new forecast snapshot lane.
- **Conservative pool-age reconciliation.** Duplicate DEX market rows use the youngest observed non-negative age. A pool under 24 hours old, or with unknown age, cannot be presented as a comparable 24-hour return; its source-reported move is preserved separately for audit.
- **Evidence-gated calibration.** The model uses resolved 24-hour mark-to-market outcomes only when the observed mark is at or after the 24-hour horizon and no more than six hours late, with valid forecast-price provenance and timestamps not later than the model's as-of time. A row receives a calibrated probability only after at least 25 eligible historical outcomes overall and at least 10 in its score bucket. Otherwise the prior estimate may be shown for research, but `score_is_probability` remains false and `calibrated_prob_24h` remains null. The reported 90% interval uses a normal approximation to the Beta posterior; it is not an exact Beta quantile. These outcomes are market marks, not executed-trade results.
- **Auditable memory.** Persistent local scanner memory stores forecast and outcome partitions, the manifest, a network-rotation ledger and calibration state. Forecast returns are marked-to-market observations, not executable fills.

## Local scanning

Start one complete local scan from PowerShell:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_parallel_scans.ps1 -Count 1
```

The launcher opens a visible monitor window showing the elapsed timer, current source activity and final status. `-ExecutionPolicy Bypass` applies only to this PowerShell process; it does not change the computer's saved policy. Direct CLI use is also available when Python 3 is on your PATH, but does not open the live monitor window: `python scripts/run_local_scan.py --outdir rocket-runs/run-1`.

The local runner uses at most 4 scanner request threads and 4 parallel preflight probes by default, independently of hosted workflow settings. Tune either from 1 to 12 with `--workers` and `--preflight-workers`; these limits are passed to the existing scanner rather than creating a separate scan implementation. Ctrl+C requests a graceful stop of the active subprocess tree; any child that does not exit within five seconds is force-stopped, and the run is recorded as interrupted.

Read local machine and storage details without starting a scan or contacting market-data providers:

```powershell
python scripts/local_diagnostics.py
```

The read-only command reports CPU, available RAM, Python version, project/data **volume type** (for example, Fixed or Network) and free space, and NVIDIA GPU details when available. Windows volume type does not identify physical media such as SSD, HDD or NVMe; physical-media type remains unreported. Unsupported or unavailable hardware details are labeled; the command does not print environment variables or credentials. Local `scan-status.json` and reports also record the scanner version and a configuration fingerprint for the effective local scan settings.

To enter supported credentials without echoing or saving them, add `-PromptForApiKeys`. The launcher prompts for Birdeye, Blockscout PRO, CoinGecko Demo, GoPlus, Jupiter, Solana RPC and 0x values; blank input leaves that setting unchanged. It also optionally prompts for the non-secret Blockscout chain-ID map and 0x taker address. Add `-Birdeye` to enable Birdeye discovery for that run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_parallel_scans.ps1 -Count 1 -PromptForApiKeys -Birdeye
```

These process-only values are inherited by the monitor and scanner for that launch and are not persisted for later PowerShell sessions. Do not start a second scan just to add keys while a scan is already running; an existing process cannot receive environment changes made afterward. `BITQUERY_API_KEY` and `HELIUS_API_KEY` are not used by this release.

Run another scan at the same time from a second terminal with a different output directory:

```powershell
python scripts/run_local_scan.py --outdir rocket-runs/run-2
```

To start several local scans at once from PowerShell, use the launcher. It gives every run a separate output directory and returns the process IDs:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_parallel_scans.ps1 -Count 3
```

Each scan has its own report, checkpoints, log, and memory snapshot; every scan opens its own live monitor window. Successful runs merge historical discovery, forecast, and outcome records into `rocket-hunter-data/scanner-memory` under a cross-process lock. The supplied historical memory is bundled at that default path, so the first local run does not need a Git remote or GitHub Actions. If you remove the memory directory, the runner can restore it only from an already available local `origin/scanner-memory` Git ref; it refuses to start silently with empty history if neither source exists. Shared Birdeye and DexPaprika request reservations also use locks and their existing internal free-tier ceilings. Concurrent runs can overlap in discovery; provider-side rate limits and your computer's capacity still apply. No GitHub account, Actions quota, or internet GitHub access is needed to run.

The runner records the selected local concurrency and discovery limits in `scan-status.json`, and reserves about 28% of its overall time budget for qualification, learning updates, report rendering, and memory merge. The default is 170 minutes; set `--max-runtime-minutes` up to 180. Failed or interrupted runs remain marked as such and clear action-ready fields. Check `scan-status.json`, `forward-24h-rocket.md`, `forward-24h-rocket.json`, and `local-run.log` inside that run's output folder.

## Read-only historical research export

The compressed JSONL forecast and outcome ledgers remain authoritative. Create a separate, content-addressed CSV snapshot with the standard-library default:

```powershell
python scripts/export_research_data.py
```

Parquet is optional and requires the isolated PyArrow dependency:

```powershell
python -m pip install -r requirements-export.txt
python scripts/export_research_data.py --format parquet
```

Both formats read forecast and outcome ledgers without changing scanner memory or scan behavior. Exports are written under `rocket-hunter-data/research-exports`, separated from scanner memory and local run outputs. `--memory-dir`, `--export-root`, `--outdir`, and `--run-root` control the read-only inputs and new destination. Source partitions are checksummed before export and checked for changes before publication; an identical complete snapshot can be reused, while an existing unrelated directory is never overwritten.

The versioned schema in `schemas/research-export-v1.json` describes normalized rows. Export files are partitioned by record type and source date; the manifest records source and exported-file hashes, counts, and provenance. Complete source records and forecast/outcome identity remain available, missing values stay null, and malformed or duplicate rows are reported in `validation-rejections.jsonl` and `validation-summary.json`. CSV is gzip-compressed; Parquet uses Zstandard compression and bounded batches. Neither format turns observed marks into executable fills or creates performance estimates. Run the focused exporter tests with `python -m unittest discover -s tests -p "test_export_research_data.py"`.

## Read-only outcome analysis

Phase 3 has a standard-library-only descriptive analysis of the authoritative forecast and outcome ledgers:

```powershell
python scripts/analyze_research_outcomes.py
```

It writes a content-addressed report under `rocket-hunter-data/research-analysis/`, separate from scanner memory, Parquet/CSV exports, and active scan outputs. The report includes forecast detection counts, per-horizon outcome coverage, observed-mark threshold attainment for +40%, +60%, and +100%, and negative endpoint marks where the data supports them. Source partition hashes, schema/version provenance, rejected records, and coverage details are in `report.json`; `report.md` summarizes the interpretation. Identical source bytes reuse and checksum-verify the same report. `--memory-dir` and `--output-root` select read-only inputs and a separate output location.

Features come only from the forecast record captured at detection; later outcome records are labels. Only sampled marks observed from the scheduled horizon through six hours afterward are included in threshold summaries. These are endpoint mark attainments, not complete intrahorizon target-touch rates: without price paths, non-attainment and time-to-target cannot be established. Negative endpoint marks are not maximum adverse excursion. The records contain no fills or execution quotes, so fees, slippage, and executable returns are unsupported and never inferred. This analysis does not measure strategy effectiveness or change production rankings. Run its focused tests with `python -m unittest discover -s tests -p "test_analyze_research_outcomes.py"`.

## Foreground watchlist observation collection

To explicitly start a bounded local session using the existing active watchlist and the scanner's read-only GeckoTerminal/CEX hourly OHLCV adapters:

```powershell
python scripts/collect_watchlist_observations.py --duration-minutes 60 --interval-seconds 300
```

The collector runs in the foreground, handles Ctrl+C, and writes append-only, versioned JSONL observations and a session manifest under `rocket-hunter-data/research-data/watchlist-observations/`. It reads `scanner-memory/active-24h-watchlist.json` but never modifies the watchlist, scanner memory, forecasts/outcomes, exports, rankings, or scan outputs. The default limits are 12 candidates, 120 provider requests, a 60-minute duration, and a 5-minute cycle interval. Hard bounds are 1-12 candidates, 1-500 requests, 1-1440 minutes, and 60-3600 seconds. Existing provider adapters return completed hourly bars; requests are paced at least as slowly as the watcher's 6.2-second GeckoTerminal interval. A global collector lock allows only one writer/session at a time. `--once` performs at most one pass; `--dry-run` validates the watchlist and prints selected identities without network access or output writes.

The collection records source event times from returned bars, local request-received and ingestion times, source URL/adapter, explicit identity components (missing values remain null), watchlist hash, scanner version, source run ID where present, and collector run/configuration provenance. On GeckoTerminal HTTP 418/429, the collector remains stop-on-rate-limit: it makes no retry and skips remaining complete candidates. The adapter retains only the provider, HTTP status, parsed Retry-After duration/expiry (capped to seven days), and whether that cooldown is usable; it never stores response headers. A usable expiry is persisted under the separate research-data root, and subsequent explicit runs skip requests until it expires. Without a valid Retry-After value, the throttle cause and cooldown remain unknown; no cooldown is invented. The adapters normalize OHLCV and do not retain raw provider bodies or response headers. If hourly bars have complete coverage, they can support candle-level target presence, hourly-bar-resolution adverse excursion, and close-mark paths. Exact intrahour crossing time/order, claims across missing candles, instantaneous prices at poll time, and executable returns (fills, fees, slippage) remain unsupported. The collector does not calculate strategy results or claim effectiveness.

A 24-hour collection session does not guarantee a complete analyzable 24-hour outcome window: the analyzer excludes the detection-overlapping candle, and missing/gapped or censored bars also block complete coverage. After collection, run `python scripts/research_readiness.py` and extend collection if its complete-window count is still zero; do not infer misses from missing coverage.

Focused tests mock all provider access: `python -m unittest discover -s tests -p "test_collect_watchlist_observations.py"`.

## Hourly candle outcome analysis

Analyze only the already-collected observation files; this command makes no network requests:

```powershell
python scripts/analyze_candle_outcomes.py
```

The analyzer writes a new content-addressed `candle-outcomes-v3-*` directory under `rocket-hunter-data/research-analysis/`, with `report.json`, `report.md`, and a provenance/checksum manifest. It does not overwrite the original Phase 3 report or modify forecasts, observations, exports, scan outputs, or production rankings. It joins only exact normalized chain + token + pool identities, rejects conflicting quote/orientation fields, and uses only the collector's exact-token USD GeckoTerminal hourly source with a fresh recorded USD forecast price. Detection-overlapping and horizon-overlapping bars are excluded; malformed/incomplete bars, missing hours, duplicate/conflicting bars, censored windows, and unjoinable records are reported. Repeated polls of the same bar are deduplicated.

Only complete, fully elapsed 24-hour candle windows enter the +40%, +60%, and +100% presence denominators. A target result is candle-level presence, not exact intrahour timing; a candle crossing a target and a below-baseline low has unknown internal ordering. Adverse movement is measured from hourly lows against the frozen detection mark, not as executable return. The active-watchlist sample is not full-universe performance; fills, fees, slippage, exact crossing times, and unsupported identity/price fields are never inferred. If no safely joinable history exists, the report describes coverage and joinability instead. Focused fixture-only tests use `python -m unittest discover -s tests -p "test_analyze_candle_outcomes.py"`; they make no network calls.
Only complete, fully elapsed 24-hour candle windows enter the +40%, +60%, and +100% presence denominators. A target result is candle-level presence, not exact intrahour timing; a candle crossing a target and a below-baseline low has unknown internal ordering. Adverse movement is measured from hourly lows against the frozen detection mark, not as executable return. The active-watchlist sample is not full-universe performance; fills, fees, slippage, exact crossing times, and unsupported identity/price fields are never inferred. If no safely joinable history exists, the report describes coverage and joinability instead. Focused fixture-only tests use `python -m unittest discover -s tests -p "test_analyze_candle_outcomes.py"`; they make no network calls.
## Read-only research readiness

Summarize existing collection manifests, usable hourly bars, cooldown records, and analysis artifacts without contacting providers or changing scanner memory:

```powershell
python scripts/research_readiness.py
```

The report distinguishes rate limits and missing coverage from target outcomes. It counts only valid completed hourly bars in `OBSERVED` records and reports complete 24-hour denominators from the latest candle analysis. It never certifies strategy effectiveness or platform readiness; results remain limited by the source artifacts and their stated coverage. Use `--collection-root` and `--analysis-root` to inspect fixture or alternate artifact directories. Run its fixture-only tests with `python -m unittest discover -s tests -p "test_research_readiness.py"`.

## Phase 4 shadow signal analysis

The local, read-only exploratory analysis compares the detection-time DEX Screener 1-hour change field with later scheduled 24-hour endpoint marks:

```powershell
python scripts/analyze_phase4_shadow.py
```

It requires exact chain + token + pool identity, source URL and fresh feature timestamp, and exact forecast-ID/market-key/time outcome linkage. It uses a fixed UTC time split, derives the signal cutpoint from training features only, and writes versioned reports under `rocket-hunter-data/research-analysis/phase4-shadow-v1-*`. Missing/unavailable outcomes are coverage gaps, not negative outcomes. Its results are exploratory endpoint-mark associations only—not target-touch rates, backtest performance, profitability, or evidence of effectiveness. It does not collect data, change scanner memory, or affect production rankings or gates. Focused fixture-only tests use `python -m unittest discover -s tests -p "test_analyze_phase4_shadow.py"`.

The [Phase 4 signal inventory](LOCAL_INTELLIGENCE_PHASE4_INVENTORY.md) documents inputs, provenance, identity requirements, and production wiring for discovery, momentum, on-chain, security, liquidity, and execution. Forecasts now prospectively capture the existing v4.25 composite and component values in the additive `feature_snapshot.v425_early_breakout` object, tagged `v4.25-early-breakout.v1`; unavailable or malformed values remain null. Existing run ID and any available scanner/config fingerprint provenance are retained. Historical snapshots are not backfilled, and future captured forecasts plus matched outcomes are still needed before the composite can be evaluated. This change does not alter rankings, gates, outcomes, or the separate live hourly-observation coverage blocker.

## Optional GitHub workflows

| Workflow | Schedule / trigger | Purpose |
|---|---|---|
| `.github/workflows/full-rocket-hunt.yml` | Manual dispatch or push; PRs run validation only | Optional hosted route for rotating DEX-first scan, persistent memory and final identity/price/safety/quote/TP gates |
| `.github/workflows/fast-refresh.yml` | Manual dispatch only | Optional lightweight sampled public-feed refresh; not required for local full scans |
| `.github/workflows/test-forward-contract.yml` | PRs affecting runtime/source/docs; manual dispatch | Read-only regression and CLI validation; it must not push commits back to the feature branch |

GitHub Actions are optional. Their account spending limits do not apply to the local runner described above. Runtime still depends on provider latency, public API limits and source availability. Failed or partial sources must remain visible in the artifact; a successful validation job does not prove that the live market scan completed.

## Scan artifact files

Each local run writes into its own output directory (for example, `rocket-runs/run-1`) and contains separate Markdown reports:

- `rocket-hunt.md`: the broad collector/discovery report.
- `forward-24h-rocket.md`: the final forward-watchlist report, rendered from `forward-24h-rocket.json` **after** the age guard and v4.19.0 calibration stages have finished.
- `verified_actionable_opportunities`: separate final shortlist, populated only by the final +40% and independent evidence gate; `ZERO_VERIFIED_OPPORTUNITIES` is valid when no candidate passes.
- `scan-status.json`: start, finish, duration and terminal state. Interrupted or failed runs clear action-ready fields and cannot be mistaken for successful current results.
- `forward-24h-rocket.json`: structured candidate, target, provenance and quality-gate details.
- `v418-quality-audit.json`: age-guard, data-coverage, calibration and learning audit.
- `provider-network-state.json` in persistent local memory: provider-scoped chain identifier coverage and cumulative discovery history.
- `birdeye_discovery_v424` fields in the JSON report: selected chain/family, CU reservation, registry state and rotation position.

Use `forward-24h-rocket.md` for the forward shortlist and open its corresponding JSON/audit when checking a candidate. The Markdown and JSON are generated from the same finalized forward JSON, so the readable report carries the final `NOT_SHORTLISTED` / execution-readiness state rather than an earlier pre-audit view.

## Learning and probability boundaries

- `discovery/YYYY-MM-DD.jsonl.gz`: every identity-bearing market discovered, including records missing price or comparable 24-hour history; these remain discovery-only until evidence is available.
- `forecasts/YYYY-MM-DD.jsonl.gz`: source-visible features captured at snapshot time, limited to forecast-eligible rows.
- `outcomes/YYYY-MM-DD.jsonl.gz`: later same-market price marks at supported horizons with observation timestamps.
- `memory-manifest.json`: run/version metadata and record counts.
- `network-rotation-state.json`: cumulative GeckoTerminal networks seen and coverage only when the registry size is known.
- `provider-registries/dextools-blockchains.json`: cached DEXTools free-tier blockchain registry when the optional key is configured.
- `provider-network-state.json`: cumulative provider-specific network identifiers, observed chain counts and registry limitations.
- `birdeye-rotation-state.json` / `birdeye-cu-ledger.json`: per-run chain/family rotation position and scanner-estimated Standard-tier CU usage.
- `dexpaprika-request-ledger.json`: rolling keyless request reservations and last observed provider quota header.
- `active-24h-watchlist.json` / `completed-24h-watchlist.json`: research-only 24-hour peak/TP-touch monitoring history.
- `provider-registries/birdeye-networks.json`: seven-day cached supported-chain registry.
- `calibration-state.json`: eligible historical outcome counts, prequential Brier/log-loss diagnostics and reliability bins.

Scores are not executable-edge estimates. Mark-to-market outcomes do not model queue priority, fees, slippage, adverse selection or achievable fills. A sample-size threshold is a guardrail, not proof of calibration. Before using calibrated values operationally, evaluate performance on a time-separated holdout, inspect calibration by market type/chain and compare against a baseline. The current calibrator does not train or promote model weights.

The local runner retains the existing forecast, outcome, discovery, calibration and 24-hour reference-position ledgers under `rocket-hunter-data/scanner-memory`. Forecast marks remain distinct from executed trade fills; this repository does not store a verified personal trade ledger for the historical examples alone. Learning scores can reorder research candidates but cannot change the deterministic +40.00% or safety/execution gates. Scheduled scanning is disabled; run a full scan manually when needed.

## Local verification and optional release validation

1. Run the deterministic test suite, compile checks and CLI smoke checks before a live scan. The unittest suite installs a network deny guard in the test process and Python subprocesses; provider tests must use mocks or fixtures.
2. Start a local full scan from a new `rocket-runs/<run-id>` directory and inspect `scan-status.json`, the final forward JSON/Markdown, audit reports and `local-run.log`.
3. Confirm the report uses the final +40% threshold and exact identity, current-price, safety and two-way quote checks. An empty actionable list is a valid result.
4. Confirm local memory manifests retain the full/fast scan pointers separately and that forecast/outcome partitions are merged without duplicates.
5. Check timing, provider coverage and missing-evidence counts. Repeated runs rotate through the discovered network registry; that does not prove every market or pool was discovered.

GitHub Actions can validate or publish artifacts if you choose to use it. It is not required to run or persist local scans. A green offline test suite does not verify live provider availability; the first live local run remains the production check.

## Local validation

After checking out the feature branch:

```bash
tar -xzf source/rocket-hunt-core-v4.15.2.tar.gz -C .
tar -xzf source/rocket-hunt-v4.15.3-overlay.tar.gz -C .
gzip -dc source/patch_forward_report.py.gz > source/patch_forward_report.py
python source/patch_forward_report.py
mkdir -p tests
gzip -dc source/test_forward_24h_required_fields.py.gz > tests/test_forward_24h_required_fields.py
tar -xzf source/v418-research-overlay.tar.gz -C .
# Apply the documented v4.18.1 pipeline version and extract its forward-age guard/tests.
gzip -dc source/v4181-forward-age-guard.py.gz > scripts/v418_forward_guard.py
gzip -dc source/test_v4181-forward-age-guard.py.gz > tests/test_v418_forward_guard.py
python -m scripts.v419_upgrade --install --repo-root .
python -m scripts.v419_upgrade --self-test
python -m scripts.v420_upgrade --install --repo-root .
python -m scripts.v420_upgrade --self-test
python -m unittest discover -s tests -p 'test_*.py' -v
python -m compileall -q scripts tests
```


Optional EVM-wide coverage: set the GitHub Actions secret BLOCKSCOUT_PRO_API_KEY to a Blockscout PRO key (the provider currently advertises a free tier). The scanner discovers supported chain IDs at runtime. BLOCKSCOUT_CHAIN_IDS_JSON can optionally map scanner-specific chain slugs to supported numeric IDs. No key is supplied or stored in this repository.
