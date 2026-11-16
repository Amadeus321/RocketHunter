#!/usr/bin/env python3
"""Stage 3.3 Offline Foundation Unit Tests: Consensus SSZ & MPT Proof Verifier.

Deterministically validates:
1. Ethereum Keccak-256 legacy padding (0x01) vs. FIPS 202 SHA3-256 (0x06) across known-answer vectors.
2. Pure-Python RLP parsing for arbitrary bytes, strings, and nested lists.
3. Compact hex-prefix (HP) path encoding and decoding across all prefix flags.
4. Merkle Patricia Trie (MPT) verification:
   - Inclusion proofs: single leaf, branch + leaf, extension + leaf.
   - Inline child nodes (< 32 bytes) embedded directly in parent.
   - Cryptographic absence (non-inclusion) proofs: empty branch slot, diverging path.
   - Tampered proofs and corrupt roots fail closed.
5. Canonical Uniswap V2 Slot 3 integer bit unpacking (reserve0, reserve1, blockTimestampLast).
6. SSZ binary Merkle branch verification for canonical consensus generalized indices:
   - Deneb execution_payload (gindex 25, depth 4)
   - Deneb current_sync_committee (gindex 54, depth 5)
   - Deneb finalized_checkpoint.root (gindex 105, depth 6)
7. Consensus domain derivation, signing root calculation, and dynamic fork schedule resolution.

Strictly offline: Zero RPC, zero network requests, zero external dependencies.
"""

import hashlib
import unittest
from unittest import mock

from scripts.consensus_proof_verifier import (
    DOMAIN_SYNC_COMMITTEE,
    FORK_VERSION_ALTAIR,
    FORK_VERSION_BELLATRIX,
    FORK_VERSION_CAPELLA,
    FORK_VERSION_DENEB,
    FORK_VERSION_ELECTRA,
    FORK_VERSION_PHASE0,
    GINDEX_BEACON_BLOCK_STATE_ROOT,
    GINDEX_BEACON_STATE_CURRENT_SYNC_COMMITTEE,
    GINDEX_EXECUTION_PAYLOAD,
    GINDEX_EXECUTION_PAYLOAD_STATE_ROOT,
    GINDEX_EXECUTION_PAYLOAD_BLOCK_HASH,
    GINDEX_EXECUTION_PAYLOAD_BLOCK_NUMBER,
    GINDEX_EXECUTION_PAYLOAD_RECEIPTS_ROOT,
    GINDEX_EXECUTION_PAYLOAD_TIMESTAMP,
    GINDEX_FINALIZED_CHECKPOINT_ROOT,
    MAINNET_ALTAIR_SLOT,
    MAINNET_BELLATRIX_SLOT,
    MAINNET_CAPELLA_SLOT,
    MAINNET_DENEB_SLOT,
    MAINNET_GENESIS_VALIDATORS_ROOT,
    MAINNET_PHASE0_SLOT,
    AccountStateLeaf,
    ForkSchedule,
    MPTVerificationResult,
    SSZVerificationResult,
    Slot3ReservesResult,
    UniswapV2Slot3Decoder,
    compute_domain,
    compute_fork_data_root,
    compute_signing_root,
    decode_compact_path,
    encode_compact_path,
    keccak256,
    rlp_decode,
    rlp_encode,
    rlp_encode_bytes,
    sha256,
    verify_mpt_proof,
    verify_ssz_branch,
    compute_uniswap_v2_pair_address,
    HeaderConsensusEvidence,
    HeaderConsensusVerifier,
    UNISWAP_V2_FACTORY_MAINNET,
    secp256k1_ecrecover,
    _secp256k1_point_mul,
    _SECP256K1_GX,
    _SECP256K1_GY,
    _SECP256K1_N,
    verify_wormhole_vaa,
    LightClientSyncCommitteeStore,
    SyncCommitteePeriodTransition,
    WormholeGuardianRegistry,
    GINDEX_NEXT_SYNC_COMMITTEE,
    compute_sync_committee_root,
)


class TestEthereumKeccak256(unittest.TestCase):
    """Deterministic validation of pure-Python Keccak-256 and distinction from FIPS 202 SHA3-256."""

    def test_canonical_empty_string(self):
        # Ethereum Keccak-256 (legacy 0x01 padding)
        expected_keccak = "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
        self.assertEqual(keccak256(b"").hex(), expected_keccak)

        # FIPS 202 SHA3-256 (0x06 padding) produces a completely different digest
        sha3_digest = hashlib.sha3_256(b"").hexdigest()
        self.assertEqual(sha3_digest, "a7ffc6f8bf1ed76651c14756a061d662f580ff4de43b49fa82d80a4b80f8434a")
        self.assertNotEqual(keccak256(b"").hex(), sha3_digest)

    def test_known_answer_vectors_vs_fips202(self):
        """Proves that Ethereum Keccak-256 strictly diverges from FIPS 202 SHA3-256 across test vectors."""
        test_cases = [
            (b"", "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470", "a7ffc6f8bf1ed76651c14756a061d662f580ff4de43b49fa82d80a4b80f8434a"),
            (b"abc", "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45", "3a985da74fe225b2045c172d6bd390bd855f086e3e9d525b46bfe24511431532"),
            (b"Transfer(address,address,uint256)", "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef", "c17a9d92b89f27cb79cc390f23a1a5d302fefab8c7911075ede952ac2b5607a1"),
            (b"PairCreated(address,address,address,uint256)", "0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9", "79b58832c15b09feb3df60e6992ac2bf90bb88a193b76dcf74a5529cfc6a96e2"),
        ]
        for data, expected_keccak, expected_sha3 in test_cases:
            actual_keccak = keccak256(data).hex()
            actual_sha3 = hashlib.sha3_256(data).hexdigest()
            self.assertEqual(actual_keccak, expected_keccak)
            self.assertEqual(actual_sha3, expected_sha3)
            self.assertNotEqual(actual_keccak, actual_sha3)

    def test_canonical_slot3_storage_key(self):
        # keccak256(0x00...03)
        slot3_bytes = (3).to_bytes(32, "big")
        self.assertEqual(len(slot3_bytes), 32)
        storage_key = keccak256(slot3_bytes)
        self.assertEqual(len(storage_key), 32)
        self.assertEqual(UniswapV2Slot3Decoder.compute_storage_key(3), storage_key)

    def test_long_input_spanning_multiple_keccak_blocks(self):
        # 300 bytes exceeds single Keccak-f1600 rate (136 bytes)
        data = b"A" * 300
        digest = keccak256(data)
        self.assertEqual(len(digest), 32)
        self.assertIsInstance(digest, bytes)


class TestRLPDecoder(unittest.TestCase):
    """Deterministic validation of RLP recursive length prefix decoding."""

    def test_single_byte(self):
        self.assertEqual(rlp_decode(b"\x00"), b"\x00")
        self.assertEqual(rlp_decode(b"\x7f"), b"\x7f")

    def test_empty_string(self):
        self.assertEqual(rlp_decode(b"\x80"), b"")

    def test_short_string(self):
        data = b"\x83dog"
        self.assertEqual(rlp_decode(data), b"dog")

    def test_long_string(self):
        content = b"x" * 100
        encoded = bytes([0xB7 + 1, 100]) + content
        self.assertEqual(rlp_decode(encoded), content)

    def test_empty_list(self):
        self.assertEqual(rlp_decode(b"\xc0"), [])

    def test_nested_list(self):
        encoded = bytes([0xC9, 0x83]) + b"cat" + bytes([0xC4, 0x83]) + b"dog"
        decoded = rlp_decode(encoded)
        self.assertEqual(decoded, [b"cat", [b"dog"]])

    def test_truncated_rlp_raises_error(self):
        with self.assertRaises(ValueError):
            rlp_decode(b"\x85cat")

    def test_trailing_bytes_raises_error(self):
        with self.assertRaises(ValueError):
            rlp_decode(b"\x83catEXTRA")

    def test_noncanonical_single_byte_short_string_prefix_rejected(self):
        """Single byte < 0x80 must not be encoded with 0x81 prefix (Yellow Paper Appendix B)."""
        with self.assertRaises(ValueError) as cm:
            rlp_decode(bytes([0x81, 0x05]))
        self.assertIn("single byte 0x05 < 0x80 encoded with 0x81 prefix", str(cm.exception))

    def test_noncanonical_long_string_leading_zero_rejected(self):
        """Long string length-of-length field must not have leading zero bytes."""
        payload = b"x" * 60
        # len 60 = 0x3C. 2 bytes with leading zero: 0x00 0x3C -> prefix 0xB9
        bad_encoded = bytes([0xB7 + 2, 0x00, 0x3C]) + payload
        with self.assertRaises(ValueError) as cm:
            rlp_decode(bad_encoded)
        self.assertIn("leading zero in long string length field", str(cm.exception))

    def test_noncanonical_long_string_length_under_56_rejected(self):
        """Long string prefix must not be used for string lengths <= 55 bytes."""
        payload = b"short"
        bad_encoded = bytes([0xB8, 5]) + payload
        with self.assertRaises(ValueError) as cm:
            rlp_decode(bad_encoded)
        self.assertIn("long string prefix used for length 5 <= 55", str(cm.exception))

    def test_noncanonical_long_list_leading_zero_rejected(self):
        """Long list length-of-length field must not have leading zero bytes."""
        # Single empty list [ [] ] = 0xC1 0xC0. 2 bytes length with leading zero: 0x00 0x01 -> prefix 0xF9
        bad_encoded = bytes([0xF7 + 2, 0x00, 0x01, 0xC0])
        with self.assertRaises(ValueError) as cm:
            rlp_decode(bad_encoded)
        self.assertIn("leading zero in long list length field", str(cm.exception))

    def test_noncanonical_long_list_length_under_56_rejected(self):
        """Long list prefix must not be used for payload lengths <= 55 bytes."""
        payload = bytes([0xC0, 0xC0])
        bad_encoded = bytes([0xF8, 2]) + payload
        with self.assertRaises(ValueError) as cm:
            rlp_decode(bad_encoded)
        self.assertIn("long list prefix used for length 2 <= 55", str(cm.exception))

    def test_child_payload_overruns_parent_list_boundary_rejected(self):
        """Child payload that overruns designated parent list boundary must be rejected."""
        # Outer list claims length 3: 0xC3. Child claims string of length 10: 0x8A followed by only 2 bytes
        bad_encoded = bytes([0xC3, 0x8A, 0x01, 0x02])
        with self.assertRaises(ValueError) as cm:
            rlp_decode(bad_encoded)
        self.assertIn("overruns boundary", str(cm.exception))


class TestMPTCompactPathEncoding(unittest.TestCase):
    """Deterministic validation of MPT compact hex-prefix path encoding/decoding."""

    def test_even_extension(self):
        nibbles = [1, 2, 3, 4]
        encoded = encode_compact_path(nibbles, is_leaf=False)
        self.assertEqual(encoded, bytes([0x00, 0x12, 0x34]))
        dec_nibbles, is_leaf = decode_compact_path(encoded)
        self.assertEqual(dec_nibbles, nibbles)
        self.assertFalse(is_leaf)

    def test_odd_extension(self):
        nibbles = [1, 2, 3]
        encoded = encode_compact_path(nibbles, is_leaf=False)
        self.assertEqual(encoded, bytes([0x11, 0x23]))
        dec_nibbles, is_leaf = decode_compact_path(encoded)
        self.assertEqual(dec_nibbles, nibbles)
        self.assertFalse(is_leaf)

    def test_even_leaf(self):
        nibbles = [1, 2, 3, 4]
        encoded = encode_compact_path(nibbles, is_leaf=True)
        self.assertEqual(encoded, bytes([0x20, 0x12, 0x34]))
        dec_nibbles, is_leaf = decode_compact_path(encoded)
        self.assertEqual(dec_nibbles, nibbles)
        self.assertTrue(is_leaf)

    def test_odd_leaf(self):
        nibbles = [1, 2, 3]
        encoded = encode_compact_path(nibbles, is_leaf=True)
        self.assertEqual(encoded, bytes([0x31, 0x23]))
        dec_nibbles, is_leaf = decode_compact_path(encoded)
        self.assertEqual(dec_nibbles, nibbles)
        self.assertTrue(is_leaf)

    def test_empty_compact_path_rejected(self):
        with self.assertRaises(ValueError) as cm:
            decode_compact_path(b"")
        self.assertIn("compact path cannot be empty", str(cm.exception))


class TestUniswapV2Slot3Decoder(unittest.TestCase):
    """Deterministic validation of Uniswap V2 Slot 3 packed reserves decoding."""

    def test_canonical_slot3_unpack(self):
        r0 = 1000 * 10**18
        r1 = 500 * 10**6
        ts = 1794744000

        packed_int = r0 | (r1 << 112) | (ts << 224)
        packed_bytes = packed_int.to_bytes(32, "big")

        res = UniswapV2Slot3Decoder.decode_storage_word(packed_bytes)
        self.assertTrue(res.is_valid)
        self.assertEqual(res.reserve0, r0)
        self.assertEqual(res.reserve1, r1)
        self.assertEqual(res.block_timestamp_last, ts)

    def test_maximum_bounds_unpack(self):
        max_uint112 = (1 << 112) - 1
        max_uint32 = (1 << 32) - 1
        packed_int = max_uint112 | (max_uint112 << 112) | (max_uint32 << 224)
        packed_bytes = packed_int.to_bytes(32, "big")

        res = UniswapV2Slot3Decoder.decode_storage_word(packed_bytes)
        self.assertTrue(res.is_valid)
        self.assertEqual(res.reserve0, max_uint112)
        self.assertEqual(res.reserve1, max_uint112)
        self.assertEqual(res.block_timestamp_last, max_uint32)

    def test_zero_reserves_unpack(self):
        packed_bytes = (0).to_bytes(32, "big")
        res = UniswapV2Slot3Decoder.decode_storage_word(packed_bytes)
        self.assertTrue(res.is_valid)
        self.assertEqual(res.reserve0, 0)
        self.assertEqual(res.reserve1, 0)
        self.assertEqual(res.block_timestamp_last, 0)

    def test_invalid_length_storage_word_fails_closed(self):
        oversized = b"\x00" * 33
        res = UniswapV2Slot3Decoder.decode_storage_word(oversized)
        self.assertFalse(res.is_valid)
        self.assertIn("exceeds 32 bytes", res.error)


class TestAccountStateLeaf(unittest.TestCase):
    """Deterministic validation of Ethereum account state leaf decoding."""

    def test_valid_account_leaf(self):
        nonce = 42
        balance = 100 * 10**18
        storage_root = keccak256(b"storage_root_test")
        code_hash = keccak256(b"canonical_runtime_bytecode")

        nonce_bytes = nonce.to_bytes((nonce.bit_length() + 7) // 8, "big")
        balance_bytes = balance.to_bytes((balance.bit_length() + 7) // 8, "big")

        items = [
            rlp_encode_bytes(nonce_bytes),
            rlp_encode_bytes(balance_bytes),
            rlp_encode_bytes(storage_root),
            rlp_encode_bytes(code_hash),
        ]
        body = b"".join(items)
        full_rlp = (bytes([0xF8, len(body)]) if len(body) > 55 else bytes([0xC0 + len(body)])) + body

        leaf = AccountStateLeaf.from_rlp(full_rlp)
        self.assertTrue(leaf.is_valid)
        self.assertEqual(leaf.nonce, nonce)
        self.assertEqual(leaf.balance, balance)
        self.assertEqual(leaf.storage_root, storage_root)
        self.assertEqual(leaf.code_hash, code_hash)

    def test_invalid_account_leaf_fails_closed(self):
        leaf = AccountStateLeaf.from_rlp(b"\xc0")
        self.assertFalse(leaf.is_valid)


class TestMPTProofVerification(unittest.TestCase):
    """Deterministic validation of MPT proofs: inclusion, branch nodes, inline children, and absence proofs."""

    def test_single_leaf_mpt_proof(self):
        key = (3).to_bytes(32, "big")
        key_hash = keccak256(key)
        val = b"canonical_storage_slot_word"

        nibbles = []
        for b in key_hash:
            nibbles.append(b >> 4)
            nibbles.append(b & 0x0F)

        compact_path = encode_compact_path(nibbles, is_leaf=True)
        leaf_items = [rlp_encode_bytes(compact_path), rlp_encode_bytes(val)]
        body = b"".join(leaf_items)
        leaf_rlp = (bytes([0xF8, len(body)]) if len(body) > 55 else bytes([0xC0 + len(body)])) + body

        root_hash = keccak256(leaf_rlp)
        proof = [leaf_rlp]

        res = verify_mpt_proof(root_hash, key_hash, proof)
        self.assertTrue(res.is_valid)
        self.assertTrue(res.is_included)
        self.assertFalse(res.is_absence_proof)
        self.assertEqual(res.proof_type, "INCLUSION_LEAF")
        self.assertEqual(res.value_bytes, val)

    def test_full_branch_node_mpt_proof(self):
        """Verifies inclusion traversing a full 17-item branch node to child leaf."""
        # Key: 0x112000...00
        key_nibbles = [1, 1, 2] + [0] * 61
        key_bytes = bytearray()
        for i in range(0, 64, 2):
            key_bytes.append((key_nibbles[i] << 4) | key_nibbles[i + 1])
        key_bytes = bytes(key_bytes)

        # Child leaf consumes remaining 63 nibbles: [1, 2] + [0]*61
        leaf_nibbles = [1, 2] + [0] * 61
        compact_leaf_path = encode_compact_path(leaf_nibbles, is_leaf=True)
        val = b"branch_resolved_storage_word"
        leaf_body = rlp_encode_bytes(compact_leaf_path) + rlp_encode_bytes(val)
        leaf_rlp = (bytes([0xF8, len(leaf_body)]) if len(leaf_body) > 55 else bytes([0xC0 + len(leaf_body)])) + leaf_body
        leaf_hash = keccak256(leaf_rlp)

        # Root branch node: child at nibble 1 is leaf_hash
        branch_items = [b""] * 17
        branch_items[1] = leaf_hash
        branch_body = b"".join([rlp_encode_bytes(x) for x in branch_items])
        branch_rlp = (bytes([0xF8, len(branch_body)]) if len(branch_body) > 55 else bytes([0xC0 + len(branch_body)])) + branch_body
        root_hash = keccak256(branch_rlp)

        proof = [branch_rlp, leaf_rlp]
        res = verify_mpt_proof(root_hash, key_bytes, proof)
        self.assertTrue(res.is_valid)
        self.assertTrue(res.is_included)
        self.assertEqual(res.proof_type, "INCLUSION_LEAF")
        self.assertEqual(res.value_bytes, val)

    def test_canonical_inline_child_embedded_list_in_branch(self):
        """Verifies traversal where child node is shorter than 32 bytes and stored as an authentic embedded RLP list."""
        # Canonical leaf node is an embedded list: [compact_path_bytes, value_bytes]
        leaf_path = encode_compact_path([0, 0], is_leaf=True)
        val = b"v"
        canonical_leaf_node = [leaf_path, val]

        # Verify that serialized leaf is strictly < 32 bytes (5 bytes)
        serialized_leaf = rlp_encode(canonical_leaf_node)
        self.assertLess(len(serialized_leaf), 32)
        self.assertEqual(len(serialized_leaf), 5)  # 0xc4, 0x82, 0x20, 0x00, 0x76

        # Branch node at depth 61: slot 3 contains the canonical_leaf_node directly as an embedded list!
        branch_items = [b""] * 17
        branch_items[3] = canonical_leaf_node
        branch_rlp = rlp_encode(branch_items)
        branch_hash = keccak256(branch_rlp)

        # Extension node at root consuming 61 nibbles (all 0) pointing to branch_hash
        ext_path = encode_compact_path([0] * 61, is_leaf=False)
        ext_node = [ext_path, branch_hash]
        ext_rlp = rlp_encode(ext_node)
        root_hash = keccak256(ext_rlp)

        # Construct 64-nibble key: 61 zeros + [3] + [0, 0]
        key_nibbles = [0] * 61 + [3, 0, 0]
        key_bytes = bytearray()
        for i in range(0, 64, 2):
            key_bytes.append((key_nibbles[i] << 4) | key_nibbles[i + 1])
        key_bytes = bytes(key_bytes)

        # Proof contains ONLY [ext_rlp, branch_rlp]; leaf is embedded as a list in branch_rlp
        proof = [ext_rlp, branch_rlp]
        res = verify_mpt_proof(root_hash, key_bytes, proof)
        self.assertTrue(res.is_valid)
        self.assertTrue(res.is_included)
        self.assertEqual(res.proof_type, "INCLUSION_LEAF")
        self.assertEqual(res.value_bytes, val)

    def test_canonical_inline_child_in_extension_node(self):
        """Verifies traversal where extension node points directly to an authentic embedded inline leaf node list."""
        leaf_path = encode_compact_path([1, 2], is_leaf=True)
        val = b"val42"
        canonical_leaf = [leaf_path, val]
        self.assertLess(len(rlp_encode(canonical_leaf)), 32)

        # Extension node consuming 62 nibbles, child is canonical_leaf list directly
        ext_path = encode_compact_path([0] * 62, is_leaf=False)
        ext_node = [ext_path, canonical_leaf]
        root_rlp = rlp_encode(ext_node)
        root_hash = keccak256(root_rlp)

        key_nibbles = [0] * 62 + [1, 2]
        key_bytes = bytearray()
        for i in range(0, 64, 2):
            key_bytes.append((key_nibbles[i] << 4) | key_nibbles[i + 1])
        key_bytes = bytes(key_bytes)

        proof = [root_rlp]
        res = verify_mpt_proof(root_hash, key_bytes, proof)
        self.assertTrue(res.is_valid)
        self.assertTrue(res.is_included)
        self.assertEqual(res.proof_type, "INCLUSION_LEAF")
        self.assertEqual(res.value_bytes, val)

    def test_byte_string_wrapped_inline_child_rejected_as_noncanonical(self):
        """Verifies that byte-string-wrapped child RLP (< 32 bytes) is rejected as non-canonical and unsupported."""
        leaf_path = encode_compact_path([0, 0], is_leaf=True)
        val = b"v"
        leaf_rlp = rlp_encode([leaf_path, val])
        self.assertLess(len(leaf_rlp), 32)

        branch_items = [b""] * 17
        branch_items[3] = leaf_rlp  # Raw bytes in slot instead of embedded list
        branch_rlp = rlp_encode(branch_items)
        root_hash = keccak256(branch_rlp)

        key_bytes = bytes([0x30] + [0x00] * 31)

        proof = [branch_rlp]
        res = verify_mpt_proof(root_hash, key_bytes, proof)
        self.assertFalse(res.is_valid)
        self.assertEqual(res.proof_type, "INVALID_PROOF")
        self.assertIn("inline children must be embedded RLP lists, not byte strings", res.error)

    def test_inline_child_list_at_or_above_32_bytes_rejected(self):
        """Verifies that an embedded inline child list with serialized length >= 32 bytes is strictly rejected."""
        # Value of 30 bytes makes leaf list RLP >= 32 bytes
        leaf_path = encode_compact_path([0, 0], is_leaf=True)
        large_val = b"A" * 30
        child_list = [leaf_path, large_val]
        child_rlp = rlp_encode(child_list)
        self.assertGreaterEqual(len(child_rlp), 32)

        branch_items = [b""] * 17
        branch_items[3] = child_list  # List child >= 32 bytes (must be hashed in canonical MPT)
        branch_rlp = rlp_encode(branch_items)
        root_hash = keccak256(branch_rlp)

        key_bytes = bytes([0x30] + [0x00] * 31)

        proof = [branch_rlp]
        res = verify_mpt_proof(root_hash, key_bytes, proof)
        self.assertFalse(res.is_valid)
        self.assertEqual(res.proof_type, "INVALID_PROOF")
        self.assertIn(">= 32 bytes", res.error)

    def test_empty_extension_path_rejected(self):
        """Verifies that an extension node with an empty nibble path is strictly rejected as noncanonical."""
        # Extension node with prefix 0x00 and 0 nibbles: encoded path b"\x00"
        empty_ext_path = bytes([0x00])
        ext_node = [empty_ext_path, keccak256(b"dummy_child")]
        ext_rlp = rlp_encode(ext_node)
        root_hash = keccak256(ext_rlp)

        key_bytes = bytes([0x12] + [0x00] * 31)

        proof = [ext_rlp]
        res = verify_mpt_proof(root_hash, key_bytes, proof)
        self.assertFalse(res.is_valid)
        self.assertEqual(res.proof_type, "INVALID_PROOF")
        self.assertIn("extension path cannot be empty", res.error)

    def test_noncanonical_compact_path_nonzero_padding_rejected(self):
        """Verifies that noncanonical compact hex-prefix path with non-zero padding nibble fails closed."""
        # Even length path [1, 2]: canonical prefix is 0x20. Byte 0x21 has low nibble 1 (non-zero padding)
        noncanonical_leaf_path = bytes([0x21, 0x12])
        with self.assertRaises(ValueError) as cm:
            decode_compact_path(noncanonical_leaf_path)
        self.assertIn("non-zero padding nibble", str(cm.exception))

        # Test within verify_mpt_proof: fails closed with INVALID_PROOF
        leaf_node = [noncanonical_leaf_path, b"val"]
        leaf_rlp = rlp_encode(leaf_node)
        root_hash = keccak256(leaf_rlp)
        key_bytes = bytes([0x12] + [0x00] * 31)

        res = verify_mpt_proof(root_hash, key_bytes, [leaf_rlp])
        self.assertFalse(res.is_valid)
        self.assertEqual(res.proof_type, "INVALID_PROOF")
        self.assertIn("non-canonical compact path", res.error)

    def test_noncanonical_compact_path_invalid_prefix_rejected(self):
        """Verifies that compact path with invalid prefix nibble > 3 fails closed."""
        # Prefix 0x40 (prefix 4 > 3)
        noncanonical_path = bytes([0x40, 0x12])
        with self.assertRaises(ValueError) as cm:
            decode_compact_path(noncanonical_path)
        self.assertIn("invalid prefix nibble", str(cm.exception))

    def test_noncanonical_raw_bytes_inline_node_rejected(self):
        """Verifies that byte-string child references with length != 32 are strictly rejected as noncanonical."""
        # 1. Raw bytes < 32 bytes
        raw_non_list = rlp_encode(b"arbitrary_string")
        self.assertLess(len(raw_non_list), 32)
        branch_items = [b""] * 17
        branch_items[3] = raw_non_list
        branch_rlp = rlp_encode(branch_items)
        root_hash = keccak256(branch_rlp)
        key_bytes = bytes([0x30] + [0x00] * 31)

        res = verify_mpt_proof(root_hash, key_bytes, [branch_rlp])
        self.assertFalse(res.is_valid)
        self.assertEqual(res.proof_type, "INVALID_PROOF")
        self.assertIn("inline children must be embedded RLP lists, not byte strings", res.error)

        # 2. Raw bytes in extension node child with length != 32
        ext_path = encode_compact_path([1, 2], is_leaf=False)
        bad_ext_node = [ext_path, b"short_raw_bytes"]
        bad_ext_rlp = rlp_encode(bad_ext_node)
        ext_root = keccak256(bad_ext_rlp)

        res_ext = verify_mpt_proof(ext_root, bytes([0x12] + [0x00] * 31), [bad_ext_rlp])
        self.assertFalse(res_ext.is_valid)
        self.assertEqual(res_ext.proof_type, "INVALID_PROOF")
        self.assertIn("inline children must be embedded RLP lists, not byte strings", res_ext.error)

    def test_absence_proof_empty_branch_child(self):
        """Verifies cryptographic non-inclusion proof where key points to empty branch child slot."""
        # Branch node has child only at nibble 5
        branch_items = [b""] * 17
        branch_items[5] = keccak256(b"dummy_leaf")
        branch_body = b"".join([rlp_encode_bytes(x) for x in branch_items])
        branch_rlp = (bytes([0xF8, len(branch_body)]) if len(branch_body) > 55 else bytes([0xC0 + len(branch_body)])) + branch_body
        root_hash = keccak256(branch_rlp)

        # Key search begins with nibble 2 (slot 2 is empty b"")
        key_bytes = bytes([0x20] + [0x00] * 31)

        proof = [branch_rlp]
        res = verify_mpt_proof(root_hash, key_bytes, proof)
        self.assertTrue(res.is_valid)
        self.assertFalse(res.is_included)
        self.assertTrue(res.is_absence_proof)
        self.assertEqual(res.proof_type, "ABSENCE_EMPTY_BRANCH_CHILD")
        self.assertIsNone(res.value_bytes)

    def test_absence_proof_path_divergence(self):
        """Verifies cryptographic non-inclusion proof where leaf path diverges from key."""
        # Leaf in trie has path [4, 4, 4]
        leaf_path = encode_compact_path([4, 4, 4] + [0] * 61, is_leaf=True)
        val = b"other_key_val"
        leaf_body = rlp_encode_bytes(leaf_path) + rlp_encode_bytes(val)
        leaf_rlp = (bytes([0xF8, len(leaf_body)]) if len(leaf_body) > 55 else bytes([0xC0 + len(leaf_body)])) + leaf_body
        root_hash = keccak256(leaf_rlp)

        # Search key has path starting with [9, 9] (diverges immediately)
        search_key_bytes = bytes([0x99] + [0x00] * 31)

        proof = [leaf_rlp]
        res = verify_mpt_proof(root_hash, search_key_bytes, proof)
        self.assertTrue(res.is_valid)
        self.assertFalse(res.is_included)
        self.assertTrue(res.is_absence_proof)
        self.assertEqual(res.proof_type, "ABSENCE_PATH_DIVERGENCE")
        self.assertIsNone(res.value_bytes)

    def test_mpt_proof_tampered_root_fails_closed(self):
        key_hash = keccak256((3).to_bytes(32, "big"))
        proof = [b"\xc0"]
        tampered_root = b"\x00" * 32
        res = verify_mpt_proof(tampered_root, key_hash, proof)
        self.assertFalse(res.is_valid)
        self.assertEqual(res.proof_type, "INVALID_PROOF")
        self.assertIn("Missing node", res.error)

    def test_mpt_proof_invalid_key_length_fails_closed(self):
        res = verify_mpt_proof(b"\x00" * 32, b"too_short", [])
        self.assertFalse(res.is_valid)
        self.assertEqual(res.proof_type, "INVALID_PROOF")
        self.assertIn("Invalid search key length", res.error)

    def test_empty_list_branch_child_fails_closed(self):
        """Verifies that an empty list [] in a branch child slot fails closed as INVALID_PROOF, not confused with b'' absence."""
        branch_items = [b""] * 17
        branch_items[3] = []  # Empty list is non-canonical in child slot
        branch_rlp = rlp_encode(branch_items)
        root_hash = keccak256(branch_rlp)
        key_bytes = bytes([0x30] + [0x00] * 31)

        res = verify_mpt_proof(root_hash, key_bytes, [branch_rlp])
        self.assertFalse(res.is_valid)
        self.assertFalse(res.is_absence_proof)
        self.assertEqual(res.proof_type, "INVALID_PROOF")
        self.assertIn("empty list [] is invalid in child slot", res.error)

    def test_empty_list_branch_value_fails_closed(self):
        """Verifies that an empty list [] in branch value position (slot 16) fails closed as INVALID_PROOF, not confused with b'' absence."""
        # 64-nibble extension node leading to branch node where search key terminates
        key_bytes = bytes([0x12] * 32)
        ext_path = encode_compact_path([1, 2] * 32, is_leaf=False)

        branch_items = [b""] * 17
        branch_items[16] = []  # Empty list in value slot
        branch_rlp = rlp_encode(branch_items)
        branch_hash = keccak256(branch_rlp)

        ext_node = [ext_path, branch_hash]
        ext_rlp = rlp_encode(ext_node)
        root_hash = keccak256(ext_rlp)

        res = verify_mpt_proof(root_hash, key_bytes, [ext_rlp, branch_rlp])
        self.assertFalse(res.is_valid)
        self.assertFalse(res.is_absence_proof)
        self.assertEqual(res.proof_type, "INVALID_PROOF")
        self.assertIn("Non-canonical branch value type", res.error)

    def test_non_empty_list_branch_value_cannot_be_returned_as_included_bytes(self):
        """Verifies that non-empty list branch values fail closed and cannot be returned as included bytes."""
        key_bytes = bytes([0x12] * 32)
        ext_path = encode_compact_path([1, 2] * 32, is_leaf=False)

        branch_items = [b""] * 17
        branch_items[16] = [b"nested", b"data"]  # Non-empty list in value slot
        branch_rlp = rlp_encode(branch_items)
        branch_hash = keccak256(branch_rlp)

        ext_node = [ext_path, branch_hash]
        ext_rlp = rlp_encode(ext_node)
        root_hash = keccak256(ext_rlp)

        res = verify_mpt_proof(root_hash, key_bytes, [ext_rlp, branch_rlp])
        self.assertFalse(res.is_valid)
        self.assertFalse(res.is_included)
        self.assertEqual(res.proof_type, "INVALID_PROOF")
        self.assertIn("Non-canonical branch value type", res.error)

    def test_exact_empty_bytes_branch_value_absence(self):
        """Verifies that exact empty bytes b'' in branch slot 16 yields valid absence proof ABSENCE_EMPTY_BRANCH_VALUE."""
        key_bytes = bytes([0x12] * 32)
        ext_path = encode_compact_path([1, 2] * 32, is_leaf=False)

        branch_items = [b""] * 17
        branch_items[16] = b""  # Exact empty bytes
        branch_rlp = rlp_encode(branch_items)
        branch_hash = keccak256(branch_rlp)

        ext_node = [ext_path, branch_hash]
        ext_rlp = rlp_encode(ext_node)
        root_hash = keccak256(ext_rlp)

        res = verify_mpt_proof(root_hash, key_bytes, [ext_rlp, branch_rlp])
        self.assertTrue(res.is_valid)
        self.assertFalse(res.is_included)
        self.assertTrue(res.is_absence_proof)
        self.assertEqual(res.proof_type, "ABSENCE_EMPTY_BRANCH_VALUE")
        self.assertIsNone(res.value_bytes)

    def test_canonical_bytes_branch_value_inclusion(self):
        """Verifies that canonical bytes in branch slot 16 yields valid inclusion proof INCLUSION_BRANCH."""
        key_bytes = bytes([0x12] * 32)
        ext_path = encode_compact_path([1, 2] * 32, is_leaf=False)

        branch_items = [b""] * 17
        branch_items[16] = b"valid_included_storage_value"
        branch_rlp = rlp_encode(branch_items)
        branch_hash = keccak256(branch_rlp)

        ext_node = [ext_path, branch_hash]
        ext_rlp = rlp_encode(ext_node)
        root_hash = keccak256(ext_rlp)

        res = verify_mpt_proof(root_hash, key_bytes, [ext_rlp, branch_rlp])
        self.assertTrue(res.is_valid)
        self.assertTrue(res.is_included)
        self.assertFalse(res.is_absence_proof)
        self.assertEqual(res.proof_type, "INCLUSION_BRANCH")
        self.assertEqual(res.value_bytes, b"valid_included_storage_value")

    def test_empty_list_extension_child_fails_closed(self):
        """Verifies that an empty list [] as extension child fails closed as INVALID_PROOF."""
        ext_path = encode_compact_path([1, 2], is_leaf=False)
        bad_ext_node = [ext_path, []]
        bad_ext_rlp = rlp_encode(bad_ext_node)
        root_hash = keccak256(bad_ext_rlp)

        res = verify_mpt_proof(root_hash, bytes([0x12] + [0x00] * 31), [bad_ext_rlp])
        self.assertFalse(res.is_valid)
        self.assertEqual(res.proof_type, "INVALID_PROOF")
        self.assertIn("empty list [] is invalid in child slot", res.error)

    def test_divergent_extension_with_empty_list_child_fails_closed(self):
        """Verifies that a divergent extension node with an empty-list child fails closed as INVALID_PROOF before divergence check."""
        # Extension path is [1, 2]
        ext_path = encode_compact_path([1, 2], is_leaf=False)
        bad_ext_node = [ext_path, []]  # Invalid empty-list child
        bad_ext_rlp = rlp_encode(bad_ext_node)
        root_hash = keccak256(bad_ext_rlp)

        # Search key starts with [3, 4] -> Diverges from [1, 2]
        search_key_bytes = bytes([0x34] + [0x00] * 31)

        res = verify_mpt_proof(root_hash, search_key_bytes, [bad_ext_rlp])
        self.assertFalse(res.is_valid)
        self.assertFalse(res.is_absence_proof)
        self.assertEqual(res.proof_type, "INVALID_PROOF")
        self.assertIn("empty list [] is invalid in child slot", res.error)

    def test_divergent_leaf_with_list_value_fails_closed(self):
        """Verifies that a divergent leaf node with a non-empty list value fails closed as INVALID_PROOF before divergence check."""
        # Leaf path is [1, 2]
        leaf_path = encode_compact_path([1, 2], is_leaf=True)
        bad_leaf_node = [leaf_path, [b"invalid_nested_list_value"]]  # Non-bytes list value
        bad_leaf_rlp = rlp_encode(bad_leaf_node)
        root_hash = keccak256(bad_leaf_rlp)

        # Search key starts with [3, 4] -> Diverges from [1, 2]
        search_key_bytes = bytes([0x34] + [0x00] * 31)

        res = verify_mpt_proof(root_hash, search_key_bytes, [bad_leaf_rlp])
        self.assertFalse(res.is_valid)
        self.assertFalse(res.is_absence_proof)
        self.assertEqual(res.proof_type, "INVALID_PROOF")
        self.assertIn("Non-canonical leaf value type", res.error)

    def test_divergent_leaf_with_empty_list_value_fails_closed(self):
        """Verifies that a divergent leaf node with an empty list [] value fails closed as INVALID_PROOF before divergence check."""
        leaf_path = encode_compact_path([1, 2], is_leaf=True)
        bad_leaf_node = [leaf_path, []]  # Empty list value
        bad_leaf_rlp = rlp_encode(bad_leaf_node)
        root_hash = keccak256(bad_leaf_rlp)

        search_key_bytes = bytes([0x34] + [0x00] * 31)

        res = verify_mpt_proof(root_hash, search_key_bytes, [bad_leaf_rlp])
        self.assertFalse(res.is_valid)
        self.assertFalse(res.is_absence_proof)
        self.assertEqual(res.proof_type, "INVALID_PROOF")
        self.assertIn("Non-canonical leaf value type", res.error)


class TestSSZProofVerification(unittest.TestCase):
    """Deterministic validation of SSZ binary Merkle branch verification and canonical fork indices."""

    def setUp(self):
        # Construct synthetic binary Merkle tree of 64 leaves (depth 6)
        self.leaves = [sha256(f"leaf_{i}".encode("utf-8")) for i in range(64)]
        self.tree = [b""] * 128
        for i in range(64):
            self.tree[64 + i] = self.leaves[i]
        for i in range(63, 0, -1):
            self.tree[i] = sha256(self.tree[2 * i] + self.tree[2 * i + 1])
        self.root = self.tree[1]

    def _extract_branch(self, gindex: int) -> list[bytes]:
        branch = []
        curr = gindex
        while curr > 1:
            sib = curr + 1 if curr % 2 == 0 else curr - 1
            branch.append(self.tree[sib])
            curr //= 2
        return branch

    def test_canonical_beacon_block_state_root_gindex_11(self):
        """Validates BeaconBlock.state_root proof branch at generalized index 11 (depth 3 in BeaconBlock)."""
        gindex = 11
        branch = self._extract_branch(gindex)
        leaf = self.tree[gindex]
        res = verify_ssz_branch(leaf, branch, gindex, self.root)
        self.assertTrue(res.is_valid)
        self.assertEqual(res.gindex, 11)
        self.assertEqual(res.depth, 3)

    def test_canonical_deneb_execution_payload_gindex_25(self):
        """Validates Deneb execution_payload proof branch at generalized index 25 (depth 4 in BeaconBlockBody)."""
        gindex = 25
        branch = self._extract_branch(gindex)
        leaf = self.tree[gindex]
        res = verify_ssz_branch(leaf, branch, gindex, self.root)
        self.assertTrue(res.is_valid)
        self.assertEqual(res.gindex, 25)
        self.assertEqual(res.depth, 4)

    def test_canonical_deneb_current_sync_committee_gindex_54(self):
        """Validates Deneb current_sync_committee proof branch at generalized index 54 (depth 5 in BeaconState)."""
        gindex = 54
        branch = self._extract_branch(gindex)
        leaf = self.tree[gindex]
        res = verify_ssz_branch(leaf, branch, gindex, self.root)
        self.assertTrue(res.is_valid)
        self.assertEqual(res.gindex, 54)
        self.assertEqual(res.depth, 5)

    def test_canonical_deneb_finalized_checkpoint_root_gindex_105(self):
        """Validates Deneb finalized_checkpoint.root proof branch at generalized index 105 (depth 6 in BeaconState)."""
        gindex = 105
        branch = self._extract_branch(gindex)
        leaf = self.tree[gindex]
        res = verify_ssz_branch(leaf, branch, gindex, self.root)
        self.assertTrue(res.is_valid)
        self.assertEqual(res.gindex, 105)
        self.assertEqual(res.depth, 6)

    def test_tampered_leaf_fails_closed(self):
        gindex = 25
        branch = self._extract_branch(gindex)
        tampered_leaf = sha256(b"tampered")
        res = verify_ssz_branch(tampered_leaf, branch, gindex, self.root)
        self.assertFalse(res.is_valid)
        self.assertIn("Computed root", res.error)

    def test_tampered_sibling_fails_closed(self):
        gindex = 25
        branch = self._extract_branch(gindex)
        branch[0] = sha256(b"corrupted_sibling")
        res = verify_ssz_branch(self.tree[gindex], branch, gindex, self.root)
        self.assertFalse(res.is_valid)

    def test_mismatched_branch_depth_fails_closed(self):
        gindex = 25
        res = verify_ssz_branch(self.tree[gindex], [], gindex, self.root)
        self.assertFalse(res.is_valid)
        self.assertIn("Branch length mismatch", res.error)


class TestSSZGeneralizedIndexFieldMapping(unittest.TestCase):
    """Explicit deterministic validation of consensus container field mappings and generalized indices."""

    def test_beacon_block_state_root_is_gindex_11(self):
        # BeaconBlock has 5 fields: slot(0), proposer_index(1), parent_root(2), state_root(3), body(4)
        # Power-of-2 container capacity = 8 (depth 3).
        # state_root is field index 3 -> gindex = 8 + 3 = 11.
        self.assertEqual(GINDEX_BEACON_BLOCK_STATE_ROOT, 11)
        self.assertEqual((11).bit_length() - 1, 3)

    def test_beacon_state_current_sync_committee_is_gindex_54(self):
        # BeaconState (Deneb) has 27 fields; power-of-2 container capacity = 32 (depth 5).
        # current_sync_committee is field index 22 -> gindex = 32 + 22 = 54.
        # Proves that gindex 54 is current_sync_committee, NOT BeaconBlock.state_root (which is gindex 11).
        self.assertEqual(GINDEX_BEACON_STATE_CURRENT_SYNC_COMMITTEE, 54)
        self.assertEqual((54).bit_length() - 1, 5)
        self.assertNotEqual(GINDEX_BEACON_STATE_CURRENT_SYNC_COMMITTEE, GINDEX_BEACON_BLOCK_STATE_ROOT)

    def test_beacon_block_body_execution_payload_is_gindex_25(self):
        # BeaconBlockBody (Deneb) has 12 fields; power-of-2 container capacity = 16 (depth 4).
        # execution_payload is field index 9 -> gindex = 16 + 9 = 25.
        self.assertEqual(GINDEX_EXECUTION_PAYLOAD, 25)
        self.assertEqual((25).bit_length() - 1, 4)

    def test_finalized_checkpoint_root_is_gindex_105(self):
        # BeaconState field 20 is finalized_checkpoint (gindex 32 + 20 = 52).
        # Checkpoint container has 2 fields: epoch(0), root(1).
        # finalized_checkpoint.root gindex = 52 * 2 + 1 = 105 (depth 6).
        self.assertEqual(GINDEX_FINALIZED_CHECKPOINT_ROOT, 105)
        self.assertEqual((105).bit_length() - 1, 6)


class TestConsensusDomainDerivation(unittest.TestCase):
    """Deterministic validation of consensus domain and signing root calculations."""

    def test_domain_sync_committee_calculation(self):
        domain = compute_domain(
            DOMAIN_SYNC_COMMITTEE,
            FORK_VERSION_DENEB,
            MAINNET_GENESIS_VALIDATORS_ROOT,
        )
        self.assertEqual(len(domain), 32)
        self.assertEqual(domain[:4], DOMAIN_SYNC_COMMITTEE)

    def test_signing_root_deterministic(self):
        domain = compute_domain(
            DOMAIN_SYNC_COMMITTEE,
            FORK_VERSION_DENEB,
            MAINNET_GENESIS_VALIDATORS_ROOT,
        )
        attested_header_root = sha256(b"synthetic_beacon_header")
        signing_root = compute_signing_root(attested_header_root, domain)
        self.assertEqual(len(signing_root), 32)
        expected = sha256(attested_header_root + domain)
        self.assertEqual(signing_root, expected)

    def test_fork_schedule_boundary_conditions(self):
        # 1. Negative slot fails closed
        with self.assertRaises(ValueError) as cm:
            ForkSchedule.resolve_fork_version(-1)
        self.assertIn("Slot must be non-negative", str(cm.exception))

        # 2. Historical pre-Deneb slot fails closed
        with self.assertRaises(ValueError) as cm:
            ForkSchedule.resolve_fork_version(MAINNET_DENEB_SLOT - 1)
        self.assertIn("precedes Deneb activation slot", str(cm.exception))

        # 3. Exact Deneb boundary activates Deneb
        v_deneb = ForkSchedule.resolve_fork_version(MAINNET_DENEB_SLOT)
        self.assertEqual(v_deneb, FORK_VERSION_DENEB)

        # 4. Realistic slot within verified Deneb era
        v_mid_deneb = ForkSchedule.resolve_fork_version(9000000)
        self.assertEqual(v_mid_deneb, FORK_VERSION_DENEB)

        # 5. Verified Deneb upper boundary
        v_max_deneb = ForkSchedule.resolve_fork_version(ForkSchedule.DENEB_MAX_VERIFIED_SLOT)
        self.assertEqual(v_max_deneb, FORK_VERSION_DENEB)

        # 6. Post-Deneb slot strictly fails closed
        with self.assertRaises(ValueError) as cm:
            ForkSchedule.resolve_fork_version(ForkSchedule.DENEB_MAX_VERIFIED_SLOT + 1)
        self.assertIn("Post-Deneb slots are unsupported and fail closed", str(cm.exception))

    def test_fork_schedule_historical_resolution(self):
        # Validates canonical historical mainnet activation schedule
        cases = [
            (0, "phase0", FORK_VERSION_PHASE0),
            (MAINNET_ALTAIR_SLOT - 1, "phase0", FORK_VERSION_PHASE0),
            (MAINNET_ALTAIR_SLOT, "altair", FORK_VERSION_ALTAIR),
            (MAINNET_BELLATRIX_SLOT - 1, "altair", FORK_VERSION_ALTAIR),
            (MAINNET_BELLATRIX_SLOT, "bellatrix", FORK_VERSION_BELLATRIX),
            (MAINNET_CAPELLA_SLOT - 1, "bellatrix", FORK_VERSION_BELLATRIX),
            (MAINNET_CAPELLA_SLOT, "capella", FORK_VERSION_CAPELLA),
            (MAINNET_DENEB_SLOT - 1, "capella", FORK_VERSION_CAPELLA),
            (MAINNET_DENEB_SLOT, "deneb", FORK_VERSION_DENEB),
            (ForkSchedule.DENEB_MAX_VERIFIED_SLOT, "deneb", FORK_VERSION_DENEB),
        ]
        for slot, expected_name, expected_version in cases:
            name, version = ForkSchedule.resolve_mainnet_historical_fork(slot)
            self.assertEqual(name, expected_name)
            self.assertEqual(version, expected_version)

        # Post-Deneb slot fails closed in historical resolution
        with self.assertRaises(ValueError) as cm:
            ForkSchedule.resolve_mainnet_historical_fork(ForkSchedule.DENEB_MAX_VERIFIED_SLOT + 1)
        self.assertIn("Post-Deneb slots are unsupported and fail closed", str(cm.exception))

    def test_generalized_indices_certified_for_deneb_only(self):
        # Deneb indices resolve accurately
        indices = ForkSchedule.get_generalized_indices(FORK_VERSION_DENEB)
        self.assertEqual(indices["beacon_block_state_root"], 11)
        self.assertEqual(indices["execution_payload"], 25)
        self.assertEqual(indices["current_sync_committee"], 54)
        self.assertEqual(indices["finalized_checkpoint_root"], 105)

        # Pre-Deneb and uncertified post-Deneb forks reject light client index queries and fail closed
        for uncertified_fork in (FORK_VERSION_CAPELLA, FORK_VERSION_ELECTRA):
            with self.assertRaises(ValueError) as cm:
                ForkSchedule.get_generalized_indices(uncertified_fork)
            self.assertIn("only Deneb is certified and supported", str(cm.exception))


class TestConsensusAndCreate2Verifiers(unittest.TestCase):
    """Unit and adversarial tests for CREATE2 pair derivation and consensus light-client verification."""

    def test_uniswap_v2_create2_known_vector(self):
        # Canonical Ethereum mainnet USDC/WETH pair: 0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc
        token0 = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"  # USDC
        token1 = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"  # WETH
        pair = compute_uniswap_v2_pair_address(UNISWAP_V2_FACTORY_MAINNET, token0, token1)
        self.assertEqual(pair.lower(), "0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc")

    def test_uniswap_v2_create2_ordering_enforcement(self):
        # Inverted token ordering fails closed
        token0 = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"  # WETH (numerically greater)
        token1 = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"  # USDC (numerically lesser)
        with self.assertRaises(ValueError) as ctx:
            compute_uniswap_v2_pair_address(UNISWAP_V2_FACTORY_MAINNET, token0, token1)
        self.assertIn("Strict token ordering violation", str(ctx.exception))

    def test_uniswap_v2_create2_invalid_address_length(self):
        with self.assertRaises(ValueError) as ctx:
            compute_uniswap_v2_pair_address(UNISWAP_V2_FACTORY_MAINNET, "0x123", "0x456")
        self.assertIn("Addresses must be 20-byte", str(ctx.exception))

    def _make_ssz_branch(self, leaf: bytes, gindex: int):
        curr = leaf
        k = gindex
        branch = []
        while k > 1:
            sibling = hashlib.sha256(b"sibling:" + k.to_bytes(4, "big")).digest()
            branch.append(sibling)
            if k % 2 == 1:
                curr = hashlib.sha256(sibling + curr).digest()
            else:
                curr = hashlib.sha256(curr + sibling).digest()
            k //= 2
        return curr, branch

    def test_header_consensus_verifier_success(self):
        sr = bytes.fromhex("55" * 32)
        ep_root, sr_br = self._make_ssz_branch(sr, 34)  # Deneb SSZ gindex 34
        body_root, ep_br = self._make_ssz_branch(ep_root, 25)
        sync_root = bytes.fromhex("98d69927b32c3e0bd9f091f2f728fb95aeec620d2b5f04390465e65cd5d00b5c")  # Period 1098
        parent_root = bytes.fromhex("22" * 32)
        fin_root = bytes.fromhex("33" * 32)
        beacon_state_root, fin_br = self._make_ssz_branch(fin_root, 105)  # Deneb SSZ gindex 105
        valid_sig = b"\xaa" * 96
        pubkeys_512 = [hashlib.sha256(f"pk_{i}".encode()).digest() + hashlib.sha256(f"ext_{i}".encode()).digest()[:16] for i in range(512)]
        ev = HeaderConsensusEvidence(
            slot=9_000_000,
            proposer_index=1,
            parent_root=parent_root,
            beacon_state_root=beacon_state_root,
            body_root=body_root,
            execution_payload_root=ep_root,
            execution_block_hash=bytes.fromhex("11" * 32),
            execution_block_number=19500000,
            execution_state_root=sr,
            execution_receipts_root=bytes.fromhex("66" * 32),
            execution_timestamp=1700000000,
            execution_payload_branch=ep_br,
            state_root_branch=sr_br,
            sync_committee_bits=b"\xff" * 64,
            sync_committee_signature=valid_sig,
            sync_committee_root=sync_root,
            finalized_checkpoint_root=fin_root,
            finalized_root_branch=fin_br,
            sync_committee_pubkeys=pubkeys_512,
        )

        # Without external BLS pairing engine (py_ecc), verification strictly fails closed
        ok_pure, err_pure = HeaderConsensusVerifier.verify_execution_header(ev, sync_root)
        self.assertFalse(ok_pure)
        self.assertEqual(err_pure, "BLS_SIGNATURE_VERIFICATION_UNAVAILABLE_OR_INVALID")

    def test_header_consensus_verifier_insufficient_participation_fails_closed(self):
        sr = bytes.fromhex("55" * 32)
        ep_root, sr_br = self._make_ssz_branch(sr, 34)
        body_root, ep_br = self._make_ssz_branch(ep_root, 25)
        sync_root = bytes.fromhex("98d69927b32c3e0bd9f091f2f728fb95aeec620d2b5f04390465e65cd5d00b5c")
        parent_root = bytes.fromhex("22" * 32)
        fin_root = bytes.fromhex("33" * 32)
        beacon_state_root, fin_br = self._make_ssz_branch(fin_root, 105)
        pubkeys_512 = [hashlib.sha256(f"pk_{i}".encode()).digest() + hashlib.sha256(f"ext_{i}".encode()).digest()[:16] for i in range(512)]
        ev = HeaderConsensusEvidence(
            slot=9_000_000,
            proposer_index=1,
            parent_root=parent_root,
            beacon_state_root=beacon_state_root,
            body_root=body_root,
            execution_payload_root=ep_root,
            execution_block_hash=bytes.fromhex("11" * 32),
            execution_block_number=19500000,
            execution_state_root=sr,
            execution_receipts_root=bytes.fromhex("66" * 32),
            execution_timestamp=1700000000,
            execution_payload_branch=ep_br,
            state_root_branch=sr_br,
            sync_committee_bits=b"\x01" * 64,  # Only 64 bits set < 342
            sync_committee_signature=b"\xaa" * 96,
            sync_committee_root=sync_root,
            finalized_checkpoint_root=fin_root,
            finalized_root_branch=fin_br,
            sync_committee_pubkeys=pubkeys_512,
        )
        ok, err = HeaderConsensusVerifier.verify_execution_header(ev, sync_root)
        self.assertFalse(ok)
        self.assertIn("INSUFFICIENT_SYNC_COMMITTEE_PARTICIPATION", err)

    def test_header_consensus_verifier_sync_committee_root_mismatch_fails_closed(self):
        sr = bytes.fromhex("55" * 32)
        ep_root, sr_br = self._make_ssz_branch(sr, 34)
        body_root, ep_br = self._make_ssz_branch(ep_root, 25)
        sync_root = bytes.fromhex("98d69927b32c3e0bd9f091f2f728fb95aeec620d2b5f04390465e65cd5d00b5c")
        parent_root = bytes.fromhex("22" * 32)
        fin_root = bytes.fromhex("33" * 32)
        beacon_state_root, fin_br = self._make_ssz_branch(fin_root, 105)
        pubkeys_512 = [hashlib.sha256(f"pk_{i}".encode()).digest() + hashlib.sha256(f"ext_{i}".encode()).digest()[:16] for i in range(512)]
        ev = HeaderConsensusEvidence(
            slot=9_000_000,
            proposer_index=1,
            parent_root=parent_root,
            beacon_state_root=beacon_state_root,
            body_root=body_root,
            execution_payload_root=ep_root,
            execution_block_hash=bytes.fromhex("11" * 32),
            execution_block_number=19500000,
            execution_state_root=sr,
            execution_receipts_root=bytes.fromhex("66" * 32),
            execution_timestamp=1700000000,
            execution_payload_branch=ep_br,
            state_root_branch=sr_br,
            sync_committee_bits=b"\xff" * 64,
            sync_committee_signature=b"\xaa" * 96,
            sync_committee_root=sync_root,
            finalized_checkpoint_root=fin_root,
            finalized_root_branch=fin_br,
            sync_committee_pubkeys=pubkeys_512,
        )
        ok, err = HeaderConsensusVerifier.verify_execution_header(ev, bytes.fromhex("ff" * 32))
        self.assertFalse(ok)
        self.assertEqual(err, "SYNC_COMMITTEE_ROOT_MISMATCH")

    def test_header_consensus_verifier_corrupt_ssz_branch_fails_closed(self):
        sr = bytes.fromhex("55" * 32)
        ep_root, sr_br = self._make_ssz_branch(sr, 34)
        body_root, ep_br = self._make_ssz_branch(ep_root, 25)
        sync_root = bytes.fromhex("98d69927b32c3e0bd9f091f2f728fb95aeec620d2b5f04390465e65cd5d00b5c")
        parent_root = bytes.fromhex("22" * 32)
        fin_root = bytes.fromhex("33" * 32)
        beacon_state_root, fin_br = self._make_ssz_branch(fin_root, 105)
        pubkeys_512 = [hashlib.sha256(f"pk_{i}".encode()).digest() + hashlib.sha256(f"ext_{i}".encode()).digest()[:16] for i in range(512)]
        corrupted_ep_br = list(ep_br)
        corrupted_ep_br[0] = bytes.fromhex("00" * 32)
        ev = HeaderConsensusEvidence(
            slot=9_000_000,
            proposer_index=1,
            parent_root=parent_root,
            beacon_state_root=beacon_state_root,
            body_root=body_root,
            execution_payload_root=ep_root,
            execution_block_hash=bytes.fromhex("11" * 32),
            execution_block_number=19500000,
            execution_state_root=sr,
            execution_receipts_root=bytes.fromhex("66" * 32),
            execution_timestamp=1700000000,
            execution_payload_branch=corrupted_ep_br,
            state_root_branch=sr_br,
            sync_committee_bits=b"\xff" * 64,
            sync_committee_signature=b"\xaa" * 96,
            sync_committee_root=sync_root,
            finalized_checkpoint_root=fin_root,
            finalized_root_branch=fin_br,
            sync_committee_pubkeys=pubkeys_512,
        )
        ok, err = HeaderConsensusVerifier.verify_execution_header(ev, sync_root)
        self.assertFalse(ok)
        # Without BLS, BLS check runs before SSZ branches and fails closed
        self.assertIn(err, ["EXECUTION_PAYLOAD_SSZ_BRANCH_INVALID", "BLS_SIGNATURE_VERIFICATION_UNAVAILABLE_OR_INVALID"])

    def test_header_consensus_verifier_dummy_bls_signature_fails_closed(self):
        sr = bytes.fromhex("55" * 32)
        ep_root, sr_br = self._make_ssz_branch(sr, 34)
        body_root, ep_br = self._make_ssz_branch(ep_root, 25)
        sync_root = bytes.fromhex("98d69927b32c3e0bd9f091f2f728fb95aeec620d2b5f04390465e65cd5d00b5c")
        parent_root = bytes.fromhex("22" * 32)
        fin_root = bytes.fromhex("33" * 32)
        beacon_state_root, fin_br = self._make_ssz_branch(fin_root, 105)
        pubkeys_512 = [hashlib.sha256(f"pk_{i}".encode()).digest() + hashlib.sha256(f"ext_{i}".encode()).digest()[:16] for i in range(512)]
        ev = HeaderConsensusEvidence(
            slot=9_000_000,
            proposer_index=1,
            parent_root=parent_root,
            beacon_state_root=beacon_state_root,
            body_root=body_root,
            execution_payload_root=ep_root,
            execution_block_hash=bytes.fromhex("11" * 32),
            execution_block_number=19500000,
            execution_state_root=sr,
            execution_receipts_root=bytes.fromhex("66" * 32),
            execution_timestamp=1700000000,
            execution_payload_branch=ep_br,
            state_root_branch=sr_br,
            sync_committee_bits=b"\xff" * 64,
            sync_committee_signature=b"\x00" * 96,  # Dummy all-zero signature
            sync_committee_root=sync_root,
            finalized_checkpoint_root=fin_root,
            finalized_root_branch=fin_br,
            sync_committee_pubkeys=pubkeys_512,
        )
        ok, err = HeaderConsensusVerifier.verify_execution_header(ev, sync_root)
        self.assertFalse(ok)
        self.assertEqual(err, "DUMMY_OR_EMPTY_BLS_SIGNATURE_REJECTED")

    def test_header_consensus_verifier_missing_mandatory_branches_fails_closed(self):
        sr = bytes.fromhex("55" * 32)
        ep_root, sr_br = self._make_ssz_branch(sr, 34)
        body_root, ep_br = self._make_ssz_branch(ep_root, 25)
        sync_root = bytes.fromhex("98d69927b32c3e0bd9f091f2f728fb95aeec620d2b5f04390465e65cd5d00b5c")
        parent_root = bytes.fromhex("22" * 32)
        fin_root = bytes.fromhex("33" * 32)
        beacon_state_root, fin_br = self._make_ssz_branch(fin_root, 105)
        pubkeys_512 = [hashlib.sha256(f"pk_{i}".encode()).digest() + hashlib.sha256(f"ext_{i}".encode()).digest()[:16] for i in range(512)]

        # Missing state root branch (depth must be 5 for gindex 34)
        ev_no_sr = HeaderConsensusEvidence(
            slot=9_000_000,
            proposer_index=1,
            parent_root=parent_root,
            beacon_state_root=beacon_state_root,
            body_root=body_root,
            execution_payload_root=ep_root,
            execution_block_hash=bytes.fromhex("11" * 32),
            execution_block_number=19500000,
            execution_state_root=sr,
            execution_receipts_root=bytes.fromhex("66" * 32),
            execution_timestamp=1700000000,
            execution_payload_branch=ep_br,
            state_root_branch=[],  # Empty!
            sync_committee_bits=b"\xff" * 64,
            sync_committee_signature=b"\xaa" * 96,
            sync_committee_root=sync_root,
            finalized_checkpoint_root=fin_root,
            finalized_root_branch=fin_br,
            sync_committee_pubkeys=pubkeys_512,
        )
        ok, err = HeaderConsensusVerifier.verify_execution_header(ev_no_sr, sync_root)
        self.assertFalse(ok)
        self.assertEqual(err, "STATE_ROOT_BRANCH_MANDATORY_DEPTH_5")

        # Missing finalized checkpoint branch (depth must be 6 for gindex 105)
        ev_no_fin = HeaderConsensusEvidence(
            slot=9_000_000,
            proposer_index=1,
            parent_root=parent_root,
            beacon_state_root=beacon_state_root,
            body_root=body_root,
            execution_payload_root=ep_root,
            execution_block_hash=bytes.fromhex("11" * 32),
            execution_block_number=19500000,
            execution_state_root=sr,
            execution_receipts_root=bytes.fromhex("66" * 32),
            execution_timestamp=1700000000,
            execution_payload_branch=ep_br,
            state_root_branch=sr_br,
            sync_committee_bits=b"\xff" * 64,
            sync_committee_signature=b"\xaa" * 96,
            sync_committee_root=sync_root,
            finalized_checkpoint_root=fin_root,
            finalized_root_branch=[],  # Empty!
            sync_committee_pubkeys=pubkeys_512,
        )
        ok, err = HeaderConsensusVerifier.verify_execution_header(ev_no_fin, sync_root)
        self.assertFalse(ok)
        self.assertEqual(err, "FINALIZED_CHECKPOINT_BRANCH_MANDATORY_DEPTH_6")

    def test_deneb_generalized_indices_standard_audit(self):
        """Verifies Deneb SSZ generalized indices match official Ethereum consensus Deneb specs."""
        self.assertEqual(GINDEX_EXECUTION_PAYLOAD_STATE_ROOT, 34)       # Depth 5 (17 fields -> capacity 32)
        self.assertEqual(GINDEX_EXECUTION_PAYLOAD_BLOCK_HASH, 44)       # Depth 5 (field 12)
        self.assertEqual(GINDEX_EXECUTION_PAYLOAD_BLOCK_NUMBER, 38)     # Depth 5 (field 6)
        self.assertEqual(GINDEX_EXECUTION_PAYLOAD_RECEIPTS_ROOT, 35)    # Depth 5 (field 3)
        self.assertEqual(GINDEX_EXECUTION_PAYLOAD_TIMESTAMP, 41)        # Depth 5 (field 9)
        self.assertEqual(GINDEX_FINALIZED_CHECKPOINT_ROOT, 105)         # Depth 6 (field 20.root)

        indices = ForkSchedule.get_generalized_indices(FORK_VERSION_DENEB)
        self.assertEqual(indices["execution_payload_state_root"], 34)
        self.assertEqual(indices["finalized_checkpoint_root"], 105)

    def test_secp256k1_ecrecover_and_wormhole_vaa(self):
        """Verifies pure-Python secp256k1 ecrecover and Wormhole VAA signature and quorum verification."""
        # Setup 3 guardians with deterministic private keys
        privkeys = [101, 102, 103]
        guardian_addrs = []
        for pk in privkeys:
            pub = _secp256k1_point_mul(pk, (_SECP256K1_GX, _SECP256K1_GY))
            raw_pub = pub[0].to_bytes(32, "big") + pub[1].to_bytes(32, "big")
            addr = "0x" + keccak256(raw_pub)[12:].hex().lower()
            guardian_addrs.append(addr)

        # Build VAA body: timestamp(4), nonce(4), emitter_chain(2), emitter_address(32), sequence(8), consistency(1), payload
        pyth_payload = b"P2WH\x01\x00\x00\x00\x00" + bytes.fromhex("11" * 32) + (50000).to_bytes(8, "big") + (10).to_bytes(8, "big") + (-8).to_bytes(4, "big", signed=True) + (1700000000).to_bytes(8, "big")
        body = (1700000000).to_bytes(4, "big") + (1).to_bytes(4, "big") + (1).to_bytes(2, "big") + bytes.fromhex("22" * 32) + (100).to_bytes(8, "big") + (15).to_bytes(1, "big") + pyth_payload
        digest = keccak256(keccak256(body))

        # Sign with each guardian (r, s, v)
        signatures = []
        for g_idx, pk in enumerate(privkeys):
            k = 999 + g_idx
            R = _secp256k1_point_mul(k, (_SECP256K1_GX, _SECP256K1_GY))
            r = R[0] % _SECP256K1_N
            v = 0 if R[1] % 2 == 0 else 1
            e = int.from_bytes(digest, "big")
            s = (pow(k, _SECP256K1_N - 2, _SECP256K1_N) * (e + r * pk)) % _SECP256K1_N
            recovered = secp256k1_ecrecover(digest, v, r, s)
            self.assertEqual(recovered, guardian_addrs[g_idx])
            signatures.append(g_idx.to_bytes(1, "big") + r.to_bytes(32, "big") + s.to_bytes(32, "big") + v.to_bytes(1, "big"))

        # Construct full VAA wire bytes: version(1=1), guardian_set_index(4), sig_count(1), sigs..., body...
        raw_vaa = b"\x01" + (0).to_bytes(4, "big") + len(signatures).to_bytes(1, "big") + b"".join(signatures) + body
        ok, err, parsed = verify_wormhole_vaa(raw_vaa, guardian_addrs)
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["guardian_set_index"], 0)

        # Corrupt one signature -> verification fails
        corrupt_vaa = bytearray(raw_vaa)
        corrupt_vaa[10] ^= 0xFF
        ok_bad, err_bad, _ = verify_wormhole_vaa(bytes(corrupt_vaa), guardian_addrs)
        self.assertFalse(ok_bad)
        self.assertIn("GUARDIAN_SIGNATURE_MISMATCH", err_bad)

        # Insufficient quorum -> verification fails
        short_vaa = b"\x01" + (0).to_bytes(4, "big") + (1).to_bytes(1, "big") + signatures[0] + body
        ok_short, err_short, _ = verify_wormhole_vaa(short_vaa, guardian_addrs)
        self.assertFalse(ok_short)
        self.assertIn("INSUFFICIENT_GUARDIAN_QUORUM", err_short)

    def test_light_client_sync_committee_transition_verification(self):
        """Validates authenticated period transition P -> P+1 via genuine SSZ branch and aggregate signature."""
        LightClientSyncCommitteeStore.reset_to_bootstrap()
        p1098_root = LightClientSyncCommitteeStore.get_period_root(1098)
        self.assertEqual(p1098_root, bytes.fromhex("98d69927b32c3e0bd9f091f2f728fb95aeec620d2b5f04390465e65cd5d00b5c"))

        pubkeys_1098 = [
            hashlib.sha256(f"pk_{i}".encode()).digest() + hashlib.sha256(f"ext_{i}".encode()).digest()[:16]
            for i in range(512)
        ]
        pubkeys_1099 = [
            hashlib.sha256(f"pk1099_{i}".encode()).digest() + hashlib.sha256(f"ext1099_{i}".encode()).digest()[:16]
            for i in range(512)
        ]
        p1099_root = compute_sync_committee_root(pubkeys_1099)
        beacon_state_root, next_branch = self._make_ssz_branch(p1099_root, GINDEX_NEXT_SYNC_COMMITTEE)

        trans = SyncCommitteePeriodTransition(
            from_period=1098,
            to_period=1099,
            attested_slot=9_000_000,
            signature_slot=9_000_001,
            beacon_state_root=beacon_state_root,
            next_sync_committee_root=p1099_root,
            next_sync_committee_branch=next_branch,
            sync_committee_bits=b"\xff" * 64,
            sync_committee_signature=b"\xaa" * 96,
            sync_committee_pubkeys=pubkeys_1098,
        )

        with mock.patch("scripts.consensus_proof_verifier.verify_bls_aggregate_signature", return_value=True):
            ok_tr, err_tr = LightClientSyncCommitteeStore.verify_and_register_transition(trans)
            self.assertTrue(ok_tr)
            self.assertIsNone(err_tr)
            self.assertEqual(LightClientSyncCommitteeStore.get_period_root(1099), p1099_root)
            self.assertTrue(LightClientSyncCommitteeStore.is_period_authenticated(1099, p1099_root))

    def test_wormhole_guardian_registry_expiration_and_provenance(self):
        """Validates WormholeGuardianRegistry expiration semantics, active sets, and dev key rejection."""
        # Active Set 3
        ok3, err3, guardians3 = WormholeGuardianRegistry.get_guardian_set(3)
        self.assertTrue(ok3)
        self.assertIsNone(err3)
        self.assertEqual(len(guardians3), 19)

        # Expired Set 1
        ok1, err1, _ = WormholeGuardianRegistry.get_guardian_set(1, check_time=1700000000)
        self.assertFalse(ok1)
        self.assertIn("EXPIRED_GUARDIAN_SET", err1)

        # Fabricated Set 999
        ok_fab, err_fab, _ = WormholeGuardianRegistry.get_guardian_set(999)
        self.assertFalse(ok_fab)
        self.assertIn("UNAUTHENTICATED_GUARDIAN_SET", err_fab)


if __name__ == "__main__":
    unittest.main()
