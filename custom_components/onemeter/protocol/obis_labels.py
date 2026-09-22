"""Human labels for OBIS codes this project has identified.

Kept in `protocol/` (not `obis_map.py`) deliberately: it is HA-free, so the
invariants in `tests/test_obis_labels.py` run in the default suite — `obis_map`
imports Home Assistant for its sensor metadata and would drag the whole test file
behind an HA install.

These labels exist for codes we know but ship no descriptor for: the
disabled-by-default raw sensors (`sensor.OneMeterRawObisSensor`) would otherwise
be a bare `A.B.C.D` in the entity list.

Only codes with evidence behind the name belong here: the two energy codes (tags
0x16/0x1B) and the two vendor fields 255.1.1.11/14 (tags 0x56/0x57). Codes with a
descriptor are deliberately absent — their own sensor carries the name.
"""
from __future__ import annotations

OBIS_LABELS: dict[bytes, str] = {
    # The C field follows the OBIS convention: 3.8.x / 4.8.x are reactive energy
    # (inductive / capacitive), by the same numbering that makes 1.8.0 active
    # import and 2.8.0 active export. This is the standard's meaning for the code,
    # not an inference from this device. An earlier "tariff 2/3" comment in
    # advert.py was a guess, and the wrong one: the capture shows each family
    # carrying its own total alongside its own tariff slots, so these are not
    # another family's tariffs. If the meter's own documentation ever says
    # otherwise, correct it here.
    bytes([0, 3, 8, 0]): "reactive energy, inductive",
    bytes([0, 4, 8, 0]): "reactive energy, capacitive",
    # OBIS reserves an A field of 255.1.1.x for the meter vendor (obis_map.py's
    # own wording is "vendor-specific OBIS A-field"), so this is the strongest
    # claim that is true of them — the tags identify *which* fields they are, not
    # what the values mean.
    bytes([0xFF, 1, 1, 11]): "vendor-specific field",
    bytes([0xFF, 1, 1, 14]): "vendor-specific field",
}


def label_for(obis: bytes) -> str | None:
    """Short human label for an identified code, or None when we don't know it."""
    return OBIS_LABELS.get(obis)
