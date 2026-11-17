"""Portable, bounded RFC 6962/9162-style proofs for the Operator DAG.

Proofs authenticate SHA-256 digests of canonical DAG nodes. They do not
expose private source events or establish any consensus/finality claims.
"""
from __future__ import annotations

import hashlib
import hmac
import re

MAX_LEAVES = 16_384
_HEX = re.compile(r"[0-9a-f]{64}\Z")


def _raw(value: str) -> bytes:
    if type(value) is not str or _HEX.fullmatch(value) is None:
        raise ValueError("Expected a lowercase 32-byte SHA-256 digest")
    return bytes.fromhex(value)


def _node(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def _leaf(value: str) -> bytes:
    return hashlib.sha256(b"\x00" + _raw(value)).digest()


def _split(length: int) -> int:
    return 1 << ((length - 1).bit_length() - 1)


def _tree(data: list[bytes]) -> bytes:
    if len(data) == 1:
        return data[0]
    split = _split(len(data))
    return _node(_tree(data[:split]), _tree(data[split:]))


def _leaves(digests: list[str]) -> list[bytes]:
    if type(digests) is not list or len(digests) > MAX_LEAVES:
        raise ValueError("Invalid or oversized Operator DAG leaf set")
    return [_leaf(d) for d in digests]


def root(digests: list[str]) -> str:
    data = _leaves(digests)
    return (_tree(data) if data else hashlib.sha256(b"").digest()).hex()


def inclusion(digests: list[str], index: int) -> list[str]:
    data = _leaves(digests)
    if type(index) is not int or index < 0 or index >= len(data):
        raise ValueError("Operator DAG leaf index is out of range")
    def walk(leaves, i):
        if len(leaves) == 1:
            return []
        k = _split(len(leaves))
        if i < k:
            return walk(leaves[:k], i) + [_tree(leaves[k:])]
        return walk(leaves[k:], i-k) + [_tree(leaves[:k])]
    return [x.hex() for x in walk(data, index)]


def verify_inclusion(leaf_digest: str, index: int, size: int, expected_root: str, proof: list[str]) -> bool:
    _raw(expected_root)
    if (type(size) is not int or not 1 <= size <= MAX_LEAVES or
            type(index) is not int or not 0 <= index < size or
            type(proof) is not list or len(proof) > 32):
        raise ValueError("Malformed inclusion proof boundaries")
    nodes = [_raw(p) for p in proof]
    leaf = _leaf(leaf_digest)
    # Recursive positional verifier consumes the proof from the leaf upward.
    def walk(i, length):
        nonlocal consumed
        if length == 1:
            return leaf
        k = _split(length)
        if i < k:
            left = walk(i, k)
            if consumed >= len(nodes):
                raise ValueError("Truncated inclusion proof")
            sibling = nodes[consumed]
            consumed += 1
            return _node(left, sibling)
        right = walk(i-k, length-k)
        if consumed >= len(nodes):
            raise ValueError("Truncated inclusion proof")
        sibling = nodes[consumed]
        consumed += 1
        return _node(sibling, right)
    consumed = 0
    try:
        result = walk(index, size)
    except ValueError:
        return False
    return consumed == len(nodes) and hmac.compare_digest(result, _raw(expected_root))


def consistency(digests: list[str], old_size: int) -> list[str]:
    data = _leaves(digests)
    if type(old_size) is not int or not 1 <= old_size <= len(data):
        raise ValueError("Invalid older checkpoint size")
    if old_size == len(data):
        return []
    def walk(m, leaves, complete):
        if m == len(leaves):
            return [] if complete else [_tree(leaves)]
        k = _split(len(leaves))
        if m <= k:
            return walk(m, leaves[:k], complete) + [_tree(leaves[k:])]
        return walk(m-k, leaves[k:], False) + [_tree(leaves[:k])]
    return [x.hex() for x in walk(old_size, data, True)]


def verify_consistency(old_size: int, new_size: int, old_root: str,
                       new_root: str, proof: list[str]) -> bool:
    old = _raw(old_root)
    new = _raw(new_root)
    if (type(old_size) is not int or type(new_size) is not int or
            not 1 <= old_size <= new_size <= MAX_LEAVES or
            type(proof) is not list or len(proof) > 64):
        raise ValueError("Malformed consistency proof boundaries")
    hashes = [_raw(p) for p in proof]
    if old_size == new_size:
        return not hashes and hmac.compare_digest(old, new)
    fn, sn = old_size - 1, new_size - 1
    while fn & 1:
        fn >>= 1
        sn >>= 1
    if fn == 0:
        fr = sr = old
    else:
        if not hashes:
            return False
        fr = sr = hashes.pop(0)
    for p in hashes:
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            fr = _node(p, fr)
            sr = _node(p, sr)
            while fn and not (fn & 1):
                fn >>= 1
                sn >>= 1
        else:
            sr = _node(sr, p)
        fn >>= 1
        sn >>= 1
    return sn == 0 and hmac.compare_digest(fr, old) and hmac.compare_digest(sr, new)
