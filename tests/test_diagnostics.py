"""PROP-DIAG-001..005, from spec/capabilities/11-diagnostics.yaml.

Every model here is built from the operations the test issued -- the generated
tree, the generated effect plan, the generated provider set -- and never from
the runtime's own bookkeeping. That is the whole point of a diagnostic surface:
if the report were derived from the same walk the report is checked against, it
would agree with itself while being wrong.
"""

from __future__ import annotations

import copy
import dataclasses
import gc
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import cordis.effect as effect_module
from cordis.diagnostics import (
    FiberSnapshot,
    inspect,
    pending,
    render_tree,
    walk,
)
from cordis.errors import mount_sites
from cordis.fiber import FiberState
from cordis.inject import inject
from cordis.plugin import PluginHost, fiber_of, scope_of
from cordis.registry import Service
from tests.support import TreeSpec
from tests.support import strategies as gen

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from cordis.context import Context
    from cordis.effect import EffectHandle, EffectNode
    from cordis.fiber import Fiber

#: What a location reads as when capture is off. Spelled out rather than
#: imported: the test is checking the published behaviour of the flag, not
#: agreeing with the constant the implementation happens to use.
UNKNOWN = "<unknown>"

POOL = ("s0", "s1", "s2", "s3")


# --------------------------------------------------------------------------
# Shared harness
# --------------------------------------------------------------------------


async def quiet(host: PluginHost) -> None:
    """Let the tree finish reacting, so an invariant is read at quiescence."""
    for _ in range(8):
        before = _states(host)
        for fiber in _fibers(host.root):
            await fiber
        await host.runtime.quiesce()
        if _states(host) == before:
            return
    raise AssertionError("the tree never stopped changing")


def _fibers(fiber: Fiber) -> Iterator[Fiber]:
    yield fiber
    for child in fiber.children:
        yield from _fibers(child)


def _states(host: PluginHost) -> dict[int, str]:
    return {fiber.uid: fiber.state.name for fiber in _fibers(host.root)}


def named(fn: Callable[..., Any], label: str) -> Any:
    """Give a plugin body the label a mount will derive from it."""
    fn.__name__ = label
    fn.__qualname__ = label
    return fn


async def undo(handle: EffectHandle) -> None:
    result = handle()
    if result is not None:
        await result


def leaf(label: str) -> str:
    """The name a mount derived, out of the path label it composed.

    A mount label is ``<parent label>/<derived name>#<mint index>``: unique
    across a tree, and not predictable from the plugin alone, since the index
    counts every mount the parent has ever minted. The tests assert on the name
    they gave; the composition rule is `10-plugin-mounting`'s business.
    """
    return label.rsplit("/", 1)[-1].split("#")[0]


def leaf_of(label: str | None) -> str | None:
    """:func:`leaf`, for the optional labels a :class:`Blockage` carries."""
    return None if label is None else leaf(label)


def structure(snapshot: FiberSnapshot) -> dict[str, tuple[str, ...]]:
    """Name -> child names, for the whole snapshot tree."""
    return {
        leaf(node.label): tuple(leaf(child.label) for child in node.children)
        for node in walk(snapshot)
    }


# --------------------------------------------------------------------------
# PROP-DIAG-001
# --------------------------------------------------------------------------


def branch(spec: TreeSpec, seen: dict[str, Fiber]) -> Any:
    """A plugin that mounts the children its spec declares, and names itself."""

    def apply(ctx: Context) -> None:
        mine = fiber_of(ctx)
        assert mine is not None
        seen[spec.label] = mine
        for child in spec.children:
            ctx.plugin(branch(child, seen))

    return named(apply, spec.label)


def surviving(spec: TreeSpec, dropped: frozenset[str]) -> dict[str, tuple[str, ...]]:
    """The tree the test *should* be looking at, from the operations it issued."""
    if spec.label in dropped:
        return {}
    live = [child for child in spec.children if child.label not in dropped]
    model = {spec.label: tuple(child.label for child in live)}
    for child in live:
        model.update(surviving(child, dropped))
    return model


@st.composite
def trees_with_disposals(draw: st.DrawFn) -> tuple[TreeSpec, tuple[str, ...]]:
    spec = draw(gen.tree_specs(max_children=2, max_leaves=5))
    labels = [node.label for _path, node in spec.walk() if node.label != spec.label]
    order = draw(st.lists(st.sampled_from(labels), max_size=3)) if labels else []
    return spec, tuple(order)


@pytest.mark.tier_local
@settings(max_examples=100, deadline=None)
@given(plan=trees_with_disposals())
async def test_the_snapshot_is_the_tree_that_is_actually_there(
    plan: tuple[TreeSpec, tuple[str, ...]],
) -> None:
    """PROP-DIAG-001: fiber set and parent-child structure match what was mounted.

    Failure value: a disposed fiber remaining in the snapshot, so an operator
    debugging a reload sees a plugin that no longer exists and chases a
    phantom.
    """
    spec, order = plan
    host = PluginHost()
    seen: dict[str, Fiber] = {}
    root = host.root.plugin(branch(spec, seen))
    await quiet(host)

    dropped: set[str] = set()
    for label in order:
        await seen[label].dispose()
        await quiet(host)
        # Disposing a node takes its descendants with it, which the model has
        # to know because the runtime will not be asked.
        dropped |= {node.label for _p, node in _subtree(spec, label).walk()}

    snapshot = inspect(root)
    assert structure(snapshot) == surviving(spec, frozenset(dropped))

    uids = [node.uid for node in walk(snapshot)]
    assert len(set(uids)) == len(uids), "two instances shared an identity"


def _subtree(spec: TreeSpec, label: str) -> TreeSpec:
    for _path, node in spec.walk():
        if node.label == label:
            return node
    raise AssertionError(f"no node labelled {label}")


# --------------------------------------------------------------------------
# PROP-DIAG-002
# --------------------------------------------------------------------------


def provider(name: str, *, needs: Sequence[str] = ()) -> type[Service]:
    """A plugin that *declares* it will bind ``name``, and what it needs first."""
    return type(
        f"Svc_{name}",
        (Service,),
        {"name": name, "inject": tuple(needs), "__module__": __name__},
    )


def waiter(who: str, needs: Sequence[str]) -> Any:
    return inject(*needs)(named(lambda ctx: None, who))


@pytest.mark.tier_pr
@settings(max_examples=200, deadline=None)
@given(
    wants=st.lists(
        st.frozensets(st.sampled_from(POOL), min_size=1, max_size=3),
        min_size=1,
        max_size=3,
    ),
    supplied=st.frozensets(st.sampled_from(POOL), max_size=4),
)
async def test_pending_names_every_unmet_dependency_and_only_those(
    wants: list[frozenset[str]], supplied: frozenset[str]
) -> None:
    """PROP-DIAG-002: the report is `declared - provided`, exactly.

    Failure value: reporting the first unmet dependency only, so an operator
    provides it, restarts, and discovers a second one -- repeated for as many
    dependencies as the plugin declares.
    """
    host = PluginHost()
    for name in sorted(supplied):
        host.root.plugin(provider(name))
    for index, needs in enumerate(wants):
        host.root.plugin(waiter(f"c{index}", sorted(needs)))
    await quiet(host)

    reports = {leaf(report.label): report for report in pending(host)}
    for index, needs in enumerate(wants):
        expected = tuple(sorted(needs - supplied))
        who = f"c{index}"
        if expected:
            assert who in reports, f"{who} is waiting on {expected} and unreported"
            assert reports[who].names == expected
            # Nothing else in the tree promised these names, so each is
            # simply unprovided rather than attributable to anyone.
            assert all(item.provider is None for item in reports[who].blocked)
        else:
            assert who not in reports

    snapshot = inspect(host)
    for node in walk(snapshot):
        assert (node.state is FiberState.PENDING) == bool(node.unmet)


@pytest.mark.tier_pr
@settings(max_examples=100, deadline=None)
@given(length=st.integers(min_value=2, max_value=5))
async def test_a_cascade_is_attributed_to_the_end_of_the_chain(length: int) -> None:
    """PROP-DIAG-002, SEM-006 half: forty pending fibers, one cause.

    Failure value: reporting every link of a chain as its own problem, so the
    operator reads forty lines and cannot tell which one to act on.
    """
    host = PluginHost()
    # p0 provides n0 and needs n1; p1 provides n1 and needs n2; ... and the
    # last link needs a name nobody in the tree ever mentions.
    names = [f"n{index}" for index in range(length)]
    for index, name in enumerate(names):
        needs = names[index + 1] if index + 1 < length else "absent"
        host.root.plugin(provider(name, needs=[needs]))
    host.root.plugin(waiter("app", [names[0]]))
    await quiet(host)

    reports = {leaf(report.label): report for report in pending(host)}
    assert set(reports) == {"app", *(f"Svc_{name}" for name in names)}

    blocked = reports["app"].blocked
    assert [item.name for item in blocked] == [names[0]]
    assert leaf_of(blocked[0].provider) == f"Svc_{names[0]}"
    assert leaf_of(blocked[0].root) == f"Svc_{names[-1]}", "the far end is the fix"


# --------------------------------------------------------------------------
# PROP-DIAG-003
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EffectPlan:
    """One registration: where it goes, and whether it is disposed again."""

    label: str
    nested: bool
    dropped: bool


@st.composite
def effect_plans(draw: st.DrawFn) -> tuple[EffectPlan, ...]:
    count = draw(st.integers(min_value=0, max_value=5))
    nested = draw(st.lists(st.booleans(), min_size=count, max_size=count))
    dropped = draw(st.lists(st.booleans(), min_size=count, max_size=count))
    return tuple(
        EffectPlan(label=f"e{index}", nested=nested[index], dropped=dropped[index])
        for index in range(count)
    )


def registrar(label: str, plans: Sequence[EffectPlan], out: list[EffectHandle]) -> Any:
    """A plugin that registers the generated effects, nesting where asked."""

    def apply(ctx: Context) -> None:
        scope = scope_of(ctx)
        for plan in plans:
            target = scope.child(f"{plan.label}#box") if plan.nested else scope
            handle = target.effect(lambda: lambda: None, label=plan.label)
            if plan.dropped:
                out.append(handle)

    return named(apply, label)


def expected_labels(root: str, plans: Sequence[EffectPlan]) -> list[str | None]:
    """Every node label the effect tree should carry, from the plan alone.

    ``root`` is the fiber's own scope label, which the mount composes; every
    other label here comes from the generated plan.
    """
    labels: list[str | None] = [root]
    for plan in plans:
        if plan.nested:
            labels.append(f"{plan.label}#box")  # the box outlives its content
        if not plan.dropped:
            labels.append(plan.label)
    return sorted(labels, key=str)


def expected_shape(root: str, plans: Sequence[EffectPlan]) -> object:
    """The nesting the plan asked for, in the form :func:`shape` reports.

    "Exactly once, under the fiber that registered it" is a statement about
    where a label sits, not only about how many there are: flattening a
    composite preserves the multiset and destroys the tree.
    """
    kids: list[object] = []
    for plan in plans:
        if plan.nested:
            # The box is a scope, not a registration: disposing its content
            # leaves it in place, empty.
            content = () if plan.dropped else ((plan.label, ()),)
            kids.append((f"{plan.label}#box", content))
        elif not plan.dropped:
            kids.append((plan.label, ()))
    return (root, tuple(kids))


def node_labels(node: EffectNode) -> list[str | None]:
    return sorted(_flatten(node, lambda item: item.label), key=str)


def locations(node: EffectNode) -> list[str]:
    return _flatten(node, lambda item: item.location)


def shape(node: EffectNode) -> object:
    return (node.label, tuple(shape(child) for child in node.children))


def _flatten(node: EffectNode, read: Callable[[EffectNode], Any]) -> list[Any]:
    found = [read(node)]
    for child in node.children:
        found.extend(_flatten(child, read))
    return found


async def run_effects(plans: Sequence[EffectPlan]) -> EffectNode:
    host = PluginHost()
    handles: list[EffectHandle] = []
    fiber = host.root.plugin(registrar("worker", plans, handles))
    # A second instance, mounted underneath, whose effects must not appear in
    # its parent's tree: the nursery is pruned exactly so that a label is
    # reported once rather than once per ancestor.
    fiber.plugin(registrar("nested-worker", plans, []))
    await quiet(host)
    for handle in handles:
        await undo(handle)
    return inspect(fiber).effects


@pytest.mark.tier_local
@settings(max_examples=100, deadline=None)
@given(plans=effect_plans())
async def test_every_live_effect_appears_once_under_its_own_fiber(
    plans: tuple[EffectPlan, ...],
) -> None:
    """PROP-DIAG-003: the label multiset is exactly what was registered and kept.

    Failure value: nested composite effects being flattened onto the fiber, so
    a leak inside one registration is indistinguishable from twenty
    independent registrations and the actual culprit is unfindable.

    SEM-005's half: the same plan with location capture off must produce the
    same tree, differing in the location field and nowhere else.
    """
    tree = await run_effects(plans)
    assert tree.label is not None
    assert leaf(tree.label) == "worker", "the fiber's own scope roots its tree"
    assert node_labels(tree) == expected_labels(tree.label, plans)
    assert shape(tree) == expected_shape(tree.label, plans)
    assert all(location != UNKNOWN for location in locations(tree))

    effect_module.CAPTURE_LOCATIONS = False
    try:
        blind = await run_effects(plans)
    finally:
        effect_module.CAPTURE_LOCATIONS = True

    assert shape(blind) == shape(tree)
    assert set(locations(blind)) == {UNKNOWN}


# --------------------------------------------------------------------------
# PROP-DIAG-004
# --------------------------------------------------------------------------


def containers(value: object) -> set[int]:
    """The identity of every container in a config, itself included.

    Sharing one of these is what "a copy" has to rule out: an operator's
    diagnostic tool holding the same list the plugin is reading from can edit
    the running application by accident.
    """
    found: set[int] = set()
    if isinstance(value, dict):
        found.add(id(value))
        for item in value.values():
            found |= containers(item)
    elif isinstance(value, list | tuple | set | frozenset):
        found.add(id(value))
        for item in value:
            found |= containers(item)
    return found


@pytest.mark.tier_local
@settings(max_examples=100, deadline=None)
@given(
    mounts=st.lists(st.integers(min_value=0, max_value=3), max_size=4),
    drop=st.booleans(),
)
async def test_a_snapshot_is_a_value_and_stays_one(
    mounts: list[int], drop: bool
) -> None:
    """PROP-DIAG-004: the runtime cannot reach into a snapshot, or vice versa.

    Failure value: returning the live children list, so a diagnostic tool
    iterating a snapshot during a reload raises "list changed size during
    iteration" -- a crash in the tool used to debug crashes.
    """
    host = PluginHost()
    config: dict[str, Any] = {"depth": 1, "tags": ["a", "b"], "inner": {"seen": ["x"]}}
    original = copy.deepcopy(config)
    fiber = host.root.plugin(named(lambda ctx: None, "subject"), config)
    await quiet(host)

    snapshot = inspect(host)
    mirror = copy.deepcopy(snapshot)

    # The other direction, on a snapshot of its own so that this test's own
    # edits can never be mistaken for the runtime's: nothing handed over is a
    # container the runtime is also holding, at any depth.
    held = _subject(inspect(host)).config
    assert isinstance(held, dict)
    assert containers(held).isdisjoint(containers(config)), "a shared container"
    held["depth"] = 99
    held["tags"] = ()

    for index in mounts:
        host.root.plugin(named(lambda ctx: None, f"later-{index}"))
        await quiet(host)
    if drop:
        await fiber.dispose()
        await quiet(host)

    assert snapshot == mirror, "the runtime changed a snapshot that was already taken"

    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.label = "rewritten"  # type: ignore[misc]

    assert config == original, "an edit to a snapshot reached the instance"


def _subject(snapshot: FiberSnapshot) -> FiberSnapshot:
    found = [node for node in walk(snapshot) if leaf(node.label) == "subject"]
    assert len(found) == 1, "the instance under test is not in its own snapshot"
    return found[0]


# --------------------------------------------------------------------------
# PROP-DIAG-005
# --------------------------------------------------------------------------


class AlphaError(RuntimeError):
    """One of the exception types a generated plugin raises."""


class BetaError(ValueError):
    """Another, of a different base class, so `except` clauses differ."""


def chain(labels: Sequence[str], failure: type[Exception]) -> Any:
    """A plugin that awaits the mount of the next one, innermost raising."""
    label, rest = labels[0], labels[1:]

    async def apply(ctx: Context) -> None:
        if not rest:
            raise failure(label)
        await ctx.plugin(chain(rest, failure))

    return named(apply, label)


@pytest.mark.tier_local
@settings(max_examples=100, deadline=None)
@given(
    depth=st.integers(min_value=1, max_value=4),
    failure=st.sampled_from((AlphaError, BetaError)),
)
async def test_a_failure_names_every_mount_that_led_to_it(
    depth: int, failure: type[Exception]
) -> None:
    """PROP-DIAG-005: notes innermost-first, original type, original traceback.

    Failure value: wrapping the exception in a framework error class, so user
    code catching its own exception type stops working the moment the plugin is
    mounted by the loader rather than directly.
    """
    host = PluginHost()
    labels = [f"level{index}" for index in range(depth)]
    top = host.root.plugin(chain(labels, failure))

    with pytest.raises(failure) as caught:
        await top

    assert type(caught.value) is failure, "the type a plugin author catches"
    assert caught.value.__traceback__ is not None, "the frames that led there"
    sites = tuple(leaf(site) for site in mount_sites(caught.value))
    assert sites == tuple(reversed(labels))

    snapshot = inspect(host)
    failed = [node for node in walk(snapshot) if node.state is FiberState.FAILED]
    assert [leaf(node.label) for node in failed] == [labels[0]]
    assert failed[0].error is not None
    assert failure.__name__ in failed[0].error


# --------------------------------------------------------------------------
# The seams the properties do not reach
# --------------------------------------------------------------------------


@pytest.mark.tier_local
async def test_an_identity_is_never_handed_out_twice() -> None:
    """A snapshot outlives its instance, which is why `uid` is a serial.

    An address does not: churn short-lived mounts and CPython hands the same
    one back within a few iterations. A tool holding yesterday's snapshot would
    then match it against a plugin that has never existed.
    """
    host = PluginHost()
    seen: list[int] = []
    for _ in range(150):
        fiber = host.root.plugin(named(lambda ctx: None, "churn"))
        seen.append(inspect(fiber).uid)
        await fiber.dispose()
        del fiber
        gc.collect()
    assert len(set(seen)) == len(seen), "an identity came back after its instance"


@pytest.mark.tier_local
async def test_a_host_and_its_root_snapshot_the_same_thing() -> None:
    host = PluginHost()
    host.root.plugin(named(lambda ctx: None, "one"))
    await quiet(host)
    assert inspect(host) == inspect(host.root)


@pytest.mark.tier_local
async def test_an_isolated_provider_is_not_the_answer_to_a_global_consumer() -> None:
    """The open question, resolved: attribution never crosses a realm."""
    host = PluginHost()
    host.root.plugin(waiter("app", ["s0"]))
    # The provider is pending too, and would bind `s0` -- but inside its own
    # realm, where the consumer will never look.
    host.root.plugin(provider("s0", needs=["absent"]), isolate=["s0"])
    await quiet(host)

    reports = {leaf(report.label): report for report in pending(host)}
    assert reports["app"].blocked[0].provider is None
    assert reports["app"].blocked[0].root is None


@pytest.mark.tier_local
async def test_mutually_pending_providers_terminate() -> None:
    """Two plugins each declaring the other's dependency: a cycle, walked once."""
    host = PluginHost()
    host.root.plugin(provider("a", needs=["b"]))
    host.root.plugin(provider("b", needs=["a"]))
    await quiet(host)

    reports = {leaf(report.label): report for report in pending(host)}
    assert set(reports) == {"Svc_a", "Svc_b"}
    assert leaf_of(reports["Svc_a"].blocked[0].provider) == "Svc_b"
    assert leaf_of(reports["Svc_a"].blocked[0].root) == "Svc_b"


@pytest.mark.tier_local
async def test_the_text_rendering_says_what_an_operator_needs() -> None:
    host = PluginHost()
    app = host.root.plugin(waiter("app", ["s0"]))
    await quiet(host)

    text = render_tree(inspect(host))
    assert f"{app.label} [PENDING]" in text
    assert "waiting on s0" in text
    assert "location" not in text  # effects are off by default

    detailed = render_tree(inspect(host), effects=True)
    assert detailed.count("\n") > text.count("\n")


@pytest.mark.tier_local
async def test_the_json_rendering_is_json() -> None:
    host = PluginHost()
    host.root.plugin(named(lambda ctx: None, "one"), {"a": 1})
    await quiet(host)

    body = json.loads(render_tree(inspect(host), style="json", effects=True))
    child = body["children"][0]
    assert leaf(child["label"]) == "one"
    assert child["state"] == "ACTIVE"
    assert child["config"] == {"a": 1}
    assert "effects" in child


@pytest.mark.tier_local
def test_the_model_agrees_with_itself() -> None:
    """The surviving-tree model, against a hand-worked case."""
    spec = _spec("root", _spec("root.0", _spec("root.0.0")), _spec("root.1"))
    assert surviving(spec, frozenset()) == {
        "root": ("root.0", "root.1"),
        "root.0": ("root.0.0",),
        "root.0.0": (),
        "root.1": (),
    }
    assert surviving(spec, frozenset({"root.0"})) == {"root": ("root.1",), "root.1": ()}


def _spec(label: str, *children: TreeSpec) -> TreeSpec:
    return TreeSpec(label=label, meta={}, children=children)
