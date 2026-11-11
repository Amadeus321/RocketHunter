# Direct Action Guide for Copilot: What to Do Next

This document contains immediate, actionable instructions for GitHub Copilot working on the **Rocket Hunter** local intelligence platform.

---

## Current Situation Summary
- **Code Status**: Offline analysis, bounded collectors, export tools, missed-opportunity analysis, and offline dashboards are implemented and verified.
- **Test Suite**: 297 unit tests passing via `python -m unittest discover -s tests -p "test_*.py"`.
- **v4.25 Discovery Decoupled**: DexPaprika keyless pool searches are now ensured during `build_output` even when `--birdeye` is omitted, resolving the zero-capture issue for prospective runs. Scan `local-2f962fd99b0f4a89a467faf20de2c495` completed cleanly in 96m with scored candidates.
- **GeckoTerminal Access**: Verified 200 OK using `Accept: application/json;version=20230203` and 12-second request pacing.
- **Current Evidence Baseline**: 20 sessions (18 finalized, 2 historical unfinalized), **2,709 completed hourly bars**, **81 loaded observations**, 60 exact-identity joins, **255,363 forecasts**, and **6 complete 24h windows**. Retrospective candle research milestones: touched the mandatory single-TP +40% threshold. Overall status remains NOT READY. Note: The trade plan is strictly **1 single Take Profit (minimum +40.00%) with NO stop loss and NO trailing stop**. Do not report multi-target trade plans.
- **Single-Cycle & Rate-Limit Statuses Recognized**: `research_readiness.py` and `status_dashboard.py` treat single-cycle runs (`ONE_CYCLE_COMPLETED`, `DURATION_ELAPSED`, `PROVIDER_RATE_LIMITED`) as finalized, counting their bars and removing false pending blockers.
- **Platform Readiness**: **NOT READY**. (Must remain NOT READY until sufficient coverage and reproducible holdout evidence are acquired).

---

## Critical Invariants & Rules of Engagement

1. **Strict 180-Minute Cap**: No scan, collection, or batch task may run longer than 180 minutes. No 24/7 background processes.
2. **Read-Only Local Research**: Do NOT submit orders, sign transactions, or touch private keys.
3. **Do Not Touch or Read Partial Runs**: Two collection runs (`collection-20261109T163435Z-910ade60e1f5` and `collection-20261109T165326Z-a85372a9454b`) lack final manifests. Leave their `observations.jsonl` untouched and excluded.
4. **Never Overwrite Historical Scanner Memory**: `rocket-hunter-data/scanner-memory/` is immutable ground truth.
5. **Testing Command**: Run ONLY `python -m unittest discover -s tests -p "test_*.py"`. NEVER run root `pytest`.
6. **No Overlapping Jobs**: Do not run a collector and a scanner at the same time.

---

## Action Plan: 6 Immediate Steps

### 1. Verify Current Clean State
```powershell
# 1. Ensure no rogue processes are running
Get-Process python* -ErrorAction SilentlyContinue

# 2. Run the unit test suite (must pass 287 tests)
python -m unittest discover -s tests -p "test_*.py"

# 3. Verify baseline metrics
python scripts/research_readiness.py
```

### 2. Collect Bounded Watchlist Observations (Expand Phase 3 Coverage)
The main bottleneck to evaluating strategies is the lack of complete 24-hour observation windows (currently only 1).
Run bounded foreground collections:
```powershell
# Single-cycle sweep across all 8 candidates:
python scripts/collect_watchlist_observations.py --once

# OR a bounded foreground session (e.g., 30 minutes, 300s polling interval):
python scripts/collect_watchlist_observations.py --duration-minutes 30 --interval-seconds 300 --max-requests 60
```
*Note: Ensure the collection finishes cleanly with a valid `manifest.json`.*

### 3. Re-Run Outcome Analysis & Dashboard
After the collection run finalizes:
```powershell
# Recompute candle outcomes from manifest-backed data
python scripts/analyze_candle_outcomes.py

# Update readiness metrics
python scripts/research_readiness.py

# Rebuild the HTML dashboard
python scripts/status_dashboard.py
```
Check `status-dashboard.html` to confirm whether completed 24-hour windows and valid bars have increased.

### 4. Generate Fresh Prospective v4.25 Forecasts (Phase 4)
Because historical scanner memory has 0 `v425_early_breakout` captures, generate prospective forecasts through the local runner:
```powershell
# Generate a new local scan with automatic v4.25 post-scan scoring
python scripts/run_local_scan.py --outdir "rocket-runs/local-run-$(Get-Date -Format 'yyyyMMddTHHmmssZ')" --max-runtime-minutes 170
```
Verify that the scan merged into memory cleanly and that newly appended forecasts have the `feature_snapshot.v425_early_breakout` block.

### 5. Evaluate v4.25 Shadow Strategy (Once Outcomes Mature)
After prospective forecasts have aged and matching 24-hour outcome candles have been collected:
```powershell
python scripts/analyze_v425_shadow.py --split-cutoff-utc "<CUTOFF_TIMESTAMP_UTC>"
```
*Remember: Marks are exploratory endpoint marks, not fill prices or profit proof.*

### 6. Document Milestone & Keep Invariants
- Record new counts in `LOCAL_INTELLIGENCE_READINESS.md` and `CODEX_HANDOFF_LOCAL_INTELLIGENCE.md`.
- Keep overall status as **NOT READY**.
- Re-run test suite: `python -m unittest discover -s tests -p "test_*.py"`.
