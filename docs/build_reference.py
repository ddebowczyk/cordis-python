#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Generate ``docs/reference.md`` from the code and the capability catalog.

The reference is derived, never written: every entry is a name in
``cordis.__all__``, its signature as the interpreter reports it, and the first
paragraph of its own docstring. What groups those entries is the specification
-- each symbol appears under the capability record whose declared surface names
it -- so the documentation cannot describe a different library than the one the
records govern.

That is also why there is no prose here to maintain. A symbol's obligations are
its record's normative rules, which the section header links to; repeating them
in a second place would create a second thing to be wrong.

Run: ``docs/build_reference.py`` (or ``just docs``). With ``--check`` it writes
nothing and exits non-zero if the file on disk is stale, which is what CI runs.

The output must not depend on which interpreter produced it: CI pins no version
for this step, so a file that reads differently on 3.11 than on 3.13 fails the
check for a reason that has nothing to do with the library. Everything rendered
here therefore comes from the package's own declarations -- a docstring the
name itself carries, a signature the library actually writes -- and never from
a stdlib base class, whose wording and signatures CPython revises between
releases.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import re
import sys
from enum import Enum
from pathlib import Path
from typing import Any, NamedTuple

import yaml

ROOT = Path(__file__).resolve().parent.parent
RECORDS = ROOT / "spec" / "capabilities"
TARGET = ROOT / "docs" / "reference.md"

HEADER = """\
# API reference

Every name `cordis` exports, grouped by the capability record that declares it.
Signatures and summaries come from the code; the grouping comes from
`spec/capabilities/`. Both are generated -- run `just docs` after changing
either.

A summary here says what a symbol *is*. What it must *do* is in its record's
normative rules, which each section links to, and what holds it to them is the
property cards listed there.
"""


class Record(NamedTuple):
    """One capability, reduced to what the reference needs of it."""

    path: Path
    id: str
    title: str
    summary: str
    tier: int
    surface: str
    rules: int
    cards: int


def _records() -> list[Record]:
    found: list[Record] = []
    for path in sorted(RECORDS.glob("*.yaml")):
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        design = raw.get("python_design") or {}
        found.append(
            Record(
                path=path,
                id=raw["id"],
                title=raw["title"],
                summary=" ".join((raw.get("summary") or "").split()),
                tier=raw["tier"],
                surface="\n".join(design.get("surface") or ()),
                rules=len(raw.get("semantics") or ()),
                cards=len(raw.get("properties") or ()),
            )
        )
    return found


def _owner(name: str, records: list[Record]) -> Record | None:
    """The record that declares ``name``, earliest first.

    A name in two surfaces belongs to the capability that introduced it; the
    later record is describing how it participates, not defining it. A surface
    is a sketch of the module, so "declares" means the name appears where a
    declaration would -- `class Foo:`, `def foo(...)`, `FOO: Final = ...` --
    and a mere mention (a base class, a parameter type) only counts when no
    record declares it at all.
    """
    declaration = re.compile(
        rf"^\s*(?:class |async def |def )?{re.escape(name)}\b\s*[:(=]", re.MULTILINE
    )
    word = re.compile(rf"\b{re.escape(name)}\b")
    for match in (declaration, word):
        for record in records:
            if match.search(record.surface):
                return record
    return None


def _summary(obj: object) -> str:
    """The first paragraph of the name's *own* docstring, on one line.

    `inspect.getdoc` falls back to an inherited docstring, and for anything
    that is not a class or a function that means documenting the value's type:
    `SETTLED` is a frozenset, so the reference grew "Build an immutable
    unordered collection of unique elements." That is not a description of
    `SETTLED`, and CPython rewords those strings between releases -- which is
    how a generated file came to depend on which interpreter generated it.
    """
    if not _home(obj):
        # A name bound to something the standard library defines documents that
        # object rather than the name: `PluginTarget` is `object`, whose
        # docstring reads "The base class of the class hierarchy" on CPython
        # and "The most base type" on PyPy. The same bug in another dress.
        return ""
    if inspect.isclass(obj):
        # `vars`, not `getdoc`: a class with no docstring of its own must not
        # inherit its base's, which would attribute a parent's contract to it.
        text = vars(obj).get("__doc__")
    elif inspect.isfunction(obj):
        text = obj.__doc__
    else:
        text = None
    if not isinstance(text, str):
        return ""
    paragraph = inspect.cleandoc(text).split("\n\n", 1)[0]
    return " ".join(paragraph.split())


def _kind(obj: object) -> str:
    if inspect.isclass(obj):
        return "class"
    if inspect.isfunction(obj):
        return "function"
    return type(obj).__name__


class _Bare:
    """An annotation that renders as it was written.

    The package is compiled with postponed evaluation, so `inspect` holds every
    annotation as a string and quotes it when it formats one. A reader wants
    `str | None`, not `'str | None'`, and a default of `'ctx'` must keep its
    quotes -- so only annotations are unwrapped, and only here.
    """

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text

    def __repr__(self) -> str:
        return self.text


def _bare(annotation: object) -> object:
    if annotation is inspect.Parameter.empty or not isinstance(annotation, str):
        return annotation
    return _Bare(annotation)


def _signature(name: str, obj: object) -> str:
    """``name`` as a reader would write it, with its parameters when it has any."""
    if not (inspect.isclass(obj) or inspect.isfunction(obj)):
        return name
    if isinstance(obj, type) and issubclass(obj, Enum):
        # An enum is read, not constructed. Whatever `inspect` reports here is
        # `EnumType.__call__` -- `(*values)` on 3.13, a long lookup signature
        # on 3.11 -- so rendering it both misleads the reader and makes the
        # generated file depend on the interpreter that generated it.
        return name
    try:
        found = inspect.signature(obj)
    except (TypeError, ValueError):  # pragma: no cover -- builtins, C types
        return name
    if "*args" in str(found) and "**kwargs" in str(found):
        # A Protocol, or a class that inherits its `__init__`. Reporting the
        # inherited signature would be a lie a reader could act on.
        return name
    # A constructor's return annotation is the class; saying `-> None` here is
    # an artefact of reading `__init__`.
    returns = (
        inspect.Signature.empty if inspect.isclass(obj) else found.return_annotation
    )
    found = found.replace(
        parameters=[
            parameter.replace(annotation=_bare(parameter.annotation))
            for parameter in found.parameters.values()
        ],
        return_annotation=_bare(returns),
    )
    text = f"{name}{found}"
    return text if len(text) <= 88 else f"{name}(...)"


def _home(obj: object) -> str:
    """The module a symbol is defined in, which is where its source lives."""
    module = getattr(obj, "__module__", None)
    return module if isinstance(module, str) and module.startswith("cordis") else ""


def _entry(name: str, obj: object) -> list[str]:
    lines = [f"### `{_signature(name, obj)}`", ""]
    home = _home(obj)
    detail = f"*{_kind(obj)}*" + (f" &middot; `{home}`" if home else "")
    lines += [detail, ""]
    summary = _summary(obj)
    if summary:
        lines += [summary, ""]
    return lines


def _section(record: Record, names: list[str], module: Any) -> list[str]:  # noqa: ANN401
    link = record.path.relative_to(ROOT).as_posix()
    lines = [
        f"## {record.title}",
        "",
        f"Tier {record.tier} &middot; [`{record.id}`](../{link}) &middot; "
        f"{record.rules} normative rules, {record.cards} property cards",
        "",
    ]
    if record.summary:
        lines += [record.summary, ""]
    for name in names:
        lines += _entry(name, getattr(module, name))
    return lines


def render() -> str:
    module = importlib.import_module("cordis")
    records = _records()
    exports = sorted(module.__all__)

    grouped: dict[str, list[str]] = {record.id: [] for record in records}
    orphans: list[str] = []
    for name in exports:
        owner = _owner(name, records)
        if owner is None:
            orphans.append(name)
        else:
            grouped[owner.id].append(name)

    counted = f"{len(exports)} exported names across {len(records)} capabilities."
    lines = [HEADER, counted, ""]
    for record in records:
        if grouped[record.id]:
            lines += _section(record, grouped[record.id], module)
    if orphans:  # pragma: no cover -- test_public_api fails first
        lines += ["## Undeclared", "", "No record declares these:", ""]
        lines += [f"- `{name}`" for name in orphans] + [""]
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="write nothing; fail if docs/reference.md is out of date",
    )
    options = parser.parse_args()

    sys.path.insert(0, str(ROOT / "src"))
    built = render()
    if not options.check:
        TARGET.parent.mkdir(exist_ok=True)
        TARGET.write_text(built, encoding="utf-8")
        print(f"wrote {TARGET.relative_to(ROOT)}")
        return 0
    current = TARGET.read_text(encoding="utf-8") if TARGET.is_file() else ""
    if current == built:
        print("docs/reference.md is current")
        return 0
    print("docs/reference.md is stale; run `just docs`", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
