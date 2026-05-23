import pytest

from onemeter.protocol.cipher import OneMeterCipher


def test_rejects_wrong_key_length():
    with pytest.raises(ValueError, match="key must be 16"):
        OneMeterCipher(key=b"\x00" * 15, iv=b"\x00" * 16)


def test_rejects_wrong_iv_length():
    with pytest.raises(ValueError, match="iv must be 16"):
        OneMeterCipher(key=b"\x00" * 16, iv=b"\x00" * 15)


def test_constant_iv_is_symmetric(test_key, test_iv):
    """XOR with the keystream is self-inverse: encrypt(encrypt(x)) == x."""
    c = OneMeterCipher(test_key, test_iv)
    pt = bytes(range(16))
    ct = c.apply_constant_iv(pt)
    assert c.apply_constant_iv(ct) == pt


def test_constant_iv_is_not_identity(test_key, test_iv):
    """Sanity: the cipher actually changes the bytes."""
    c = OneMeterCipher(test_key, test_iv)
    pt = b"\x00" * 16
    ct = c.apply_constant_iv(pt)
    assert ct != pt


def test_chained_decrypt_matches_encrypt_with_prev_iv(test_key, test_iv):
    """The CFB chain rule: decrypting frame N with prev_ct as IV is the
    same as encrypting plaintext N using prev_ct as IV (XOR is symmetric)."""
    c = OneMeterCipher(test_key, test_iv)
    prev_ct = bytes(range(16, 32))
    pt = bytes(range(16))
    # In our model, apply_chained takes a *ciphertext* block plus the
    # previous ciphertext, and returns plaintext. Round-trip: build a
    # ciphertext by XORing pt with the keystream derived from prev_ct.
    ks = c._keystream(prev_ct)  # noqa: SLF001 — fine for testing
    fake_ct = bytes(p ^ k for p, k in zip(pt, ks, strict=True))
    assert c.apply_chained(fake_ct, prev_ct) == pt


def test_block_too_long_rejected(test_key, test_iv):
    c = OneMeterCipher(test_key, test_iv)
    with pytest.raises(ValueError, match="block too long"):
        c.apply_constant_iv(b"\x00" * 17)
