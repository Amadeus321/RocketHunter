"""Read-only launch-wallet and LP-position evidence enrichers.

The module is intentionally conservative:
- RugCheck provides Solana token report/insider data; token-wide LP percentages
  are never treated as proof for a specific market.
- Solana pool-specific burn proof is attempted only when a Raydium response
  identifies that exact pool and its LP mint.
- EVM pool proof is limited to V2-style ERC-20 LP tokens whose token0/token1,
  totalSupply and burn-address balances can be read from chain RPC.
- Blockscout transfer history provides a token-transfer relationship graph,
  not proof that wallets share a beneficial owner.
Unsupported, truncated or stale data remains UNVERIFIED.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

RUGCHECK_ROOT = "https://api.rugcheck.xyz/v1"
RAYDIUM_ROOT = "https://api-v3.raydium.io"
SOLANA_RPC_DEFAULT = "https://api.mainnet-beta.solana.com"
SOLANA_INCINERATOR = "1nc1nerator11111111111111111111111111111111"
EVM_BURN_ADDRESSES = (
    "0x000000000000000000000000000000000000dead",
    "0x0000000000000000000000000000000000000000",
)
BLOCKSCOUT_HOSTS = {
    "ethereum": "https://eth.blockscout.com",
    "eth": "https://eth.blockscout.com",
    "base": "https://base.blockscout.com",
    "arbitrum": "https://arbitrum.blockscout.com",
    "optimism": "https://optimism.blockscout.com",
    "polygon": "https://polygon.blockscout.com",
    "polygon_pos": "https://polygon.blockscout.com",
    "gnosis": "https://gnosis.blockscout.com",
    "scroll": "https://scroll.blockscout.com",
    "linea": "https://linea.blockscout.com",
    "avalanche": "https://avalanche.blockscout.com",
}
EVM_RPC_DEFAULTS = {
    "ethereum": "https://ethereum-rpc.publicnode.com",
    "eth": "https://ethereum-rpc.publicnode.com",
    "base": "https://mainnet.base.org",
    "arbitrum": "https://arb1.arbitrum.io/rpc",
    "optimism": "https://mainnet.optimism.io",
    "polygon": "https://polygon-bor-rpc.publicnode.com",
    "polygon_pos": "https://polygon-bor-rpc.publicnode.com",
    "avalanche": "https://api.avax.network/ext/bc/C/rpc",
    "bsc": "https://bsc-dataseed.bnbchain.org",
    "linea": "https://rpc.linea.build",
    "scroll": "https://rpc.scroll.io",
}
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
MAX_SOLANA_SIGNATURES = 1000
MAX_SOLANA_OLDEST_TX_SCANS = 20
MAX_BLOCKSCOUT_HOLDER_PAGES = 8
MAX_BLOCKSCOUT_TRANSFER_PAGES = 12
MAX_BLOCKSCOUT_TRANSFERS = 600
MAX_EVM_CANDIDATE_HOLDERS = 300
MAX_BLOCKSCOUT_COMMON_FUNDER_HOLDERS = 32
BLOCKSCOUT_PRO_API_ROOT = "https://api.blockscout.com"
BLOCKSCOUT_PRO_CONFIG_URL = BLOCKSCOUT_PRO_API_ROOT + "/api/json/config"
BLOCKSCOUT_CHAINS_REGISTRY_URL = "https://chains.blockscout.com/api/chains"
_BLOCKSCOUT_PRO_REGISTRY_CACHE = None
_BLOCKSCOUT_PRO_REGISTRY_LOGS = []


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _num(value):
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _raw_integer(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        text = str(value).strip()
        return int(text) if text and text.lstrip("-").isdigit() else None
    except (TypeError, ValueError, OverflowError):
        return None


def _as_dict(value):
    return value if isinstance(value, dict) else {}


def _as_list(value):
    return value if isinstance(value, list) else []


def _unwrap(value):
    data = value.get("data") if isinstance(value, dict) else None
    if isinstance(data, dict) and any(k in data for k in ("data", "items", "nodes", "markets")):
        return data
    return value


def _http_json(url, timeout=8, headers=None):
    req_headers = {
        "Accept": "application/json",
        "User-Agent": "crypto-prebreakout-onchain-evidence/1.0",
    }
    req_headers.update(headers or {})
    req = urllib.request.Request(url, headers=req_headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return payload, {
                "ok": True, "status": response.status, "url": url,
                "retrieved_utc": utc_now(), "error": None,
            }
    except urllib.error.HTTPError as exc:
        return None, {
            "ok": False, "status": exc.code, "url": url,
            "retrieved_utc": utc_now(), "error": "HTTP %s" % exc.code,
        }
    except Exception as exc:
        return None, {
            "ok": False, "status": None, "url": url,
            "retrieved_utc": utc_now(),
            "error": "%s: %s" % (type(exc).__name__, exc),
        }


def _rpc_post(url, method, params, timeout=8):
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "crypto-prebreakout-onchain-evidence/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if isinstance(payload, dict) and payload.get("error"):
                return None, {
                    "ok": False, "status": response.status, "method": method,
                    "error": str(payload["error"]), "retrieved_utc": utc_now(),
                }
            return payload.get("result") if isinstance(payload, dict) else None, {
                "ok": isinstance(payload, dict) and "result" in payload,
                "status": response.status, "method": method,
                "error": None if isinstance(payload, dict) and "result" in payload else "Missing JSON-RPC result",
                "retrieved_utc": utc_now(),
            }
    except urllib.error.HTTPError as exc:
        return None, {"ok": False, "status": exc.code, "method": method,
                      "error": "HTTP %s" % exc.code, "retrieved_utc": utc_now()}
    except Exception as exc:
        return None, {"ok": False, "status": None, "method": method,
                      "error": "%s: %s" % (type(exc).__name__, exc), "retrieved_utc": utc_now()}


def _field(obj, *keys):
    if not isinstance(obj, dict):
        return None
    for key in keys:
        value = obj.get(key)
        if value not in (None, ""):
            return value
    return None


def _present_value(obj, *keys):
    """Return the value of the first present key, preserving explicit empty strings."""
    if not isinstance(obj, dict):
        return None
    for key in keys:
        if key in obj:
            return obj[key]
    return None


def _address(value):
    if isinstance(value, dict):
        return str(_field(value, "address", "hash", "pubkey", "id", "owner") or "").strip()
    return str(value or "").strip()


def _pct(value):
    # RugCheck topHolders.pct is already a percentage, not a 0..1 fraction.
    return _num(value)


def _holder_rows(report):
    for key in ("topHolders", "top_holders", "holders", "holderList"):
        items = report.get(key) if isinstance(report, dict) else None
        if isinstance(items, list) and items:
            return items
    return []


def _holder_pct_map(report):
    result = {}
    for item in _holder_rows(report):
        if not isinstance(item, dict):
            continue
        owner = _address(_field(item, "owner", "address", "wallet", "ownerAddress", "pubkey"))
        pct = _pct(_field(item, "pct", "percentage", "share", "ownershipPercentage", "percent"))
        if owner and pct is not None:
            result[owner] = max(result.get(owner, 0.0), pct)
    return result


def _graph_components(graph, case_sensitive=False):
    graph = _as_dict(_unwrap(graph))
    normalise = (lambda value: str(value).strip()) if case_sensitive else (lambda value: str(value).strip().lower())
    nodes = _as_list(_field(graph, "nodes", "vertices", "accounts"))
    links = _as_list(_field(graph, "links", "edges", "connections"))
    if not nodes:
        return set(), []
    ids = {}
    parents = {}
    for index, node in enumerate(nodes):
        if isinstance(node, dict):
            node_id = str(_field(node, "id", "key", "nodeId", "address", "pubkey", "wallet") or index)
            wallet = _address(_field(node, "address", "pubkey", "wallet", "owner", "account"))
        else:
            node_id, wallet = str(index), _address(node)
        ids[node_id] = normalise(wallet or node_id)
        parents[node_id] = node_id

    def find(x):
        x = str(x)
        if x not in parents:
            parents[x] = x
            ids.setdefault(x, x)
        while parents[x] != x:
            parents[x] = parents[parents[x]]
            x = parents[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parents[rb] = ra

    for link in links:
        if not isinstance(link, dict):
            continue
        a = _field(link, "source", "from", "src", "fromNode", "sourceId")
        b = _field(link, "target", "to", "dst", "toNode", "targetId")
        if isinstance(a, dict):
            a = _field(a, "id", "address", "pubkey")
        if isinstance(b, dict):
            b = _field(b, "id", "address", "pubkey")
        if a is not None and b is not None:
            union(a, b)

    groups = defaultdict(set)
    for node_id, wallet in ids.items():
        groups[find(node_id)].add(normalise(wallet))
    return set(x for group in groups.values() for x in group), list(groups.values())


def _cluster_metrics(report, graph, case_sensitive=False):
    normalise = (lambda value: str(value).strip()) if case_sensitive else (lambda value: str(value).strip().lower())
    holder_pcts_raw = _holder_pct_map(report)
    holder_pcts = {normalise(address): pct for address, pct in holder_pcts_raw.items()}
    graph_data = _as_dict(_unwrap(graph))
    graph_nodes = _as_list(_field(graph_data, "nodes", "vertices", "accounts"))
    for node in graph_nodes:
        if not isinstance(node, dict):
            continue
        owner = _address(_field(node, "address", "pubkey", "wallet", "owner", "account"))
        pct = _pct(_field(node, "pct", "percentage", "share", "ownershipPercentage", "percent"))
        if owner and pct is not None:
            owner_key = normalise(owner)
            holder_pcts[owner_key] = max(holder_pcts.get(owner_key, 0.0), pct)
    graph_addresses, components = _graph_components(graph, case_sensitive=case_sensitive)
    if not holder_pcts or not graph_addresses:
        return {
            "coverage_pct": None,
            "largest_linked_wallet_cluster_pct": None,
            "holder_count": len(holder_pcts),
            "graph_node_count": len(graph_addresses),
            "graph_edge_components": len(components),
        }
    matched = {
        address: pct for address, pct in holder_pcts.items()
        if normalise(address) in graph_addresses
    }
    coverage = min(100.0, sum(matched.values()))
    largest = max(
        (sum(matched.get(address, 0.0) for address in group) for group in components),
        default=0.0,
    )
    return {
        "coverage_pct": round(coverage, 4),
        "largest_linked_wallet_cluster_pct": round(min(100.0, largest), 4),
        "holder_count": len(holder_pcts),
        "graph_node_count": len(graph_addresses),
        "graph_edge_components": len(components),
    }


def _solana_rpc(timeout=8):
    return os.environ.get("SOLANA_RPC_URL", "").strip() or SOLANA_RPC_DEFAULT


def _solana_initial_creator_allocation(mint, creator, timeout=8):
    """Inspect the earliest bounded mint-only transaction, not current creator balance."""
    rpc = _solana_rpc(timeout)
    signatures, log = _rpc_post(
        rpc, "getSignaturesForAddress",
        [mint, {"limit": MAX_SOLANA_SIGNATURES, "commitment": "confirmed"}],
        timeout,
    )
    if not isinstance(signatures, list) or not signatures:
        return {"status": "UNVERIFIED", "reason": log.get("error") or "No mint transaction history"}
    if len(signatures) >= MAX_SOLANA_SIGNATURES:
        return {"status": "UNVERIFIED", "reason": "Mint history exceeds the 1,000-signature scan bound"}
    selected = None
    # RPC signatures are newest-first. Search oldest-first; InitializeMint
    # alone is not the allocation event, so look for the first transaction
    # with positive post-balance entries and no pre-existing balance for mint.
    for signature_row in list(reversed(signatures))[:MAX_SOLANA_OLDEST_TX_SCANS]:
        signature = signature_row.get("signature") if isinstance(signature_row, dict) else None
        if not signature:
            continue
        tx, tx_log = _rpc_post(
            rpc, "getTransaction",
            [signature, {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}],
            timeout,
        )
        if not isinstance(tx, dict) or not isinstance(tx.get("meta"), dict):
            continue
        meta = tx["meta"]
        pre_items = [x for x in _as_list(meta.get("preTokenBalances"))
                     if isinstance(x, dict) and x.get("mint") == mint]
        post_items = [x for x in _as_list(meta.get("postTokenBalances"))
                      if isinstance(x, dict) and x.get("mint") == mint]
        if pre_items or not post_items:
            continue
        allocations = {}
        for item in post_items:
            owner = str(item.get("owner") or "")
            amount = _raw_integer((_as_dict(item.get("uiTokenAmount"))).get("amount"))
            if owner and amount is not None and amount > 0:
                allocations[owner] = allocations.get(owner, 0) + amount
        if sum(allocations.values()) > 0:
            selected = (signature, allocations)
            break
    if selected is None:
        return {
            "status": "UNVERIFIED",
            "reason": "No mint-only initial allocation transaction found in the oldest 1,000 mint-account transactions",
        }
    first_sig, deltas = selected
    minted = sum(deltas.values())
    creator_pct = deltas.get(str(creator or ""), 0.0) / minted * 100.0 if creator else None
    if creator_pct is None:
        return {"status": "UNVERIFIED", "reason": "RugCheck creator identity is unavailable"}
    current_supply, supply_log = _rpc_post(
        rpc, "getTokenSupply", [mint, {"commitment": "confirmed"}], timeout)
    current_raw = _raw_integer(_as_dict(_as_dict(current_supply).get("value")).get("amount"))
    if current_raw is None or current_raw != int(minted):
        return {
            "status": "UNVERIFIED",
            "reason": "Earliest mint-only allocation does not exactly reconcile with current mint supply; later minting or burns may make the launch denominator incomplete.",
            "initial_mint_total_raw": str(int(minted)),
            "current_mint_supply_raw": str(current_raw) if current_raw is not None else None,
            "supply_rpc_error": supply_log.get("error"),
        }
    return {
        "status": "VERIFIED",
        "source": "Solana RPC getSignaturesForAddress/getTransaction earliest mint-only transaction",
        "as_of_utc": utc_now(),
        "first_mint_transaction": first_sig,
        "creator_address": creator,
        "deployer_initial_supply_pct": round(creator_pct, 6),
        "initial_mint_distribution_pct": {
            owner: round(amount / minted * 100.0, 6) for owner, amount in deltas.items()
        },
        "initial_mint_total_raw": str(int(minted)),
        "history_complete_within_limit": True,
        "initial_mint_total_reconciles_with_current_supply": True,
        "note": "Share is calculated from positive post-token-balance allocations in the earliest mint-only transaction, and is verified only when that transaction's allocation total exactly reconciles with the current mint supply.",
    }


def _raydium_pool(pool, timeout=8):
    url = RAYDIUM_ROOT + "/pools/info/ids?" + urllib.parse.urlencode({"ids": pool})
    data, log = _http_json(url, timeout)
    if not isinstance(data, dict) or data.get("success") is not True:
        return None, log
    payload = data.get("data")
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = _as_list(payload.get("data") or payload.get("items")) or [payload]
    else:
        rows = []
    # Raydium IDs are Solana public keys and are case-sensitive.
    match = next((x for x in rows if isinstance(x, dict) and
                  str(_field(x, "id", "poolId", "ammId", "pool_id", "address") or "") == pool), None)
    return match, log


def _solana_pool_identity(row, report, timeout=8):
    mint = str(row.get("contract_address") or row.get("token_address") or "")
    pool = str(row.get("pool_address") or row.get("pair_address") or "")
    chain = str(row.get("chain") or "").lower()
    raydium, ray_log = _raydium_pool(pool, timeout) if chain in {"solana", "sol"} else (None, None)
    if raydium:
        mint_a = _address(_field(raydium, "mintA", "mint_a", "baseMint"))
        mint_b = _address(_field(raydium, "mintB", "mint_b", "quoteMint"))
        lp_mint = _address(_field(raydium, "lpMint", "lp_mint", "lpTokenMint"))
        if mint and mint_a and mint_b and mint in {mint_a, mint_b} and lp_mint:
            return {
                "status": "VERIFIED", "source": "Raydium API v3 exact pool metadata",
                "as_of_utc": ray_log.get("retrieved_utc"),
                "chain": "solana", "contract_address": mint, "pool_address": pool,
                "quote_token_address": mint_b if mint_a == mint else mint_a,
                "lp_mint": lp_mint, "pool_program": _field(raydium, "programId", "program_id", "type"),
            }, raydium, ray_log

    markets = _as_list(_field(report, "markets"))
    for market in markets:
        if not isinstance(market, dict):
            continue
        market_id = str(_field(market, "pubkey", "id", "marketAddress", "market_address",
                               "poolAddress", "pool_address", "address") or "")
        if market_id != pool:
            continue
        mint_a = _address(_field(market, "mintA", "baseMint", "base_mint"))
        mint_b = _address(_field(market, "mintB", "quoteMint", "quote_mint"))
        if mint_a and mint_b and mint in {mint_a, mint_b}:
            lp = _as_dict(market.get("lp"))
            return {
                "status": "VERIFIED", "source": "RugCheck exact market identity",
                "as_of_utc": utc_now(), "chain": "solana",
                "contract_address": mint, "pool_address": pool,
                "quote_token_address": mint_b if mint_a == mint else mint_a,
                "lp_mint": _address(_field(market, "lpMint", "lp_mint")) or _address(_field(lp, "mint", "address")),
            }, market, None
    return {
        "status": "UNVERIFIED", "source": "RugCheck/Raydium public pool metadata",
        "as_of_utc": utc_now(), "chain": "solana", "contract_address": mint,
        "pool_address": pool, "reason": "No market record independently matched both the token mint and exact pool address.",
    }, raydium, ray_log


def _solana_lp_lock_share(pool, lp_mint, timeout=8):
    if not lp_mint:
        return {"status": "UNVERIFIED", "reason": "Exact pool LP mint was not returned"}
    rpc = _solana_rpc(timeout)
    supply, supply_log = _rpc_post(rpc, "getTokenSupply", [lp_mint, {"commitment": "confirmed"}], timeout)
    largest, largest_log = _rpc_post(rpc, "getTokenLargestAccounts", [lp_mint, {"commitment": "confirmed"}], timeout)
    if not isinstance(supply, dict) or not isinstance(largest, dict):
        return {"status": "UNVERIFIED", "reason": "Could not retrieve LP mint supply and largest accounts",
                "rpc_errors": [supply_log.get("error"), largest_log.get("error")]}
    supply_amount = _raw_integer((_as_dict(supply.get("value"))).get("amount"))
    accounts = _as_list(largest.get("value"))
    addresses = [str(x.get("address")) for x in accounts if isinstance(x, dict) and x.get("address")]
    if supply_amount is None or supply_amount <= 0 or not addresses:
        return {"status": "UNVERIFIED", "reason": "LP token supply or largest-holder account data is missing"}
    infos, info_log = _rpc_post(
        rpc, "getMultipleAccounts",
        [addresses, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        timeout,
    )
    values = _as_list(_as_dict(infos).get("value"))
    if not values or len(values) != len(addresses):
        return {"status": "UNVERIFIED", "reason": "LP token account ownership could not be resolved",
                "rpc_error": info_log.get("error")}
    dead_address_balance = 0
    incinerator_accounts = []
    for address, account in zip(addresses, values):
        parsed = _as_dict(_as_dict(_as_dict(_as_dict(account).get("data")).get("parsed")).get("info"))
        authority = str(parsed.get("owner") or "")
        if str(parsed.get("mint") or "") != lp_mint:
            return {"status": "UNVERIFIED", "reason": "LP token account mint did not match the verified pool LP mint"}
        token_amount = _raw_integer(_as_dict(parsed.get("tokenAmount")).get("amount"))
        if authority == SOLANA_INCINERATOR and token_amount is not None:
            dead_address_balance += token_amount
            incinerator_accounts.append(address)
    pct = dead_address_balance / supply_amount * 100.0
    if pct >= 95.0:
        return {
            "status": "VERIFIED", "source": "Solana RPC exact LP mint supply and largest token-account owners",
            "as_of_utc": utc_now(), "pool_address": pool, "lp_mint": lp_mint,
            "position_identity_verified": True, "control_type": "LOCKED",
            "locked_pct": round(pct, 6), "dead_address_share_pct": round(pct, 6),
            "lp_supply_raw": str(int(supply_amount)), "incinerator_token_accounts": incinerator_accounts,
            "note": "LP tokens held by Solana's canonical unspendable incinerator address are locked, not burned; sending tokens there does not reduce mint supply.",
            "coverage_limit": "At most 20 largest token accounts were queried; this share is a lower bound, not exhaustive proof of all locked LP tokens.",
        }
    return {
        "status": "UNVERIFIED", "source": "Solana RPC exact LP mint supply and largest token-account owners",
        "as_of_utc": utc_now(), "pool_address": pool, "lp_mint": lp_mint,
        "position_identity_verified": True, "control_type": "UNVERIFIED",
        "dead_address_share_pct": round(pct, 6),
        "reason": "Less than 95% of the exact LP mint supply is held by the canonical incinerator; no verified locker position was identified.",
        "limitation": "The largest-account view covers at most 20 accounts and does not by itself identify third-party lock programs.",
    }


def _risk_is_critical(risk):
    if not isinstance(risk, dict):
        return False
    level = str(_field(risk, "level", "severity", "riskLevel") or "").lower()
    name = str(_field(risk, "name", "risk", "description", "title") or "").lower()
    if level in {"danger", "critical", "high"}:
        return True
    critical_terms = (
        "honeypot", "freeze authority", "mint authority", "permanent delegate",
        "transfer hook", "cannot sell", "blacklist", "transfer fee", "mutable metadata",
        "creator control", "ownership reclaim", "selfdestruct",
    )
    return any(term in name for term in critical_terms)


def _solana_security(report, summary, retrieved_utc):
    token = _as_dict(_field(report, "token", "tokenInfo", "token_info"))
    mint = _as_dict(_field(report, "mint", "mintInfo", "mint_info"))
    token_meta = _as_dict(_field(report, "tokenMeta", "token_meta", "metadata"))
    risks = _as_list(_field(report, "risks")) or _as_list(_field(summary, "risks"))
    mint_keys = ("mintAuthority", "mint_authority")
    freeze_keys = ("freezeAuthority", "freeze_authority")
    mint_authority = _present_value(token, *mint_keys)
    if not any(key in token for key in mint_keys):
        mint_authority = _present_value(report, *mint_keys)
        if not any(key in report for key in mint_keys):
            mint_authority = _present_value(mint, *mint_keys)
    freeze_authority = _present_value(token, *freeze_keys)
    if not any(key in token for key in freeze_keys):
        freeze_authority = _present_value(report, *freeze_keys)
        if not any(key in report for key in freeze_keys):
            freeze_authority = _present_value(mint, *freeze_keys)
    token_program = _field(token, "tokenProgram", "token_program") or _field(mint, "tokenProgram", "token_program") or _field(report, "tokenProgram", "token_program") or _field(summary, "tokenProgram", "token_program")
    mutable = _field(token_meta, "mutable", "isMutable", "is_mutable")
    # Check Token-2022 extensions
    token_extensions = _as_dict(_field(report, "tokenExtensions", "token_extensions", "extensions"))
    has_transfer_fee = bool(_field(token_extensions, "transferFee", "transfer_fee", "fee"))
    has_permanent_delegate = bool(_field(token_extensions, "permanentDelegate", "permanent_delegate"))
    has_transfer_hook = bool(_field(token_extensions, "transferHook", "transfer_hook"))
    has_confidential_transfers = bool(_field(token_extensions, "confidentialTransfers", "confidential_transfers"))
    default_account_state = str(_field(token_extensions, "defaultAccountState", "default_account_state") or "").lower()
    is_default_frozen = "frozen" in default_account_state

    extension_risks = []
    if has_permanent_delegate:
        extension_risks.append({"name": "PermanentDelegateExtension", "level": "danger", "description": "Token has active permanent delegate able to seize/transfer funds"})
    if has_transfer_hook:
        extension_risks.append({"name": "TransferHookExtension", "level": "danger", "description": "Token uses external transfer hook capable of blocking sales"})
    if is_default_frozen:
        extension_risks.append({"name": "DefaultFrozenAccountExtension", "level": "danger", "description": "Token accounts frozen by default"})

    critical = [x for x in risks if _risk_is_critical(x)] + extension_risks
    mint_present = ("mintAuthority" in token or "mint_authority" in token or
                    "mintAuthority" in report or "mint_authority" in report or
                    "mintAuthority" in mint or "mint_authority" in mint)
    freeze_present = ("freezeAuthority" in token or "freeze_authority" in token or
                      "freezeAuthority" in report or "freeze_authority" in report or
                      "freezeAuthority" in mint or "freeze_authority" in mint)
    risks_present = isinstance(report.get("risks"), list) or isinstance(summary.get("risks"), list)
    mutable_present = "mutable" in token_meta or "isMutable" in token_meta or "is_mutable" in token_meta
    complete = bool((token or mint or (mint_present and freeze_present)) and
                    risks_present and mint_present and freeze_present and mutable_present)
    return {
        "chain": "solana", "source": "RugCheck",
        "retrieved_utc": retrieved_utc,
        "solana_security": {
            "status": "VERIFIED" if complete else "UNVERIFIED",
            "source": "RugCheck token report and summary",
            "as_of_utc": retrieved_utc,
            "mint_authority": mint_authority,
            "mint_authority_present": mint_present,
            "freeze_authority": freeze_authority,
            "freeze_authority_present": freeze_present,
            "token_program": token_program,
            "token_extensions": {
                "has_transfer_fee": has_transfer_fee,
                "has_permanent_delegate": has_permanent_delegate,
                "has_transfer_hook": has_transfer_hook,
                "has_confidential_transfers": has_confidential_transfers,
                "is_default_frozen": is_default_frozen,
            },
            "metadata_mutable": mutable,
            "metadata_mutability_present": mutable_present,
            "risks_field_present": risks_present,
            "risk_count": len(risks) + len(extension_risks),
            "critical_risks": critical,
            "risk_names": [
                str(_field(x, "name", "risk", "description", "title") or "UNNAMED_RISK")
                for x in (risks + extension_risks) if isinstance(x, dict)
            ],
        },
    }


def _rugcheck_url(path: str) -> str:
    key = os.environ.get("RUGCHECK_API_KEY", "").strip()
    base = RUGCHECK_ROOT + path
    if key:
        delim = "&" if "?" in base else "?"
        return f"{base}{delim}key={urllib.parse.quote(key)}"
    return base


def _rugcheck_enrich(row, route_item=None, timeout=8):
    mint = str(row.get("contract_address") or row.get("token_address") or "").strip()
    pool = str(row.get("pool_address") or row.get("pair_address") or "").strip()
    if not mint or not pool:
        return {"loss_guard_evidence": {}, "security_item": None,
                "summary": {"status": "UNVERIFIED", "reason": "Token mint or exact pool is missing"}}
    encoded = urllib.parse.quote(mint, safe="")
    report, report_log = _http_json(_rugcheck_url("/tokens/" + encoded + "/report"), timeout)
    summary, summary_log = _http_json(_rugcheck_url("/tokens/" + encoded + "/report/summary"), timeout)
    graph, graph_log = _http_json(_rugcheck_url("/tokens/" + encoded + "/insiders/graph"), timeout)
    networks, networks_log = _http_json(_rugcheck_url("/tokens/" + encoded + "/insiders/networks"), timeout)
    if not isinstance(report, dict):
        return {
            "loss_guard_evidence": {},
            "security_item": None,
            "summary": {
                "status": "UNVERIFIED",
                "reason": report_log.get("error") or "RugCheck token report unavailable",
                "provider_calls": [report_log, summary_log, graph_log, networks_log],
            },
        }

    retrieved = report_log.get("retrieved_utc") or utc_now()
    creator = _address(_field(report, "creator", "creatorAddress", "creator_address"))
    token = _as_dict(_field(report, "token", "tokenInfo", "token_info"))
    creator = creator or _address(_field(token, "creator", "creatorAddress", "creator_address"))
    identity, market, ray_log = _solana_pool_identity(row, report, timeout)
    lp_mint = _field(identity, "lp_mint")
    lp_position = _solana_lp_lock_share(pool, lp_mint, timeout) if identity.get("status") == "VERIFIED" else {
        "status": "UNVERIFIED", "reason": identity.get("reason") or "Exact pool identity not independently established",
    }
    graph_payload = _as_dict(graph)
    metrics = _cluster_metrics(report, graph if isinstance(graph, dict) else {}, case_sensitive=True)
    metrics["network_type"] = _field(graph_payload, "network_type", "networkType")
    metrics["network_account_cap"] = 2000
    metrics["network_cap_reached"] = len(_as_list(graph_payload.get("nodes"))) >= 2000
    if metrics.get("largest_linked_wallet_cluster_pct") is None and isinstance(networks, dict):
        network_rows = _as_list(_field(_as_dict(_unwrap(networks)), "networks", "items"))
        network_exposure = []
        holder_map = _holder_pct_map(report)
        for network_row in network_rows:
            if not isinstance(network_row, dict):
                continue
            members = _as_list(_field(network_row, "nodes", "wallets", "accounts", "members"))
            member_addresses = {_address(x) for x in members if _address(x)}
            pct = _pct(_field(network_row, "pct", "percentage", "tokenPct", "token_pct"))
            if pct is None:
                pct = sum(holder_map.get(x, 0.0) for x in member_addresses)
            if pct is not None:
                network_exposure.append(pct)
        if network_exposure:
            metrics["largest_linked_wallet_cluster_pct"] = round(min(100.0, max(network_exposure)), 4)
            metrics["network_source"] = "RugCheck insider networks endpoint"
    initial = _solana_initial_creator_allocation(mint, creator, timeout)
    launch = {
        "status": "VERIFIED" if (
            metrics.get("coverage_pct") is not None and metrics["coverage_pct"] >= 80.0 and
            metrics.get("largest_linked_wallet_cluster_pct") is not None and
            initial.get("status") == "VERIFIED" and
            initial.get("deployer_initial_supply_pct") is not None and
            not metrics.get("network_cap_reached")
        ) else "UNVERIFIED",
        "source": "RugCheck insider graph/top holders + Solana RPC earliest mint transaction",
        "cluster_semantics": "RugCheck insider-network linkage; a relationship signal, not proof of common beneficial ownership",
        "graph_network_type": metrics.get("network_type"),
        "graph_network_cap_reached": metrics.get("network_cap_reached"),
        "coverage_semantics": "Top-holder supply represented by graph-linked accounts; not a complete universe of all wallet ownership",
        "as_of_utc": retrieved,
        "coverage_pct": metrics.get("coverage_pct"),
        "deployer_initial_supply_pct": initial.get("deployer_initial_supply_pct"),
        "largest_linked_wallet_cluster_pct": metrics.get("largest_linked_wallet_cluster_pct"),
        "graph_metrics": metrics,
        "initial_mint_analysis": initial,
        "creator_address": creator,
        "reason": None,
    }
    if launch["status"] != "VERIFIED":
        launch["reason"] = "Wallet graph coverage, connected cluster or earliest mint transaction was incomplete."
    evidence = {
        "pool_identity": identity,
        "launch_wallet": launch,
        "lp_position": lp_position,
        "expected_notional_usd": _num((route_item or {}).get("notional_usd")),
        "rugcheck_security": _solana_security(report, _as_dict(summary), retrieved).get("solana_security"),
        "rugcheck_summary": {
            "lp_locked_pct_token_wide_only": _num(_field(_as_dict(summary), "lpLockedPct", "lp_locked_pct")),
            "score": _field(_as_dict(summary), "score", "score_normalised"),
            "risk_count": len(_as_list(_field(report, "risks"))),
        },
    }
    security = _solana_security(report, _as_dict(summary), retrieved)
    return {
        "loss_guard_evidence": evidence,
        "security_item": security,
        "summary": {
            "status": "COMPLETED" if launch.get("status") == "VERIFIED" and lp_position.get("status") == "VERIFIED" else "COMPLETED_WITH_GAPS",
            "provider": "RugCheck + Solana mainnet RPC + Raydium API v3 where applicable",
            "creator": creator,
            "lp_mint": lp_mint,
            "pool_identity_status": identity.get("status"),
            "wallet_status": launch.get("status"),
            "lp_position_status": lp_position.get("status"),
            "token_wide_lp_locked_pct_not_used_as_pool_proof": evidence["rugcheck_summary"]["lp_locked_pct_token_wide_only"],
            "provider_calls": [report_log, summary_log, graph_log, networks_log] + ([ray_log] if ray_log else []),
        },
    }


def _rpc_url_map():
    mapping = dict(EVM_RPC_DEFAULTS)
    raw = os.environ.get("CHAIN_RPC_URLS_JSON", "").strip()
    if raw:
        try:
            supplied = json.loads(raw)
            if isinstance(supplied, dict):
                mapping.update({str(k).lower(): str(v) for k, v in supplied.items() if v})
        except (TypeError, ValueError):
            pass
    return mapping


def _evm_rpc(chain, method, params, timeout=8):
    mapping = _rpc_url_map()
    url = mapping.get(str(chain or "").lower())
    if not url:
        return None, {"ok": False, "error": "No RPC configured for chain " + str(chain)}
    return _rpc_post(url, method, params, timeout)


def _eth_call(to, data, chain, timeout=8):
    result, log = _evm_rpc(chain, "eth_call", [{"to": to, "data": data}, "latest"], timeout)
    if not isinstance(result, str) or not result.startswith("0x") or len(result) < 66:
        return None, log
    try:
        return int(result, 16), log
    except ValueError:
        return None, log


def _evm_pool_identity(row, timeout=8):
    pool = str(row.get("pool_address") or row.get("pair_address") or "").lower()
    token = str(row.get("contract_address") or "").lower()
    chain = str(row.get("chain") or "").lower()
    if not (pool.startswith("0x") and len(pool) == 42 and token.startswith("0x") and len(token) == 42):
        return {"status": "UNVERIFIED", "reason": "EVM token/pool address format is invalid"}
    token0, log0 = _eth_call(pool, "0x0dfe1681", chain, timeout)
    token1, log1 = _eth_call(pool, "0xd21220a7", chain, timeout)
    if token0 is None or token1 is None:
        return {"status": "UNVERIFIED", "reason": "Pool token0/token1 could not be read from RPC",
                "rpc_errors": [log0.get("error"), log1.get("error")]}
    addr0 = "0x" + format(token0, "064x")[-40:]
    addr1 = "0x" + format(token1, "064x")[-40:]
    if token not in (addr0.lower(), addr1.lower()):
        return {"status": "UNVERIFIED", "reason": "Exact pool contract does not contain the candidate token"}
    quote = addr1 if addr0.lower() == token else addr0
    return {
        "status": "VERIFIED", "source": "EVM JSON-RPC pool token0/token1 calls",
        "as_of_utc": utc_now(), "chain": chain, "contract_address": token,
        "pool_address": pool, "quote_token_address": quote.lower(),
        "pool_token0": addr0.lower(), "pool_token1": addr1.lower(),
    }


def _evm_lp_lock_share(pool, chain, timeout=8):
    total, total_log = _eth_call(pool, "0x18160ddd", chain, timeout)
    if total is None or total <= 0:
        # Check if the pool is a concentrated-liquidity pool (e.g. Uniswap V3 slot0() selector: 0x3850c7bd)
        slot0_val, _ = _eth_call(pool, "0x3850c7bd", chain, timeout)
        if slot0_val is not None:
            return {
                "status": "UNVERIFIED",
                "lp_model": "CONCENTRATED_LIQUIDITY_NFT",
                "is_concentrated_liquidity": True,
                "reason": "Uniswap V3 / concentrated liquidity pool detected; liquidity positions are non-fungible ERC-721 NFTs. Conventional ERC-20 LP token burn check is not applicable. UNKNOWN must remain UNVERIFIED.",
            }
        return {
            "status": "UNVERIFIED",
            "lp_model": "UNKNOWN_POOL_MODEL",
            "is_concentrated_liquidity": False,
            "reason": "Pool did not expose a standard ERC-20 LP totalSupply(). UNKNOWN cannot become PASS.",
        }
    dead_address_balance = 0
    burn_balances = []
    for address in EVM_BURN_ADDRESSES:
        data = "0x70a08231" + address[2:].lower().rjust(64, "0")
        balance, log = _eth_call(pool, data, chain, timeout)
        burn_balances.append(balance)
        if balance is not None:
            dead_address_balance += balance
    if any(balance is None for balance in burn_balances):
        return {"status": "UNVERIFIED", "reason": "Could not read balanceOf for every canonical zero/dead address"}
    pct = dead_address_balance / total * 100.0
    if pct >= 95.0:
        return {
            "status": "VERIFIED", "source": "EVM JSON-RPC LP ERC-20 totalSupply/balanceOf",
            "as_of_utc": utc_now(), "pool_address": pool,
            "position_identity_verified": True, "control_type": "LOCKED",
            "locked_pct": round(pct, 6), "dead_address_share_pct": round(pct, 6),
            "lp_supply_raw": str(total),
            "note": "LP tokens held by canonical EVM zero/dead addresses are classified as locked, not burned. Verified only for V2-style LP ERC-20s whose pool token0/token1 also matched; other position models are not treated as locked.",
        }
    return {
        "status": "UNVERIFIED", "source": "EVM JSON-RPC LP ERC-20 totalSupply/balanceOf",
        "as_of_utc": utc_now(), "pool_address": pool,
        "position_identity_verified": True, "control_type": "UNVERIFIED",
        "dead_address_share_pct": round(pct, 6),
        "reason": "Less than 95% of the exact pool LP ERC-20 supply is at the canonical zero/dead addresses; no exact locker proof is available.",
    }


def _blockscout_page(base, path, params=None, timeout=8):
    url = base.rstrip("/") + path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    headers = {}
    if base.startswith(BLOCKSCOUT_PRO_API_ROOT + "/"):
        key = os.environ.get("BLOCKSCOUT_PRO_API_KEY", "").strip()
        if key:
            headers["Authorization"] = "Bearer " + key
    return _http_json(url, timeout, headers=headers)




BLOCKSCOUT_CHAIN_ALIASES = {
    "eth": ("ethereum", "ethereum mainnet"),
    "ethereum": ("ethereum", "ethereum mainnet"),
    "op": ("optimism", "op mainnet"),
    "optimism": ("optimism", "op mainnet"),
    "arbitrum": ("arbitrum", "arbitrum one"),
    "arbitrum_one": ("arbitrum one", "arbitrum"),
    "polygon": ("polygon", "polygon pos", "polygon mainnet"),
    "polygon_pos": ("polygon pos", "polygon", "polygon mainnet"),
    "avax": ("avalanche", "avalanche c-chain"),
    "avalanche": ("avalanche", "avalanche c-chain"),
    "gnosis": ("gnosis", "gnosis chain"),
    "bsc": ("bsc", "bnb smart chain", "binance smart chain"),
    "bnb": ("bnb smart chain", "binance smart chain", "bsc"),
    "zksync": ("zksync era", "zksync"),
    "zksync_era": ("zksync era", "zksync"),
    "opbnb": ("opbnb", "op bnb"),
    "worldchain": ("world chain", "worldchain"),
}


def _normalise_chain_name(value):
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _blockscout_pro_registry(timeout=8):
    """Dynamically discover Blockscout PRO chain IDs and mainnet names (no key needed)."""
    global _BLOCKSCOUT_PRO_REGISTRY_CACHE, _BLOCKSCOUT_PRO_REGISTRY_LOGS
    if _BLOCKSCOUT_PRO_REGISTRY_CACHE is not None:
        return _BLOCKSCOUT_PRO_REGISTRY_CACHE, list(_BLOCKSCOUT_PRO_REGISTRY_LOGS)
    config, config_log = _http_json(BLOCKSCOUT_PRO_CONFIG_URL, timeout)
    logs = [config_log]
    chain_map = {}
    cfg_chains = _as_dict(config.get("chains")) if isinstance(config, dict) else {}
    chain_ids = [str(x) for x in cfg_chains if str(x).isdigit()]
    if not chain_ids:
        _BLOCKSCOUT_PRO_REGISTRY_CACHE = {}
        _BLOCKSCOUT_PRO_REGISTRY_LOGS = logs
        return {}, logs
    for start in range(0, len(chain_ids), 100):
        ids = chain_ids[start:start + 100]
        url = BLOCKSCOUT_CHAINS_REGISTRY_URL + "?" + urllib.parse.urlencode({"chain_ids": ",".join(ids)})
        metadata, meta_log = _http_json(url, timeout)
        logs.append(meta_log)
        entries = {}
        if isinstance(metadata, dict):
            entries = metadata
        elif isinstance(metadata, list):
            entries = {
                str(_field(item, "id", "chainId", "chain_id")): item
                for item in metadata if isinstance(item, dict)
            }
        for chain_id in ids:
            item = _as_dict(entries.get(chain_id))
            config_item = _as_dict(cfg_chains.get(chain_id))
            if not item and not config_item:
                continue
            is_testnet = _explicit_bool(
                item.get("isTestnet") if "isTestnet" in item else item.get("is_testnet"))
            if is_testnet is True:
                continue
            names = []
            for source in (item, config_item):
                for field in ("name", "shortName", "short_name", "slug", "chain", "title", "network"):
                    if source.get(field):
                        names.append(str(source[field]))
            if not names:
                names.append(chain_id)
            chain_map[chain_id] = sorted(set(names))
    aliases_to_id = {}
    for chain_id, names in chain_map.items():
        for name in names:
            aliases_to_id[_normalise_chain_name(name)] = chain_id
    _BLOCKSCOUT_PRO_REGISTRY_CACHE = {
        "chain_ids": set(chain_ids),
        "chain_names": chain_map,
        "aliases_to_id": aliases_to_id,
    }
    _BLOCKSCOUT_PRO_REGISTRY_LOGS = logs
    return _BLOCKSCOUT_PRO_REGISTRY_CACHE, logs


def _blockscout_pro_chain_id(chain, timeout=8):
    """Resolve scanner chain slug against the current Blockscout mainnet registry."""
    registry, _ = _blockscout_pro_registry(timeout)
    if not registry:
        return None
    raw_custom = os.environ.get("BLOCKSCOUT_CHAIN_IDS_JSON", "").strip()
    if raw_custom:
        try:
            custom = json.loads(raw_custom)
            if isinstance(custom, dict):
                custom_id = str(custom.get(str(chain or "").lower(), "")).strip()
                if custom_id in registry["chain_names"]:
                    return custom_id
        except (TypeError, ValueError):
            pass
    normalized = _normalise_chain_name(chain)
    names = BLOCKSCOUT_CHAIN_ALIASES.get(str(chain or "").lower(), (str(chain or ""),))
    candidates = {_normalise_chain_name(x) for x in names}
    candidates.add(normalized)
    for candidate in candidates:
        chain_id = registry["aliases_to_id"].get(candidate)
        if chain_id:
            return chain_id
    return None


def _blockscout_base_for_chain(chain, timeout=8):
    """Prefer the dynamic PRO gateway when keyed; otherwise use a public instance."""
    key = os.environ.get("BLOCKSCOUT_PRO_API_KEY", "").strip()
    if key:
        chain_id = _blockscout_pro_chain_id(chain, timeout)
        if chain_id:
            return BLOCKSCOUT_PRO_API_ROOT + "/" + chain_id, "Blockscout PRO API"
    public = BLOCKSCOUT_HOSTS.get(str(chain or "").lower())
    if public:
        return public, "Blockscout public instance"
    return None, "No Blockscout index configured for this chain"

def _explicit_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    return None


def _service_address_label(account):
    if not isinstance(account, dict):
        return ""
    values = [account.get("name"), account.get("label"), account.get("metadata")]
    for key in ("public_tags", "private_tags", "tags", "labels"):
        values.extend(_as_list(account.get(key)))
    parts = []
    for value in values:
        if isinstance(value, dict):
            parts.extend(str(x) for x in value.values() if x not in (None, ""))
        elif value not in (None, ""):
            parts.append(str(value))
    return " ".join(parts).lower()


SERVICE_FUNDER_LABEL_TERMS = (
    "exchange", "cex", "deposit wallet", "hot wallet", "cold wallet",
    "bridge", "router", "dex", "market maker", "liquidity provider",
    "custodian", "faucet", "treasury", "vault", "staking contract",
)


def _blockscout_common_funder_graph(base, holder_rows, owner_pcts, timeout=8):
    """Build a bounded graph from complete earliest inbound native-coin transfers.

    This is a common-funding relationship signal, not proof of common control.
    Holder histories with a pagination cursor, missing EOA classification, no
    inbound native transfer, or a service-labeled funder are not promoted.
    """
    selected = [x for x in _as_list(holder_rows) if isinstance(x, dict)][:MAX_BLOCKSCOUT_COMMON_FUNDER_HOLDERS]
    logs = []
    results = []

    def inspect_holder(item):
        account = _as_dict(item.get("address"))
        holder = str(_field(account, "hash") or item.get("address_hash") or "").lower()
        pct = owner_pcts.get(holder)
        is_contract = _explicit_bool(account.get("is_contract"))
        if not holder or pct is None or is_contract is not False:
            return {"status": "UNVERIFIED", "holder": holder or None,
                    "reason": "Holder identity or explicit EOA classification is missing"}
        data, log = _blockscout_page(
            base, "/api/v2/addresses/" + holder + "/transactions",
            {"filter": "to"}, timeout,
        )
        txs = _as_list(_field(data, "items"))
        cursor = _field(data, "next_page_params")
        if not isinstance(data, dict) or cursor:
            return {"status": "UNVERIFIED", "holder": holder, "pct": pct,
                    "reason": "Inbound native-transaction history is incomplete or paginated",
                    "provider_log": log}
        incoming = []
        for tx in txs:
            if not isinstance(tx, dict):
                continue
            value = _raw_integer(tx.get("value"))
            from_obj = _as_dict(tx.get("from"))
            funder = str(_field(from_obj, "hash") or tx.get("from_hash") or "").lower()
            to_obj = _as_dict(tx.get("to"))
            recipient = str(_field(to_obj, "hash") or tx.get("to_hash") or "").lower()
            block = _raw_integer(_field(tx, "block_number", "blockNumber"))
            if value is None or value <= 0 or not funder or funder == holder:
                continue
            if recipient and recipient != holder:
                continue
            incoming.append((block if block is not None else 0, funder, from_obj, tx))
        if not incoming:
            return {"status": "UNVERIFIED", "holder": holder, "pct": pct,
                    "reason": "No direct inbound native-coin funding transaction found",
                    "provider_log": log}
        _, funder, funder_info, tx = min(incoming, key=lambda x: x[0])
        return {
            "status": "VERIFIED", "holder": holder, "pct": pct,
            "funder": funder, "funder_info": funder_info,
            "funding_tx": str(_field(tx, "hash", "transaction_hash") or ""),
            "funding_block": _field(tx, "block_number", "blockNumber"),
            "provider_log": log,
        }

    with ThreadPoolExecutor(max_workers=min(4, max(1, len(selected)))) as pool:
        for result in pool.map(inspect_holder, selected):
            results.append(result)
            if result.get("provider_log"):
                logs.append(result["provider_log"])

    funders = defaultdict(list)
    covered = 0.0
    for result in results:
        if result.get("status") != "VERIFIED":
            continue
        covered += float(result["pct"])
        funders[result["funder"]].append(result)

    shared_clusters = []
    for funder, members in funders.items():
        if len(members) < 2:
            continue
        info = _as_dict(members[0].get("funder_info"))
        is_contract = _explicit_bool(info.get("is_contract"))
        label = _service_address_label(info)
        if is_contract is None:
            detail, log = _blockscout_page(
                base, "/api/v2/addresses/" + funder, timeout=timeout)
            logs.append(log)
            detail = _as_dict(detail)
            is_contract = _explicit_bool(detail.get("is_contract"))
            label = label or _service_address_label(detail)
        if is_contract is not False:
            continue
        if any(term in label for term in SERVICE_FUNDER_LABEL_TERMS):
            continue
        cluster_pct = sum(float(member["pct"]) for member in members)
        shared_clusters.append({
            "funder_address": funder,
            "holder_count": len(members),
            "supply_pct": round(min(100.0, cluster_pct), 6),
            "holder_addresses": [member["holder"] for member in members],
            "funding_transactions": [member.get("funding_tx") for member in members],
            "classification": "shared_first_inbound_native_funder_eoa",
            "note": "A shared first-funder link is a risk relationship, not proof that wallets have the same beneficial owner.",
        })

    largest = max((x["supply_pct"] for x in shared_clusters), default=0.0)
    status = "VERIFIED" if covered >= 80.0 else "UNVERIFIED"
    return {
        "status": status,
        "source": "Blockscout v2 complete address inbound native-transaction histories",
        "as_of_utc": utc_now(),
        "coverage_pct": round(min(100.0, covered), 6),
        "largest_linked_wallet_cluster_pct": round(largest, 6),
        "holder_addresses_scanned": len(selected),
        "holder_addresses_with_complete_funder": len([x for x in results if x.get("status") == "VERIFIED"]),
        "shared_funder_cluster_count": len(shared_clusters),
        "shared_funder_clusters": sorted(shared_clusters, key=lambda x: x["supply_pct"], reverse=True)[:20],
        "holder_results": results,
        "provider_logs": logs,
        "semantics": "A cluster means multiple top-holder EOAs share the same earliest observed direct native-coin funder after excluding known service-labeled and contract funders. It is a relationship heuristic, not proof of common beneficial ownership.",
        "limitations": [
            "Only the first %d top-holder rows are examined." % MAX_BLOCKSCOUT_COMMON_FUNDER_HOLDERS,
            "A holder is counted only when Blockscout returns its complete inbound native-transaction history on one non-paginated response.",
            "Internal transactions and token-only funding are not included in this first pass.",
            "Unknown contract classifications and service-labeled funders are not counted as common-funder clusters.",
        ],
    }


def _blockscout_holder_history(chain, token, timeout=8):
    base, provider_name = _blockscout_base_for_chain(chain, timeout)
    if not base:
        return {"status": "UNVERIFIED", "reason": "No configured Blockscout public or PRO index for this chain",
                "provider": provider_name}, None
    token_data, token_log = _blockscout_page(base, "/api/v2/tokens/" + token, timeout=timeout)
    holders = []
    next_params = None
    holder_complete = False
    holder_logs = [token_log]
    for page in range(MAX_BLOCKSCOUT_HOLDER_PAGES):
        params = dict(next_params or {})
        data, log = _blockscout_page(base, "/api/v2/tokens/" + token + "/holders", params, timeout)
        holder_logs.append(log)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            break
        holders.extend(data["items"])
        next_params = data.get("next_page_params")
        if not next_params:
            holder_complete = True
            break
    detail = _as_dict(token_data)
    supply = _raw_integer(_field(detail, "total_supply", "totalSupply"))
    decimals = _num(detail.get("decimals"))
    if supply is None or supply <= 0:
        return {"status": "UNVERIFIED", "reason": "Blockscout token total supply missing",
                "holder_count": len(holders), "provider_logs": holder_logs}, None
    owner_pcts = {}
    for item in holders:
        if not isinstance(item, dict):
            continue
        account = _as_dict(item.get("address"))
        address = str(_field(account, "hash") or _field(item, "address_hash", "holder") or "").lower()
        value = _raw_integer(_field(item, "value", "balance"))
        if address and value is not None:
            owner_pcts[address] = max(owner_pcts.get(address, 0.0), value / supply * 100.0)
    transfer_rows = []
    transfer_next = None
    transfer_complete = False
    transfer_logs = []
    for page in range(MAX_BLOCKSCOUT_TRANSFER_PAGES):
        params = dict(transfer_next or {})
        data, log = _blockscout_page(
            base, "/api/v2/tokens/" + token + "/transfers", params, timeout)
        transfer_logs.append(log)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            break
        transfer_rows.extend(data["items"])
        transfer_next = data.get("next_page_params")
        if not transfer_next:
            transfer_complete = True
            break
        if len(transfer_rows) >= MAX_BLOCKSCOUT_TRANSFERS:
            break

    common_funder_graph = _blockscout_common_funder_graph(
        base, holders, owner_pcts, timeout=timeout)
    parents = {address: address for address in owner_pcts}
    graph_nodes = set()
    mint_events = []
    for item in transfer_rows:
        if not isinstance(item, dict):
            continue
        frm = str(_field(_as_dict(item.get("from")), "hash") or item.get("from_hash") or "").lower()
        to = str(_field(_as_dict(item.get("to")), "hash") or item.get("to_hash") or "").lower()
        if to:
            graph_nodes.add(to)
            parents.setdefault(to, to)
        if frm and frm != ZERO_ADDRESS:
            graph_nodes.add(frm)
            parents.setdefault(frm, frm)
            if to:
                a, b = frm, to
                while parents[a] != a:
                    parents[a] = parents[parents[a]]
                    a = parents[a]
                while parents[b] != b:
                    parents[b] = parents[parents[b]]
                    b = parents[b]
                if a != b:
                    parents[b] = a
        if frm == ZERO_ADDRESS and to:
            total = _as_dict(item.get("total"))
            value = _raw_integer(_field(total, "value") or item.get("value"))
            block = _num(_field(item, "block_number", "blockNumber"))
            tx_hash = str(_field(item, "transaction_hash", "tx_hash") or "")
            if value is not None:
                mint_events.append((block if block is not None else 0, tx_hash, to, value))

    def root(x):
        parents.setdefault(x, x)
        while parents[x] != x:
            parents[x] = parents[parents[x]]
            x = parents[x]
        return x

    components = defaultdict(set)
    for wallet in graph_nodes:
        components[root(wallet)].add(wallet)
    matched = {a: p for a, p in owner_pcts.items() if a in graph_nodes}
    coverage = sum(matched.values())
    largest = max((sum(matched.get(a, 0.0) for a in group) for group in components.values()), default=0.0)

    creator = _address(_field(detail, "creator_address_hash", "creatorAddressHash", "creator")).lower()
    deployer_initial_pct = None
    if mint_events and transfer_complete:
        first_block = min(x[0] for x in mint_events)
        first_events = [x for x in mint_events if x[0] == first_block]
        minted_first = sum(x[3] for x in first_events)
        creator_minted = sum(x[3] for x in first_events if x[2] == creator) if creator else None
        if minted_first > 0 and creator_minted is not None and minted_first == supply:
            deployer_initial_pct = creator_minted / supply * 100.0
    # Direct token-transfer edges are retained as a separate flow graph.
    # The qualified wallet cluster comes only from complete earliest inbound
    # native-funder histories, not token transfers.
    launch_ok = (
        common_funder_graph.get("status") == "VERIFIED" and
        deployer_initial_pct is not None
    )
    launch = {
        "status": "VERIFIED" if launch_ok else "UNVERIFIED",
        "provider": provider_name,
        "source": provider_name + " holder balances + common first-funder graph + complete ERC-20 mint history",
        "as_of_utc": utc_now(),
        "coverage_pct": common_funder_graph.get("coverage_pct"),
        "transfer_graph_holder_coverage_pct": round(min(100.0, coverage), 6) if graph_nodes else None,
        "deployer_initial_supply_pct": round(deployer_initial_pct, 6) if deployer_initial_pct is not None else None,
        "largest_linked_wallet_cluster_pct": common_funder_graph.get("largest_linked_wallet_cluster_pct"),
        "transfer_graph_largest_component_supply_pct": round(min(100.0, largest), 6) if graph_nodes else None,
        "common_funder_cluster_status": common_funder_graph.get("status"),
        "common_funder_graph": common_funder_graph,
        "coverage_semantics": common_funder_graph.get("semantics"),
        "creator_address": creator or None,
        "holder_count": len(owner_pcts),
        "holder_supply_coverage_pct": round(min(100.0, sum(owner_pcts.values())), 6),
        "transfer_rows_scanned": len(transfer_rows),
        "transfer_history_complete": transfer_complete,
        "holder_list_complete": holder_complete,
        "transfer_page_limit_reached": bool(transfer_next),
        "graph_type": "direct ERC-20 token-transfer links; connected does not mean common beneficial ownership",
        "provider_logs": holder_logs + transfer_logs + (
            _BLOCKSCOUT_PRO_REGISTRY_LOGS if provider_name == "Blockscout PRO API" else []),
    }
    if not launch_ok:
        launch["reason"] = ("Common-funder history did not cover 80% of supply or the first ERC-20 mint "
                            "allocation did not reconcile exactly with current total supply. "
                            "Incomplete and paginated holder histories are not inferred.")
    return launch, {"token": detail, "holders": len(holders), "logs": holder_logs + transfer_logs}


def _evm_enrich(row, route_item=None, timeout=8):
    chain = str(row.get("chain") or "").lower()
    token = str(row.get("contract_address") or "").lower()
    pool = str(row.get("pool_address") or row.get("pair_address") or "").lower()
    identity = _evm_pool_identity(row, timeout)
    launch, history_meta = _blockscout_holder_history(chain, token, timeout)
    lp = _evm_lp_lock_share(pool, chain, timeout) if identity.get("status") == "VERIFIED" else {
        "status": "UNVERIFIED", "reason": "Exact pool identity could not be independently verified",
    }
    evidence = {
        "pool_identity": identity,
        "launch_wallet": launch,
        "lp_position": lp,
        "expected_notional_usd": _num((route_item or {}).get("notional_usd")),
        "evm_holder_history": history_meta,
    }
    return {
        "loss_guard_evidence": evidence,
        "security_item": None,
        "summary": {
            "status": "COMPLETED" if launch.get("status") == "VERIFIED" and lp.get("status") == "VERIFIED" else "COMPLETED_WITH_GAPS",
            "provider": launch.get("provider") or "Blockscout v2 + read-only EVM JSON-RPC",
            "pool_identity_status": identity.get("status"),
            "wallet_status": launch.get("status"),
            "lp_position_status": lp.get("status"),
            "blockscout_history": history_meta,
        },
    }


def enrich_candidate(row, route_item=None, timeout=8):
    """Return source-attributed evidence; never convert absent provider data into a pass."""
    chain = str(row.get("chain") or "").lower()
    if chain in {"solana", "sol"}:
        return _rugcheck_enrich(row, route_item, timeout)
    if chain in BLOCKSCOUT_HOSTS:
        return _evm_enrich(row, route_item, timeout)
    return {
        "loss_guard_evidence": {
            "expected_notional_usd": _num((route_item or {}).get("notional_usd")),
        },
        "security_item": None,
        "summary": {
            "status": "UNVERIFIED",
            "provider": "No configured wallet/LP evidence adapter",
            "reason": "No chain-aware launch-wallet and LP-position adapter is currently implemented for " + chain,
        },
    }
