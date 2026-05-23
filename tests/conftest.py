"""Pytest fixtures.

Synthetic test KEY and IV — **not** the live device's credentials. These
are used to exercise the cipher and framing code paths with deterministic
known inputs. The live device's key stays in HA's config-entry store
and never touches the test suite.
"""
import pytest

# Arbitrary 16-byte values. The values themselves are not load-bearing —
# any constant KEY+IV pair would do. Documented so future maintainers
# know these are NOT real device credentials.
TEST_KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
TEST_IV = bytes.fromhex("101112131415161718191a1b1c1d1e1f")


@pytest.fixture
def test_key() -> bytes:
    return TEST_KEY


@pytest.fixture
def test_iv() -> bytes:
    return TEST_IV
