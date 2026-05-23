"""OneMeter cipher: AES-128 keystream XOR.

Single-fragment requests, single-fragment responses, and multi-fragment
response *header* frames all use a constant IV. Multi-fragment response
*data* frames use CFB-128 chaining: each frame's IV is the previous
frame's ciphertext.
"""
from __future__ import annotations

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b, strict=True))


class OneMeterCipher:
    """Stateless cipher primitives bound to one (key, iv) pair.

    The cipher itself holds no session state. The caller decides which IV
    to use for each block (the constant IV, or a chained one).
    """

    def __init__(self, key: bytes, iv: bytes) -> None:
        if len(key) != 16:
            raise ValueError(f"key must be 16 bytes, got {len(key)}")
        if len(iv) != 16:
            raise ValueError(f"iv must be 16 bytes, got {len(iv)}")
        self._key = key
        self._iv = iv
        # The constant-IV keystream is reused for almost every frame.
        # Cache it once.
        self._constant_keystream = self._keystream(iv)

    @property
    def iv(self) -> bytes:
        return self._iv

    def _keystream(self, iv_block: bytes) -> bytes:
        """AES-128-ECB(key, iv_block) — one 16-byte block."""
        c = Cipher(algorithms.AES(self._key), modes.ECB())
        return c.encryptor().update(iv_block)

    def apply_constant_iv(self, block: bytes) -> bytes:
        """XOR `block` with the constant-IV keystream. Symmetric."""
        if len(block) > 16:
            raise ValueError(f"block too long: {len(block)}")
        return _xor(block, self._constant_keystream[: len(block)])

    def apply_chained(self, block: bytes, prev_ciphertext: bytes) -> bytes:
        """Decrypt one CFB-chained block.

        Used for multi-fragment response *data* frames (frame 2..N of a
        multi-fragment response). The IV is the previous ciphertext.
        """
        if len(prev_ciphertext) != 16:
            raise ValueError(f"prev_ciphertext must be 16 bytes, got {len(prev_ciphertext)}")
        ks = self._keystream(prev_ciphertext)
        return _xor(block, ks[: len(block)])
