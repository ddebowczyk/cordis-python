"""PROP-SEAM-001..005, from spec/capabilities/19-capability-seam.yaml.

The seam is a claim about *dependencies*, and a dependency is not a runtime
observation -- code that never runs still imports. PROP-SEAM-004 therefore
builds real modules from generated source and reads what ended up in their
`__dict__`s, alongside the behavioural half; a test that only swapped providers
and watched the answers change would pass on the arrangement the capability
exists to forbid.

The other four are about the registry the seam hands to a Definition package:
validation that mutates nothing, removal that belongs to the caller's scope,
notification that a broken observer cannot derail, and a resolver that leaves
no field for a use site to default on its own.
"""

from __future__ import annotations

import abc
import copy
import dataclasses
import itertools
import sys
import types
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cordis.errors import (
    ConfigValidationError,
    RegistryConflictError,
    ServiceNotFoundError,
)
from cordis.plugin import PluginHost
from cordis.registry import ChangeKind
from cordis.seam import (
    UNSET,
    Definition,
    Registry,
    RegistryChange,
    RegistryFailure,
    resolve_spec,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from cordis.context import Context
    from cordis.effect import EffectHandle, EffectNode
    from cordis.plugin import PluginHandle


# --------------------------------------------------------------------------
# A registry to exercise
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Tool:
    """The kind of thing a capability package registers: named, and refusable."""

    key: str
    payload: int = 0
    acceptable: bool = True


class Tools(Registry[Tool]):
    """A Definition package's registry: it supplies the two pure questions."""

    def key_of(self, item: Tool, /) -> str:
        if not item.key:
            raise ValueError("a tool must have a name")
        return item.key

    def check(self, item: Tool, /) -> None:
        if not item.acceptable:
            raise ValueError(f"{item.key!r} is not acceptable")


async def undo(handle: EffectHandle) -> None:
    """Dispose one registration, whatever shape its disposer turned out to be."""
    finished = handle()
    if finished is not None:
        await finished


def keys(draw: st.DrawFn, *, min_size: int = 0, max_size: int = 6) -> list[str]:
    """Distinct tool names, so a generated batch is registerable as a batch."""
    return draw(
        st.lists(
            st.sampled_from(["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]),
            min_size=min_size,
            max_size=max_size,
            unique=True,
        )
    )


# --------------------------------------------------------------------------
# PROP-SEAM-001
# --------------------------------------------------------------------------

#: Where a rejection is detected. Each one happens at a different point of
#: `_validate`, and the property is that none of them can reach the state.
FAULTS = ("no key", "duplicate", "unacceptable")


@st.composite
def rejection_cases(draw: st.DrawFn) -> tuple[list[str], str]:
    return keys(draw, min_size=1), draw(st.sampled_from(FAULTS))


def offending(fault: str, existing: Sequence[str]) -> Tool:
    if fault == "no key":
        return Tool(key="")
    if fault == "duplicate":
        return Tool(key=existing[0], payload=999)
    return Tool(key="intruder", acceptable=False)


@pytest.mark.tier_local
@settings(max_examples=200, deadline=None)
@given(case=rejection_cases())
async def test_a_rejected_candidate_changes_nothing(
    case: tuple[list[str], str],
) -> None:
    """PROP-SEAM-001: a failed registration leaves the registry as it was.

    Failure value: validation interleaved with insertion, so a rejected tool
    leaves a partial entry that shadows the valid tool of the same name -- and
    the operator sees the *good* tool stop working because a bad one was
    offered.
    """
    names, fault = case
    host = PluginHost()
    ctx = host.root.context
    registry = Tools()
    held = {name: Tool(key=name, payload=index) for index, name in enumerate(names)}
    for tool in held.values():
        registry.register(tool, ctx=ctx)

    before = copy.deepcopy(dict(registry.entries()))
    seen: list[RegistryChange[Tool]] = []
    registry.observe(seen.append)

    with pytest.raises((ValueError, RegistryConflictError)):
        registry.register(offending(fault, names), ctx=ctx)

    assert dict(registry.entries()) == before
    assert len(registry) == len(names)
    # Identity, not equality: an implementation that rebuilt the dict from
    # equal-but-new objects would have mutated exactly what must not move.
    for name, tool in held.items():
        assert registry.get(name) is tool
    assert seen == [], "a rejected candidate announced itself"


# --------------------------------------------------------------------------
# PROP-SEAM-002
# --------------------------------------------------------------------------


@st.composite
def tree_cases(draw: st.DrawFn) -> tuple[tuple[tuple[str, ...], ...], tuple[int, ...]]:
    """Plugins, what each registers, and the order they are unloaded in."""
    names = keys(draw, min_size=1)
    counts = draw(
        st.lists(st.integers(min_value=0, max_value=3), min_size=1, max_size=4)
    )
    pool = itertools.cycle(names)
    owned = tuple(
        tuple(f"{next(pool)}#{index}#{slot}" for slot in range(count))
        for index, count in enumerate(counts)
    )
    order = draw(st.permutations(range(len(owned))))
    return owned, tuple(order)


@pytest.mark.tier_local
@settings(max_examples=200, deadline=None)
@given(case=tree_cases())
async def test_registrations_belong_to_the_scope_that_made_them(
    case: tuple[tuple[tuple[str, ...], ...], tuple[int, ...]],
) -> None:
    """PROP-SEAM-002: unloading a plugin removes what it registered.

    No test calls an unregister, because there is none to call: the property
    is that the caller's scope already owns the removal (SEM-005).

    Failure value: a registry that hands back a disposer nobody is obliged to
    call, so the plugin that forgets leaves a tool pointing into an unloaded
    provider and the failure surfaces at the next request.
    """
    owned, order = case
    host = PluginHost()
    registry = Tools()
    handles: list[PluginHandle] = [
        host.root.plugin(contributor(registry, batch)) for batch in owned
    ]
    await host.runtime.quiesce()

    live = set(range(len(owned)))
    assert set(registry.entries()) == expected(owned, live)

    for index in order:
        await handles[index].dispose()
        live.discard(index)
        assert set(registry.entries()) == expected(owned, live)

    assert len(registry) == 0


def expected(owned: Sequence[Sequence[str]], live: set[int]) -> set[str]:
    return {name for index in live for name in owned[index]}


def contributor(registry: Tools, batch: Sequence[str]) -> Callable[[Context], None]:
    def plugin(ctx: Context) -> None:
        for name in batch:
            registry.register(Tool(key=name), ctx=ctx)

    return plugin


# --------------------------------------------------------------------------
# PROP-SEAM-003
# --------------------------------------------------------------------------


@dataclass
class Observer:
    """One listener, and what it did with what it was told."""

    index: int
    raises: bool
    heard: list[RegistryChange[Tool]] = field(default_factory=list)

    def __call__(self, change: RegistryChange[Tool]) -> None:
        self.heard.append(change)
        if self.raises:
            msg = f"observer {self.index} is broken"
            raise RuntimeError(msg)


@st.composite
def observer_cases(draw: st.DrawFn) -> tuple[tuple[bool, ...], list[str], list[int]]:
    broken = draw(st.lists(st.booleans(), min_size=1, max_size=4))
    # By construction, so the precondition "at least one listener raises" is
    # not left to chance and then filtered.
    broken[draw(st.integers(min_value=0, max_value=len(broken) - 1))] = True
    names = keys(draw, min_size=1)
    drops = draw(st.lists(st.integers(min_value=0, max_value=len(names) - 1)))
    return tuple(broken), names, drops


@pytest.mark.tier_local
@settings(max_examples=200, deadline=None)
@given(case=observer_cases())
async def test_a_broken_observer_derails_nothing(
    case: tuple[tuple[bool, ...], list[str], list[int]],
) -> None:
    """PROP-SEAM-003: a raising listener changes neither the mutation nor peers.

    Failure value: a metrics listener that throws on a malformed name aborting
    the registration transaction, so a cosmetic observer bug decides which
    tools an application has.
    """
    broken, names, drops = case
    host = PluginHost()
    ctx = host.root.context
    registry = Tools()
    observers = [Observer(index=i, raises=raises) for i, raises in enumerate(broken)]
    for observer in observers:
        registry.observe(observer)
    failures: list[RegistryFailure[Tool]] = []
    registry.on_error(failures.append)

    model: dict[str, Tool] = {}
    changes = 0
    handles: dict[str, EffectHandle] = {}
    for name in names:
        tool = Tool(key=name)
        handles[name] = registry.register(tool, ctx=ctx)
        model[name] = tool
        changes += 1
    for position in drops:
        name = names[position]
        if name in handles:
            await undo(handles.pop(name))
            del model[name]
            changes += 1

    assert dict(registry.entries()) == model

    for observer in observers:
        assert len(observer.heard) == changes, "a peer was skipped"
    assert len(failures) == changes * sum(broken)
    assert {failure.error.args[0] for failure in failures} == {
        f"observer {observer.index} is broken"
        for observer in observers
        if observer.raises
    }


# --------------------------------------------------------------------------
# PROP-SEAM-004
# --------------------------------------------------------------------------

DEFINITION_SOURCE = '''\
"""The contract: one artefact holding the registry name and both types."""

from __future__ import annotations

import abc
from dataclasses import dataclass

from cordis.seam import Definition


@dataclass(frozen=True)
class Request:
    value: int


class Widget(Definition):
    name = "widget"

    @abc.abstractmethod
    def run(self, request: Request) -> int: ...
'''

PROVIDER_SOURCE = '''\
"""A Provider: imports the Definition, and nothing else from the seam."""

from __future__ import annotations

from {definition} import Request, Widget


class Provider(Widget):
    def run(self, request: Request) -> int:
        return request.value * {factor} + {offset}
'''

CONSUMER_SOURCE = '''\
"""A Consumer: imports the Definition, and never a Provider."""

from __future__ import annotations

from {definition} import Request, Widget


def ask(ctx, value):
    return ctx.require(Widget).run(Request(value))
'''

_SEAMS = itertools.count()


@dataclass(frozen=True)
class Seam:
    """The three roles, as real modules."""

    definition: types.ModuleType
    providers: tuple[types.ModuleType, ...]
    consumers: tuple[types.ModuleType, ...]


def _module(name: str, source: str) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__file__ = f"<generated {name}>"
    sys.modules[name] = module
    exec(compile(source, name, "exec"), module.__dict__)
    return module


def build_seam(shapes: Sequence[tuple[int, int]], consumers: int) -> Seam:
    """Definition, one Provider per shape, and `consumers` Consumers."""
    tag = next(_SEAMS)
    definition = _module(f"seam_def_{tag}", DEFINITION_SOURCE)
    return Seam(
        definition=definition,
        providers=tuple(
            _module(
                f"seam_provider_{tag}_{index}",
                PROVIDER_SOURCE.format(
                    definition=definition.__name__, factor=factor, offset=offset
                ),
            )
            for index, (factor, offset) in enumerate(shapes)
        ),
        consumers=tuple(
            _module(
                f"seam_consumer_{tag}_{index}",
                CONSUMER_SOURCE.format(definition=definition.__name__),
            )
            for index in range(consumers)
        ),
    )


def discard(seam: Seam) -> None:
    for module in (seam.definition, *seam.providers, *seam.consumers):
        sys.modules.pop(module.__name__, None)


def depends_on(module: types.ModuleType) -> set[str]:
    """Every module ``module`` reached at import time, by name.

    Both halves of an import land in the module's own `__dict__`: `import x`
    binds the module object, `from x import y` binds an object whose
    `__module__` names it. Reading the dict catches the type-only import that
    a behavioural test never notices.
    """
    found: set[str] = set()
    for value in vars(module).values():
        if isinstance(value, types.ModuleType):
            found.add(value.__name__)
        origin = getattr(value, "__module__", None)
        if isinstance(origin, str):
            found.add(origin)
    return found


@st.composite
def seam_cases(
    draw: st.DrawFn,
) -> tuple[tuple[tuple[int, int], ...], int, tuple[int, ...]]:
    shapes = draw(
        st.lists(
            st.tuples(
                st.integers(min_value=1, max_value=9),
                st.integers(min_value=0, max_value=9),
            ),
            min_size=2,
            max_size=4,
            unique=True,
        )
    )
    consumers = draw(st.integers(min_value=1, max_value=6))
    swaps = draw(
        st.lists(
            st.integers(min_value=0, max_value=len(shapes) - 1), min_size=1, max_size=6
        )
    )
    return tuple(shapes), consumers, tuple(swaps)


@pytest.mark.tier_pr
@settings(max_examples=30, deadline=None)
@given(case=seam_cases())
async def test_swapping_the_provider_leaves_every_consumer_working(
    case: tuple[tuple[tuple[int, int], ...], int, tuple[int, ...]],
) -> None:
    """PROP-SEAM-004: providers swap; consumers neither change nor import them.

    Failure value: a Consumer importing a Provider for a type annotation only.
    It type-checks and it runs, until the deployment that does not install that
    Provider -- where the Consumer fails at import, with a traceback that names
    neither the seam nor the configuration.
    """
    shapes, consumer_count, swaps = case
    seam = build_seam(shapes, consumer_count)
    provider_names = {module.__name__ for module in seam.providers}
    consumer_names = {module.__name__ for module in seam.consumers}
    try:
        # The structural half. Vacuity is guarded: each role must be shown to
        # import the Definition, or "imports no Provider" is satisfied by a
        # module that imports nothing at all.
        for consumer in seam.consumers:
            reached = depends_on(consumer)
            assert seam.definition.__name__ in reached
            assert not (reached & provider_names)
        for provider in seam.providers:
            reached = depends_on(provider)
            assert seam.definition.__name__ in reached
            assert not (reached & consumer_names)

        # The behavioural half, through one unmodified copy of the consumers.
        host = PluginHost()
        ctx = host.root.context
        for chosen in swaps:
            factor, offset = shapes[chosen]
            mounted = host.root.plugin(seam.providers[chosen].Provider)
            await host.runtime.quiesce()
            for consumer in seam.consumers:
                for value in (0, 3, 7):
                    assert consumer.ask(ctx, value) == value * factor + offset
            await mounted.dispose()
        await host.dispose()
    finally:
        discard(seam)


# --------------------------------------------------------------------------
# PROP-SEAM-005
# --------------------------------------------------------------------------


@dataclass
class Retry:
    attempts: int = 3
    backoff: float = 0.5


@dataclass
class Endpoint:
    """A spec whose every field has one default, in one place."""

    url: str = "https://example.invalid"
    timeout: float = 30.0
    verify: bool = True
    headers: dict[str, str] = field(default_factory=dict)
    retry: Retry = field(default_factory=Retry)


@dataclass
class Credentials:
    token: str = UNSET


@dataclass
class Partial:
    """A spec with fields nothing resolves: the drift SEM-003 forbids.

    One at the top level and one a field deeper, because a completeness scan
    that only looked at the outermost dataclass would pass a spec whose nested
    section is the half nobody filled in.
    """

    url: str = "https://example.invalid"
    region: str = UNSET
    credentials: Credentials = field(default_factory=Credentials)


GIVEN = {
    "url": st.just("https://example.test"),
    "timeout": st.floats(min_value=0.1, max_value=100, allow_nan=False),
    "verify": st.booleans(),
    "headers": st.dictionaries(st.text(max_size=4), st.text(max_size=4), max_size=2),
    "retry": st.fixed_dictionaries({"attempts": st.integers(min_value=0, max_value=5)}),
}


def sentinels(value: object) -> list[str]:
    """Every field still holding the sentinel, at any depth, by path."""
    if not dataclasses.is_dataclass(value) or isinstance(value, type):
        return []
    found: list[str] = []
    for spec in dataclasses.fields(value):
        held = getattr(value, spec.name)
        if held is UNSET:
            found.append(spec.name)
        found.extend(f"{spec.name}.{path}" for path in sentinels(held))
    return found


@pytest.mark.tier_local
@settings(max_examples=200, deadline=None)
@given(raw=st.fixed_dictionaries({}, optional=GIVEN))
async def test_resolution_is_total_and_idempotent(raw: dict[str, object]) -> None:
    """PROP-SEAM-005: resolution fills every field, and resolving again is a no-op.

    Completeness is read off `dataclasses.fields` rather than a hand-written
    list, so a field added to the spec and forgotten by the resolver is a test
    failure without anyone remembering to extend the test.

    Failure value: a new optional field carried as None into every use site,
    each of which grows its own inline default -- SEM-003's exact drift.
    """
    resolved = resolve_spec(Endpoint, raw)

    assert sentinels(resolved) == []
    assert resolve_spec(Endpoint, resolved) == resolved
    # Total means total: a spec asked for with nothing at all is the spec
    # every default answers for.
    assert resolve_spec(Endpoint) == resolve_spec(Endpoint, {})
    # Every key that was given survives; the rest came from the one place a
    # default is written.
    for name, value in raw.items():
        if name != "retry":
            assert getattr(resolved, name) == value
    assert resolved.retry.backoff == Retry().backoff


@pytest.mark.tier_local
async def test_a_field_no_resolver_fills_is_named() -> None:
    """A spec that is not total fails at resolution, naming every gap."""
    with pytest.raises(ConfigValidationError) as caught:
        resolve_spec(Partial, {})

    assert [issue.path for issue in caught.value.issues] == [
        ("region",),
        ("credentials", "token"),
    ]
    assert "region" in str(caught.value)
    assert "credentials.token" in str(caught.value)
    # And it is the sentinel that is refused, not the field: supply them and
    # the same spec resolves.
    filled = resolve_spec(Partial, {"region": "eu", "credentials": {"token": "t"}})
    assert (filled.region, filled.credentials.token) == ("eu", "t")


@pytest.mark.tier_local
async def test_an_invalid_config_is_refused_before_it_is_completed() -> None:
    """Type failures still arrive as config issues, all of them at once."""
    with pytest.raises(ConfigValidationError) as caught:
        resolve_spec(Endpoint, {"timeout": "soon", "nope": 1})

    assert {issue.path for issue in caught.value.issues} == {("timeout",), ("nope",)}


# --------------------------------------------------------------------------
# Seams around the registry surface
# --------------------------------------------------------------------------


@pytest.mark.tier_local
async def test_a_definition_needs_no_registry_name_but_a_provider_does() -> None:
    """The abstract base is nameless; anything concrete is not (SEM-001)."""
    from cordis.errors import InvalidPluginError
    from cordis.seam import Definition

    class Widget(Definition):
        name = "widget"

    assert Widget.name == "widget"

    with pytest.raises(InvalidPluginError):

        class Nameless(Definition):  # a Provider that forgot
            pass


@pytest.mark.tier_local
async def test_the_registry_view_is_a_snapshot_nobody_can_edit() -> None:
    host = PluginHost()
    registry = Tools()
    registry.register(Tool(key="alpha"), ctx=host.root.context)

    view = registry.entries()
    with pytest.raises(TypeError):
        view["beta"] = Tool(key="beta")  # type: ignore[index]

    # And it is a snapshot, not a window: what a caller was handed does not
    # move under them when the registry does.
    registry.register(Tool(key="beta"), ctx=host.root.context)
    assert set(view) == {"alpha"}
    assert set(registry.entries()) == {"alpha", "beta"}


@pytest.mark.tier_local
async def test_a_conflict_names_the_registry_and_the_key() -> None:
    host = PluginHost()
    ctx = host.root.context
    registry = Tools()
    incumbent = Tool(key="alpha", payload=1)
    registry.register(incumbent, ctx=ctx)

    with pytest.raises(RegistryConflictError) as caught:
        registry.register(Tool(key="alpha", payload=2), ctx=ctx)

    assert caught.value.key == "alpha"
    assert caught.value.registry == "Tools"
    assert registry.get("alpha") is incumbent


@pytest.mark.tier_local
async def test_the_change_a_listener_hears_carries_the_item() -> None:
    host = PluginHost()
    registry = Tools()
    heard: list[RegistryChange[Tool]] = []
    stop = registry.observe(heard.append)
    tool = Tool(key="alpha", payload=7)

    handle = registry.register(tool, ctx=host.root.context)
    await undo(handle)
    stop()
    registry.register(Tool(key="beta"), ctx=host.root.context)

    assert [(change.kind, change.key, change.item) for change in heard] == [
        (ChangeKind.ADDED, "alpha", tool),
        (ChangeKind.REMOVED, "alpha", tool),
    ]


@pytest.mark.tier_local
async def test_a_broken_observer_with_no_channel_reaches_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Containment is not silence: with no handler, the traceback still shows."""
    host = PluginHost()
    registry = Tools()

    def broken(_change: RegistryChange[Tool]) -> None:
        msg = "no channel installed"
        raise RuntimeError(msg)

    registry.observe(broken)
    registry.register(Tool(key="alpha"), ctx=host.root.context)

    assert "no channel installed" in capsys.readouterr().err


@pytest.mark.tier_local
async def test_registrations_appear_in_the_effect_tree() -> None:
    """The entry is an effect, so diagnostics can see who registered what."""
    from cordis.diagnostics import inspect, walk

    host = PluginHost()
    registry = Tools()

    def plugin(ctx: Context) -> None:
        registry.register(Tool(key="alpha"), ctx=ctx, label="tool:alpha")

    host.root.plugin(plugin)
    await host.runtime.quiesce()

    labels = {
        node.label
        for snapshot in walk(inspect(host))
        for node in _effects(snapshot.effects)
    }
    assert "tool:alpha" in labels


def _effects(node: EffectNode) -> Iterator[EffectNode]:
    yield node
    for child in node.children:
        yield from _effects(child)


# --------------------------------------------------------------------------
# Resolving a capability by its Definition
# --------------------------------------------------------------------------


class Clipboard(Definition):
    """A Definition: abstract, and the sole thing a consumer imports."""

    name = "clipboard"

    @abc.abstractmethod
    def paste(self) -> str: ...


class Plain(Clipboard):
    """One provider of it."""

    def paste(self) -> str:
        return "plain"


@pytest.mark.tier_local
async def test_a_definition_resolves_to_whichever_provider_is_bound() -> None:
    """`Definition.of(ctx)` is the consumer's spelling, and it stays typed.

    `ctx.require(Clipboard)` is what a reader reaches for and what
    `mypy --strict` rejects: `require` takes `type[T]`, and an abstract class
    is not that. The classmethod resolves by name instead, so no consumer
    needs a suppression on the line the seam exists for.
    """
    host = PluginHost()
    host.root.plugin(Plain)
    await host.runtime.quiesce()

    found = Clipboard.of(host.root.context)

    assert isinstance(found, Plain)
    assert found.paste() == "plain"
    await host.dispose()


@pytest.mark.tier_local
async def test_an_unbound_definition_says_which_name_is_missing() -> None:
    """The failure is the registry's, not a None the consumer has to check."""
    host = PluginHost()
    with pytest.raises(ServiceNotFoundError, match="clipboard"):
        Clipboard.of(host.root.context)
    await host.dispose()
