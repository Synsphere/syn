"""Independent bounded RFC 9162-style prefix verifier for the Windows broker.

No imports from WSL's Syn runtime, SQLAlchemy, filesystem graph, or peer URLs.
"""
from __future__ import annotations
import hashlib
import hmac
import re

MAX_LEAVES = 16384


def raw(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("Invalid Merkle digest")
    return bytes.fromhex(value)


def branch(left, right):
    return hashlib.sha256(b"\x01" + left + right).digest()


def verify_prefix(old_size, new_size, old_root, new_root, proof):
    old, new = raw(old_root), raw(new_root)
    if (type(old_size) is not int or type(new_size) is not int
            or not 1 <= old_size <= new_size <= MAX_LEAVES
            or type(proof) is not list or len(proof) > 64):
        raise ValueError("Invalid bounded consistency proof")
    path = [raw(value) for value in proof]
    if old_size == new_size:
        return not path and hmac.compare_digest(old, new)
    first, second = old_size - 1, new_size - 1
    while first & 1:
        first >>= 1
        second >>= 1
    if first == 0:
        a = b = old
    else:
        if not path:
            return False
        a = b = path.pop(0)
    for sibling in path:
        if second == 0:
            return False
        if first & 1 or first == second:
            a, b = branch(sibling, a), branch(sibling, b)
            while first and not first & 1:
                first >>= 1
                second >>= 1
        else:
            b = branch(b, sibling)
        first >>= 1
        second >>= 1
    return second == 0 and hmac.compare_digest(a, old) and hmac.compare_digest(b, new)
