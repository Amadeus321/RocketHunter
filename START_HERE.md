# 🚀 Rocket Hunter v4.30 — Getting Started Guide
**Algorithmic DeFi & Microcap Pre-Breakout Research Platform**

Welcome to Rocket Hunter v4.30! This platform provides autonomous multi-chain research intelligence to detect imminent pre-breakout token opportunities before volume surges occur.

---

## 1. Quick Start (1-Click Run)

### Method A: Standalone Windows Application
1. Double-click **`RocketHunter.exe`**.
2. The Rocket Hunter native desktop window will open immediately.
3. Use the left navigation sidebar to explore:
   - **Control Center:** Launch new scans, monitor system clock, and view active stages.
   - **Forward Watchlist:** View the latest 24-hour opportunity report (`forward-24h-rocket.html`).
   - **Status Dashboard:** Inspect comprehensive historical run metrics (`status-dashboard.html`).
   - **Live Monitor:** Watch real-time log output and progress percentages.

### Method B: 1-Click Launcher Batch File (`Launch_RocketHunter.bat`)
If you prefer launching via command script or if your Windows environment restricts unsigned `.exe` files:
1. Double-click **`Launch_RocketHunter.bat`**.
2. It will launch the desktop control center automatically.

---

## 2. Navigating the Platform

### Starting a Research Scan
- Click **"Start New Scan"** in the sidebar or Control Center.
- A new scan will launch in an isolated process. Progress is tracked live in the **Live Monitor** tab.
- Scans evaluate over 9,000+ liquidity pools across Ethereum, Solana, Base, Arbitrum, BSC, and Polygon.
- When complete, the **Forward Watchlist** tab updates automatically with all qualified candidates.

### Reviewing Opportunities
- Every qualified candidate must satisfy:
  - **Single Technical Take Profit:** Minimum $+40.00\%$ target upside.
  - **Terminal Horizon:** Maximum $24.0\text{ hours}$ holding limit.
  - **Security Gate:** Passed honeypot, blacklist, transfer tax ($\le 10\%$), and liquidity lock filters.
  - **Liquidity Floor:** Minimum active pool depth cushion.
- Unverified candidates are clearly segregated in **Section 2: Unverified Research Watchlist (NOT QUALIFIED)**.

---

## 3. Windows Troubleshooting & Security Notices

### Windows SmartScreen Notice
- Because `RocketHunter.exe` is an independent release without an enterprise code-signing certificate, Windows SmartScreen may show:
  > *"Windows protected your PC"*
- **Resolution:** Click **"More info"**, then click **"Run anyway"**.

### Application Control / AppLocker Policy Notice
- On strictly managed corporate or enterprise computers with Windows Defender Application Control (WDAC) or AppLocker enabled:
- If you see `An Application Control policy has blocked this file` (WinError 4551):
  - Use **`Launch_RocketHunter.bat`** instead.
  - Or run the automatic **GitHub Actions cloud scans** (every 6 hours) which run completely online without requiring any local executable execution.

---

## 4. Backing Up and Preserving Your Research Data

All local data is stored in the `rocket-hunter-data/` directory:
- **`user_positions_journal.json`:** Your active and historical tracked positions.
- **`ai_learning_engine.json`:** Quantitative feature weights and model calibration.
- **`notification_config.json`:** Email / webhook notification configuration.

### How to Back Up:
1. Copy the entire `rocket-hunter-data/` folder to a safe location (e.g. `rocket-hunter-data-backup/` or external drive).
2. To restore, simply copy the backed-up `rocket-hunter-data/` folder back into the application directory.
3. Existing data is protected by file locks and will never be overwritten by scans.

---

## 5. Strict Research-Only Operating Policy

> [!IMPORTANT]
> **RESEARCH USE ONLY:**
> Rocket Hunter is strictly an analytical research and discovery engine.
> - **Zero Automated Trading:** Rocket Hunter will never execute trades on your behalf.
> - **Zero Wallet Access:** The software contains no wallet connections, private key handlers, or transaction signing capabilities.
> - **No Financial Advice:** Research outputs and hypothetical upside targets do not constitute investment advice or guarantees of profitability.
