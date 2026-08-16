"""The public surface, held to the specification that declares it.

Four things are checked, and each one exists because the alternative is a
library that drifts away from its own contract:

* every name `cordis` exports appears in some capability record's declared
  surface, so an export nobody specified cannot be added by accident;
* every name a module exports is either re-exported or listed here with a
  reason, so keeping something out of the front door is a decision rather than
  an omission;
* importing `cordis` pulls in nothing an application did not ask for;
* nothing in the package is spelled in a way the oldest supported interpreter
  rejects at import time.
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import pkgutil
import re
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType

import pytest
import yaml

import cordis

ROOT = Path(__file__).resolve().parent.parent
RECORDS = ROOT / "spec" / "capabilities"

#: Module-level exports deliberately not re-exported from `cordis`, and why.
#: A new name in a module's `__all__` fails this file until it appears in one
#: list or the other, which is the point: the front door is curated.
HELD_BACK: dict[str, dict[str, str]] = {
    "config": {
        "DECLARED_SCHEMA_ATTR": "the attribute name a schema is stashed under",
        "render_path": "issue rendering, used by the error module",
    },
    "context": {
        "CONTEXT_BRAND": "the marker a proxy is recognised by",
        "RESERVED_NAMES": "names a context refuses to shadow",
        "is_passthrough": "a proxy predicate the kernel asks, not a plugin",
    },
    "effect": {
        "CAPTURE_LOCATIONS": "a diagnostics switch, read through cordis.diagnostics",
        "Disposer": "the callable an effect returns; named where it is written",
        "EffectFn": "the callable an effect is; likewise",
        "EffectNode": "a scope tree node, reached through cordis.diagnostics",
    },
    "exporters": {
        "ConsoleExporter": "imported by name so `import cordis` never pulls in logging",
        "StdlibExporter": "the same, for the standard library's logging module",
    },
    "fiber": {"check_transition": "the transition table's own guard"},
    "inject": {
        "DECLARED_ATTR": "an attribute name, not a thing a plugin calls",
        "INJECT_ATTR": "likewise",
        "PROVIDES_ATTR": "likewise",
    },
    "plugin": {
        "CONFIG_KEY": "context metadata keys; `config_of` is the reader",
        "MOUNT_KEY": "likewise; `ctx.plugin` is the reader",
        "SCOPE_KEY": "likewise; `scope_of` is the reader",
    },
    "registry": {
        "Gate": "an internal admission callable",
        "REALM_KEY": "context metadata key; `realm_key` builds it",
    },
}


def _surfaces() -> dict[str, str]:
    found: dict[str, str] = {}
    for path in sorted(RECORDS.glob("*.yaml")):
        record = yaml.safe_load(path.read_text(encoding="utf-8"))
        design = record.get("python_design") or {}
        found[record["id"]] = "\n".join(design.get("surface") or ())
    return found


SURFACES = _surfaces()
EXPORTS = tuple(cordis.__all__)
MODULES = tuple(info.name for info in pkgutil.iter_modules(cordis.__path__))


@pytest.mark.parametrize("name", EXPORTS)
def test_every_export_is_declared_by_a_capability_record(name: str) -> None:
    """The spec is the source of truth for the API, not the other way round."""
    word = re.compile(rf"\b{re.escape(name)}\b")
    owners = [found for found, text in SURFACES.items() if word.search(text)]
    assert owners, f"{name} is exported but no capability record declares it"


@pytest.mark.parametrize("name", EXPORTS)
def test_every_export_resolves_and_says_what_it_is(name: str) -> None:
    obj = getattr(cordis, name)
    if inspect.isclass(obj) or inspect.isfunction(obj):
        assert (obj.__doc__ or "").strip(), f"{name} has no docstring"


def test_nothing_is_exported_twice() -> None:
    assert len(EXPORTS) == len(set(EXPORTS))


@pytest.mark.parametrize("module", MODULES)
def test_a_module_export_is_either_public_or_held_back_on_purpose(module: str) -> None:
    """Adding a public name forces a decision about the front door."""
    found = importlib.import_module(f"cordis.{module}")
    names = set(getattr(found, "__all__", ()))
    assert names, f"cordis.{module} declares no __all__"
    held = set(HELD_BACK.get(module, {}))
    undecided = sorted(names - set(EXPORTS) - held)
    assert not undecided, (
        f"cordis.{module}: neither exported nor held back: {undecided}"
    )
    stale = sorted(held & set(EXPORTS))
    assert not stale, f"cordis.{module}: held back but also exported: {stale}"


@pytest.mark.parametrize("module", MODULES)
def test_a_held_back_name_still_exists(module: str) -> None:
    found = importlib.import_module(f"cordis.{module}")
    for name in HELD_BACK.get(module, {}):
        assert hasattr(found, name), f"cordis.{module}.{name} is held back but gone"


def test_importing_cordis_configures_no_logging() -> None:
    """The claim in the package docstring, checked in a clean interpreter.

    The `logging` module object arrives whatever we do -- `asyncio` imports it
    -- so the claim worth holding is the one an application can observe: after
    `import cordis` no exporter module has been reached, the root logger has no
    handlers and no `cordis` logger exists to have a level set on it.
    """
    script = (
        "import cordis, logging, sys;"
        "print('cordis.exporters' in sys.modules,"
        " bool(logging.root.handlers),"
        " any(n == 'cordis' or n.startswith('cordis.')"
        "     for n in logging.Logger.manager.loggerDict))"
    )
    found = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    )
    assert found.stdout.split() == ["False", "False", "False"], found.stdout


@pytest.mark.parametrize("module", MODULES)
def test_no_dataclass_default_is_a_mappingproxy(module: str) -> None:
    """A guard the minimum interpreter would otherwise be the only one to raise.

    Python 3.11's `dataclasses` refuses a `MappingProxyType` written as a plain
    default -- it reads as mutable -- while 3.12 accepts it. The package
    supports 3.11, so the plain spelling is not a style slip but an
    `ImportError` for a third of the support matrix, discovered only by whoever
    installs on the oldest version. Here it fails on any interpreter, at the
    speed of an attribute read: use `field(default_factory=lambda: SHARED)`.
    """
    found = importlib.import_module(f"cordis.{module}")
    for obj in vars(found).values():
        if not inspect.isclass(obj) or not dataclasses.is_dataclass(obj):
            continue
        if obj.__module__ != found.__name__:
            continue
        for spec in dataclasses.fields(obj):
            assert not isinstance(spec.default, MappingProxyType), (
                f"cordis.{module}.{obj.__name__}.{spec.name} defaults to a "
                f"mappingproxy; Python 3.11 rejects that. Use "
                f"field(default_factory=...)"
            )


def test_the_package_ships_its_types() -> None:
    assert (ROOT / "src" / "cordis" / "py.typed").is_file()
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"Typing :: Typed"' in pyproject
