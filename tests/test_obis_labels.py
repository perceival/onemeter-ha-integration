"""Labels for identified-but-descriptorless OBIS codes.

The deliverable of this module is the label *text*, so these tests pin the whole
map rather than the fact that some label exists: a wrong string, an addition or a
removal all fail here. HA-free on purpose — the labels live in
`protocol/obis_labels.py` — so the default suite checks them. The two invariants
that need the descriptor map (which imports Home Assistant) live in
`tests/test_platform_setup.py`.
"""
import pytest
from onemeter.protocol.obis_labels import OBIS_LABELS, label_for

EXPECTED = {
    bytes([0, 3, 8, 0]): "reactive energy, inductive",
    bytes([0, 4, 8, 0]): "reactive energy, capacitive",
    bytes([0xFF, 1, 1, 11]): "vendor-specific field",
    bytes([0xFF, 1, 1, 14]): "vendor-specific field",
}


def test_the_label_map_is_exactly_what_is_expected():
    """Pins every string and every key: this is the user-visible deliverable."""
    assert OBIS_LABELS == EXPECTED


@pytest.mark.parametrize("obis,label", sorted(EXPECTED.items()), ids=lambda v: getattr(v, "hex", lambda: str(v))())
def test_label_for_returns_the_expected_label(obis, label):
    assert label_for(obis) == label


@pytest.mark.parametrize(
    "obis",
    [bytes([0, 7, 8, 0]), bytes([0x99, 1, 2, 3]), bytes([0x4F, 0, 0, 0])],
    ids=lambda o: o.hex(),
)
def test_unidentified_codes_have_no_label(obis):
    """A label is a claim about what a register *is*; guessing one for an
    unmapped code would be worse than showing the code."""
    assert label_for(obis) is None
