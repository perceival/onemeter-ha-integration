"""Human labels for OBIS codes this project has identified.

Kept in `protocol/` (not `obis_map.py`) deliberately: it is HA-free, so the
invariants in `tests/test_obis_labels.py` run in the default suite — `obis_map`
imports Home Assistant for its sensor metadata and would drag the whole test file
behind an HA install.

These labels exist for codes we know but ship no descriptor for: the
disabled-by-default raw sensors (`sensor.OneMeterRawObisSensor`) would otherwise
be a bare `A.B.C.D` in the entity list.

Only codes with evidence behind the name belong here. The evidence, by group:

* The energy families and their tariff slots follow the Polish market's data-type
  catalogue published by PSE (the transmission system operator), "Pozostałe
  elementy komunikatów biznesowych — typy danych", which names every code in
  Polish and English: `1.8.x` "energia czynna pobrana" / "active energy
  consumed", `2.8.x` "energia czynna oddana" / "active energy returned", `3.8.x`
  "energia bierna pobrana" / "reactive energy consumed", `4.8.x` "energia bierna
  oddana" / "reactive energy returned", the trailing `.n` given as "w strefie
  czasowej T=n" (in time zone T=n). Note that the *inductive/capacitive* wording
  belongs to that catalogue's quadrant codes (`5.8.x`–`8.8.x` = Q1–Q4), not to
  `3.8`/`4.8`: for a household the reactive drawn is in practice the inductive
  part, which is why the two namings get used interchangeably, but these labels
  use the catalogue's own words for the code. They say "consumed"/"returned"
  where the descriptor sensors for the same directions say "import"/"export"
  (`obis_map.py`): the two vocabularies name the same registers.
* `0.0.9.1` / `0.0.9.2` are the generic time and date objects: "current time" and
  "current date" in the OMS OBIS annex, and "Aktualny czas zegara" / "Aktualna
  data" for these two codes in Apator's manual for the NORAX 3D, one of the
  variants of the NORAX 3 family this codebase documents (`obis_map.py`). The
  *pairing* is both the standard's and the vendor's; the value encoding these two
  carry on this meter is not decoded, so their raw sensors must not be read as
  timestamps.
* `255.1.1.x`: OBIS reserves an A field of 255 for the meter vendor
  (`obis_map.py`'s own wording is "vendor-specific OBIS A-field"), so that is the
  strongest claim that is true of them — the tags identify *which* fields they
  are, not what the values mean.

Deliberately absent — `1.67.1.0`: the C value 67 is not one this project can
place, and the register's value runs far above anything the measurement registers
carry here, which reads as an identifier or a counter rather than a measurement.
A guess in the entity name would be worse than showing the bare code.
"""
from __future__ import annotations

OBIS_LABELS: dict[bytes, str] = {
    # The meter's own clock, not the OneMeter's (that is a separate sensor, and
    # the only *clock* whose state is a timestamp). See the module docstring: the
    # register identity is the vendor's, its value encoding is undecoded.
    bytes([0, 0, 9, 1]): "meter clock, time",
    bytes([0, 0, 9, 2]): "meter clock, date",
    # Energy, by direction and tariff slot. `.0` is the family total and has a
    # descriptor of its own (`1.8.0`/`2.8.0`), so only the tariff slots appear.
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
    # Tariff-4 slot of the meter-reading family whose total and tariffs 1-3 ship
    # as the `energy_total` / `energy_tariff_*` sensors (C=15, absolute active
    # energy). It simply has no descriptor of its own yet — the tariffs already
    # supported are the ones a Polish supply uses.
    bytes([0, 15, 8, 4]): "active energy, tariff 4",
    # Vendor-reserved A field — see the module docstring.
    bytes([0xFF, 1, 1, 11]): "vendor-specific field",
    bytes([0xFF, 1, 1, 14]): "vendor-specific field",
}


def label_for(obis: bytes) -> str | None:
    """Short human label for an identified code, or None when we don't know it."""
    return OBIS_LABELS.get(obis)
