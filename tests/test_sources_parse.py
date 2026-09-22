"""Every source file must at least parse.

The suite is deliberately free of Home Assistant, so it never imports the
platform modules — which means a syntax error in `sensor.py` (or any other HA
facing file) passes the whole suite and is caught only when HA loads it. This
test is the cheap gate for that: it parses every file under
`custom_components/onemeter/` without importing any of them.
"""
import ast
import pathlib

import pytest

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "onemeter"

SOURCES = sorted(PACKAGE.rglob("*.py"))


def test_the_package_has_sources_to_check():
    """Guards against the glob silently matching nothing (a moved directory
    would otherwise turn this file into a no-op that always passes)."""
    assert SOURCES, f"no Python sources found under {PACKAGE}"


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(PACKAGE)))
def test_source_parses(path: pathlib.Path):
    ast.parse(path.read_text(), filename=str(path))
