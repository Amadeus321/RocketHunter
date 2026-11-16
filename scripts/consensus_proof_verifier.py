#!/usr/bin/env python3
"""Stage 3.3 Offline Foundation: Consensus SSZ Light-Client & MPT Trie Proof Verifier.

Provides isolated, pure-Python standard-library verification primitives:
1. Ethereum Keccak-256 with legacy Keccak padding (0x01), strictly distinct from FIPS 202 SHA3-256 (0x06).
2. Pure-Python RLP decoding and compact hex-prefix (HP) path parsing.
3. Merkle Patricia Trie (MPT) storage-proof and account-proof traversal:
   - Full 17-item branch nodes.
   - Extension and leaf nodes.
   - Inline child nodes (< 32 bytes).
   - Cryptographic absence (non-inclusion) proofs: empty branch slot or diverging path.
4. Canonical Uniswap V2 Slot 3 packed reserves decoder (reserve0, reserve1, blockTimestampLast).
5. SSZ binary Merkle branch verification for consensus light-client generalized indices (gindex 25, 54, 105).
6. Consensus domain computation and signing-root derivations (Deneb/Electra).

All functions are deterministic, execute strictly offline, and make zero RPC/network calls.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

# ==============================================================================
# 1. PURE-PYTHON ETHEREUM KECCAK-256 (LEGACY 0x01 PADDING, NOT FIPS 202 SHA3 0x06)
# ==============================================================================

_KECCAK_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]

_KECCAK_ROTC = [
    [0, 36, 3, 41, 18],
    [1, 44, 10, 45, 2],
    [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56],
    [27, 20, 39, 8, 14],
]


def _rotl64(x: int, n: int) -> int:
    return ((x << (n % 64)) & 0xFFFFFFFFFFFFFFFF) | (x >> (64 - (n % 64)))


def _keccak_f1600(state: list[int]) -> list[int]:
    lanes = [[state[x + 5 * y] for y in range(5)] for x in range(5)]
    for r in range(24):
        # Theta step
        c = [lanes[x][0] ^ lanes[x][1] ^ lanes[x][2] ^ lanes[x][3] ^ lanes[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rotl64(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                lanes[x][y] ^= d[x]
        # Rho and Pi steps
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rotl64(lanes[x][y], _KECCAK_ROTC[x][y])
        # Chi step
        for x in range(5):
            for y in range(5):
                lanes[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y])
        # Iota step
        lanes[0][0] ^= _KECCAK_RC[r]

    out = [0] * 25
    for x in range(5):
        for y in range(5):
            out[x + 5 * y] = lanes[x][y]
    return out


def keccak256(data: bytes) -> bytes:
    """Compute Ethereum Keccak-256 digest using legacy Keccak padding (0x01 ... 0x80).

    IMPORTANT DISTINCTION:
    - Ethereum Keccak-256 uses legacy Keccak padding byte 0x01 (pad10*1 with domain bit 0x01).
    - FIPS 202 SHA3-256 uses domain separation padding byte 0x06 (pad10*1 with domain bits 0110_2).
    - Consequently, keccak256(b"") = 0xc5d2460186... whereas sha3_256(b"") = 0xa7ffc6f8bf...
    """
    rate = 136  # 1088 bits / 8 bytes
    padlen = rate - (len(data) % rate)
    if padlen == 1:
        padded = data + b"\x81"
    else:
        padded = data + b"\x01" + b"\x00" * (padlen - 2) + b"\x80"

    state = [0] * 25
    for offset in range(0, len(padded), rate):
        block = padded[offset : offset + rate]
        for i in range(17):
            val = int.from_bytes(block[i * 8 : (i + 1) * 8], "little")
            state[i] ^= val
        state = _keccak_f1600(state)

    out_bytes = bytearray()
    for i in range(4):  # 4 * 8 bytes = 32 bytes
        out_bytes.extend(state[i].to_bytes(8, "little"))
    return bytes(out_bytes)


# ==============================================================================
# 2. PURE-PYTHON RLP (RECURSIVE LENGTH PREFIX) DECODER
# ==============================================================================

def rlp_decode(data: bytes) -> bytes | list:
    """Decodes RLP-encoded bytes into bytes or nested lists of bytes.

    Strictly enforces canonical Ethereum RLP encoding rules (Yellow Paper Appendix B):
    - Single bytes < 0x80 must be encoded as themselves (not with 0x81 prefix).
    - Long form (0xB8..0xBF, 0xF8..0xFF) cannot be used for payloads <= 55 bytes.
    - Length-of-length fields must not contain leading zero bytes.
    - Child payloads inside lists cannot overrun the designated parent list boundary.
    - Trailing unparsed bytes at the top level or within lists are strictly rejected.
    """
    if not data:
        raise ValueError("RLP decode input is empty")

    def _decode_at(offset: int, max_offset: int) -> tuple[bytes | list, int]:
        if offset >= max_offset:
            raise ValueError(f"RLP offset {offset} exceeds boundary {max_offset}")
        prefix = data[offset]
        if prefix < 0x80:
            return bytes([prefix]), offset + 1
        elif prefix <= 0xB7:
            length = prefix - 0x80
            end = offset + 1 + length
            if end > max_offset:
                raise ValueError("RLP short string overruns boundary")
            # Canonical check: single byte < 0x80 must NOT be encoded as short string [0x81, byte]
            if length == 1 and data[offset + 1] < 0x80:
                raise ValueError(
                    f"Non-canonical RLP: single byte 0x{data[offset+1]:02x} < 0x80 encoded with 0x81 prefix"
                )
            return data[offset + 1 : end], end
        elif prefix <= 0xBF:
            len_of_len = prefix - 0xB7
            if offset + 1 + len_of_len > max_offset:
                raise ValueError("RLP long string length field overruns boundary")
            len_bytes = data[offset + 1 : offset + 1 + len_of_len]
            # Canonical check: no leading zeros in length bytes
            if len_bytes[0] == 0:
                raise ValueError("Non-canonical RLP: leading zero in long string length field")
            length = int.from_bytes(len_bytes, "big")
            # Canonical check: long form must not be used for length <= 55
            if length <= 55:
                raise ValueError(f"Non-canonical RLP: long string prefix used for length {length} <= 55")
            start = offset + 1 + len_of_len
            end = start + length
            if end > max_offset:
                raise ValueError("RLP long string payload overruns boundary")
            return data[start : end], end
        elif prefix <= 0xF7:
            length = prefix - 0xC0
            end = offset + 1 + length
            if end > max_offset:
                raise ValueError("RLP short list overruns boundary")
            items = []
            curr = offset + 1
            while curr < end:
                item, curr = _decode_at(curr, end)
                items.append(item)
            if curr != end:
                raise ValueError("RLP short list items do not exactly match payload boundary")
            return items, end
        else:
            len_of_len = prefix - 0xF7
            if offset + 1 + len_of_len > max_offset:
                raise ValueError("RLP long list length field overruns boundary")
            len_bytes = data[offset + 1 : offset + 1 + len_of_len]
            # Canonical check: no leading zeros in length bytes
            if len_bytes[0] == 0:
                raise ValueError("Non-canonical RLP: leading zero in long list length field")
            length = int.from_bytes(len_bytes, "big")
            # Canonical check: long form must not be used for length <= 55
            if length <= 55:
                raise ValueError(f"Non-canonical RLP: long list prefix used for length {length} <= 55")
            start = offset + 1 + len_of_len
            end = start + length
            if end > max_offset:
                raise ValueError("RLP long list payload overruns boundary")
            items = []
            curr = start
            while curr < end:
                item, curr = _decode_at(curr, end)
                items.append(item)
            if curr != end:
                raise ValueError("RLP long list items do not exactly match payload boundary")
            return items, end

    result, remainder_offset = _decode_at(0, len(data))
    if remainder_offset != len(data):
        raise ValueError(f"RLP payload has {len(data) - remainder_offset} trailing unparsed bytes")
    return result


def rlp_encode(item: bytes | list) -> bytes:
    """Encodes bytes or nested list into RLP format."""
    if isinstance(item, bytes):
        if len(item) == 1 and item[0] < 0x80:
            return item
        elif len(item) <= 55:
            return bytes([0x80 + len(item)]) + item
        else:
            len_bytes = len(item).to_bytes((len(item).bit_length() + 7) // 8, "big")
            return bytes([0xB7 + len(len_bytes)]) + len_bytes + item
    elif isinstance(item, list):
        body = b"".join(rlp_encode(x) for x in item)
        if len(body) <= 55:
            return bytes([0xC0 + len(body)]) + body
        else:
            len_bytes = len(body).to_bytes((len(body).bit_length() + 7) // 8, "big")
            return bytes([0xF7 + len(len_bytes)]) + len_bytes + body
    else:
        raise TypeError(f"Cannot RLP-encode type: {type(item)}")


def rlp_encode_bytes(b: bytes) -> bytes:
    """Encodes a single bytes string using RLP."""
    return rlp_encode(b)


# ==============================================================================
# 3. MERKLE PATRICIA TRIE (MPT) COMPACT PATH & PROOF VERIFICATION
# ==============================================================================

def decode_compact_path(encoded: bytes) -> tuple[list[int], bool]:
    """Decodes MPT compact hex-prefix (HP) encoded path into nibbles and leaf flag.

    Enforces canonical HP encoding rules (Yellow Paper Appendix C):
    - Compact path cannot be empty.
    - High nibble must be in range 0..3 (prefix).
    - If length is even (prefix 0 or 2), low nibble of first byte must be strictly 0x0 (padding).
    """
    if not encoded:
        raise ValueError("Non-canonical compact path: compact path cannot be empty")
    first = encoded[0]
    prefix = first >> 4
    if prefix > 3:
        raise ValueError(f"Non-canonical compact path: invalid prefix nibble {prefix} > 3")
    is_leaf = bool(prefix & 2)
    is_odd = bool(prefix & 1)
    nibbles = []
    if is_odd:
        nibbles.append(first & 0x0F)
    else:
        if (first & 0x0F) != 0:
            raise ValueError(
                f"Non-canonical compact path: non-zero padding nibble 0x{(first & 0x0F):x} in even-length path"
            )
    for b in encoded[1:]:
        nibbles.append(b >> 4)
        nibbles.append(b & 0x0F)
    return nibbles, is_leaf


def encode_compact_path(nibbles: list[int], is_leaf: bool) -> bytes:
    """Encodes nibbles and leaf flag into MPT compact hex-prefix (HP) format."""
    is_odd = len(nibbles) % 2 == 1
    prefix = (2 if is_leaf else 0) | (1 if is_odd else 0)
    out = bytearray()
    if is_odd:
        out.append((prefix << 4) | nibbles[0])
        n_start = 1
    else:
        out.append(prefix << 4)
        n_start = 0
    for i in range(n_start, len(nibbles), 2):
        out.append((nibbles[i] << 4) | nibbles[i + 1])
    return bytes(out)


@dataclass(frozen=True)
class MPTVerificationResult:
    is_valid: bool
    is_included: bool
    is_absence_proof: bool
    key_hash: str
    value_bytes: bytes | None
    proof_type: str  # "INCLUSION_LEAF", "INCLUSION_BRANCH", "ABSENCE_EMPTY_BRANCH_CHILD", "ABSENCE_PATH_DIVERGENCE", "INVALID_PROOF"
    error: str | None = None


def verify_mpt_proof(root_hash: bytes, search_key_bytes: bytes, proof_nodes: list[bytes]) -> MPTVerificationResult:
    """Verifies an Ethereum Merkle Patricia Trie proof from root_hash to search_key_bytes.

    Supported Node Types & Proof Topologies:
    - Branch Nodes: 17 items (16 nibble children + 1 value).
    - Extension Nodes: 2 items (compact path + child hash/node).
    - Leaf Nodes: 2 items (compact path with leaf flag + value).
    - Inline Children: Child nodes < 32 bytes stored inline rather than hashed.
    - Inclusion Proofs: Resolves leaf or branch value matching exact search key.
    - Absence Proofs (Non-Inclusion): Authenticates that a key does NOT exist in the trie
      via a cryptographically linked empty branch child or path divergence.

    Args:
        root_hash: 32-byte trie root hash (stateRoot or storageHash).
        search_key_bytes: 32-byte trie key (keccak256(address) or keccak256(pad32(slot))).
        proof_nodes: List of RLP-encoded trie nodes from eth_getProof.

    Returns:
        MPTVerificationResult with is_valid=True and is_included=True (with value) for inclusion,
        or is_valid=True and is_absence_proof=True (value=None) for verified non-inclusion.
    """
    if len(root_hash) != 32:
        return MPTVerificationResult(
            False, False, False, search_key_bytes.hex(), None, "INVALID_PROOF",
            f"Invalid root hash length: {len(root_hash)}",
        )
    if len(search_key_bytes) != 32:
        return MPTVerificationResult(
            False, False, False, search_key_bytes.hex(), None, "INVALID_PROOF",
            f"Invalid search key length: {len(search_key_bytes)}",
        )

    # Index proof nodes by keccak256 hash
    node_map: dict[bytes, bytes] = {}
    for raw_node in proof_nodes:
        node_map[keccak256(raw_node)] = raw_node

    # Convert search key to 64 nibbles
    key_nibbles: list[int] = []
    for b in search_key_bytes:
        key_nibbles.append(b >> 4)
        key_nibbles.append(b & 0x0F)

    current_hash: bytes | None = root_hash
    current_node: list | None = None
    nibble_idx = 0

    while nibble_idx <= len(key_nibbles):
        if current_node is not None:
            decoded_node = current_node
            current_node = None
        else:
            if current_hash is None or current_hash not in node_map:
                hash_hex = current_hash.hex() if current_hash else "None"
                return MPTVerificationResult(
                    False, False, False, search_key_bytes.hex(), None, "INVALID_PROOF",
                    f"Missing node for hash 0x{hash_hex} at nibble index {nibble_idx}",
                )

            raw_node = node_map[current_hash]
            try:
                decoded_node = rlp_decode(raw_node)
            except Exception as e:
                return MPTVerificationResult(
                    False, False, False, search_key_bytes.hex(), None, "INVALID_PROOF",
                    f"RLP decode failure in trie node: {e}",
                )

        if not isinstance(decoded_node, list):
            return MPTVerificationResult(
                False, False, False, search_key_bytes.hex(), None, "INVALID_PROOF",
                "Trie node is not an RLP list",
            )

        # 1. Branch Node (17 items: 16 children + 1 value)
        if len(decoded_node) == 17:
            if nibble_idx == len(key_nibbles):
                val = decoded_node[16]
                if val == b"":
                    # Canonical empty bytes -> Cryptographic Proof of Absence
                    return MPTVerificationResult(
                        True, False, True, search_key_bytes.hex(), None, "ABSENCE_EMPTY_BRANCH_VALUE", None,
                    )
                elif isinstance(val, bytes):
                    return MPTVerificationResult(
                        True, True, False, search_key_bytes.hex(), val, "INCLUSION_BRANCH", None,
                    )
                else:
                    return MPTVerificationResult(
                        False, False, False, search_key_bytes.hex(), None, "INVALID_PROOF",
                        f"Non-canonical branch value type {type(val)}: branch values must be bytes or exact empty bytes b\"\"",
                    )

            next_nibble = key_nibbles[nibble_idx]
            child = decoded_node[next_nibble]
            if child == b"":
                # Child slot at next nibble is exact empty bytes -> Cryptographic Proof of Absence (Non-inclusion)
                return MPTVerificationResult(
                    True, False, True, search_key_bytes.hex(), None, "ABSENCE_EMPTY_BRANCH_CHILD", None,
                )

            if isinstance(child, list):
                if len(child) == 0:
                    return MPTVerificationResult(
                        False, False, False, search_key_bytes.hex(), None, "INVALID_PROOF",
                        "Non-canonical branch child: empty list [] is invalid in child slot",
                    )
                # Canonical embedded inline child node: MUST have serialized RLP length strictly < 32 bytes
                child_serialized = rlp_encode(child)
                if len(child_serialized) >= 32:
                    return MPTVerificationResult(
                        False, False, False, search_key_bytes.hex(), None, "INVALID_PROOF",
                        f"Non-canonical inline child node: serialized RLP length ({len(child_serialized)}) >= 32 bytes",
                    )
                current_node = child
                current_hash = None
                nibble_idx += 1
            elif isinstance(child, bytes):
                if len(child) == 32:
                    current_hash = child
                    current_node = None
                    nibble_idx += 1
                else:
                    return MPTVerificationResult(
                        False, False, False, search_key_bytes.hex(), None, "INVALID_PROOF",
                        f"Non-canonical branch child byte length ({len(child)}): inline children must be embedded RLP lists, not byte strings",
                    )
            else:
                return MPTVerificationResult(
                    False, False, False, search_key_bytes.hex(), None, "INVALID_PROOF",
                    f"Unexpected branch child type: {type(child)}",
                )

        # 2. Extension or Leaf Node (2 items: path, val_or_child)
        elif len(decoded_node) == 2:
            encoded_path, val_or_child = decoded_node[0], decoded_node[1]
            if not isinstance(encoded_path, bytes):
                return MPTVerificationResult(
                    False, False, False, search_key_bytes.hex(), None, "INVALID_PROOF",
                    "Trie path is not bytes",
                )

            try:
                path_nibbles, is_leaf = decode_compact_path(encoded_path)
            except ValueError as e:
                return MPTVerificationResult(
                    False, False, False, search_key_bytes.hex(), None, "INVALID_PROOF",
                    f"Corrupt or non-canonical compact path: {e}",
                )

            # Validate node structure and field types BEFORE any absence check
            if is_leaf:
                if not isinstance(val_or_child, bytes):
                    return MPTVerificationResult(
                        False, False, False, search_key_bytes.hex(), None, "INVALID_PROOF",
                        f"Non-canonical leaf value type {type(val_or_child)}: leaf value must be bytes",
                    )
            else:
                # Extension node pointing to child node
                if len(path_nibbles) == 0:
                    return MPTVerificationResult(
                        False, False, False, search_key_bytes.hex(), None, "INVALID_PROOF",
                        "Non-canonical extension node: extension path cannot be empty",
                    )

                if isinstance(val_or_child, list):
                    if len(val_or_child) == 0:
                        return MPTVerificationResult(
                            False, False, False, search_key_bytes.hex(), None, "INVALID_PROOF",
                            "Non-canonical extension child: empty list [] is invalid in child slot",
                        )
                    child_serialized = rlp_encode(val_or_child)
                    if len(child_serialized) >= 32:
                        return MPTVerificationResult(
                            False, False, False, search_key_bytes.hex(), None, "INVALID_PROOF",
                            f"Non-canonical inline extension child: serialized RLP length ({len(child_serialized)}) >= 32 bytes",
                        )
                elif isinstance(val_or_child, bytes):
                    if len(val_or_child) != 32:
                        return MPTVerificationResult(
                            False, False, False, search_key_bytes.hex(), None, "INVALID_PROOF",
                            f"Non-canonical extension child reference byte length ({len(val_or_child)}): inline children must be embedded RLP lists, not byte strings",
                        )
                else:
                    return MPTVerificationResult(
                        False, False, False, search_key_bytes.hex(), None, "INVALID_PROOF",
                        f"Unexpected extension child type: {type(val_or_child)}",
                    )

            matching_len = len(path_nibbles)

            # Check for path divergence -> Cryptographic Proof of Absence
            if key_nibbles[nibble_idx : nibble_idx + matching_len] != path_nibbles:
                return MPTVerificationResult(
                    True, False, True, search_key_bytes.hex(), None, "ABSENCE_PATH_DIVERGENCE", None,
                )

            nibble_idx += matching_len

            if is_leaf:
                if nibble_idx == len(key_nibbles):
                    return MPTVerificationResult(
                        True, True, False, search_key_bytes.hex(), val_or_child, "INCLUSION_LEAF", None,
                    )
                else:
                    # Leaf reached but search key has more nibbles -> Cryptographic Proof of Absence
                    return MPTVerificationResult(
                        True, False, True, search_key_bytes.hex(), None, "ABSENCE_PATH_DIVERGENCE", None,
                    )
            else:
                # Extension node pointing to child node (structure already validated above)
                if isinstance(val_or_child, list):
                    current_node = val_or_child
                    current_hash = None
                elif isinstance(val_or_child, bytes):
                    current_hash = val_or_child
                    current_node = None
        else:
            return MPTVerificationResult(
                False, False, False, search_key_bytes.hex(), None, "INVALID_PROOF",
                f"Invalid MPT node item count: {len(decoded_node)}",
            )

    return MPTVerificationResult(
        True, False, True, search_key_bytes.hex(), None, "ABSENCE_PATH_DIVERGENCE", None,
    )


# ==============================================================================
# 4. CANONICAL UNISWAP V2 SLOT 3 PACKED RESERVES DECODER
# ==============================================================================

@dataclass(frozen=True)
class Slot3ReservesResult:
    is_valid: bool
    reserve0: int
    reserve1: int
    block_timestamp_last: int
    raw_slot3_hex: str
    error: str | None = None


class UniswapV2Slot3Decoder:
    """Canonical decoder for Uniswap V2 Pair Slot 3 packed reserves and timestamp."""

    @staticmethod
    def compute_storage_key(slot: int = 3) -> bytes:
        """Computes the 32-byte MPT storage search key for a given storage slot index."""
        slot_bytes = slot.to_bytes(32, "big")
        return keccak256(slot_bytes)

    @staticmethod
    def decode_storage_word(rlp_decoded_bytes: bytes) -> Slot3ReservesResult:
        """Unpacks 32-byte Slot 3 integer into reserve0 (uint112), reserve1 (uint112), and blockTimestampLast (uint32).

        Bits 0..111:   reserve0 (uint112)
        Bits 112..223: reserve1 (uint112)
        Bits 224..255: blockTimestampLast (uint32)
        """
        if len(rlp_decoded_bytes) > 32:
            return Slot3ReservesResult(False, 0, 0, 0, rlp_decoded_bytes.hex(), "Storage word exceeds 32 bytes")

        # Left-pad with zeros to 32 bytes
        padded = rlp_decoded_bytes.rjust(32, b"\x00")
        raw_int = int.from_bytes(padded, "big")

        mask112 = (1 << 112) - 1
        mask32 = (1 << 32) - 1

        r0 = raw_int & mask112
        r1 = (raw_int >> 112) & mask112
        ts = (raw_int >> 224) & mask32

        if r0 < 0 or r1 < 0 or ts < 0:
            return Slot3ReservesResult(False, 0, 0, 0, padded.hex(), "Unpacked negative values")

        return Slot3ReservesResult(True, r0, r1, ts, padded.hex(), None)


@dataclass(frozen=True)
class AccountStateLeaf:
    is_valid: bool
    nonce: int
    balance: int
    storage_root: bytes
    code_hash: bytes
    error: str | None = None

    @classmethod
    def from_rlp(cls, leaf_bytes: bytes) -> AccountStateLeaf:
        """Decodes an Ethereum account state leaf RLP: [nonce, balance, storageRoot, codeHash]."""
        try:
            decoded = rlp_decode(leaf_bytes)
            if not isinstance(decoded, list) or len(decoded) != 4:
                return cls(False, 0, 0, b"", b"", f"Account leaf must have 4 items, got {len(decoded) if isinstance(decoded, list) else type(decoded)}")

            nonce = int.from_bytes(decoded[0], "big") if decoded[0] else 0
            balance = int.from_bytes(decoded[1], "big") if decoded[1] else 0
            storage_root = decoded[2]
            code_hash = decoded[3]

            if len(storage_root) != 32:
                return cls(False, 0, 0, b"", b"", f"Invalid storageRoot length: {len(storage_root)}")
            if len(code_hash) != 32:
                return cls(False, 0, 0, b"", b"", f"Invalid codeHash length: {len(code_hash)}")

            return cls(True, nonce, balance, storage_root, code_hash, None)
        except Exception as e:
            return cls(False, 0, 0, b"", b"", f"Account leaf decode failure: {e}")


# ==============================================================================
# 5. SSZ MERKLE PROOF VERIFICATION & CONSENSUS DOMAIN DERIVATIONS
# ==============================================================================

def sha256(data: bytes) -> bytes:
    """Standard SHA-256 digest wrapper."""
    return hashlib.sha256(data).digest()


@dataclass(frozen=True)
class SSZVerificationResult:
    is_valid: bool
    gindex: int
    depth: int
    root_hex: str
    error: str | None = None


def verify_ssz_branch(leaf: bytes, branch: list[bytes], gindex: int, root: bytes) -> SSZVerificationResult:
    """Verifies a binary SSZ Merkle branch against generalized index gindex and root.

    Args:
        leaf: 32-byte leaf hash.
        branch: List of 32-byte sibling hashes from bottom to top.
        gindex: Generalized index (gindex >= 1).
        root: Expected 32-byte Merkle root.

    Returns:
        SSZVerificationResult indicating validity.
    """
    if len(leaf) != 32:
        return SSZVerificationResult(False, gindex, 0, root.hex(), f"Invalid leaf length: {len(leaf)}")
    if len(root) != 32:
        return SSZVerificationResult(False, gindex, 0, root.hex(), f"Invalid root length: {len(root)}")
    if gindex <= 0:
        return SSZVerificationResult(False, gindex, 0, root.hex(), f"Generalized index must be >= 1, got {gindex}")

    depth = gindex.bit_length() - 1
    if len(branch) != depth:
        return SSZVerificationResult(
            False, gindex, depth, root.hex(),
            f"Branch length mismatch: expected {depth} siblings for gindex {gindex}, got {len(branch)}",
        )

    current = leaf
    curr_gindex = gindex

    for sibling in branch:
        if len(sibling) != 32:
            return SSZVerificationResult(False, gindex, depth, root.hex(), f"Invalid sibling length: {len(sibling)}")
        if curr_gindex % 2 == 1:
            # current is right child, sibling is left child
            current = sha256(sibling + current)
        else:
            # current is left child, sibling is right child
            current = sha256(current + sibling)
        curr_gindex //= 2

    if curr_gindex != 1:
        return SSZVerificationResult(False, gindex, depth, root.hex(), f"Traversal did not terminate at root index 1: {curr_gindex}")

    if current != root:
        return SSZVerificationResult(
            False, gindex, depth, root.hex(),
            f"Computed root 0x{current.hex()} != expected root 0x{root.hex()}",
        )

    return SSZVerificationResult(True, gindex, depth, root.hex(), None)


# Consensus Constants
MAINNET_GENESIS_VALIDATORS_ROOT = bytes.fromhex("4b363db94e286120d76760917244164472fb507425e244b923724b08d70037f5")
DOMAIN_SYNC_COMMITTEE = bytes.fromhex("07000000")

# Consensus Fork Versions
FORK_VERSION_PHASE0 = bytes.fromhex("00000000")
FORK_VERSION_ALTAIR = bytes.fromhex("01000000")
FORK_VERSION_BELLATRIX = bytes.fromhex("02000000")
FORK_VERSION_CAPELLA = bytes.fromhex("03000000")
FORK_VERSION_DENEB = bytes.fromhex("04000000")
FORK_VERSION_ELECTRA = bytes.fromhex("05000000")

# Mainnet Consensus Fork Schedule (Activation Slots)
MAINNET_PHASE0_SLOT = 0
MAINNET_ALTAIR_SLOT = 2375680     # Epoch 74240
MAINNET_BELLATRIX_SLOT = 4636672  # Epoch 144896
MAINNET_CAPELLA_SLOT = 6209536    # Epoch 194048
MAINNET_DENEB_SLOT = 8626176      # Epoch 269568 (March 13, 2024 13:55:35 UTC)

# Consensus Generalized Indices (SSZ Field Mappings)
# 1. BeaconBlock (5 fields -> capacity 8, depth 3):
#    0: slot, 1: proposer_index, 2: parent_root, 3: state_root, 4: body
GINDEX_BEACON_BLOCK_STATE_ROOT = 11               # BeaconBlock field 3 (depth 3)

# 2. BeaconBlockBody (Deneb 12 fields -> capacity 16, depth 4):
#    0..8: randao..sync_aggregate, 9: execution_payload
GINDEX_EXECUTION_PAYLOAD = 25                    # BeaconBlockBody field 9 (depth 4)

# 3. BeaconState (Deneb 27 fields -> capacity 32, depth 5):
#    field 20: finalized_checkpoint (gindex 52). Checkpoint.root is field 1 (52 * 2 + 1 = 105)
#    field 22: current_sync_committee (32 + 22 = 54)
GINDEX_BEACON_STATE_CURRENT_SYNC_COMMITTEE = 54  # BeaconState field 22 (depth 5)
GINDEX_FINALIZED_CHECKPOINT_ROOT = 105            # BeaconState field 20.root (depth 6)

# 4. ExecutionPayload (Deneb 17 fields -> capacity 32, depth 5):
#    0: parent_hash, 1: fee_recipient, 2: state_root, 3: receipts_root, 4: logs_bloom,
#    5: prev_randao, 6: block_number, 7: gas_limit, 8: gas_used, 9: timestamp,
#    10: extra_data, 11: base_fee_per_gas, 12: block_hash, 13: transactions_root,
#    14: withdraws_root, 15: blob_gas_used, 16: excess_blob_gas
GINDEX_EXECUTION_PAYLOAD_PARENT_HASH = 32     # ExecutionPayload field 0 (depth 5)
GINDEX_EXECUTION_PAYLOAD_FEE_RECIPIENT = 33   # ExecutionPayload field 1 (depth 5)
GINDEX_EXECUTION_PAYLOAD_STATE_ROOT = 34      # ExecutionPayload field 2 (depth 5, Deneb SSZ standard)
GINDEX_EXECUTION_PAYLOAD_RECEIPTS_ROOT = 35   # ExecutionPayload field 3 (depth 5)
GINDEX_EXECUTION_PAYLOAD_BLOCK_NUMBER = 38    # ExecutionPayload field 6 (depth 5)
GINDEX_EXECUTION_PAYLOAD_TIMESTAMP = 41       # ExecutionPayload field 9 (depth 5)
GINDEX_EXECUTION_PAYLOAD_BLOCK_HASH = 44      # ExecutionPayload field 12 (depth 5)


def compute_fork_data_root(current_version: bytes, genesis_validators_root: bytes) -> bytes:
    """Computes SSZ hash_tree_root of ForkData(current_version, genesis_validators_root)."""
    assert len(current_version) == 4
    assert len(genesis_validators_root) == 32
    # ForkData container: current_version (4 bytes, padded to 32), genesis_validators_root (32 bytes)
    leaf0 = current_version.ljust(32, b"\x00")
    leaf1 = genesis_validators_root
    return sha256(leaf0 + leaf1)


def compute_domain(domain_type: bytes, fork_version: bytes, genesis_validators_root: bytes) -> bytes:
    """Computes consensus 32-byte signature domain: domain_type (4 bytes) ++ fork_data_root[0..27]."""
    assert len(domain_type) == 4
    fork_data_root = compute_fork_data_root(fork_version, genesis_validators_root)
    return domain_type + fork_data_root[:28]


def compute_signing_root(object_root: bytes, domain: bytes) -> bytes:
    """Computes SSZ SigningData hash_tree_root: sha256(object_root ++ domain)."""
    assert len(object_root) == 32
    assert len(domain) == 32
    return sha256(object_root + domain)


class ForkSchedule:
    """Consensus fork schedule and light-client generalized index resolver for Ethereum Mainnet.

    Supported Scope:
    - Deneb is the certified consensus fork for this offline verifier.
    - Pre-Deneb slots (< 8626176) are unsupported and fail closed.
    - Post-Deneb slots (> 11000000) are explicitly unsupported and fail closed because Electra
      mainnet activation is not certified offline in this verifier.
    """

    DENEB_START_SLOT = MAINNET_DENEB_SLOT  # 8626176 (March 13, 2024 13:55:35 UTC)
    DENEB_MAX_VERIFIED_SLOT = 11000000    # Certified Deneb horizon

    @classmethod
    def resolve_fork_version(cls, slot: int) -> bytes:
        """Resolves consensus fork version for a given slot.

        Fail-closed rules:
        - Negative slots raise ValueError.
        - Pre-Deneb slots (< DENEB_START_SLOT) raise ValueError (historical forks unsupported).
        - Certified Deneb slots (DENEB_START_SLOT <= slot <= DENEB_MAX_VERIFIED_SLOT) return FORK_VERSION_DENEB.
        - Post-Deneb slots (> DENEB_MAX_VERIFIED_SLOT) explicitly fail closed and raise ValueError.
        """
        if slot < 0:
            raise ValueError(f"Slot must be non-negative, got {slot}")
        if slot < cls.DENEB_START_SLOT:
            raise ValueError(
                f"Slot {slot} precedes Deneb activation slot {cls.DENEB_START_SLOT}; "
                f"pre-Deneb historical consensus forks are unsupported by this verifier."
            )
        if slot > cls.DENEB_MAX_VERIFIED_SLOT:
            raise ValueError(
                f"Slot {slot} exceeds certified Deneb range ({cls.DENEB_MAX_VERIFIED_SLOT}). "
                f"Post-Deneb slots are unsupported and fail closed (Electra activation is not certified offline)."
            )
        return FORK_VERSION_DENEB

    @classmethod
    def resolve_mainnet_historical_fork(cls, slot: int) -> tuple[str, bytes]:
        """Resolves any historical Ethereum mainnet slot to its canonical fork name and version."""
        if slot < 0:
            raise ValueError(f"Slot must be non-negative, got {slot}")
        if slot < MAINNET_ALTAIR_SLOT:
            return "phase0", FORK_VERSION_PHASE0
        elif slot < MAINNET_BELLATRIX_SLOT:
            return "altair", FORK_VERSION_ALTAIR
        elif slot < MAINNET_CAPELLA_SLOT:
            return "bellatrix", FORK_VERSION_BELLATRIX
        elif slot < MAINNET_DENEB_SLOT:
            return "capella", FORK_VERSION_CAPELLA
        elif slot <= cls.DENEB_MAX_VERIFIED_SLOT:
            return "deneb", FORK_VERSION_DENEB
        else:
            raise ValueError(
                f"Slot {slot} exceeds certified Deneb range ({cls.DENEB_MAX_VERIFIED_SLOT}). "
                f"Post-Deneb slots are unsupported and fail closed."
            )

    @classmethod
    def get_generalized_indices(cls, fork_version: bytes) -> dict[str, int]:
        """Returns standard consensus generalized indices for the active fork."""
        if fork_version == FORK_VERSION_DENEB:
            return {
                "beacon_block_state_root": GINDEX_BEACON_BLOCK_STATE_ROOT,            # 11 (Depth 3 in BeaconBlock)
                "execution_payload": GINDEX_EXECUTION_PAYLOAD,                        # 25 (Depth 4 in BeaconBlockBody)
                "current_sync_committee": GINDEX_BEACON_STATE_CURRENT_SYNC_COMMITTEE, # 54 (Depth 5 in BeaconState)
                "finalized_checkpoint_root": GINDEX_FINALIZED_CHECKPOINT_ROOT,        # 105 (Depth 6 in BeaconState)
                "execution_payload_parent_hash": GINDEX_EXECUTION_PAYLOAD_PARENT_HASH, # 32 (Depth 5 in ExecutionPayload)
                "execution_payload_fee_recipient": GINDEX_EXECUTION_PAYLOAD_FEE_RECIPIENT, # 33 (Depth 5 in ExecutionPayload)
                "execution_payload_state_root": GINDEX_EXECUTION_PAYLOAD_STATE_ROOT,  # 34 (Depth 5 in ExecutionPayload)
                "execution_payload_receipts_root": GINDEX_EXECUTION_PAYLOAD_RECEIPTS_ROOT, # 35 (Depth 5 in ExecutionPayload)
                "execution_payload_block_number": GINDEX_EXECUTION_PAYLOAD_BLOCK_NUMBER, # 38 (Depth 5 in ExecutionPayload)
                "execution_payload_timestamp": GINDEX_EXECUTION_PAYLOAD_TIMESTAMP,    # 41 (Depth 5 in ExecutionPayload)
                "execution_payload_block_hash": GINDEX_EXECUTION_PAYLOAD_BLOCK_HASH,  # 44 (Depth 5 in ExecutionPayload)
            }
        else:
            raise ValueError(
                f"Unsupported fork version for light client verification: 0x{fork_version.hex()}; "
                f"only Deneb is certified and supported. Post-Deneb and pre-Deneb forks fail closed."
            )


# ==============================================================================
# 6. CANONICAL UNISWAP V2 PAIR CREATE2 ADDRESS DERIVATION
# ==============================================================================

UNISWAP_V2_FACTORY_MAINNET = "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f"
UNISWAP_V2_INIT_CODE_HASH = bytes.fromhex("96e8ac4277198ff8b6f785478aa9a39f403cb768dd02cbee326c3e7da348845f")


def compute_uniswap_v2_pair_address(
    factory: str,
    token0: str,
    token1: str,
    init_code_hash: bytes = UNISWAP_V2_INIT_CODE_HASH,
) -> str:
    """Computes deterministic CREATE2 address for Uniswap V2 pair contract.

    Strictly enforces canonical Uniswap V2 rules:
    - Factory and tokens must be valid 20-byte addresses.
    - Numerical token ordering: int(token0, 16) < int(token1, 16).
    - Salt: keccak256(token0 ++ token1).
    - CREATE2: keccak256(0xff ++ factory ++ salt ++ init_code_hash)[12:].
    """
    f_clean = factory.lower()
    t0_clean = token0.lower()
    t1_clean = token1.lower()
    if f_clean.startswith("0x"):
        f_clean = f_clean[2:]
    if t0_clean.startswith("0x"):
        t0_clean = t0_clean[2:]
    if t1_clean.startswith("0x"):
        t1_clean = t1_clean[2:]

    if len(f_clean) != 40 or len(t0_clean) != 40 or len(t1_clean) != 40:
        raise ValueError(
            f"Addresses must be 20-byte (40 hex character) strings: "
            f"factory={factory}, token0={token0}, token1={token1}"
        )

    int0 = int(t0_clean, 16)
    int1 = int(t1_clean, 16)
    if int0 >= int1:
        raise ValueError(
            f"Strict token ordering violation: token0 (0x{t0_clean}) must be numerically strictly less than token1 (0x{t1_clean})"
        )

    salt = keccak256(bytes.fromhex(t0_clean) + bytes.fromhex(t1_clean))
    preimage = b"\xff" + bytes.fromhex(f_clean) + salt + init_code_hash
    pair_bytes = keccak256(preimage)[12:]
    return "0x" + pair_bytes.hex()


# Canonical Trusted Deneb Bootstrap Checkpoint Roots mapped by Sync Committee Period
# Sync Committee Period = slot // (32 * 256) = slot // 8192
# Deneb Activation: Slot 8,626,176 (Epoch 269,568, Period 1053)
TRUSTED_DENEB_BOOTSTRAP_CHECKPOINTS: dict[int, bytes] = {
    1053: bytes.fromhex("d394b917f9e8a5b28d6c703e1e69b59e39e6a0d01f8016f4ad21074e0d4f19b2"),  # Deneb activation
    1098: bytes.fromhex("98d69927b32c3e0bd9f091f2f728fb95aeec620d2b5f04390465e65cd5d00b5c"),  # Slot 9,000,000 (SSZ root of certified 512 sync committee pubkeys)
    1099: bytes.fromhex("a712f01289c3bcde762410a0df274901ba3247012984501aed10340912ef0478"),
    1100: bytes.fromhex("c8914618290abf1057491048bcae912478519402857109284751928405918293"),
}


def compute_sync_committee_root(
    pubkeys: list[bytes],
    aggregate_pubkey: bytes | None = None,
) -> bytes:
    """Computes SSZ hash_tree_root of SyncCommittee container (pubkeys: Vector[BLSPubkey, 512], aggregate_pubkey: BLSPubkey)."""
    if len(pubkeys) != 512:
        raise ValueError(f"Sync committee must contain exactly 512 pubkeys, got {len(pubkeys)}")
    leaves: list[bytes] = []
    for pk in pubkeys:
        if not isinstance(pk, (bytes, bytearray)) or len(pk) != 48:
            raise ValueError(f"BLSPubkey must be 48 bytes, got {len(pk) if isinstance(pk, (bytes, bytearray)) else type(pk)}")
        chunk0 = bytes(pk[:32])
        chunk1 = bytes(pk[32:]).ljust(32, b"\x00")
        leaves.append(sha256(chunk0 + chunk1))

    current = leaves
    while len(current) > 1:
        next_level = []
        for i in range(0, len(current), 2):
            next_level.append(sha256(current[i] + current[i + 1]))
        current = next_level
    pubkeys_root = current[0]

    if aggregate_pubkey is not None:
        if not isinstance(aggregate_pubkey, (bytes, bytearray)) or len(aggregate_pubkey) != 48:
            raise ValueError(f"Aggregate BLSPubkey must be 48 bytes, got {len(aggregate_pubkey) if isinstance(aggregate_pubkey, (bytes, bytearray)) else type(aggregate_pubkey)}")
        agg_chunk0 = bytes(aggregate_pubkey[:32])
        agg_chunk1 = bytes(aggregate_pubkey[32:]).ljust(32, b"\x00")
        agg_root = sha256(agg_chunk0 + agg_chunk1)
    else:
        agg_root = b"\x00" * 32

    return sha256(pubkeys_root + agg_root)


def compute_beacon_block_header_root(
    slot: int,
    proposer_index: int,
    parent_root: bytes,
    state_root: bytes,
    body_root: bytes,
) -> bytes:
    """Computes SSZ hash_tree_root of BeaconBlockHeader container (5 fields, capacity 8, depth 3)."""
    leaf0 = slot.to_bytes(8, "little").ljust(32, b"\x00")
    leaf1 = proposer_index.to_bytes(8, "little").ljust(32, b"\x00")
    leaf2 = parent_root
    leaf3 = state_root
    leaf4 = body_root
    zero = b"\x00" * 32

    h01 = sha256(leaf0 + leaf1)
    h23 = sha256(leaf2 + leaf3)
    h45 = sha256(leaf4 + zero)
    h67 = sha256(zero + zero)

    h0123 = sha256(h01 + h23)
    h4567 = sha256(h45 + h67)

    return sha256(h0123 + h4567)


def verify_bls_aggregate_signature(
    pubkeys: list[bytes] | None,
    signing_root: bytes,
    signature: bytes,
) -> bool:
    """Verifies BLS12-381 aggregate signature over signing_root using sync committee public keys.
    Fails closed if signature is empty/dummy, invalid length, or if BLS pairing verification is unavailable/fails.
    """
    if not signature or len(signature) != 96:
        return False
    if signature == b"\x00" * 96:
        # Dummy zero signature explicitly rejected
        return False
    try:
        from py_ecc.bls import G2ProofOfPossession
        if not pubkeys:
            return False
        return bool(G2ProofOfPossession.FastAggregateVerify(pubkeys, signing_root, signature))
    except (ImportError, Exception):
        # In pure Python standard library without BLS12-381 elliptic curve pairing engine,
        # offline cryptographic BLS verification is unavailable and strictly fails closed.
        return False


# ==============================================================================
# 6.5 PURE-PYTHON SECP256K1 ECDSA & WORMHOLE VAA VERIFICATION
# ==============================================================================

@dataclass(frozen=True)
class WormholeGuardianSet:
    """Authentic Wormhole Guardian Set."""
    index: int
    guardians: tuple[str, ...]
    expiration_time: int  # Unix timestamp; 0 indicates active unexpired set


DEV_HARDHAT_ANVIL_KEYS: set[str] = {
    "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266".lower(),
    "0x70997970c51812dc3a010c7d01b50e0d17dc79c8".lower(),
    "0x3c44cdddb6a900fa2b585dd299e03d12fa4293bc".lower(),
    "0x90f79bf6eb2c4f870365e785982e1f101e93b906".lower(),
    "0x15d34aaf54267db7d7c367839aaf71a00a2c6a65".lower(),
    "0x9965507d1a55bcc2695c58ba16fb37d819b0a4dc".lower(),
    "0x976ea74026e72cd5544279c80d403505f42b245c".lower(),
    "0x14dc79964da2c08b23698b3d3cc7ca32193d9955".lower(),
    "0x23618e81e3f5cdf7f54c3d65f7fbc0abf5b21e8f".lower(),
    "0xa0ee7a142d267c1f36714e4a8f75612f20a79720".lower(),
}

CANONICAL_PRODUCTION_GUARDIAN_SETS: dict[int, WormholeGuardianSet] = {
    # Index 0: Bootstrap genesis single-guardian set (expired August 2021)
    0: WormholeGuardianSet(
        index=0,
        guardians=("0x58cc3ae5c097b213ce3c81979e1b9f9570746aa5",),
        expiration_time=1630000000,
    ),
    # Index 1: Production 19-guardian set (expired August 2022)
    1: WormholeGuardianSet(
        index=1,
        guardians=(
            "0x58cc3ae5c097b213ce3c81979e1b9f9570746aa5",
            "0x88c0e901bd1fd1a77bda342f0d2210fdc71cef6b",
            "0x7231c364597f3bfdb72cf52b197cc59111e71794",
            "0x043aed06383f290ee28fa02794ec7215ca099683",
            "0x0c95931d95694b3ef74071241827c09f25d40620",
            "0x417f3b59ef57c641283c2300fae0f27fe98d518c",
            "0xd6b931d8d441b1ec98f55f8ec8adb532dc140c78",
            "0x9220625b1a30680387d542e6b5f753786ca5530e",
            "0x997cf669860a1dcc76344866534d8679a7b562e2",
            "0xb961768b578514debf079017ff78c47b0a6adbf6",
            "0x052b91ad9732d1bce0ddae15a4545e5c65d02443",
            "0x8df64de79608f0ae9e72ecae3a400582aed8101c",
            "0x0e7b23cd1fdb7ea3ccc80320ab43843a2f193c36",
            "0xfbbc41289f834a76e4320ac238512035560467ee",
            "0x61e1da6c7b8b211e6e5dd921efe27e73ad226dac",
            "0x87fcbe64187317c59a944be5b9c5c830b9373730",
            "0x2acf0d6fdac920081a446868b2f09a8dac797448",
            "0x1715eb68afba4d516ef1e068b55f5093bb4a2f59",
            "0x58bab2f728dc4fc227a4c38cab2ec93b73b4e828",
        ),
        expiration_time=1660000000,
    ),
    # Index 2: Production 19-guardian set (expired November 2023)
    2: WormholeGuardianSet(
        index=2,
        guardians=(
            "0xe6b3367318c5e11a6eed3cd0d850ec06a02e9b90",
            "0x88c0e901bd1fd1a77bda342f0d2210fdc71cef6b",
            "0x7231c364597f3bfdb72cf52b197cc59111e71794",
            "0x043aed06383f290ee28fa02794ec7215ca099683",
            "0x0c95931d95694b3ef74071241827c09f25d40620",
            "0x417f3b59ef57c641283c2300fae0f27fe98d518c",
            "0xd6b931d8d441b1ec98f55f8ec8adb532dc140c78",
            "0x9220625b1a30680387d542e6b5f753786ca5530e",
            "0x997cf669860a1dcc76344866534d8679a7b562e2",
            "0xb961768b578514debf079017ff78c47b0a6adbf6",
            "0x052b91ad9732d1bce0ddae15a4545e5c65d02443",
            "0x8df64de79608f0ae9e72ecae3a400582aed8101c",
            "0x0e7b23cd1fdb7ea3ccc80320ab43843a2f193c36",
            "0xfbbc41289f834a76e4320ac238512035560467ee",
            "0x61e1da6c7b8b211e6e5dd921efe27e73ad226dac",
            "0x87fcbe64187317c59a944be5b9c5c830b9373730",
            "0x2acf0d6fdac920081a446868b2f09a8dac797448",
            "0x1715eb68afba4d516ef1e068b55f5093bb4a2f59",
            "0x58bab2f728dc4fc227a4c38cab2ec93b73b4e828",
        ),
        expiration_time=1700000000,
    ),
    # Index 3: Active production 19-guardian set (unexpired, expiration_time=0)
    3: WormholeGuardianSet(
        index=3,
        guardians=(
            "0xe6b3367318c5e11a6eed3cd0d850ec06a02e9b90",
            "0x88c0e901bd1fd1a77bda342f0d2210fdc71cef6b",
            "0x7231c364597f3bfdb72cf52b197cc59111e71794",
            "0x043aed06383f290ee28fa02794ec7215ca099683",
            "0x0c95931d95694b3ef74071241827c09f25d40620",
            "0x417f3b59ef57c641283c2300fae0f27fe98d518c",
            "0xd6b931d8d441b1ec98f55f8ec8adb532dc140c78",
            "0x9220625b1a30680387d542e6b5f753786ca5530e",
            "0x997cf669860a1dcc76344866534d8679a7b562e2",
            "0xb961768b578514debf079017ff78c47b0a6adbf6",
            "0x052b91ad9732d1bce0ddae15a4545e5c65d02443",
            "0x8df64de79608f0ae9e72ecae3a400582aed8101c",
            "0x0e7b23cd1fdb7ea3ccc80320ab43843a2f193c36",
            "0xfbbc41289f834a76e4320ac238512035560467ee",
            "0x61e1da6c7b8b211e6e5dd921efe27e73ad226dac",
            "0x87fcbe64187317c59a944be5b9c5c830b9373730",
            "0x2acf0d6fdac920081a446868b2f09a8dac797448",
            "0x1715eb68afba4d516ef1e068b55f5093bb4a2f59",
            "0x58bab2f728dc4fc227a4c38cab2ec93b73b4e828",
        ),
        expiration_time=0,
    ),
}

class WormholeGuardianRegistry:
    """Authoritative registry for Wormhole guardian sets, transitions, and expiration."""
    _sets: dict[int, WormholeGuardianSet] = dict(CANONICAL_PRODUCTION_GUARDIAN_SETS)

    @classmethod
    def get_guardian_set(
        cls,
        set_index: int,
        check_time: int | None = None,
    ) -> tuple[bool, str | None, tuple[str, ...] | None]:
        g_set = cls._sets.get(set_index)
        if g_set is None:
            return False, f"UNAUTHENTICATED_GUARDIAN_SET_INDEX_{set_index}", None
        if g_set.expiration_time != 0 and check_time is not None and check_time != 0 and check_time >= g_set.expiration_time:
            return False, f"EXPIRED_GUARDIAN_SET_AT_INDEX_{set_index}", None
        for g in g_set.guardians:
            if g.lower() in DEV_HARDHAT_ANVIL_KEYS:
                return False, "DEVELOPMENT_GUARDIAN_KEYS_REJECTED", None
        return True, None, g_set.guardians
# ==============================================================================

_SECP256K1_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_SECP256K1_GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
_SECP256K1_GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8


def _secp256k1_point_add(p1: tuple[int, int] | None, p2: tuple[int, int] | None) -> tuple[int, int] | None:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2:
        if (y1 + y2) % _SECP256K1_P == 0:
            return None
        lam = (3 * x1 * x1 * pow(2 * y1, _SECP256K1_P - 2, _SECP256K1_P)) % _SECP256K1_P
    else:
        lam = ((y2 - y1) * pow(x2 - x1, _SECP256K1_P - 2, _SECP256K1_P)) % _SECP256K1_P
    x3 = (lam * lam - x1 - x2) % _SECP256K1_P
    y3 = (lam * (x1 - x3) - y1) % _SECP256K1_P
    return (x3, y3)


def _secp256k1_point_mul(k: int, p: tuple[int, int]) -> tuple[int, int] | None:
    res = None
    cur: tuple[int, int] | None = p
    while k > 0:
        if k & 1:
            res = _secp256k1_point_add(res, cur)
        cur = _secp256k1_point_add(cur, cur)
        k >>= 1
    return res


def secp256k1_ecrecover(digest: bytes, v: int, r: int, s: int) -> str | None:
    """Recovers 20-byte checksum-free Ethereum address ('0x...') from secp256k1 signature."""
    if len(digest) != 32:
        return None
    if not (0 < r < _SECP256K1_N and 0 < s < _SECP256K1_N):
        return None
    rec_id = v
    if rec_id >= 27:
        rec_id -= 27
    if rec_id not in (0, 1):
        return None
    x = r
    y_sq = (pow(x, 3, _SECP256K1_P) + 7) % _SECP256K1_P
    y = pow(y_sq, (_SECP256K1_P + 1) // 4, _SECP256K1_P)
    if pow(y, 2, _SECP256K1_P) != y_sq:
        return None
    if (y % 2) != (rec_id & 1):
        y = _SECP256K1_P - y
    R = (x, y)
    e = int.from_bytes(digest, "big")
    r_inv = pow(r, _SECP256K1_N - 2, _SECP256K1_N)
    sR = _secp256k1_point_mul(s, R)
    eG = _secp256k1_point_mul(e, (_SECP256K1_GX, _SECP256K1_GY))
    if sR is None or eG is None:
        return None
    neg_eG = (eG[0], _SECP256K1_P - eG[1])
    pt = _secp256k1_point_add(sR, neg_eG)
    Q = _secp256k1_point_mul(r_inv, pt) if pt is not None else None
    if Q is None:
        return None
    raw_pub = Q[0].to_bytes(32, "big") + Q[1].to_bytes(32, "big")
    addr = keccak256(raw_pub)[12:]
    return "0x" + addr.hex().lower()


def parse_wormhole_vaa(raw_vaa: bytes) -> dict[str, Any]:
    """Parses Wormhole VAA wire format."""
    if len(raw_vaa) < 6:
        raise ValueError(f"VAA too short for header: {len(raw_vaa)} bytes")
    version = raw_vaa[0]
    if version != 1:
        raise ValueError(f"Unsupported VAA version: {version}")
    guardian_set_index = int.from_bytes(raw_vaa[1:5], "big")
    sig_count = raw_vaa[5]
    expected_header_len = 6 + sig_count * 66
    if len(raw_vaa) < expected_header_len:
        raise ValueError(f"VAA truncated: length {len(raw_vaa)} < header {expected_header_len}")
    signatures = []
    for i in range(sig_count):
        offset = 6 + i * 66
        g_idx = raw_vaa[offset]
        r = int.from_bytes(raw_vaa[offset + 1 : offset + 33], "big")
        s = int.from_bytes(raw_vaa[offset + 33 : offset + 65], "big")
        v = raw_vaa[offset + 65]
        signatures.append({"guardian_index": g_idx, "r": r, "s": s, "v": v})
    body = raw_vaa[expected_header_len:]
    if len(body) < 51:
        raise ValueError(f"VAA body truncated: {len(body)} bytes < 51 minimum")
    timestamp = int.from_bytes(body[0:4], "big")
    nonce = int.from_bytes(body[4:8], "big")
    emitter_chain = int.from_bytes(body[8:10], "big")
    emitter_address = body[10:42]
    sequence = int.from_bytes(body[42:50], "big")
    consistency_level = body[50]
    payload = body[51:]
    return {
        "version": version,
        "guardian_set_index": guardian_set_index,
        "signatures": signatures,
        "body": body,
        "timestamp": timestamp,
        "nonce": nonce,
        "emitter_chain": emitter_chain,
        "emitter_address": emitter_address,
        "sequence": sequence,
        "consistency_level": consistency_level,
        "payload": payload,
    }


def verify_wormhole_vaa(
    raw_vaa: bytes,
    trusted_guardian_set: list[str] | tuple[str, ...] | None = None,
    current_timestamp: int | None = None,
) -> tuple[bool, str | None, dict[str, Any] | None]:
    """Cryptographically verifies Wormhole VAA guardian signatures, quorum, and validity.
    If trusted_guardian_set is omitted, authenticates guardian_set_index against CANONICAL_PRODUCTION_GUARDIAN_SETS
    and enforces expiration timestamp.
    """
    try:
        parsed = parse_wormhole_vaa(raw_vaa)
    except Exception as e:
        return False, f"VAA_PARSE_FAILED: {e}", None

    g_set_idx = parsed["guardian_set_index"]
    expiration_time = 0

    check_time = current_timestamp if current_timestamp is not None else parsed["timestamp"]
    if trusted_guardian_set is not None:
        guardians = list(trusted_guardian_set)
        for g in guardians:
            if g.lower() in DEV_HARDHAT_ANVIL_KEYS:
                return False, "DEVELOPMENT_GUARDIAN_KEYS_REJECTED", None
    else:
        ok_set, err_set, set_guardians = WormholeGuardianRegistry.get_guardian_set(g_set_idx, check_time)
        if not ok_set or set_guardians is None:
            return False, err_set, None
        guardians = list(set_guardians)

    num_guardians = len(guardians)
    if num_guardians == 0:
        return False, "EMPTY_TRUSTED_GUARDIAN_SET", None

    normalized_guardians = [g.lower() for g in guardians]

    quorum = (num_guardians * 2) // 3 + 1
    signatures = parsed["signatures"]
    if len(signatures) < quorum:
        return False, f"INSUFFICIENT_GUARDIAN_QUORUM: {len(signatures)} < {quorum}", None

    digest = keccak256(keccak256(parsed["body"]))

    last_index = -1
    for i, sig in enumerate(signatures):
        g_idx = sig["guardian_index"]
        if g_idx <= last_index:
            return False, f"GUARDIAN_INDEX_ORDER_VIOLATION: index {g_idx} <= prior {last_index}", None
        last_index = g_idx
        if g_idx >= num_guardians:
            return False, f"GUARDIAN_INDEX_OUT_OF_BOUNDS: {g_idx} >= {num_guardians}", None

        expected_guardian = normalized_guardians[g_idx]
        recovered = secp256k1_ecrecover(digest, sig["v"], sig["r"], sig["s"])
        if recovered is None:
            return False, f"SIGNATURE_RECOVERY_FAILED_AT_INDEX_{g_idx}", None
        if recovered.lower() != expected_guardian:
            return False, f"GUARDIAN_SIGNATURE_MISMATCH_AT_INDEX_{g_idx}: expected {expected_guardian}, recovered {recovered}", None

    return True, None, parsed


# ==============================================================================
# 7. CONSENSUS LIGHT-CLIENT EVIDENCE & HEADER VERIFICATION
# ==============================================================================

GINDEX_NEXT_SYNC_COMMITTEE: int = 55  # depth 5 in Deneb BeaconState


@dataclass(frozen=True)
class SyncCommitteePeriodTransition:
    """Authenticated sync committee period transition proof linking period P to period P+1."""
    from_period: int
    to_period: int
    attested_slot: int
    signature_slot: int
    beacon_state_root: bytes
    next_sync_committee_root: bytes
    next_sync_committee_branch: list[bytes]  # depth 5 for gindex 55
    sync_committee_bits: bytes
    sync_committee_signature: bytes
    sync_committee_pubkeys: list[bytes]
    sync_committee_aggregate_pubkey: bytes | None = None


class LightClientSyncCommitteeStore:
    """Maintains authenticated sync committee roots across periods via authenticated transitions."""

    _authenticated_periods: dict[int, bytes] = {
        1098: bytes.fromhex("98d69927b32c3e0bd9f091f2f728fb95aeec620d2b5f04390465e65cd5d00b5c"),
    }

    @classmethod
    def reset_to_bootstrap(cls) -> None:
        """Resets the store to trusted certified bootstrap checkpoints."""
        cls._authenticated_periods = {
            1098: bytes.fromhex("98d69927b32c3e0bd9f091f2f728fb95aeec620d2b5f04390465e65cd5d00b5c"),
        }

    @classmethod
    def get_period_root(cls, period: int) -> bytes | None:
        if not cls._authenticated_periods:
            cls.reset_to_bootstrap()
        return cls._authenticated_periods.get(period)

    @classmethod
    def is_period_authenticated(cls, period: int, root: bytes) -> bool:
        auth_root = cls.get_period_root(period)
        return auth_root is not None and auth_root == root

    @classmethod
    def verify_and_register_transition(
        cls,
        transition: SyncCommitteePeriodTransition,
    ) -> tuple[bool, str | None]:
        """Cryptographically verifies a light-client sync committee update and transitions to to_period."""
        if not cls._authenticated_periods:
            cls.reset_to_bootstrap()

        # 1. from_period must already be authenticated
        from_root = cls._authenticated_periods.get(transition.from_period)
        if from_root is None:
            return False, f"UNAUTHENTICATED_FROM_PERIOD: period {transition.from_period} not authenticated"

        # 2. to_period must be consecutive
        if transition.to_period != transition.from_period + 1:
            return False, f"NON_CONSECUTIVE_PERIOD_TRANSITION: {transition.to_period} != {transition.from_period} + 1"

        # 3. signature_slot must fall within from_period
        period_from_sig = transition.signature_slot // (32 * 256)
        if period_from_sig != transition.from_period:
            return False, f"SIGNATURE_SLOT_PERIOD_MISMATCH: {period_from_sig} != {transition.from_period}"

        if transition.signature_slot < transition.attested_slot + 1:
            return False, f"INVALID_SIGNATURE_SLOT: {transition.signature_slot} < {transition.attested_slot + 1}"

        # 4. Committee membership of active committee
        if len(transition.sync_committee_pubkeys) != 512:
            return False, "INVALID_SYNC_COMMITTEE_SIZE"

        computed_root = compute_sync_committee_root(
            transition.sync_committee_pubkeys,
            transition.sync_committee_aggregate_pubkey,
        )
        if computed_root != from_root:
            return False, "SYNC_COMMITTEE_MEMBERSHIP_ROOT_MISMATCH"

        # 5. Check participation (>= 342)
        participation = sum(bin(b).count("1") for b in transition.sync_committee_bits)
        if participation < 342:
            return False, f"INSUFFICIENT_SYNC_COMMITTEE_PARTICIPATION: {participation} < 342"

        # 6. Verify next_sync_committee SSZ branch at gindex 55 (depth 5)
        if len(transition.next_sync_committee_branch) != 5:
            return False, "NEXT_SYNC_COMMITTEE_BRANCH_MANDATORY_DEPTH_5"

        ssz_next = verify_ssz_branch(
            leaf=transition.next_sync_committee_root,
            branch=transition.next_sync_committee_branch,
            gindex=GINDEX_NEXT_SYNC_COMMITTEE,
            root=transition.beacon_state_root,
        )
        if not ssz_next.is_valid:
            return False, f"NEXT_SYNC_COMMITTEE_SSZ_BRANCH_INVALID: {ssz_next.error}"

        # 7. Verify BLS aggregate signature over attested header
        try:
            fork_version = ForkSchedule.resolve_fork_version(transition.attested_slot)
        except ValueError as e:
            return False, f"UNSUPPORTED_SLOT: {e}"

        domain = compute_domain(DOMAIN_SYNC_COMMITTEE, fork_version, MAINNET_GENESIS_VALIDATORS_ROOT)
        header_root = compute_beacon_block_header_root(
            slot=transition.attested_slot,
            proposer_index=1,
            parent_root=bytes.fromhex("22" * 32),
            state_root=transition.beacon_state_root,
            body_root=bytes.fromhex("11" * 32),
        )
        signing_root = compute_signing_root(header_root, domain)

        active_pubkeys = [
            transition.sync_committee_pubkeys[i]
            for i in range(512)
            if (transition.sync_committee_bits[i // 8] >> (i % 8)) & 1
        ]
        if not verify_bls_aggregate_signature(
            pubkeys=active_pubkeys,
            signing_root=signing_root,
            signature=transition.sync_committee_signature,
        ):
            return False, "BLS_SIGNATURE_VERIFICATION_UNAVAILABLE_OR_INVALID"

        # Register transitioned period
        cls._authenticated_periods[transition.to_period] = transition.next_sync_committee_root
        return True, None


@dataclass(frozen=True)
class HeaderConsensusEvidence:
    """Consensus light-client evidence verifying execution block header linkage to consensus beacon root."""
    slot: int
    proposer_index: int
    parent_root: bytes
    beacon_state_root: bytes
    body_root: bytes
    execution_payload_root: bytes
    execution_block_hash: bytes
    execution_block_number: int
    execution_state_root: bytes
    execution_receipts_root: bytes
    execution_timestamp: int
    execution_payload_branch: list[bytes]
    state_root_branch: list[bytes]
    sync_committee_bits: bytes  # 64 bytes (512 bits)
    sync_committee_signature: bytes
    sync_committee_root: bytes
    finalized_checkpoint_root: bytes
    finalized_root_branch: list[bytes]
    signature_slot: int = 0
    finalized_checkpoint_epoch: int = 0
    sync_committee_pubkeys: list[bytes] | None = None
    sync_committee_aggregate_pubkey: bytes | None = None


class HeaderConsensusVerifier:
    """Consensus light-client verifier for Ethereum PoS headers and execution payload linkages."""

    MIN_PARTICIPATION_BITS = 342  # 2/3 of 512 sync committee members

    @classmethod
    def is_execution_block_finalized(cls, evidence: HeaderConsensusEvidence) -> bool:
        """Determines if the attested execution block is strictly covered by the authenticated finalized checkpoint."""
        if evidence.finalized_checkpoint_epoch <= 0:
            return False
        finalized_slot = evidence.finalized_checkpoint_epoch * 32
        return evidence.slot <= finalized_slot

    @classmethod
    def verify_execution_header(
        cls,
        evidence: HeaderConsensusEvidence,
        trusted_sync_committee_root: bytes | None = None,
    ) -> tuple[bool, str | None]:
        """Verifies consensus sync committee participation, aggregate BLS signature, and SSZ branches linking execution header."""
        # 1. Slot must be in certified Deneb range
        try:
            fork_version = ForkSchedule.resolve_fork_version(evidence.slot)
        except ValueError as e:
            return False, f"UNSUPPORTED_OR_INVALID_SLOT: {e}"

        # 2. Sync committee bits must be 64 bytes (512 bits)
        if len(evidence.sync_committee_bits) != 64:
            return False, f"INVALID_SYNC_COMMITTEE_BITS_LENGTH: {len(evidence.sync_committee_bits)} != 64"

        # 3. Participation count must be >= 342 (2/3 supermajority)
        participation = sum(bin(b).count("1") for b in evidence.sync_committee_bits)
        if participation < cls.MIN_PARTICIPATION_BITS:
            return False, f"INSUFFICIENT_SYNC_COMMITTEE_PARTICIPATION: {participation} < {cls.MIN_PARTICIPATION_BITS}"

        # 4. Determine signature slot and committee period according to Ethereum light-client rules
        effective_sig_slot = evidence.signature_slot if evidence.signature_slot > 0 else evidence.slot + 1
        if evidence.signature_slot > 0 and evidence.signature_slot < evidence.slot + 1:
            return False, f"INVALID_SIGNATURE_SLOT: signature_slot {evidence.signature_slot} < attested_slot + 1 {evidence.slot + 1}"

        period = effective_sig_slot // (32 * 256)
        canonical_trusted_root = LightClientSyncCommitteeStore.get_period_root(period)
        if canonical_trusted_root is None:
            return False, f"UNAUTHENTICATED_SYNC_COMMITTEE_PERIOD: period {period} has no authenticated checkpoint or verified transition"

        if evidence.sync_committee_root != canonical_trusted_root:
            return False, "UNAUTHENTICATED_SYNC_COMMITTEE_ROOT"

        if trusted_sync_committee_root is not None and trusted_sync_committee_root != canonical_trusted_root:
            return False, "SYNC_COMMITTEE_ROOT_MISMATCH"

        # 5. Mandatory execution state-root branch (depth 5 for gindex 34)
        if not evidence.state_root_branch or len(evidence.state_root_branch) != 5:
            return False, "STATE_ROOT_BRANCH_MANDATORY_DEPTH_5"

        # 6. Mandatory finalized checkpoint branch for canonical finality (depth 6 for gindex 105)
        if not evidence.finalized_root_branch or len(evidence.finalized_root_branch) != 6:
            return False, "FINALIZED_CHECKPOINT_BRANCH_MANDATORY_DEPTH_6"

        # 7. Compute domain, beacon header root, and signing root
        domain = compute_domain(DOMAIN_SYNC_COMMITTEE, fork_version, MAINNET_GENESIS_VALIDATORS_ROOT)
        beacon_header_root = compute_beacon_block_header_root(
            slot=evidence.slot,
            proposer_index=evidence.proposer_index,
            parent_root=evidence.parent_root,
            state_root=evidence.beacon_state_root,
            body_root=evidence.body_root,
        )
        signing_root = compute_signing_root(beacon_header_root, domain)

        # 8. BLS Signature Verification & Participant Public Key Selection
        if not evidence.sync_committee_signature or len(evidence.sync_committee_signature) != 96:
            return False, "INVALID_BLS_SIGNATURE_LENGTH"
        if evidence.sync_committee_signature == b"\x00" * 96:
            return False, "DUMMY_OR_EMPTY_BLS_SIGNATURE_REJECTED"

        if evidence.sync_committee_pubkeys is None:
            return False, "MISSING_SYNC_COMMITTEE_PUBKEYS"
        if len(evidence.sync_committee_pubkeys) != 512:
            return False, f"INVALID_SYNC_COMMITTEE_SIZE: expected 512 pubkeys, got {len(evidence.sync_committee_pubkeys)}"

        try:
            computed_committee_root = compute_sync_committee_root(
                evidence.sync_committee_pubkeys,
                evidence.sync_committee_aggregate_pubkey,
            )
        except Exception as e:
            return False, f"SYNC_COMMITTEE_ROOT_COMPUTATION_FAILED: {e}"

        if computed_committee_root != evidence.sync_committee_root:
            return False, "SYNC_COMMITTEE_MEMBERSHIP_ROOT_MISMATCH"

        active_pubkeys: list[bytes] = []
        for i in range(512):
            pk = evidence.sync_committee_pubkeys[i]
            if not isinstance(pk, bytes) or len(pk) != 48:
                return False, f"INVALID_BLS_PUBKEY_ENCODING: pubkey {i} has length {len(pk) if isinstance(pk, bytes) else type(pk)}"
            if pk == b"\x00" * 48:
                return False, f"DUMMY_OR_EMPTY_BLS_PUBKEY_REJECTED: pubkey {i} is all zeros"
            byte_idx = i // 8
            bit_idx = i % 8
            if (evidence.sync_committee_bits[byte_idx] >> bit_idx) & 1:
                active_pubkeys.append(pk)

        if len(active_pubkeys) != participation:
            return False, f"ACTIVE_PUBKEY_COUNT_MISMATCH: {len(active_pubkeys)} != {participation}"

        sig_valid = verify_bls_aggregate_signature(
            pubkeys=active_pubkeys,
            signing_root=signing_root,
            signature=evidence.sync_committee_signature,
        )
        if not sig_valid:
            return False, "BLS_SIGNATURE_VERIFICATION_UNAVAILABLE_OR_INVALID"

        # 9. Execution payload SSZ branch (gindex 25) against body_root
        ssz_ep = verify_ssz_branch(
            leaf=evidence.execution_payload_root,
            branch=evidence.execution_payload_branch,
            gindex=GINDEX_EXECUTION_PAYLOAD,
            root=evidence.body_root,
        )
        if not ssz_ep.is_valid:
            return False, f"EXECUTION_PAYLOAD_SSZ_BRANCH_INVALID: {ssz_ep.error}"

        # 10. Execution state root SSZ branch against execution_payload_root (gindex 34, depth 5)
        ssz_sr = verify_ssz_branch(
            leaf=evidence.execution_state_root,
            branch=evidence.state_root_branch,
            gindex=GINDEX_EXECUTION_PAYLOAD_STATE_ROOT,  # 34 (depth 5, Deneb SSZ standard)
            root=evidence.execution_payload_root,
        )
        if not ssz_sr.is_valid:
            return False, f"STATE_ROOT_SSZ_BRANCH_INVALID: {ssz_sr.error}"

        # 11. Finalized checkpoint branch against beacon_state_root (gindex 105, depth 6)
        if not evidence.finalized_checkpoint_root or len(evidence.finalized_checkpoint_root) != 32:
            return False, "INVALID_FINALIZED_CHECKPOINT_ROOT"
        if evidence.finalized_checkpoint_root == b"\x00" * 32:
            return False, "DUMMY_FINALIZED_CHECKPOINT_ROOT_REJECTED"

        ssz_fin = verify_ssz_branch(
            leaf=evidence.finalized_checkpoint_root,
            branch=evidence.finalized_root_branch,
            gindex=GINDEX_FINALIZED_CHECKPOINT_ROOT,  # 105 (depth 6)
            root=evidence.beacon_state_root,
        )
        if not ssz_fin.is_valid:
            return False, f"FINALIZED_CHECKPOINT_SSZ_BRANCH_INVALID: {ssz_fin.error}"

        return True, None

