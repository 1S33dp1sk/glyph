# src/glyph/ids.py
from __future__ import annotations

"""
CRC64-ECMA (poly 0x42F0E1EBA9EA3693) → base36 short IDs.
Used to derive compact, stable GIDs from structured parts.
"""

# CRC64-ECMA polynomial
_POLY = 0x42F0E1EBA9EA3693
_MASK = 0xFFFFFFFFFFFFFFFF

def _make_table() -> tuple[int, ...]:
    tbl = [0] * 256
    for i in range(256):
        c = i << 56
        for _ in range(8):
            if c & (1 << 63):
                c = ((c << 1) ^ _POLY) & _MASK
            else:
                c = (c << 1) & _MASK
        tbl[i] = c & _MASK
    return tuple(tbl)

_TABLE: tuple[int, ...] = _make_table()

def crc64_ecma(data: bytes) -> int:
    """Compute CRC64-ECMA of bytes (init=0, no final xor)."""
    crc = 0
    for b in data:
        crc = _TABLE[((crc >> 56) ^ b) & 0xFF] ^ ((crc << 8) & _MASK)
    return crc & _MASK

_A36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

def _b36(n: int) -> str:
    """Unsigned base-36 (uppercase)."""
    if n == 0:
        return "0"
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(_A36[r])
    return "".join(reversed(out))

def short_id_bytes(data: bytes, *, length: int = 10) -> str:
    """
    Hash raw bytes → CRC64 → base36 → prefix of desired length (default 10).
    """
    if length <= 0:
        return ""
    return _b36(crc64_ecma(data))[:length]

def short_id(*parts: str, length: int = 10, sep: str = "|") -> str:
    """
    Join string parts with a separator, hash deterministically to a compact ID.
    Example: short_id("fn", decl_sig, eff_sig, storage, filename) -> "K61PXXH29T"
    """
    seed = sep.join(parts).encode("utf-8", "ignore")
    return short_id_bytes(seed, length=length)

__all__ = ["crc64_ecma", "short_id_bytes", "short_id"]
