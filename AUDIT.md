# Rocket Hunter repository audit — v4.26.7

## Latest qualification and audit-persistence changes

- The completed 4.26.4 run supplied by the user had 25 `top_opportunities` while 19 of those rows had less than +40% technical TP upside; its final actionable count was zero. This confirms the presentation mismatch: ranked research rows were exposed as “top opportunities” even though they had not passed the final gate.
- `top_opportunities` is now empty until final qualification, then contains only rows from the strict qualified lane. Research-ranked leaders are named separately; the preliminary forward-report pass no longer writes a reference plan into the verified/actionable fields.
- The final gate requires observed candle resistance, exact target provenance and same-market/token orientation, completed structurally valid candles, recent price/candle retrieval alignment, a non-anomalous resistance, at least $100,000 pool liquidity, a valid 1h ATR and a target no farther than `max(+40%, 3 × 1h ATR)`. It then independently requires a fresh final price, exact pool/token/chain identity, safety and loss-pattern PASS, and fresh buy and sell quotes. The technical TP arithmetic is independent of the hypothetical 24h upside scenario.
- Final JSON/Markdown puts qualified opportunities, mature unverified research, and young or unknown-age discovery-only leads into distinct arrays/sections. Non-qualified rows are explicitly marked `NOT QUALIFIED`; research ranks are not promoted to actionable output.
- Scoring adds late-chase risk flags and penalties for larger short-window run-ups, abnormal 5m-to-1h acceleration, and strong run-ups at/above the 24h high.
- `post_scan_audit.py` now retains earlier same-run CVD/order-book blocks when the later configured audit pass has no candidates or no replacement rows. A fresh attempted audit with records replaces the old result.
- Learning-memory ledgers and calibration data were not modified by this release. No claim is made that the ranking changes improve out-of-sample performance; no holdout was run.

## Scope inspected

- The checked-out `RocketHunter` repository at `e2f490d` and its existing working-tree edits.
- `RocketHunter.zip` (130 entries; ZIP integrity check passed), both identity-audit patch copies, both supplied TP-guard archives, the two root patch files, and the repository README/workflows/tests.
- All tracked `scripts/`, `tests/`, and the full-scan workflow. The attached archives contain older or isolated code; they are not the production source of truth.

The user's pre-existing edits to `scripts/rocket_hunt.py`, `scripts/v418_pipeline.py`, `scripts/v425_early_breakout.py`, and `tests/test_v418_pipeline.py` were retained. The age-guard update now also recognizes pre-v2 memory keys, so those edits do not strand older forecast records.

## What works

- DEX pool discovery preserves chain/pool/token identifiers and can merge provider rows. The current working edits canonicalize valid EVM hex addresses while preserving case-sensitive chain addresses.
- `scripts/dex_onchain_evidence.py`, `scripts/dex_loss_guard.py`, and `scripts/post_scan_audit.py` already implement fail-closed checks for token-to-pool identity, launch-wallet history, LP custody, token security and two-way router quotes. Missing evidence is a blocker.
- The production report refreshes the exact GeckoTerminal pool and asks for OHLCV for the tracked token address. The new final action gate additionally checks its timestamp, pool, token, chain and token-price orientation.
- The learning code persists identity-bearing discovery rows, forecast snapshots, delayed mark-to-market outcomes, calibration diagnostics, provider/network rotation and 24-hour reference-position observations. Outcomes are explicitly marked as observations, not fills. Calibration does not train model weights or relax safety gates.
- There are existing regression tests for identity, price orientation, on-chain evidence, safety/quotes, historical age semantics, calibration, and reference-position MFE/TP measurement.

## Breaks and bypasses found, now patched locally

- No canonical +40% gate was wired across the forecast BUY tier, report plan, and final forward board. A newly shared `scripts/tp_gate.py` now supplies the arithmetic. `39.99%` fails; `40.00%` passes the arithmetic check only.
- `next_day_forecast.rank_document()` could select a preliminary BUY row, including CEX rows, into `best_immediate_entry`. It now keeps those rows in discovery unless the DEX identity, final-price, safety, execution and TP conditions are all met. The downstream report clears preliminary entries and rebuilds the actionable lane from the stricter plan.
- The final forward board did not have a distinct verified-actionable array. It now keeps discovery candidates separate, applies the final exact-pool price and evidence gate after ranking/reference monitoring, records zero when no rows pass, and renders actionable rows only after the scan status is completed.
- The workflow extracted legacy source archives after checkout and restored only some tracked integrations. That could make the production job ignore tracked changes. The workflow now restores the tracked scan/qualification modules before overlay installers run.
- Both workflows were scheduled every two/four hours. Schedules are removed; manual dispatch and push triggers remain.
- `run_full_scan.py` had no process-wide deadline, no discovery time reserve, and could leave old output looking current after interruption. It now enforces a configurable maximum of 180 minutes (default 170; production passes 150), reserves time for checks/reporting, marks interrupted/failed outputs as non-current, and clears action-ready fields. The production workflow holds completion status until all downstream stages and memory persistence succeed; its hard timeout is 180 minutes.
- The full learning and qualification sequence was only orchestrated by the hosted workflow. `scripts/run_local_scan.py` now runs those same local stages without Actions, isolates concurrent run outputs and memory snapshots, merges append-only historical ledgers under an OS file lock, and shares locked provider request budgets. It initializes memory from the already-local `origin/scanner-memory` ref and refuses to start silently with empty history.
- The identity-key working edit changed DEX forecast keys while the age guard still matched only the old key. A compatibility lookup now corrects those historical rows without rewriting or deleting them.
- Several safety/price joins lowercased every address, including case-sensitive Solana mints and pool IDs. Chain-aware address keys now normalize only EVM hex addresses and preserve exact casing on Solana.
- `VERSION` now identifies the local parallel runner release as 4.26.3.
- Child-process deadlines now terminate complete process trees; GeckoTerminal pacing budgets count the paginated workload accurately; local sweep defaults are bounded and each launcher run has a visible elapsed-time/activity monitor.
- `run_full_scan.py` now adds its isolated runtime root to Python's import path when launched as a script, fixing the local `ModuleNotFoundError: No module named 'scripts'` startup failure.
- The Windows launcher can securely prompt for supported local API settings without persisting them, and `-Birdeye` now reaches the local runner so the existing Birdeye lane can be enabled from the monitored launcher.

## Gate locations

| Requirement | Implementation | Coverage |
|---|---|---|
| Canonical TP upside and hard +40% action threshold | `scripts/tp_gate.py`; `scripts/next_day_forecast.py`; `scripts/forward_24h_report.py`; `scripts/v425_early_breakout.py` | `test_tp_gate.py`, `test_next_day_tp40_gate.py`, `test_forward_24h_required_fields.py`, `FinalActionableGateTests` |
| Chain, exact pool, token contract, final price/orientation | `scripts/forward_24h_report.py` final pool refresh; `scripts/v425_early_breakout.py` final filter | `test_forward_24h_orientation.py`, `test_dex_onchain_evidence.py`, `FinalActionableGateTests` |
| Two-way quotes and safety | `scripts/post_scan_audit.py`; `scripts/dex_loss_guard.py`; final filter in `scripts/v425_early_breakout.py` | `test_dex_loss_guard.py`, `test_dex_onchain_evidence.py`, `FinalActionableGateTests` |
| Candle-derived TP credibility, target provenance and exact pool/time/orientation | `scripts/forward_24h_report.py`; final filter in `scripts/v425_early_breakout.py` | `test_technical_tp_validation.py`, `test_forward_24h_orientation.py`, `FinalActionableGateTests` |
| Discovery/action separation, three final output categories and zero result | `scripts/forward_24h_report.py`; `scripts/v425_early_breakout.py`; `scripts/render_forward_watchlist_markdown.py` | TP/action gate tests and `test_forward_watchlist_markdown.py` |
| Same-run CVD/order-book audit persistence | `scripts/post_scan_audit.py` | `test_post_scan_audit.py` |
| Historical eligibility and memory key compatibility | `scripts/v418_forward_guard.py`; `scripts/v418_pipeline.py`; `scripts/v419_upgrade.py` | `test_v418_forward_guard.py`, `test_v418_pipeline.py`, `test_v419_upgrade.py` |
| Runtime ceiling, interrupted-run handling and isolated-runtime imports | `scripts/run_full_scan.py`; `.github/workflows/full-rocket-hunt.yml` | `test_scan_budget.py`; CLI smoke checks |
| Local full pipeline, isolated overlapping runs, memory merge and shared quota reservations | `scripts/run_local_scan.py`; `scripts/file_lock.py`; provider budget reservations | `test_run_local_scan.py` |

## Learning memory review

The supplied bare repository's `origin/scanner-memory` ref was inspected read-only at `c000261b27913518b73c023b06ceadbf36865406`; it was not checked out, fetched, reset, or modified. Its manifest reports 225,644 historical forecast snapshots and 692,526 outcome records across three daily partitions, plus 198,606 discovery rows across two partitions. The complete memory tree, including 231 files of ledgers, provider state and cached registries, is bundled under `rocket-hunter-data/scanner-memory`. The imported calibration file reported 1,858 resolved observations (Brier 0.115492; log loss 0.393269). Recomputing with this code's strict 24-hour horizon, maximum six-hour observation lag, valid price provenance and as-of boundary produces 1,820 eligible observations (Brier 0.115097; log loss 0.392241); the imported calibration output is retained as `calibration-state-imported.json`, and the active calibration state is rebuilt from the unchanged historical ledgers.

The outcome partitions classify records as `OBSERVED_MARK_NOT_EXECUTABLE_FILL`, `UNAVAILABLE_IDENTITY_NOT_OBSERVED`, or `UNAVAILABLE_NO_VALID_PRICE_MARK`; they do not represent fills or executed trade P&L. They record mark return, horizon, observation time/lag, and mark quality, while fees/slippage are `NOT_MODELED` and intrahorizon MFE/MAE is unavailable in the reviewed samples. Exact market identities, rather than ticker-only joins, confirm these historical examples:

- `USOE/SOL`: 11 observed 24-hour marks, all negative (approximately -49.81% to -99.99%). The user's exact -44.61% is not present in this pool's 24-hour marks.
- `CORLA/WBNB`: one exact pool has three observed 24-hour marks from approximately -38.43% to -51.37%, not an observed zero. A separate `CORLA/USDT` pool has three -100% 24-hour marks; these are distinct market identities.
- `Reboot/ETH`: 13 observed 24-hour marks, mixed from approximately -29.49% to +26.85%.
- `HIGH/SOL`: the exact market with five observed 24-hour marks is negative (approximately -49.06% to -63.74%); other same-ticker pools lack a completed mark. Ticker-only matching can conflate distinct assets/pools.
- `DENIM/ETH`: 13 observed 24-hour marks, mixed from approximately -99.98% to +65.82%.

These are mark-to-market research observations, not verified personal fills or proof of the user's exact trade outcomes. This remains a historical research/calibration memory, not an execution ledger.

The local runner uses the bundled memory directory directly and makes no GitHub request. Forecast snapshots and delayed mark observations are merged only after a run's qualification and report stages succeed. Existing raw ledgers and the imported calibration output are preserved; the active calibration is recomputed from eligible observations. There is no test or evidence here that ranking improvements outperform a time-separated holdout, so no autonomous improvement claim is made.

## Verification performed

- Full deterministic suite after the scan-deadline and live-monitor changes: **174 tests passed**.
- Latest focused regression set for TP credibility, qualification, audit persistence, report categories, token orientation and local runtime compatibility: **62 tests passed**.
- Complete current regression suite: **188 tests passed**.
- `python -m compileall -q scripts tests`: passed.
- `git diff --check`: passed.
- Python `compileall` for `scripts` and `tests`: passed.
- PowerShell syntax parser accepted the updated launcher and live monitor.
- A regression test timed out a parent with a spawned child and verified the child could not survive to write its marker file.
- GeckoTerminal pacing estimate tests confirm all configured pages, network families, and registry pages are counted.
- CLI help smoke checks for the isolated overlay runtime and core scan/report commands: passed.
- PowerShell parallel launcher syntax parse and `-WhatIf` dry run: passed; no scan process was started.
- Bundled memory contains all three forecast partitions, all three outcome partitions, both discovery partitions, manifest/state ledgers and provider registries. A regression test verifies a populated local memory directory bypasses Git bootstrap and preserves its history.
- Isolated runtime overlay installation left the working source unchanged and installed v4.19/v4.20/v4.23/v4.24/v4.25 hooks into a temporary runtime copy.
- Cross-process DexPaprika quota reservation test passed after fixing Windows lock-file contention; shared Birdeye CU reservation, shared request pacing and deduplicating memory merge tests passed.
- `python -m scripts.v418_pipeline --self-test`: passed.
- `git diff --check`: passed (Git printed only line-ending normalization warnings for existing edited files).
- The first live local run was manually stopped after **6 hours 35 minutes**, exceeding the 180-minute hard limit. Its partial checkpoint had 268 recorded source entries and stopped during GeckoTerminal discovery (`ronin`, pool page 1); the status has been corrected to `INTERRUPTED`. No completed report, verification, or successful memory merge resulted. This exposed the process-tree deadline failure now covered by the new regression test.
- No complete live scan, GitHub Actions run, or workflow production artifact was verified. The user's stop command was confirmed effective; the run processes are no longer active.

## Still outstanding

- Complete one live local run and inspect provider verification, elapsed time, final quotes, zero/actionable split, and merged memory updates. No hosted workflow is required.
- Confirm the new 4.26.6 overlay is installed in `C:\Users\user\RocketHunter`; editing this workspace does not update the user's local installation.
- Evaluate any claimed improvement to discovery or ranking on a time-separated holdout; unit tests do not establish out-of-sample performance.
- The project is **not live-run verified**. The existing free-data/provider limits and optional credentials still determine how many candidates can complete the independent gates. Per-run output and local memory merge were unit tested; remote scanner-memory branch persistence is not part of the local path.
- The Blockscout credential previously pasted visibly into the conversation has not been confirmed rotated; revoke/replace it before relying on that credential.
