# DEX Loss-Pattern Guard

This policy is introduced after two reported DEX losses (USOE/SOL and CORLA/WBNB). It is a fail-closed execution-readiness control, not a token-price predictor.

## Operating rule

DEX discoveries remain visible and rankable. A DEX candidate is not execution-ready unless every mandatory evidence lane below is present, source-attributed, fresh, and linked to the exact chain, token contract and pool. Missing information is BLOCKED / UNVERIFIED, never a pass.

## Mandatory evidence lanes

1. **Identity:** independently bind chain, token mint/contract, quote-token address and exact pool. Never substitute a pair/pool address for a token contract.
2. **Launch allocation and wallet graph:** report deployer initial-supply allocation, linked-wallet supply clusters and graph coverage. Current holder percentages alone are insufficient. The initial policy blocks deployer launch allocations above 15%, linked-wallet clusters above 20%, or wallet-graph coverage below 80%.
3. **LP position:** identify the exact liquidity position for the candidate pool and the authority able to withdraw it. A generic “LP locked” percentage is not accepted unless it identifies the exact pool position. Require burned, at least 95% locked, or a verified timelocked multisig with at least 24 hours' delay.
4. **Token contract security:** require explicit structured security fields for honeypot, buy/sell restrictions, ownership controls, minting, pause/blacklist controls, source verification and taxes. Missing fields remain blocked. Proxy contracts require implementation and admin/upgradeability review.
5. **Two-way, size-bound execution:** require fresh buy and sell quotes for the exact token, chain and intended position size. A buy route alone is not sufficient.

Evidence timestamps must be no older than 60 minutes; execution quotes no older than five minutes. Pools younger than two hours are discovery-only.

## Evidence input contract

Upstream chain-aware enrichers may attach a loss_guard_evidence object to a DEX candidate:

- expected_notional_usd
- pool_identity: status, source, as_of_utc, chain, contract_address, pool_address, quote_token_address
- launch_wallet: status, source, as_of_utc, coverage_pct, deployer_initial_supply_pct, largest_linked_wallet_cluster_pct
- lp_position: status, source, as_of_utc, pool_address, position_identity_verified, control_type, and relevant lock/timelock values
- proxy_implementation when the token is a proxy: status, source, as_of_utc, implementation_address, admin_verified, upgradeability_control, and timelock_hours when applicable

The current scanner integrations do not yet independently collect every launch-wallet graph and LP-position field. Until those enrichers supply verified evidence, the guard intentionally blocks DEX entry readiness. This preserves discovery coverage without pretending the missing evidence exists.

## Regression coverage

tests/test_dex_loss_guard.py exercises missing evidence, concentrated launch allocation, linked-wallet concentration, proxy review, LP-pool mismatch, absent sell routes, quote-size mismatch and very young pools.

This control reduces known blind spots; it cannot guarantee that a token will not rug, that a quote will remain available, or that an executed trade will be profitable.


## Automatic on-chain evidence collection — v4.26 integration

The post-scan DEX guard now runs a read-only enrichment pass before evaluating entry readiness.

- **Solana:** public RugCheck token report, insider graph, and summary; a bounded scan of the earliest mint-address transactions using Solana JSON-RPC; exact pool/mint matching through Raydium API v3 where the pool is supported; LP token supply and largest token-account authority checks against the canonical incinerator address.
- **EVM V2-style pools:** direct JSON-RPC reads of the exact pair's `token0`, `token1`, LP `totalSupply`, and `balanceOf` for the canonical zero/dead addresses. This measures LP shares held at unspendable addresses and calls them **locked**, not burned.
- **EVM launch wallets:** Blockscout holder and token-transfer history are collected. Token transfers are *not* treated as proof of common funding or common beneficial ownership. Until a native-asset common-funder graph is available, EVM launch-wallet evidence remains unverified and the entry guard blocks it.
- **Unsupported pool models / chains:** concentrated-liquidity positions, nonstandard LP tokens, missing RPC endpoints, incomplete holder history, or unmatched identities remain unverified.

The RugCheck insider graph can cap or omit networks above 2,000 accounts. The Solana LP lock percentage is calculated from at most 20 largest LP token accounts, so it is a lower-bound screen, not an exhaustive locker registry. Public RPC services may rate-limit. Optional `SOLANA_RPC_URL` and `CHAIN_RPC_URLS_JSON` CI secrets allow the operator to supply more reliable read-only endpoints; public defaults remain in place.

No on-chain transaction is signed or submitted. Evidence is a point-in-time snapshot and never guarantees safety.


### EVM common-funder graph refinement

The EVM adapter inspects up to 32 top-holder EOAs per candidate using Blockscout inbound native-transaction history. It records a holder's earliest observed direct native-coin funder only when the address-history response is complete (no pagination cursor). It groups holders sharing the same unlabelled EOA funder, excludes known service labels and contract funders, and reports the largest related supply cluster separately from ordinary ERC-20 transfer flow. The result remains a relationship heuristic, not proof of common beneficial ownership. Incomplete holder histories, unknown contract classifications, internal-only funding and token-only funding remain unverified.


### Dynamic multi-chain Blockscout PRO support

When BLOCKSCOUT_PRO_API_KEY is configured, EVM holder, transfer and first-native-funder queries use the Blockscout PRO gateway. The module discovers supported chain IDs from the public PRO config endpoint and resolves names through the Blockscout chain registry on each workflow process (cached within that process). It uses a bearer token for data requests. This extends EVM wallet evidence beyond the static public Blockscout instance list to the chains currently listed by the PRO gateway. The optional BLOCKSCOUT_CHAIN_IDS_JSON secret can map scanner-specific chain slugs to the numeric chain IDs returned by that registry.

Without that key, the existing public instance list still works for supported explorers. A chain absent from both registries stays unverified. A key is not included in this repository; an operator must create it and add it as a GitHub Actions repository secret. Calls are metered against the selected Blockscout plan, so configure a suitable free/paid tier and monitor the provider's current credit allowance before increasing candidate or holder limits.
