"""The sensor platform's dynamic-entity wiring, against the real setup path.

Skipped unless Home Assistant is importable (it is not a dependency of this
suite): run it with an interpreter that has HA, e.g.
    /path/to/ha-venv/bin/python -m pytest tests/test_platform_setup.py

Why it exists: `pytest` alone cannot see this code at all (the suite never
imports the HA-facing modules), so an earlier change here broke the entire
platform — an AttributeError in `async_setup_entry` — while the suite stayed
green. These cases drive the real `async_setup_entry` with a fake coordinator
and registry, covering the two properties that matter: every setup re-adds the
raw entities it already created (or a user-enabled one silently loses its
state), and a code that has no reading yet is still picked up once it has one.
"""
import asyncio
import types

import pytest

pytest.importorskip("homeassistant")

from homeassistant.helpers import entity_registry as er  # noqa: E402
from onemeter import sensor  # noqa: E402
from onemeter.coordinator import OneMeterData  # noqa: E402
from onemeter.obis_map import KNOWN_OBIS  # noqa: E402
from onemeter.protocol.advert import ADVERT_TAG_OBIS  # noqa: E402
from onemeter.protocol.decode import ObisEntry  # noqa: E402
from onemeter.protocol.obis_labels import OBIS_LABELS  # noqa: E402

ADDR = "AA:BB:CC:DD:EE:FF"
CODE_WITH_VALUE = bytes([0, 3, 8, 0])
CODE_VALUELESS = bytes([255, 1, 1, 11])
RAW_PREFIX = f"{ADDR}_obis_raw_"


class _Entries:
    """Stands in for `registry.entities`: HA's module-level
    async_entries_for_config_entry calls get_entries_for_config_entry_id."""

    def __init__(self, entries=()):
        self._entries = list(entries)

    def get_entries_for_config_entry_id(self, config_entry_id):
        return [e for e in self._entries if e.config_entry_id == config_entry_id]


class _RegistryEntry:
    def __init__(self, unique_id, config_entry_id="entry1"):
        self.unique_id = unique_id
        self.config_entry_id = config_entry_id


def _registry(unique_ids=()):
    registry = er.EntityRegistry.__new__(er.EntityRegistry)  # __init__ needs a Store
    registry.entities = _Entries(_RegistryEntry(u) for u in unique_ids)
    return registry


class _Coordinator:
    last_update_success = True

    def __init__(self, obis_values):
        self.address = ADDR
        self.data = OneMeterData()
        self.data.cached_obis_by_code = {
            code: ObisEntry(
                obis=code,
                value=value,
                raw=code + (0 if value is None else value).to_bytes(4, "little"),
            )
            for code, value in obis_values.items()
        }
        self.obis_cb = None
        # async_setup_entry builds device info from the entry's title
        self.entry = types.SimpleNamespace(title="OneMeter")

    def register_add_register_entity_cb(self, cb):
        pass

    def register_add_obis_entity_cb(self, cb):
        self.obis_cb = cb


_LAST_ADDED: list = []


def _setup_entities(coordinator):
    """The raw entities the last _setup() handed to the platform."""
    return [e for e in _LAST_ADDED if type(e).__name__ == "OneMeterRawObisSensor"]


def _setup(coordinator, registry, monkeypatch):
    """Run the real async_setup_entry; return the entities it asked for.

    `monkeypatch` (not a bare assignment) so the patched module attribute is
    restored: rebinding sensor.entity_registry.async_get for the session would
    hand this fake to any later test that touches the entity registry.
    """
    hass = types.SimpleNamespace(data={"onemeter": {"entry1": coordinator}})
    entry = types.SimpleNamespace(entry_id="entry1", options={}, title="OneMeter")
    _LAST_ADDED.clear()
    monkeypatch.setattr(sensor.entity_registry, "async_get", lambda _hass: registry)
    asyncio.run(sensor.async_setup_entry(hass, entry, _LAST_ADDED.extend))
    return _setup_entities(coordinator)


def test_a_valueless_code_gets_no_entity_but_is_picked_up_once_it_has_one(monkeypatch):
    """The sentinel means "no reading": no entity should exist only to say
    unknown — and when the reading arrives later in the same run, the entity
    must appear without waiting for a restart."""
    coordinator = _Coordinator({CODE_WITH_VALUE: 1234, CODE_VALUELESS: 0xFFFFFFFF})
    raw = _setup(coordinator, _registry(), monkeypatch)
    assert [e.unique_id for e in raw] == [RAW_PREFIX + CODE_WITH_VALUE.hex()]
    assert coordinator.obis_cb is not None, "the platform must register its callback"

    # The device reports a reading for the code that had none.
    coordinator.data.cached_obis_by_code[CODE_VALUELESS] = ObisEntry(
        obis=CODE_VALUELESS, value=99, raw=CODE_VALUELESS + (99).to_bytes(4, "little")
    )
    coordinator.obis_cb(CODE_VALUELESS)

    # Re-derive: `raw` was a snapshot taken before the callback ran.
    raw = _setup_entities(coordinator)
    assert [e.unique_id for e in raw] == [
        RAW_PREFIX + CODE_WITH_VALUE.hex(),
        RAW_PREFIX + CODE_VALUELESS.hex(),
    ]


def test_every_setup_re_adds_the_entities_it_already_created(monkeypatch):
    """A restart (or an options change, which reloads the entry) must hand the
    existing entities to the platform again: the registry entry survives, but
    without an entity object it has no state — so an entity the user enabled
    would silently stop working."""
    coordinator = _Coordinator({CODE_WITH_VALUE: 1234})
    first = _setup(coordinator, _registry(), monkeypatch)
    assert [e.unique_id for e in first] == [RAW_PREFIX + CODE_WITH_VALUE.hex()]

    registry = _registry([e.unique_id for e in first])  # as after a restart
    second = _setup(_Coordinator({CODE_WITH_VALUE: 1234}), registry, monkeypatch)

    assert [e.unique_id for e in second] == [RAW_PREFIX + CODE_WITH_VALUE.hex()]


def test_known_codes_never_get_a_raw_twin(monkeypatch):
    """Codes with a descriptor already have a scaled sensor."""
    known = next(iter(sensor.KNOWN_OBIS))
    coordinator = _Coordinator({known: 5})
    assert _setup(coordinator, _registry(), monkeypatch) == []


# --- the name a user actually sees --------------------------------------------


def test_a_labelled_code_gets_its_label_in_parentheses(monkeypatch):
    """The point of the label change, asserted end to end: without this, dropping
    the suffix would keep every other test green."""
    labelled = bytes([0, 3, 8, 0])          # in OBIS_LABELS
    unlabelled = bytes([0, 7, 8, 0])        # not
    raw = _setup(_Coordinator({labelled: 5, unlabelled: 7}), _registry(), monkeypatch)
    names = sorted(e.name for e in raw)
    assert names == [
        "OBIS 0.3.8.0 (reactive energy, inductive)",
        "OBIS 0.7.8.0",
    ]


def test_descriptor_sensors_are_not_given_a_label(monkeypatch):
    """A code with a descriptor keeps its own named sensor. The partition test
    makes that structural; this pins the user-visible side, which the structural
    one cannot see: a label leaking onto descriptor sensors would rename sensors
    users already have."""
    known = next(iter(sensor.KNOWN_OBIS))
    _setup(_Coordinator({known: 5}), _registry(), monkeypatch)
    descriptor_names = {
        e.name for e in _LAST_ADDED if type(e).__name__ == "OneMeterObisSensor"
    }
    assert descriptor_names == {d.name for d in sensor.KNOWN_OBIS.values()}


# --- label-map invariants (need KNOWN_OBIS, which imports Home Assistant) ------


def test_labels_and_descriptors_are_a_partition():
    """A code with a descriptor already has a named sensor of its own."""
    assert not set(OBIS_LABELS) & set(KNOWN_OBIS)


def test_every_identified_broadcast_tag_is_sensored_or_labelled():
    """Guard for the map: adding a tag→OBIS identification without either a
    descriptor or a label would leave a bare code in the entity list, which is
    what the labels exist to prevent."""
    unhandled = {
        obis for obis in ADVERT_TAG_OBIS.values()
        if obis not in KNOWN_OBIS and obis not in OBIS_LABELS
    }
    assert not unhandled, f"identified but neither sensored nor labelled: {unhandled}"
