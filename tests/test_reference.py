"""The generated reference documents this library and nothing else.

`docs/reference.md` is derived from the code, and CI regenerates it on whatever
interpreter `uv` happens to pick. That makes a second requirement out of the
first: every line must come from the package's own declarations, because
anything inherited from the standard library is both a false attribution and a
moving target. CPython rewords `frozenset.__doc__` between releases and PyPy
words `object.__doc__` differently again, so a reference that quotes them is
stale on arrival -- which is exactly how it failed, once, in CI.

These tests hold the two rules that keep it still: a summary comes only from a
docstring the name itself carries, and a signature is rendered only where the
library actually writes one.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import cordis

if TYPE_CHECKING:
    from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "docs" / "build_reference.py"


def _generator() -> ModuleType:
    """Load the build script by path; it is a tool, not a package member."""
    spec = importlib.util.spec_from_file_location("build_reference", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BUILD = _generator()


@pytest.mark.tier_local
@pytest.mark.parametrize(
    "name",
    [
        "PluginTarget",  # an alias for `object`
        "SETTLED",  # a frozenset
        "TAG",  # a str
        "DEFAULT_REALM",  # an instance, whose class does have a docstring
    ],
)
def test_a_name_is_never_summarised_by_its_type(name: str) -> None:
    """Failure value: `inspect.getdoc`, which falls back to the value's class,
    so the reference tells a reader that `SETTLED` builds "an immutable
    unordered collection of unique elements" -- a sentence about frozenset,
    written under a cordis name, in wording CPython is free to change."""
    assert BUILD._summary(getattr(cordis, name)) == ""


@pytest.mark.tier_local
@pytest.mark.parametrize("name", ["Level", "FiberState", "ChangeKind"])
def test_an_enum_is_rendered_as_a_name_not_a_constructor(name: str) -> None:
    """Failure value: reporting `inspect.signature` of an enum class, which is
    `EnumType.__call__` -- `(*values)` on 3.13, a long lookup signature on 3.11
    -- so the file's contents depend on which interpreter generated it, and the
    reader is shown a constructor nobody calls."""
    found = getattr(cordis, name)
    assert BUILD._signature(name, found) == name


@pytest.mark.tier_local
def test_a_library_docstring_still_reaches_the_reference() -> None:
    """The rules above must not silence the library itself."""
    assert BUILD._summary(cordis.Context)
    assert BUILD._summary(cordis.scope_of)
    assert BUILD._signature("scope_of", cordis.scope_of).startswith("scope_of(ctx")
