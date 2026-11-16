# Rocket Hunter Local Intelligence Architecture

## Mission

Use the development PC as Rocket Hunter's research laboratory while keeping GitHub Actions as an independent production path. Local and hosted scans must continue to use the same scanner code and verification gates. Local data collection is read-only market research: it must not sign transactions or submit orders.

## What already exists

- `scripts/run_local_scan.py` runs the full scanner in an isolated, versioned runtime and gives each run its own output and memory snapshot.
- `rocket-hunter-data/scanner-memory/` holds persistent forecasts, outcomes, provider state, and calibration. The local runner merges memory under a cross-process lock.
- `rocket-runs/` stores per-run output and checkpoints. `scripts/watch_scan.ps1` shows live status, while `scripts/process_runner.py` bounds the process tree.
- `.github/workflows/` provides the separate hosted scan and regression-test paths.
- Provider request reservations and rotation state are persisted locally to maintain configured free-tier request ceilings.

Do not replace or reset these systems as part of local-platform work. In particular, scanner memory is user data and is not disposable build output.

## Machine profile and operating constraints

The target machine is an Intel Core i5-12400F (6 cores / 12 threads), 16 GB RAM, RTX 3050 (8 GB VRAM), and two SSDs, based on the user's Windows Task Manager details:

- `C:` — SanDisk PC_SN5000S, 512 GB marketed capacity (477 GB shown), system and page-file drive.
- `D:` — WD Green 2.5, 480 GB marketed capacity (447 GB shown), non-system drive without a page file.

The reported initialized capacities total 924 GB. Local diagnostics can report the Windows volume type (for example, Fixed or Network), but does not identify the physical medium or model; these SSD details are user-provided hardware information. Keep the CPU and GPU; measure real bottlenecks before recommending upgrades. A RAM upgrade to 32 GB is the first likely capacity improvement, but is not a prerequisite for the initial phases. Consider `D:` for research datasets if its free space is sufficient; keep Windows, page-file, and application needs on `C:` and measure before relocating existing data.

Start with the existing Windows + Python workflow. WSL2 can be evaluated later for Linux parity, but do not make it a dependency of the local scanner. Treat any memory budgets as operating targets, not fixed reservations. Keep enough capacity for Windows and the user's development tools, and avoid starting several heavyweight scans or research jobs concurrently on 16 GB.

## Architecture and ownership

| Concern | Local PC | GitHub |
| --- | --- | --- |
| Interactive development and experiments | Primary | Not required |
| Historical research and heavier backtests | Primary | Optional bounded checks |
| Manual production-equivalent scan | Supported | Not required |
| Scheduled production scans and artifacts | Review / development | Primary |
| Regression checks | Local | Required in CI |
| Secrets | Local environment or secret store | GitHub secrets |

The PC may be offline. Hosted production discovery must never depend on local services, local databases, or the local network. Do not commit credentials, raw secret-bearing request data, run outputs, or private research datasets.

## Data and identity rules

For every market observation, retain the chain/network, token contract or mint, pool/market address, venue, base asset, quote asset, and pair orientation as separate fields when available. A pool address is not a token identity; ticker and symbol are not unique identities.

Keep source event time and local ingestion time distinct. Preserve provider/source attribution, block or slot where supplied, scanner version, configuration fingerprint, and the decision/rejection reasons. Keep raw provider evidence separate from normalized records. Missing data stays missing; it must not be silently converted to zero or a passing safety result.

Forecast features are frozen at detection time. Future observations may be used to label outcomes, but must never leak into the original feature snapshot or strategy decision. Price marks are not executable fills; execution-adjusted returns require recorded quote, fee, and slippage evidence.

## Storage direction

Build incrementally around the current JSON/JSONL learning memory:

1. Keep the current compressed append-only forecast and outcome ledgers authoritative during migration.
2. Add a versioned normalized event schema and a read-only, idempotent export path before changing the writer. Record counts and rejected-record reasons; preserve missing values and provenance without inference.
3. Use partitioned Parquet for larger immutable historical observations; the optional local exporter uses PyArrow, isolated from scanner runtime dependencies. Keep operational state and coordination in the existing locked files or a small SQLite database with WAL where transactional queries are needed.
4. If DuckDB is introduced, give it one controlled writer. Use Parquet as the exchange boundary and allow research jobs to read snapshots. Do not let independent scan processes write concurrently to one DuckDB file.
5. Provide reconciliation counts and stable record identifiers so imports can be repeated without duplicating history. Never delete or rewrite existing memory as part of a migration.

Do not add Kafka, Redis, Kubernetes, or a permanent local service until measured throughput or coordination needs justify their resource cost.

## Delivery sequence

### Phase 1 — Local foundation

Retain the existing bounded runner, per-run isolation, checkpoints, memory lock/merge, and status reporting. Add machine/resource diagnostics and operational documentation. Record runtime versions and configuration fingerprints with each run. Keep diagnostics read-only and do not expose environment secrets.

### Phase 2 — Historical data foundation

Specify stable observation schemas and identity keys. Add idempotent export of existing forecast/outcome records to partitioned Parquet, with provenance and a validation summary. Introduce a single-writer analytical database only after the export is reliable and useful.

### Phase 3 — Outcome analysis

The initial read-only report provides forecast/outcome coverage and bounded-lag sampled-mark threshold attainment, but the current ledgers do not establish intrahorizon target touches, time-to-target, maximum adverse excursion, or executable returns. A manually started, bounded foreground collector (maximum 180 minutes per run) appends hourly OHLCV observations for identities in the active watchlist under `rocket-hunter-data/research-data/watchlist-observations/`; it does not change scanner behavior or source ledgers and does not require continuous/24-hour PC uptime. The separate `scripts/analyze_candle_outcomes.py` report joins only reliable exact identities and eligible USD hourly bars, then reports complete-window candle-level target presence, hourly-low adverse movement, gaps, duplicates, censoring, and excluded/unjoinable records. Hourly candles do not resolve exact intrahour timing/order, and the watchlist is not full-universe coverage. Exact execution results remain unsupported. Analysis freezes features at detection, uses later observations only as labels, and keeps research out of production rankings; it writes a new versioned artifact without replacing the initial Phase 3 report.

### Phase 4 — Independent research engines

Add discovery, momentum, on-chain, liquidity, security, and execution research as separately attributable evidence producers. Respect provider rate limits, use caching and request prioritization, and make unavailable evidence explicit. New engines must not bypass the existing identity, security, executable quote, or +40% TP eligibility gates.

### Phase 5 — Lifecycle monitoring and dashboard

`scripts/continuous_monitor.py` adds restartable, bounded research monitoring beside the existing hourly watcher. It consumes only the provenance-bearing `crypto-prebreakout.active-24h-watchlist.v2` contract. `scripts/v425_early_breakout.py` now carries each candidate's Phase 4 score/evidence snapshot and its original scanner version, configuration fingerprint, source run ID, source-event timestamp, and local receipt timestamp into that watchlist; later refreshes are recorded separately and do not replace the entry snapshot. The monitor validates exact token/pool identity and rejects missing, stale, mismatched, or unsupported Phase 4 provenance instead of promoting it to valid evidence. It never recalculates candidate scores: the original score is retained only for prioritization.

The monitor persists the candidate queue, exact identities, immutable observations, coverage gaps, provider failures, and target-touch/outcome state in a separate SQLite database and emits content-hashed session manifests. Sessions are capped at 180 minutes; active capacity is 12, with overflow retained in a durable queue. GeckoTerminal DEX pools support provider 1-minute and 5-minute candles plus pool snapshots; the existing CEX adapter supports hourly candles only. Pool h24 buy/sell counts are provider transaction counts, not USD volume or wallet flow. Target touches remain technical candle observations, never fills. Missing/stale/conflicting evidence fails closed, and risk inputs unavailable from selected endpoints remain unavailable. A foreground session can be started with `python scripts/continuous_monitor.py --duration-minutes 180 --interval-seconds 300 --priority-interval-seconds 60`; no scheduled workflow or always-on service is installed. The offline dashboard includes scan/collection lifecycle views and point-in-time machine diagnostics without local health thresholds.

#### Gate 1 — frozen local run/evidence interfaces

`scripts/research_run_contract.py` is the shared boundary for the local scanner, watchlist collector, and continuous monitor. It stores one exclusive lease and durable lifecycle history in `rocket-hunter-data/research-data/continuous-monitoring/monitor-state.sqlite3`; `activity_run_id` in each scan status or collection manifest is the same run ID recorded by that contract. Leases are process/host-owned, heartbeat every 10 seconds, and have a 60-second stale threshold. Recovery is a single bounded SQLite transaction: a stale lease is reclaimed only when its local owner is confirmed dead or its host differs, the prior row becomes `STALE`, and the old run is never resumed. Windows process liveness uses read-only process-query APIs; access or unknown errors fail closed. Scanner subprocesses are stopped if lease health fails; collectors and the monitor check health during polling and before committing observations. All executions remain capped at 180 minutes.

Collection manifests use one lifecycle/count validator and keep lifecycle finalization separate from evidence quality. Manifestless, `RUNNING`, and `INTERRUPTED` sessions remain `PENDING`; interrupted or lease-unhealthy producers do not count or finalize their partial observations. A terminal `FAILED` manifest and finalized provider outages are `FAILED` evidence, and neither contributes usable observations. Record-count shortfall is `MISSING`, excess or wrong-run records are `CONFLICTING`, and stale source records are `STALE`; any non-`COMPLETE` collection is excluded from usable candle outcomes. Readiness, candle analysis, and dashboard code share this policy and never read pending or terminal-`FAILED` observation files. DEX identity is the exact `(chain, token_mint, pool_address)` tuple. Provider event time and local receipt/ingestion time remain separate fields. The monitor continues to allow exactly one `single_take_profit` at least +40.00% within a maximum 24-hour horizon, with no stop-loss, trailing stop, or additional TP tiers.

### Phase 6 — Production promotion

Promote a strategy only after reproducible historical evaluation and local/CI regression coverage. Use the same core scanner and mandatory verification rules in local and hosted environments. Keep deployment and scheduled production ownership in GitHub.

## Acceptance checks for future changes

- Existing scanner-memory files and historical records remain intact.
- Concurrent local runs cannot corrupt shared provider budgets or learning memory.
- A failed or interrupted run is not reported as complete or actionable.
- Every analytical result identifies its source data, time window, scanner version, configuration, and missing-data limits.
- Readiness and dashboard collection summaries count only manifest-declared observation rows whose collection run ID matches the manifest; extra, missing, or mismatched rows remain explicit exclusions.
- A strategy replay uses only information available at its original decision time.
- No research component can override deterministic safety or execution gates.
- Tests cover new behavior when implementation begins; do not trigger live market collection merely to validate a local storage change.
