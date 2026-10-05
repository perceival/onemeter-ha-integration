"""Labels for identified-but-descriptorless OBIS codes.

The deliverable of this module is the label *text*, so these tests pin the whole
map rather than the fact that some label exists: a wrong string, an addition or a
removal all fail here. HA-free on purpose — the labels live in
`protocol/obis_labels.py` — so the default suite checks them. The two invariants
that need the descriptor map (which imports Home Assistant) live in
`tests/test_platform_setup.py`.

The pinned map below duplicates the module's keys by hand, so on its own it could
not catch a *typo'd key* (both sides would carry it). The last test is the guard
for that: every label must correspond to a code this device actually reports,
read from the private capture when it is present.
"""
import json
import pathlib

import pytest

from onemeter.protocol.decode import parse_last_obis
from onemeter.protocol.obis_labels import OBIS_LABELS, label_for

EXPECTED = {
    bytes([0, 0, 9, 1]): "meter clock, time",
    bytes([0, 0, 9, 2]): "meter clock, date",
    bytes([0, 1, 8, 1]): "active energy consumed, tariff 1",
    bytes([0, 1, 8, 2]): "active energy consumed, tariff 2",
    bytes([0, 2, 8, 1]): "active energy returned, tariff 1",
    bytes([0, 2, 8, 2]): "active energy returned, tariff 2",
    bytes([0, 3, 8, 0]): "reactive energy consumed",
    bytes([0, 3, 8, 1]): "reactive energy consumed, tariff 1",
    bytes([0, 3, 8, 2]): "reactive energy consumed, tariff 2",
    bytes([0, 4, 8, 0]): "reactive energy returned",
    bytes([0, 4, 8, 1]): "reactive energy returned, tariff 1",
    bytes([0, 4, 8, 2]): "reactive energy returned, tariff 2",
    bytes([0, 15, 8, 4]): "active energy, tariff 4",
    bytes([0xFF, 1, 1, 6]): "device time, quarter-hours",
    bytes([0xFF, 1, 1, 10]): "device clock, unix time",
    bytes([0xFF, 1, 1, 11]): "vendor-specific field",
    bytes([0xFF, 1, 1, 14]): "vendor-specific field",
}

# The code deliberately left unlabelled although the device reports it: pinning
# the *absence* against real data, so adding a guess fails a test that explains
# why (see the module docstring in protocol/obis_labels.py).
DELIBERATELY_UNLABELLED = bytes([1, 67, 1, 0])


def test_the_label_map_is_exactly_what_is_expected():
    """Pins every string and every key: this is the user-visible deliverable."""
    assert OBIS_LABELS == EXPECTED


@pytest.mark.parametrize("obis,label", sorted(EXPECTED.items()), ids=lambda v: getattr(v, "hex", lambda: str(v))())
def test_label_for_returns_the_expected_label(obis, label):
    assert label_for(obis) == label


@pytest.mark.parametrize(
    "obis",
    [
        bytes([0, 7, 8, 0]),
        bytes([0x99, 1, 2, 3]),
        bytes([0x4F, 0, 0, 0]),
        DELIBERATELY_UNLABELLED,
    ],
    ids=lambda o: o.hex(),
)
def test_unidentified_codes_have_no_label(obis):
    """A label is a claim about what a register *is*; guessing one for an
    unmapped code would be worse than showing the code."""
    assert label_for(obis) is None


_CAPTURES = pathlib.Path(__file__).parent / "private" / "captures.json"


@pytest.mark.skipif(not _CAPTURES.exists(), reason="private capture not present")
def test_every_label_is_for_a_code_the_device_reports():
    """Guards the keys against the capture: the pinned map above cannot catch a
    typo'd key on its own, and a label for a code no meter ever sends would sit
    in the entity list forever. Skips without the private file, like
    tests/test_advert.py does.
    """
    payload = bytes.fromhex(json.loads(_CAPTURES.read_text())["last_obis_payload"])
    codes = {entry.obis for entry in parse_last_obis(payload)}
    assert set(OBIS_LABELS) <= codes, sorted(
        bytes.hex(o) for o in set(OBIS_LABELS) - codes
    )
    assert DELIBERATELY_UNLABELLED in codes
    assert DELIBERATELY_UNLABELLED not in OBIS_LABELS
