import pytest
from unittest.mock import patch, MagicMock

from scripts.dex_onchain_evidence import _solana_security, _evm_lp_lock_share
from scripts import v425_early_breakout as v425


class TestPhase5SecurityAndDiscovery:
    def test_solana_token_2022_permanent_delegate_flagged_critical(self):
        report = {
            "token": {"mintAuthority": None, "freezeAuthority": None},
            "tokenMeta": {"mutable": False},
            "risks": [],
            "tokenExtensions": {
                "permanentDelegate": "MaliciousDelegateAddress111111111111111111",
            },
        }
        res = _solana_security(report, {}, "2026-11-13T12:00:00Z")
        sec = res["solana_security"]
        assert sec["token_extensions"]["has_permanent_delegate"] is True
        assert sec["token_extensions"]["token_extensions_verification_method"] == "RUGCHECK_REPORT_PARSED"
        assert sec["token_extensions"]["direct_rpc_mint_deserialization"] is False
        # Critical risks must include the permanent delegate risk
        critical_names = [r["name"] for r in sec["critical_risks"]]
        assert "PermanentDelegateExtension" in critical_names

    def test_solana_token_2022_transfer_hook_flagged_critical(self):
        report = {
            "token": {"mintAuthority": None, "freezeAuthority": None},
            "tokenMeta": {"mutable": False},
            "risks": [],
            "tokenExtensions": {
                "transferHook": "HookProgramAddress111111111111111111111111",
            },
        }
        res = _solana_security(report, {}, "2026-11-13T12:00:00Z")
        sec = res["solana_security"]
        assert sec["token_extensions"]["has_transfer_hook"] is True
        assert sec["token_extensions"]["token_extensions_verification_method"] == "RUGCHECK_REPORT_PARSED"
        assert sec["token_extensions"]["direct_rpc_mint_deserialization"] is False
        critical_names = [r["name"] for r in sec["critical_risks"]]
        assert "TransferHookExtension" in critical_names

    def test_evm_concentrated_liquidity_model_detected_and_fails_closed(self):
        # When totalSupply() returns None, but slot0() returns value, detect Uniswap V3 concentrated liquidity
        def mock_eth_call(pool, data, chain, timeout=8):
            if data == "0x18160ddd":  # totalSupply()
                return None, {}
            if data == "0x3850c7bd":  # slot0()
                return 123456789, {}
            return None, {}

        with patch("scripts.dex_onchain_evidence._eth_call", side_effect=mock_eth_call):
            lp_res = _evm_lp_lock_share("0xPoolAddressUniswapV3", "base")
            assert lp_res["status"] == "UNVERIFIED"
            assert lp_res["lp_model"] == "CONCENTRATED_LIQUIDITY_NFT"
            assert lp_res["is_concentrated_liquidity"] is True
            assert lp_res["nft_position_ownership_audited"] is False
            assert "Uniswap V3" in lp_res["reason"]

    def test_http_429_retry_after_zero_enforces_minimum_backoff(self, tmp_path):
        client = v425.DexPaprikaClient(tmp_path)
        # Verify that Retry-After: 0 does not permit immediate repeat
        headers_zero = {"retry-after": "0"}

        # Mock urllib to raise HTTPError 429
        import urllib.error
        import io
        import json
        import time
        err_zero = urllib.error.HTTPError("http://test", 429, "Too Many Requests", headers_zero, io.BytesIO(b"rate limited"))

        with patch("urllib.request.urlopen", side_effect=err_zero), \
             patch("scripts.v425_early_breakout._reserve_request", return_value=(True, 1)) as mock_reserve:
            _, _, error_msg = client.get("/pools/search", family="search")
            assert "HTTP 429 Rate limited" in error_msg
            assert "15" in error_msg
            assert mock_reserve.call_count == 1

        # Verify persistent shared cooldown was recorded to disk
        cooldown_file = tmp_path / ".dexpaprika-cooldown.json"
        assert cooldown_file.exists()
        cooldown_data = json.loads(cooldown_file.read_text(encoding="utf-8"))
        assert cooldown_data["cooldown_duration_seconds"] >= 15.0
        assert cooldown_data["cooldown_until_epoch"] >= time.time() + 14.0

        # Now test that a subsequent call across ANY query family or client instance is blocked
        # BEFORE making any network call or reserving any requests
        client2 = v425.DexPaprikaClient(tmp_path)
        with patch("urllib.request.urlopen") as mock_urlopen, \
             patch("scripts.v425_early_breakout._reserve_request") as mock_reserve2:
            payload, headers, error2 = client2.get("/tokens/trending", family="trending")
            assert payload is None
            assert "provider cooldown active across processes" in error2
            # Crucial verification: zero network calls and zero request budget consumed
            mock_urlopen.assert_not_called()
            mock_reserve2.assert_not_called()

    def test_dexpaprika_discovery_reports_partial_coverage(self, tmp_path):
        hunter = MagicMock()
        hunter.candidates = []
        hunter.source_status = []

        mock_payload_p1 = {
            "results": [{"id": "pool_1", "chain": "base"}],
            "has_next_page": True,
        }
        mock_payload_p2 = {
            "results": [{"id": "pool_2", "chain": "base"}],
            "has_next_page": False,
        }

        with patch("scripts.v425_early_breakout._memory_root", return_value=tmp_path), \
             patch("scripts.v425_early_breakout._load_registry", return_value=([{"id": "base", "display_name": "Base"}], {"registry_available": True})), \
             patch("scripts.v425_early_breakout.POOL_QUERIES", [("momentum_5m", {"order_by": "change_5m_desc"})]), \
             patch.object(v425.DexPaprikaClient, "get", side_effect=[
                 (mock_payload_p1, {"x-api-plan": "free"}, None),
                 (mock_payload_p2, {"x-api-plan": "free"}, None),
             ]), \
             patch("scripts.v425_early_breakout._normalize_pool", side_effect=lambda item, f, r, fn: {"id": item["id"], "chain": "base"}):

            res = v425._collect_dexpaprika(hunter, MagicMock())
            assert res["coverage_status"] == "PARTIAL"
            assert res["pages_retrieved"] == 2
            assert res["records_discovered"] == 2
            assert len(hunter.candidates) == 2
            assert "never an exhaustive census" in res["coverage_boundary"]
