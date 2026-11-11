# GitHub Copilot Instructions — Rocket Hunter Local Intelligence

You are GitHub Copilot assisting on the **Rocket Hunter** project.
Your primary role is to execute and support the local intelligence research roadmap safely, reproducibly, and within strict operating boundaries.

---

## 1. Core Mission & Invariants (NEVER VIOLATE)

1. **Read-Only Local Research**: Local intelligence is strictly analytical market research. Never create, sign, or submit blockchain transactions, orders, or private key operations.
2. **Hard 180-Minute Maximum**: Every collector run, scan execution, or analytical batch must strictly terminate within **180 minutes**. Continuous uptime or 24/7 background daemons are strictly prohibited.
3. **Data Immutability & Provenance**:
   - Never delete, rewrite, or reset `rocket-hunter-data/scanner-memory/` ledgers (`forecasts.jsonl.gz`, `outcomes.jsonl.gz`), historical Parquet exports, or past run reports.
   - Scanner JSONL remains the authoritative ground truth.
   - Keep source event time and local ingestion time distinct.
   - Pool address is NOT token address; ticker is NOT a unique identity. Always join on exact `(chain, token_mint, pool_address)`.
4. **Preserve Unfinalized / Partial Runs**:
   - Directories without a final `manifest.json` (such as `collection-20261109T163435Z-910ade60e1f5` and `collection-20261109T165326Z-a85372a9454b`) must be marked as pending.
   - **NEVER** open or inspect their partial `observations.jsonl`. Preserve them untouched.
5. **GeckoTerminal Access & Rate Limits**:
   - Header requirement: `Accept: application/json;version=20230203` (NOT `20230302`).
   - Maintain minimum 12-second pacing (~5 req/min) to stay well below the 10–30 req/min free-tier rate limit.
   - Stop immediately upon encountering HTTP 403, 418, or 429.
6. **No Premature Promotion / Readiness Claims**:
   - The platform status is currently **NOT READY**. Do not mark ready from a single or small sample of complete windows.
   - Phase 6 (production promotion) is BLOCKED. Production discovery and scheduled runs belong to GitHub Actions; never deploy or push changes claiming strategy effectiveness without holdout evidence and independent GitHub owner approval.
7. **Strict Single-TP Rule (Non-Negotiable User Rule)**:
   - The trade plan uses **EXACTLY 1 single Take Profit** (`single_take_profit`), requiring $\ge$ +40.00% upside.
   - **NO Stop Loss. NO Trailing Stop.**
   - Retrospective research milestones in candle analysis (+60%, +100%) are historical exploratory markers only. NEVER present them as multiple trade targets (TP1/TP2/TP3). Always clarify to the user that the trade plan is strictly ONE single TP ($\ge$ +40.00%).

---

## 2. Test & Verification Rules

- **Unit Test Command**:
  ```powershell
  python -m unittest discover -s tests -p "test_*.py"
  ```
  Expected: **294 tests passing** (includes regression coverage for keyless DexPaprika v4.25 capture decoupling and `ONE_CYCLE_COMPLETED` manifest finalization).
- **STRICT PROHIBITION**: **DO NOT** use root-level `pytest`. Root `pytest` discovers archived runtime test duplicates in subdirectories and fails with import-file mismatches.

---

## 3. Step-by-Step Action Plan: What Copilot Must Do

### Step 1: Preflight Verification
Verify system state before any collection or scanning:
1. Confirm no rogue python processes or background collectors are running:
   ```powershell
   Get-Process python* -ErrorAction SilentlyContinue
   ```
2. Verify all 287 unit tests pass:
   ```powershell
   python -m unittest discover -s tests -p "test_*.py"
   ```
3. Run readiness check to inspect current baseline:
   ```powershell
   python scripts/research_readiness.py
   ```

### Step 2: Expand Forecast-Matched Observation Coverage (Phase 3)
The primary operational gap is outcome coverage: only 1 complete 24-hour window currently exists across 58 exact-identity joins.
1. Run a bounded, foreground watchlist observation cycle (never exceed 180 minutes):
   - For a single 8-candidate cycle probe:
     ```powershell
     python scripts/collect_watchlist_observations.py --once
     ```
   - For a bounded foreground session (e.g. 30 minutes, 300s interval):
     ```powershell
     python scripts/collect_watchlist_observations.py --duration-minutes 30 --interval-seconds 300 --max-requests 60
     ```
2. **Never** run a collector simultaneously with a scan.
3. Ensure the run finishes with a valid `manifest.json`.

### Step 3: Re-Analyze Outcomes & Update Readiness
Once new manifest-backed observation sessions complete:
1. Analyze candle outcomes:
   ```powershell
   python scripts/analyze_candle_outcomes.py
   ```
2. Re-run research readiness report:
   ```powershell
   python scripts/research_readiness.py
   ```
3. Re-generate the offline status dashboard:
   ```powershell
   python scripts/status_dashboard.py
   ```
4. Check metrics: observe total valid completed hourly bars (prior baseline: 807), loaded observations (baseline: 26), exact joins (baseline: 58), and complete 24h windows (baseline: 1).

### Step 4: Generate Prospective v4.25 Scored Forecasts (Phase 4)
Historical ledgers contained zero captures of `feature_snapshot.v425_early_breakout`. To evaluate the v4.25 composite, fresh prospective forecasts must be generated through the regular pipeline:
1. Execute a local scan with an isolated output directory:
   ```powershell
   python scripts/run_local_scan.py --outdir "rocket-runs/local-run-$(Get-Date -Format 'yyyyMMddTHHmmssZ')" --max-runtime-minutes 170
   ```
2. Confirm the post-scan hook attaches prospective v4.25 score/components and complete source, timestamp, run, and scanner-version provenance.
3. **NEVER** edit scanner memory directly or fabricate historical scores.

### Step 5: Prospective Shadow Evaluation (When Outcomes Mature)
Once prospective forecasts exist and sufficient 24-hour outcome observations have matured:
1. Execute the v4.25 shadow analysis with an explicit UTC time-split cutoff:
   ```powershell
   python scripts/analyze_v425_shadow.py --split-cutoff-utc "<CUTOFF_TIMESTAMP_UTC>"
   ```
2. Retain all unavailable/excluded outcomes. Treat endpoint marks as exploratory marks only—not as executable fills, profit, or effectiveness proof.

### Step 6: Maintain Truthful Reporting
1. Update `LOCAL_INTELLIGENCE_READINESS.md` and handoff documentation with actual measured counts and exact exclusions.
2. Keep the overall readiness label **NOT READY** until all holdout evidence, regression coverage, and safety gates are genuinely satisfied.
