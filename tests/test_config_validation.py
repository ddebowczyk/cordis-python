"""PROP-CONFIG-001..007, from spec/capabilities/07-config-validation.yaml.

Schemas are generated, not hand-written, and the invalid configs are derived
from the schema that rejects them. Generating values and keeping the ones that
happen to be invalid would spend most of the budget on configs that are wrong
in the same uninteresting way -- a missing field -- and would never produce the
case the paths are for, which is a violation four levels down in a structure
that is otherwise correct.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass, field, make_dataclass
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from cordis.config import (
    ConfigIssue,
    ConfigResult,
    config_schema,
    from_dataclass,
    resolve_config,
    schema_of,
)
from cordis.errors import AsyncValidationError, ConfigValidationError
from cordis.fiber import FiberState
from cordis.plugin import PluginHost

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cordis.context import Context

# --------------------------------------------------------------------------
# Generated schemas
# --------------------------------------------------------------------------

#: What the stdlib adapter is required to understand. Anything else is a
#: declaration-time rejection, which is a different card's business.
SCALARS = ("int", "str", "bool", "ints")


@dataclass(frozen=True)
class Spec:
    """One field of a generated schema."""

    name: str
    kind: str
    defaulted: bool
    inner: tuple[Spec, ...] = ()


@st.composite
def shapes(draw: st.DrawFn, depth: int = 1) -> tuple[Spec, ...]:
    kinds = (*SCALARS, "nested") if depth > 0 else SCALARS
    count = draw(st.integers(min_value=1, max_value=3))
    specs = []
    for index in range(count):
        kind = draw(st.sampled_from(kinds))
        specs.append(
            Spec(
                name=f"f{index}",
                kind=kind,
                defaulted=draw(st.booleans()),
                inner=draw(shapes(depth - 1)) if kind == "nested" else (),
            )
        )
    return _settle(specs)


def _settle(specs: Sequence[Spec]) -> tuple[Spec, ...]:
    """Make a generated shape into one a dataclass can actually express.

    Two constraints the generator does not know about: a defaulted field
    cannot precede an undefaulted one, and a nested field can only default to
    a freshly constructed instance if that nested type is itself constructible
    with no arguments. Fixing both here rather than in `build` keeps the shape
    the single source of truth for what the schema requires -- a test that
    omits `spec.defaulted` fields has to be omitting the ones the schema really
    defaults.
    """
    settled = []
    for spec in specs:
        inner = _settle(spec.inner) if spec.inner else ()
        defaulted = spec.defaulted and all(child.defaulted for child in inner)
        settled.append(
            Spec(name=spec.name, kind=spec.kind, defaulted=defaulted, inner=inner)
        )
    return tuple(sorted(settled, key=lambda spec: spec.defaulted))


_COUNTER = [0]


def build(shape: tuple[Spec, ...]) -> type[Any]:
    """Turn a generated shape into a dataclass."""
    _COUNTER[0] += 1
    fields: list[tuple[str, Any, Any]] = []
    for spec in shape:
        annotation = _annotation(spec)
        if not spec.defaulted:
            fields.append((spec.name, annotation, field()))
            continue
        default = _default(spec, annotation)
        fields.append((spec.name, annotation, default))
    return make_dataclass(f"Generated{_COUNTER[0]}", fields)


def _annotation(spec: Spec) -> Any:
    if spec.kind == "int":
        return int
    if spec.kind == "str":
        return str
    if spec.kind == "bool":
        return bool
    if spec.kind == "ints":
        return list[int]
    return build(spec.inner)


def _default(spec: Spec, annotation: Any) -> Any:
    if spec.kind == "int":
        return field(default=7)
    if spec.kind == "str":
        return field(default="d")
    if spec.kind == "bool":
        return field(default=True)
    return field(default_factory=annotation)


def valid(shape: tuple[Spec, ...]) -> st.SearchStrategy[dict[str, Any]]:
    """A raw mapping every field of which satisfies its declared type."""
    return st.fixed_dictionaries({spec.name: _valid_value(spec) for spec in shape})


def _valid_value(spec: Spec) -> st.SearchStrategy[Any]:
    if spec.kind == "int":
        return st.integers(min_value=-100, max_value=100)
    if spec.kind == "str":
        return st.text(alphabet="abcdef", max_size=5)
    if spec.kind == "bool":
        return st.booleans()
    if spec.kind == "ints":
        return st.lists(st.integers(min_value=-9, max_value=9), max_size=3)
    return valid(spec.inner)


def _wrong_value(spec: Spec) -> st.SearchStrategy[Any]:
    """A value of the wrong shape for this field, and only the wrong shape."""
    if spec.kind == "int":
        # Not a float and not a bool-shaped int: `True` is an `int` to
        # `isinstance`, which is the whole reason bools are rejected for int
        # fields, but that belongs to its own assertion rather than to a
        # generator that is supposed to produce obvious violations.
        return st.text(alphabet="xyz", min_size=1, max_size=3)
    if spec.kind == "str":
        return st.integers(min_value=0, max_value=9)
    if spec.kind == "bool":
        return st.text(alphabet="xyz", min_size=1, max_size=3)
    if spec.kind == "ints":
        return st.one_of(st.text(alphabet="xyz", max_size=3), st.integers())
    return st.one_of(st.integers(), st.text(alphabet="xyz", max_size=3))


Path = tuple[str | int, ...]


@st.composite
def corrupted(
    draw: st.DrawFn, shape: tuple[Spec, ...]
) -> tuple[dict[str, Any], frozenset[Path]]:
    """A valid config with 1-3 fields deliberately broken, and where."""
    config = draw(valid(shape))
    targets = draw(
        st.lists(st.sampled_from(shape), min_size=1, max_size=3, unique_by=id)
    )
    paths: set[Path] = set()
    for spec in targets:
        if spec.kind == "nested" and draw(st.booleans()):
            # One level down, so the path has to have two components to be
            # useful: reporting `f0` for a broken `f0.f1` is the defect the
            # card is about.
            inner = draw(st.sampled_from(spec.inner))
            config[spec.name][inner.name] = draw(_wrong_value(inner))
            paths.add((spec.name, inner.name))
        else:
            config[spec.name] = draw(_wrong_value(spec))
            paths.add((spec.name,))
    return config, frozenset(paths)


def walk(config: object, path: Path) -> object:
    """Follow a reported path back into the config the test submitted."""
    cursor = config
    for step in path:
        if isinstance(step, int):
            assert isinstance(cursor, list), f"{path}: {step} is not an index here"
            assert step < len(cursor), f"{path}: index {step} is out of range"
            cursor = cursor[step]
        else:
            assert isinstance(cursor, dict), f"{path}: {step} is not a key here"
            assert step in cursor, f"{path}: no key {step!r} here"
            cursor = cursor[step]
    return cursor


# --------------------------------------------------------------------------
# Plugins the tests mount
# --------------------------------------------------------------------------


class Watched:
    """Every config a body was handed, and what its context reported."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, object]] = []

    def plugin(self, schema: object | None = None) -> Any:
        def apply(ctx: Context, config: object) -> None:
            self.calls.append((config, ctx.config))

        apply.__qualname__ = "watched"
        if schema is not None:
            apply.Config = schema  # type: ignore[attr-defined]
        return apply


async def settle(host: PluginHost) -> None:
    """Wait for every top-level mount, however it ended.

    A fiber that failed re-raises when awaited (04 SEM-006), and several of
    these tests are about exactly that failure, so what gets asserted on is the
    state rather than the await.
    """
    for fiber in host.root.children:
        with suppress(Exception):
            await fiber
    await host.runtime.quiesce()


# --------------------------------------------------------------------------
# PROP-CONFIG-001
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(data=st.data())
@settings(deadline=None, max_examples=50)
def test_an_invalid_config_never_reaches_the_body(data: st.DataObject) -> None:
    """Failure value: validating after constructing the child context and
    calling the body's import-time side effects, so a mistyped field still runs
    half a plugin."""
    shape = data.draw(shapes())
    schema = build(shape)
    raw, _paths = data.draw(corrupted(shape))
    watched = Watched()

    async def drive() -> None:
        host = PluginHost()
        fiber = host.root.plugin(watched.plugin(schema), raw)
        await settle(host)
        assert watched.calls == [], "the body ran on a config that does not validate"
        assert fiber.state is FiberState.FAILED
        assert isinstance(fiber.error, ConfigValidationError)
        await host.dispose()

    asyncio.run(drive())


# --------------------------------------------------------------------------
# PROP-CONFIG-002
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(data=st.data())
@settings(deadline=None, max_examples=50)
def test_the_body_receives_exactly_what_the_schema_resolved(
    data: st.DataObject,
) -> None:
    """Failure value: passing the raw dict to the body while validating a copy,
    so declared defaults are silently absent and every plugin author
    reintroduces `config.get("timeout", 30)` -- with a different number than
    the schema's."""
    shape = data.draw(shapes())
    schema = build(shape)
    raw = data.draw(valid(shape))
    # Omit a generated subset of the defaulted fields: what comes back for
    # those is the schema's business, and the framework's job is to not touch
    # it either way.
    for spec in shape:
        if spec.defaulted and data.draw(st.booleans()):
            raw.pop(spec.name, None)

    expected = from_dataclass(schema).validate(copy.deepcopy(raw))
    assume(expected.ok)
    watched = Watched()

    async def drive() -> None:
        host = PluginHost()
        fiber = host.root.plugin(watched.plugin(schema), raw)
        await settle(host)
        assert fiber.state is FiberState.ACTIVE, f"{fiber.error!r}"
        assert len(watched.calls) == 1
        observed, from_context = watched.calls[0]
        assert observed == expected.value
        # The context reports the same object, so a child mounted by this body
        # cannot see a different config than its parent was given.
        assert from_context is observed
        await host.dispose()

    asyncio.run(drive())


# --------------------------------------------------------------------------
# PROP-CONFIG-003
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(data=st.data())
@settings(deadline=None, max_examples=50)
def test_validating_twice_answers_the_same_and_changes_nothing(
    data: st.DataObject,
) -> None:
    """Failure value: a schema adapter that applies defaults by mutating the
    input dict in place, so re-reading an unchanged config file produces a
    config that differs from the previous read and restarts every plugin."""
    shape = data.draw(shapes())
    schema = from_dataclass(build(shape))
    raw = data.draw(st.one_of(valid(shape), corrupted(shape).map(lambda pair: pair[0])))
    # Fields the schema defaults are dropped from a share of the runs. An
    # adapter that defaults by writing into the mapping it was handed is
    # invisible on a config that already names every field, which is exactly
    # the config a generator produces unless it is told not to.
    for spec in shape:
        if spec.defaulted and data.draw(st.booleans()):
            raw.pop(spec.name, None)
    before = copy.deepcopy(raw)

    first = schema.validate(raw)
    second = schema.validate(raw)

    assert first.ok is second.ok
    assert first.value == second.value
    assert [str(issue) for issue in first.issues] == [
        str(issue) for issue in second.issues
    ]
    assert raw == before, "validation modified the config it was given"


# --------------------------------------------------------------------------
# PROP-CONFIG-004
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(data=st.data())
@settings(deadline=None, max_examples=50)
def test_every_violation_is_reported_at_a_path_that_exists(
    data: st.DataObject,
) -> None:
    """Failure value: reporting only the first issue, or reporting paths
    relative to a normalised internal structure so `plugins[2].timeout` is
    reported as `timeout` and the operator cannot tell which of forty rows is
    wrong."""
    shape = data.draw(shapes())
    schema = from_dataclass(build(shape))
    raw, broken = data.draw(corrupted(shape))

    result = schema.validate(raw)
    assert not result.ok
    reported = {issue.path for issue in result.issues}
    # Every deliberate corruption is named -- not just the first one.
    assert broken <= reported, f"missed {sorted(broken - reported)}"
    for path in reported:
        walk(raw, path)  # every reported path resolves in what was submitted


# --------------------------------------------------------------------------
# PROP-CONFIG-005
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(
    config=st.one_of(
        st.none(),
        st.integers(),
        st.text(max_size=5),
        st.lists(st.integers(), max_size=3),
        st.dictionaries(st.text(max_size=3), st.integers(), max_size=3),
        st.builds(object),
        st.just(print),  # a live callable, which no normaliser can round-trip
    )
)
@settings(deadline=None)
def test_a_plugin_without_a_schema_gets_exactly_what_it_was_given(
    config: object,
) -> None:
    """Failure value: round-tripping unschema'd config through a JSON
    normaliser "for consistency", which breaks any plugin configured with a
    live object such as a client handle or a callable."""
    watched = Watched()

    async def drive() -> None:
        host = PluginHost()
        fiber = host.root.plugin(watched.plugin(), config)
        await settle(host)
        assert fiber.state is FiberState.ACTIVE
        observed, from_context = watched.calls[0]
        assert observed is config
        assert from_context is config
        await host.dispose()

    asyncio.run(drive())


# --------------------------------------------------------------------------
# PROP-CONFIG-006
# --------------------------------------------------------------------------


class AsyncValidator:
    async def validate(self, raw: object, /) -> ConfigResult[object]:
        await asyncio.sleep(0)
        return ConfigResult.accepted(raw)


class AwaitableValidator:
    def validate(self, raw: object, /) -> Any:
        async def later() -> ConfigResult[object]:
            return ConfigResult.accepted(raw)

        return later()


async def _validate_module_level(raw: object, /) -> ConfigResult[object]:
    return ConfigResult.accepted(raw)


class BareCoroutineFunction:
    validate = staticmethod(_validate_module_level)


@pytest.mark.tier_local
@pytest.mark.parametrize(
    "schema",
    [AsyncValidator(), AwaitableValidator(), BareCoroutineFunction()],
    ids=["coroutine-method", "returns-awaitable", "staticmethod-coroutine"],
)
def test_an_async_validator_is_refused_at_the_mount(schema: object) -> None:
    """Failure value: awaiting the validator, so a fiber's state depends on IO
    timing -- the same config makes a plugin ACTIVE or PENDING depending on how
    quickly a remote schema service answered, and startup stops being
    deterministic."""
    watched = Watched()

    async def drive() -> None:
        host = PluginHost()
        with pytest.raises(AsyncValidationError):
            host.root.plugin(watched.plugin(schema), {"any": 1})
        assert host.root.children == ()
        assert watched.calls == []
        await host.dispose()

    asyncio.run(drive())


# --------------------------------------------------------------------------
# PROP-CONFIG-007
# --------------------------------------------------------------------------


@dataclass
class Simple:
    size: int = 3


class ClassSchema:
    """A schema written as a plain class with a classmethod."""

    @classmethod
    def validate(cls, raw: object, /) -> ConfigResult[dict[str, int]]:
        if not isinstance(raw, dict):
            return ConfigResult.rejected(ConfigIssue((), "must be a mapping"))
        return ConfigResult.accepted({"seen": len(raw)})


class InstanceSchema:
    def __init__(self, key: str) -> None:
        self.key = key

    def validate(self, raw: object, /) -> ConfigResult[dict[str, object]]:
        assert isinstance(raw, dict)
        return ConfigResult.accepted({self.key: raw.get(self.key)})


def closure_schema(label: str) -> Any:
    """A schema that is not a class at all."""

    class Made:
        def validate(self, raw: object, /) -> ConfigResult[str]:
            return ConfigResult.accepted(f"{label}:{raw}")

    return Made()


@pytest.mark.tier_local
@pytest.mark.parametrize(
    ("schema", "raw", "expected"),
    [
        (ClassSchema, {"a": 1, "b": 2}, {"seen": 2}),
        (InstanceSchema("k"), {"k": 9}, {"k": 9}),
        (closure_schema("x"), 4, "x:4"),
        (Simple, {"size": 8}, Simple(size=8)),
    ],
    ids=["class", "instance", "closure", "bare-dataclass"],
)
def test_anything_shaped_like_a_schema_is_a_schema(
    schema: object, raw: object, expected: object
) -> None:
    """Failure value: a core that only accepts registered schema types, so
    every library needs an adapter merged upstream before it can be used."""
    watched = Watched()

    async def drive() -> None:
        host = PluginHost()
        fiber = host.root.plugin(watched.plugin(schema), raw)
        await settle(host)
        assert fiber.state is FiberState.ACTIVE, f"{fiber.error!r}"
        assert watched.calls[0][0] == expected
        await host.dispose()

    asyncio.run(drive())


@pytest.mark.tier_local
def test_the_kernel_imports_nothing_outside_the_standard_library() -> None:
    """Failure value: a core that imports pydantic to define its own config
    type, so every application depending on this framework inherits a pinned
    major version of a library it may not use at all."""
    script = (
        "import importlib, pkgutil, sys, cordis\n"
        "for info in pkgutil.walk_packages(cordis.__path__, 'cordis.'):\n"
        "    importlib.import_module(info.name)\n"
        "outside = sorted(\n"
        "    name for name in sys.modules\n"
        "    if name.split('.')[0] not in sys.stdlib_module_names\n"
        "    and not name.startswith(('cordis', '_'))\n"
        ")\n"
        "print(','.join(outside))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == "", f"third-party imports: {proc.stdout.strip()}"


# --------------------------------------------------------------------------
# The declaration seam
# --------------------------------------------------------------------------


def test_a_schema_can_be_declared_three_ways() -> None:
    """Attribute, decorator, and a bare dataclass adapted on sight."""

    def bare(ctx: Context, config: Simple) -> None: ...

    bare.Config = Simple  # type: ignore[attr-defined]

    @config_schema(from_dataclass(Simple))
    def decorated(ctx: Context, config: Simple) -> None: ...

    class Declared:
        Config = Simple

        def apply(self, ctx: Context, config: Simple) -> None: ...

    for target in (bare, decorated, Declared()):
        schema = schema_of(target)
        assert schema is not None
        assert schema.validate({"size": 4}).value == Simple(size=4)
    assert schema_of(lambda ctx: None) is None


def test_an_unsupported_annotation_is_rejected_where_it_is_declared() -> None:
    """A schema the adapter cannot check must say so when it is adapted.

    Accepting it and checking nothing would be worse than refusing: the author
    would believe their config was validated.
    """

    @dataclass
    class Odd:
        when: complex = 0j

    with pytest.raises(TypeError, match="complex"):
        from_dataclass(Odd)


def test_a_bool_is_not_an_int() -> None:
    """`isinstance(True, int)` is True, and a timeout of `true` is not a
    timeout. The one place the adapter is deliberately stricter than Python."""

    @dataclass
    class Timeouts:
        seconds: int = 30

    result = from_dataclass(Timeouts).validate({"seconds": True})
    assert not result.ok
    assert result.issues[0].path == ("seconds",)


def test_resolve_config_names_the_plugin_and_every_issue() -> None:
    """The error a fiber fails with has to be readable on its own."""

    @dataclass
    class Both:
        host: str
        port: int

    with pytest.raises(ConfigValidationError) as caught:
        resolve_config(from_dataclass(Both), {"host": 1, "port": "x"}, plugin="api")
    assert "api" in str(caught.value)
    assert {issue.path for issue in caught.value.issues} == {("host",), ("port",)}


def test_dataclasses_are_left_alone_when_they_are_already_the_value() -> None:
    """A config that is already an instance of the schema passes through.

    Configuration does not always come from a file: a test, or a parent plugin
    composing a child, has the typed object in hand already, and requiring it
    to be turned back into a mapping first would be a chore with no purpose.
    """
    value = Simple(size=11)
    result = from_dataclass(Simple).validate(value)
    assert result.ok
    assert result.value is value


def test_unknown_keys_are_reported_rather_than_ignored() -> None:
    """A misspelled key is the most common config error there is.

    Silently dropping it means the operator's edit had no effect and nothing
    said so, which is the failure this capability exists to prevent.
    """
    result = from_dataclass(Simple).validate({"size": 1, "sixe": 2})
    assert not result.ok
    assert any(issue.path == ("sixe",) for issue in result.issues)


def test_a_missing_required_field_is_named() -> None:
    @dataclass
    class Needs:
        token: str

    result = from_dataclass(Needs).validate({})
    assert not result.ok
    assert result.issues[0].path == ("token",)


def test_the_shape_generator_builds_what_it_claims() -> None:
    """The generator's own check: a shape must produce a dataclass whose
    fields match it, or every card built on it is testing something else."""
    shape = _settle(
        (
            Spec("f0", "int", defaulted=False),
            Spec("f1", "str", defaulted=True),
            Spec("f2", "nested", defaulted=True, inner=(Spec("g0", "bool", True),)),
        )
    )
    built = build(shape)
    names = [f.name for f in dataclasses.fields(built)]
    assert names == ["f0", "f1", "f2"]
    resolved = from_dataclass(built).validate({"f0": 1})
    assert resolved.ok
    assert resolved.value.f1 == "d"  # type: ignore[union-attr]
    assert resolved.value.f2.g0 is True  # type: ignore[union-attr]

    # A nested type that cannot be constructed empty cannot be defaulted, and
    # the shape has to say so or the omission logic in 002 lies.
    demanding = _settle((Spec("f0", "nested", True, (Spec("g0", "int", False),)),))
    assert demanding[0].defaulted is False


def _issue_paths(issues: Sequence[ConfigIssue]) -> set[Path]:
    return {issue.path for issue in issues}
