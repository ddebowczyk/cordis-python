"""PROP-LOADER-001..009, from spec/capabilities/14-declarative-loader.yaml.

The oracle for every structural claim is the diagnostics snapshot of the
loader's own subtree, not the loader's bookkeeping: a diff that convinces
itself it converged is exactly the failure these cards are about. Entry ids
reach the snapshot through the *generated* config payload, so what the tree
reports about which rows are live comes from the test's own data.

Labels are composed by the mount machinery -- `root/loader#0/alpha#3` -- so
every comparison here is on the derived name, never the composed label.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cordis.config import config_schema, from_dataclass
from cordis.diagnostics import inspect
from cordis.errors import ConfigValidationError
from cordis.fiber import FiberState
from cordis.intercept import interceptions
from cordis.loader import (
    GROUP,
    Entry,
    ImportTargets,
    JsonSource,
    LoaderService,
    MappingSource,
    ReconcileReport,
    TargetSource,
    as_mapping,
    read_entries,
)
from cordis.plugin import PluginHost, config_of, fiber_of
from cordis.realm import isolated_names

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cordis.context import Context
    from cordis.diagnostics import FiberSnapshot
    from cordis.fiber import Fiber
    from cordis.plugin import PluginTarget


def leaf(label: str) -> str:
    """The name a mount derived, out of the path label it composed."""
    return label.rsplit("/", 1)[-1].split("#", 1)[0]


# --------------------------------------------------------------------------
# The synthetic targets
# --------------------------------------------------------------------------


def alpha(ctx: Context) -> None:
    """A leaf plugin. Its config is what identifies it in a snapshot."""


def beta(ctx: Context) -> None:
    """A second leaf, so a target change is a different name."""


def gamma(ctx: Context) -> None:
    """A third."""


def nester(ctx: Context) -> None:
    """A plugin with a child of its own, so "and its descendants" has one."""
    config = config_of(ctx)
    assert isinstance(config, dict)
    ctx.plugin(alpha, {"id": f"{config['id']}/inner", "v": 0})


def boom(ctx: Context) -> None:
    """A body that fails after it is mounted."""
    msg = "this plugin does not work"
    raise RuntimeError(msg)


@dataclass
class Picky:
    id: str
    v: int = 0


@config_schema(from_dataclass(Picky))
def picky(ctx: Context) -> None:
    """A plugin whose config the framework validates before the body runs."""


#: Targets the generated entry lists draw from. Leaves only: the structural
#: cards compare whole subtrees, and a target that mounts children of its own
#: would put rows in the snapshot that no entry asked for.
POOL: dict[str, PluginTarget] = {"alpha": alpha, "beta": beta, "gamma": gamma}

#: Everything the fake resolver knows about, including the targets that only
#: some cards use.
TARGETS: dict[str, PluginTarget] = {
    **POOL,
    "nester": nester,
    "boom": boom,
    "picky": picky,
}


class Fakes(TargetSource):
    """A Provider for the target seam, whose pool arrives as its config."""

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        pool = config_of(ctx)
        assert isinstance(pool, dict)
        # Held, not copied: the remount card edits this dict between
        # reconciles, which is what a reimport looks like from in here.
        self._pool: dict[str, PluginTarget] = pool

    def resolve(self, name: str, /) -> PluginTarget:
        found = self._pool.get(name)
        if found is None:
            msg = f"no target named {name!r}"
            raise LookupError(msg)
        return found


async def start(pool: dict[str, PluginTarget] | None = None) -> LoaderService:
    """A host with a bound target source and a mounted loader."""
    host = PluginHost()
    host.root.plugin(Fakes, dict(TARGETS) if pool is None else pool)
    await host.runtime.quiesce()
    host.root.plugin(LoaderService)
    await host.runtime.quiesce()
    loader = host.root.context.require(LoaderService)
    assert isinstance(loader, LoaderService)
    return loader


def subtree(loader: LoaderService) -> Fiber:
    """The loader's own fiber, whose children are the entries."""
    found = fiber_of(loader.ctx)
    assert found is not None
    return found


# --------------------------------------------------------------------------
# The model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """One generated entry: a plugin, or a group when ``kids`` is not None."""

    id: str
    target: str
    value: int
    disabled: bool
    kids: tuple[Row, ...] | None = None

    @property
    def group(self) -> bool:
        return self.kids is not None


def entry_of(row: Row) -> Entry:
    if row.kids is not None:
        return Entry(
            id=row.id,
            name=GROUP,
            config=[as_mapping(entry_of(kid)) for kid in row.kids],
            disabled=row.disabled,
            group=tuple(entry_of(kid) for kid in row.kids),
        )
    return Entry(
        id=row.id,
        name=row.target,
        config={"id": row.id, "v": row.value},
        disabled=row.disabled,
    )


def entries_of(rows: Sequence[Row]) -> tuple[Entry, ...]:
    return tuple(entry_of(row) for row in rows)


def expected(rows: Sequence[Row], prefix: str = "") -> dict[str, tuple[str, object]]:
    """What the live tree should look like: dotted path -> (target, payload).

    Computed from the generated rows alone. A disabled row contributes
    nothing -- neither itself nor, when it is a group, anything beneath it.
    """
    found: dict[str, tuple[str, object]] = {}
    for row in rows:
        if row.disabled:
            continue
        path = f"{prefix}{row.id}"
        if row.kids is None:
            found[path] = (row.target, row.value)
        else:
            found[path] = (GROUP, None)
            found |= expected(row.kids, f"{path}.")
    return found


def observed(node: FiberSnapshot, prefix: str = "") -> dict[str, tuple[str, object]]:
    """The same picture, read off a diagnostics snapshot.

    Groups are recognised by the name their mount derived, leaves by the id
    the generator put in their config -- so nothing here is the loader's own
    record of what it did.
    """
    found: dict[str, tuple[str, object]] = {}
    for child in node.children:
        name = leaf(child.label)
        if name.startswith(f"{GROUP}:"):
            path = f"{prefix}{name.removeprefix(f'{GROUP}:')}"
            found[path] = (GROUP, None)
            found |= observed(child, f"{path}.")
        else:
            config = child.config
            assert isinstance(config, dict)
            found[f"{prefix}{config['id']}"] = (name, config["v"])
    return found


def live(loader: LoaderService) -> dict[str, tuple[str, object]]:
    return observed(inspect(subtree(loader)))


def fibers(loader: LoaderService) -> dict[str, int]:
    """Every live entry's fiber identity, by dotted path."""
    return {path: _uid(loader, path) for path in loader.live()}


def _uid(loader: LoaderService, path: str) -> int:
    handle = loader.handle_for(path)
    assert handle is not None
    return handle.uid


# --------------------------------------------------------------------------
# Generators
# --------------------------------------------------------------------------

NAMES = ("one", "two", "three", "four")


@st.composite
def rows(draw: st.DrawFn, *, depth: int = 2, prefix: str = "") -> tuple[Row, ...]:
    chosen = draw(st.lists(st.sampled_from(NAMES), max_size=4, unique=True))
    made: list[Row] = []
    for name in chosen:
        kids: tuple[Row, ...] | None = None
        if depth > 0 and draw(st.booleans()):
            kids = draw(rows(depth=depth - 1, prefix=f"{prefix}{name}."))
        made.append(
            Row(
                id=name,
                target=draw(st.sampled_from(sorted(POOL))),
                value=draw(st.integers(min_value=0, max_value=9)),
                disabled=draw(st.booleans()),
                kids=kids,
            )
        )
    return tuple(made)


def flatten(found: Sequence[Row], prefix: str = "") -> list[tuple[str, Row]]:
    """Every row with its dotted path, groups included, depth first."""
    out: list[tuple[str, Row]] = []
    for row in found:
        path = f"{prefix}{row.id}"
        out.append((path, row))
        if row.kids is not None:
            out.extend(flatten(row.kids, f"{path}."))
    return out


def edit(found: tuple[Row, ...], path: str, change: Any) -> tuple[Row, ...]:
    """The same rows with one row, named by dotted path, replaced."""
    head, _, rest = path.partition(".")
    out: list[Row] = []
    for row in found:
        if row.id != head:
            out.append(row)
        elif rest:
            assert row.kids is not None
            out.append(replace(row, kids=edit(row.kids, rest, change)))
        else:
            out.append(replace(row, **change))
    return tuple(out)


# --------------------------------------------------------------------------
# PROP-LOADER-001
# --------------------------------------------------------------------------


@st.composite
def histories(draw: st.DrawFn) -> tuple[tuple[Row, ...], ...]:
    return tuple(draw(st.lists(rows(), min_size=1, max_size=4)))


@pytest.mark.tier_pr
@settings(max_examples=100, deadline=None)
@given(history=histories())
async def test_the_tree_converges_to_the_last_list_reconciled(
    history: tuple[tuple[Row, ...], ...],
) -> None:
    """PROP-LOADER-001: the tree equals the enabled subset of the last list.

    Failure value: a diff that matches entries positionally rather than by id,
    so inserting one row at the top remounts every row below it -- and every
    session those rows were holding goes with them.
    """
    loader = await start()
    for wanted in history:
        await loader.reconcile(entries_of(wanted))
        assert live(loader) == expected(wanted)


# --------------------------------------------------------------------------
# PROP-LOADER-002
# --------------------------------------------------------------------------


@st.composite
def permutations(draw: st.DrawFn) -> tuple[tuple[Row, ...], tuple[Row, ...]]:
    found = draw(rows())
    return found, tuple(draw(st.permutations(found)))


@pytest.mark.tier_local
@settings(max_examples=200, deadline=None)
@given(case=permutations())
async def test_reordering_a_file_changes_nothing(
    case: tuple[tuple[Row, ...], tuple[Row, ...]],
) -> None:
    """PROP-LOADER-002: order carries no meaning, so a permutation is a no-op.

    Failure value: reordering a config file for readability restarting the
    application, including dropping every session in progress.
    """
    found, shuffled = case
    loader = await start()
    await loader.reconcile(entries_of(found))
    before = fibers(loader)

    report = await loader.reconcile(entries_of(shuffled))

    assert report.quiet
    assert (report.mounted, report.updated, report.disposed) == ((), (), ())
    assert fibers(loader) == before


# --------------------------------------------------------------------------
# PROP-LOADER-003
# --------------------------------------------------------------------------


@st.composite
def edits(draw: st.DrawFn) -> tuple[tuple[Row, ...], str, int]:
    found = draw(rows())
    leaves = [
        path
        for path, row in flatten(found)
        if not row.group and not _hidden(found, path)
    ]
    if not leaves:
        found = (Row(id="one", target="alpha", value=0, disabled=False),)
        leaves = ["one"]
    return found, draw(st.sampled_from(leaves)), draw(st.integers(10, 19))


def _hidden(found: Sequence[Row], path: str) -> bool:
    """Whether ``path`` is disabled, or inside something disabled."""
    return any(
        row.disabled
        for step, row in flatten(found)
        if path == step or path.startswith(f"{step}.")
    )


@pytest.mark.tier_local
@settings(max_examples=200, deadline=None)
@given(case=edits())
async def test_editing_one_config_touches_one_entry(
    case: tuple[tuple[Row, ...], str, int],
) -> None:
    """PROP-LOADER-003: an edit reaches its own entry and nothing else.

    Failure value: comparing entries by serialising the whole list, so any
    edit invalidates every entry and each config save restarts the process
    in place.
    """
    found, path, value = case
    loader = await start()
    await loader.reconcile(entries_of(found))
    before = fibers(loader)

    report = await loader.reconcile(entries_of(edit(found, path, {"value": value})))

    assert report.updated == (path,)
    assert (report.mounted, report.disposed) == ((), ())
    assert live(loader)[path] == (dict(flatten(found))[path].target, value)
    unrelated = {step: uid for step, uid in before.items() if step != path}
    assert {step: uid for step, uid in fibers(loader).items() if step != path} == (
        unrelated
    )


@pytest.mark.tier_local
async def test_an_edited_entry_restarts_what_it_mounted() -> None:
    """ "And its descendants": what the entry itself mounted goes with it."""
    loader = await start()
    rows_ = (
        Entry(id="outer", name="nester", config={"id": "outer", "v": 0}),
        Entry(id="other", name="alpha", config={"id": "other", "v": 0}),
    )
    await loader.reconcile(rows_)
    handle = loader.handle_for("outer")
    assert handle is not None
    inner = handle.children[0].uid
    other = _uid(loader, "other")

    await loader.reconcile(
        (
            Entry(id="outer", name="nester", config={"id": "outer", "v": 1}),
            rows_[1],
        )
    )

    restarted = loader.handle_for("outer")
    assert restarted is not None
    assert restarted.children[0].uid != inner
    assert _uid(loader, "other") == other


# --------------------------------------------------------------------------
# PROP-LOADER-004
# --------------------------------------------------------------------------

FAULTS = ("no target", "bad config", "raising body")


@st.composite
def faulty(draw: st.DrawFn) -> tuple[tuple[Row, ...], dict[str, str]]:
    found = draw(st.lists(st.sampled_from(NAMES), min_size=1, max_size=4, unique=True))
    rows_ = tuple(
        Row(id=name, target="alpha", value=index, disabled=False)
        for index, name in enumerate(found)
    )
    broken = draw(
        st.lists(st.booleans(), min_size=len(rows_), max_size=len(rows_)).filter(any)
    )
    return rows_, {
        row.id: draw(st.sampled_from(FAULTS))
        for row, bad in zip(rows_, broken, strict=True)
        if bad
    }


def spoil(row: Row, fault: str) -> Entry:
    entry = entry_of(row)
    if fault == "no target":
        return replace(entry, name="not.a.target")
    if fault == "raising body":
        return replace(entry, name="boom")
    return replace(entry, name="picky", config={"id": row.id, "v": "not an int"})


@pytest.mark.tier_local
@settings(max_examples=200, deadline=None)
@given(case=faulty())
async def test_a_bad_entry_fails_alone(
    case: tuple[tuple[Row, ...], dict[str, str]],
) -> None:
    """PROP-LOADER-004: one bad row does not cost the operator the others.

    Failure value: a single unresolvable module name raising out of the mount
    loop and cancelling every concurrent mount, so one typo yields an
    application that loads nothing and reports one unrelated error.
    """
    rows_, faults = case
    loader = await start()
    entries = tuple(
        spoil(row, faults[row.id]) if row.id in faults else entry_of(row)
        for row in rows_
    )

    report = await loader.reconcile(entries)

    # Every fault is reported against its own entry. A body that raises is
    # still mounted -- what failed is the instance, not the row -- while an
    # unresolvable target or an invalid config never reaches a mount at all.
    rejected = {name for name, fault in faults.items() if fault != "raising body"}
    assert {failure.id for failure in report.failed} == set(faults)
    assert set(report.mounted) == {row.id for row in rows_} - rejected

    states = {
        node.config["id"]: node.state
        for node in inspect(subtree(loader)).children
        if isinstance(node.config, dict) and "id" in node.config
    }
    for row in rows_:
        if row.id in rejected:
            assert row.id not in states
        elif faults.get(row.id) == "raising body":
            assert states[row.id] is FiberState.FAILED
        else:
            assert states[row.id] is FiberState.ACTIVE


# --------------------------------------------------------------------------
# PROP-LOADER-005
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@settings(max_examples=200, deadline=None)
@given(found=rows())
async def test_reconciling_twice_does_nothing_the_second_time(
    found: tuple[Row, ...],
) -> None:
    """PROP-LOADER-005: the same list twice is the same tree, untouched.

    Failure value: config values that do not survive a round trip through the
    entry representation comparing unequal, so a file-watching loader remounts
    everything on every poll.
    """
    loader = await start()
    await loader.reconcile(entries_of(found))
    before = fibers(loader)

    report = await loader.reconcile(entries_of(found))

    assert report.quiet
    assert report.unchanged == tuple(before)
    assert fibers(loader) == before


# --------------------------------------------------------------------------
# PROP-LOADER-006
# --------------------------------------------------------------------------


@st.composite
def idless(draw: st.DrawFn) -> tuple[list[object], list[tuple[object, ...]]]:
    """A raw entry list with ids removed at generated positions."""
    count = draw(st.integers(min_value=1, max_value=4))
    nested = draw(st.integers(min_value=0, max_value=count - 1))
    raw: list[object] = []
    missing: list[tuple[object, ...]] = []
    for index in range(count):
        if index == nested:
            inner: list[object] = [{"id": "kid", "name": "alpha"}, {"name": "beta"}]
            missing.append((index, "config", 1, "id"))
            raw.append({"id": f"g{index}", "name": GROUP, "config": inner})
        elif draw(st.booleans()):
            raw.append({"name": "alpha"})
            missing.append((index, "id"))
        else:
            raw.append({"id": f"e{index}", "name": "alpha"})
    return raw, missing


@pytest.mark.tier_local
@settings(max_examples=200, deadline=None)
@given(case=idless())
async def test_a_list_with_an_unnamed_entry_is_rejected_whole(
    case: tuple[list[object], list[tuple[object, ...]]],
) -> None:
    """PROP-LOADER-006: no id, no mount -- and the position is named.

    Failure value: validating entries as they are mounted rather than up
    front, so a file with a mistake halfway down leaves the application in a
    half-reconciled state that matches no version of the file.
    """
    raw, missing = case
    loader = await start()
    await loader.reconcile(entries_of((Row("one", "alpha", 0, disabled=False),)))
    before = fibers(loader)

    with pytest.raises(ConfigValidationError) as caught:
        read_entries(raw)

    reported = {tuple(issue.path) for issue in caught.value.issues}
    assert reported == set(missing)
    assert fibers(loader) == before


# --------------------------------------------------------------------------
# PROP-LOADER-007
# --------------------------------------------------------------------------


@st.composite
def toggles(draw: st.DrawFn) -> tuple[tuple[Row, ...], list[str]]:
    found = draw(rows())
    paths = [path for path, _ in flatten(found)]
    if not paths:
        found = (Row(id="one", target="alpha", value=0, disabled=False),)
        paths = ["one"]
    return found, draw(st.lists(st.sampled_from(paths), min_size=1, max_size=4))


@pytest.mark.tier_local
@settings(max_examples=200, deadline=None)
@given(case=toggles())
async def test_disabling_and_enabling_restores_the_same_entry(
    case: tuple[tuple[Row, ...], list[str]],
) -> None:
    """PROP-LOADER-007: `disabled` hides an entry; it does not delete it.

    Failure value: treating `disabled: true` as removal in the diff, so
    re-enabling mounts a fresh entry that patches targeting the original id no
    longer reach.
    """
    found, paths = case
    loader = await start()
    await loader.reconcile(entries_of(found))
    before = live(loader)

    state = found
    for path in paths:
        was = dict(flatten(state))[path].disabled
        state = edit(state, path, {"disabled": not was})
        await loader.reconcile(entries_of(state))
        assert live(loader) == expected(state)

    # And back: every toggle undone, in reverse, is the tree we started with.
    for path in reversed(paths):
        was = dict(flatten(state))[path].disabled
        state = edit(state, path, {"disabled": not was})
        await loader.reconcile(entries_of(state))

    assert live(loader) == before


# --------------------------------------------------------------------------
# PROP-LOADER-008
# --------------------------------------------------------------------------


@st.composite
def groups(draw: st.DrawFn) -> tuple[tuple[Row, ...], dict[str, str]]:
    found = draw(rows(depth=3))
    paths = [path for path, row in flatten(found) if row.group]
    isolated = {
        path: draw(st.sampled_from(["alpha", "beta"]))
        for path in paths
        if draw(st.booleans())
    }
    return found, isolated


@pytest.mark.tier_pr
@settings(max_examples=50, deadline=None)
@given(case=groups())
async def test_a_group_owns_its_children_and_its_settings(
    case: tuple[tuple[Row, ...], dict[str, str]],
) -> None:
    """PROP-LOADER-008: children mount under the group, and go with it.

    Failure value: group children mounted on the loader's context rather than
    the group's, so a group-level isolation setting silently applies to
    nothing and two groups meant to have separate shells share one.
    """
    found, isolated = case
    loader = await start()
    entries = _with_isolation(entries_of(found), isolated)
    await loader.reconcile(entries)

    assert live(loader) == expected(found)

    for path, name in isolated.items():
        if path not in loader.live():
            continue  # the group is disabled, or inside one that is
        handle = loader.handle_for(path)
        assert handle is not None
        assert name in isolated_names(handle.context)
        assert name not in isolated_names(loader.ctx)

    # Disposing a group takes exactly its subtree.
    for path, row in flatten(found):
        if not row.group or path not in loader.live():
            continue
        under = {step for step in loader.live() if step.startswith(f"{path}.")}
        remaining = set(loader.live()) - under - {path}
        await loader.reconcile(_without(entries, path))
        assert set(loader.live()) == remaining
        break


def _with_isolation(
    entries: tuple[Entry, ...], isolated: dict[str, str], prefix: str = ""
) -> tuple[Entry, ...]:
    out: list[Entry] = []
    for entry in entries:
        path = f"{prefix}{entry.id}"
        if entry.group is not None:
            entry = replace(
                entry, group=_with_isolation(entry.group, isolated, f"{path}.")
            )
        if path in isolated:
            entry = replace(entry, isolate=(isolated[path],))
        out.append(entry)
    return tuple(out)


def _without(entries: tuple[Entry, ...], path: str) -> tuple[Entry, ...]:
    head, _, rest = path.partition(".")
    out: list[Entry] = []
    for entry in entries:
        if entry.id != head:
            out.append(entry)
        elif rest and entry.group is not None:
            out.append(replace(entry, group=_without(entry.group, rest)))
    return tuple(out)


@pytest.mark.tier_local
async def test_a_group_interception_reaches_its_children() -> None:
    """The other axis: a group configures what its subtree shares."""
    loader = await start()
    await loader.reconcile(
        (
            Entry(
                id="shell",
                name=GROUP,
                intercept={"alpha": {"timeout": 5}},
                group=(Entry(id="kid", name="alpha", config={"id": "kid", "v": 0}),),
            ),
            Entry(id="outside", name="alpha", config={"id": "outside", "v": 0}),
        )
    )

    inside = loader.handle_for("shell.kid")
    outside = loader.handle_for("outside")
    assert inside is not None
    assert outside is not None
    assert interceptions(inside.context, "alpha") == ({"timeout": 5},)
    assert interceptions(outside.context, "alpha") == ()


def _shell(value: int) -> Entry:
    """A group of two rows, one of which carries ``value``."""
    kids = (
        Entry(id="one", name="alpha", config={"id": "one", "v": value}),
        Entry(id="two", name="beta", config={"id": "two", "v": 0}),
    )
    return Entry(
        id="g", name=GROUP, config=[as_mapping(kid) for kid in kids], group=kids
    )


@pytest.mark.tier_local
async def test_editing_a_row_inside_a_group_leaves_the_group_alone() -> None:
    """A group's config *is* its children, so it never changes on their account.

    Comparing a group by its config would make every edit anywhere in a group
    restart the whole group -- the forty-row remount the capability exists to
    avoid, moved one level down.
    """
    loader = await start()
    await loader.reconcile((_shell(0),))
    before = fibers(loader)

    report = await loader.reconcile((_shell(1),))

    assert report.updated == ("g.one",)
    assert (report.mounted, report.disposed) == ((), ())
    assert report.unchanged == ("g", "g.two")
    assert fibers(loader) == before


@pytest.mark.tier_local
async def test_a_group_that_must_be_rebuilt_names_the_children_it_takes() -> None:
    """A group's own settings changed, so the subtree goes -- and is named.

    Isolation is decided when the instance is built, so it cannot be adopted
    in place. The report carries the dotted path of every entry that went away
    at any depth, which is how an operator learns that editing one line of a
    group restarted its children -- structurally, whether or not a logger is
    bound.
    """
    loader = await start()
    plain = Entry(
        id="shell",
        name=GROUP,
        group=(Entry(id="kid", name="alpha", config={"id": "kid", "v": 0}),),
    )
    await loader.reconcile((plain,))
    before = _uid(loader, "shell.kid")

    report = await loader.reconcile((replace(plain, isolate=("alpha",)),))

    assert set(report.disposed) == {"shell", "shell.kid"}
    assert set(report.mounted) == {"shell", "shell.kid"}
    assert _uid(loader, "shell.kid") != before
    assert live(loader) == {"shell": (GROUP, None), "shell.kid": ("alpha", 0)}


# --------------------------------------------------------------------------
# remount: the hot-reload amendment
# --------------------------------------------------------------------------


async def test_remounting_rebuilds_only_the_named_entries() -> None:
    """The entry list did not change; the code behind one row did.

    No edit to the file can express that, so `remount` takes paths instead of
    entries and rebuilds those, leaving every other row's instance -- and its
    fiber identity -- alone.
    """
    loader = await start()
    entries = (
        Entry(id="one", name="alpha", config={"id": "one", "v": 0}),
        Entry(id="two", name="beta", config={"id": "two", "v": 1}),
    )
    await loader.reconcile(entries)
    before = fibers(loader)

    report = await loader.remount(("one",))

    assert report.disposed == ("one",)
    assert report.mounted == ("one",)
    assert report.unchanged == ("two",)
    assert not report.failed
    after = fibers(loader)
    assert after["one"] != before["one"]
    assert after["two"] == before["two"]
    assert live(loader) == {"one": ("alpha", 0), "two": ("beta", 1)}


async def test_a_remount_reimports_between_the_old_and_the_new() -> None:
    """`between` runs in the one window a reimport is safe in.

    After every named instance is gone -- so nothing holds the old module --
    and before any is rebuilt, with targets resolved again afterwards, so what
    gets mounted is the code `between` just loaded rather than the code the
    plan was made against (hot-reload SEM-002).
    """
    pool: dict[str, PluginTarget] = dict(TARGETS)
    loader = await start(pool)
    await loader.reconcile(
        (
            Entry(id="one", name="alpha", config={"id": "one", "v": 0}),
            Entry(id="two", name="beta", config={"id": "two", "v": 1}),
        )
    )
    seen: dict[str, tuple[str, object]] = {}

    def reimport() -> None:
        seen.update(live(loader))
        pool["alpha"] = gamma  # what `importlib.reload` leaves behind

    report = await loader.remount(("one",), between=reimport)

    assert seen == {"two": ("beta", 1)}, "the old instance outlived the reimport"
    assert report.mounted == ("one",)
    assert live(loader) == {"one": ("gamma", 0), "two": ("beta", 1)}


async def test_a_remount_that_cannot_resolve_fails_that_entry_alone() -> None:
    """`between` broke the target: the row fails, the rest of the tree stands.

    A reimport that leaves a module unimportable must not take the entries it
    had nothing to do with, and the entry it did take is named.
    """
    pool: dict[str, PluginTarget] = dict(TARGETS)
    loader = await start(pool)
    await loader.reconcile(
        (
            Entry(id="one", name="alpha", config={"id": "one", "v": 0}),
            Entry(id="two", name="beta", config={"id": "two", "v": 1}),
        )
    )

    report = await loader.remount(("one",), between=lambda: pool.pop("alpha"))

    assert report.disposed == ("one",)
    assert report.mounted == ()
    assert [(f.id, f.reason) for f in report.failed] == [("one", "unresolvable target")]
    assert live(loader) == {"two": ("beta", 1)}


async def test_remounting_a_group_rebuilds_its_children() -> None:
    """A group's children are entries too, and a reimport reaches them.

    Naming the group is naming the subtree: the child is disposed with it and
    mounted from whatever `between` left in place.
    """
    pool: dict[str, PluginTarget] = dict(TARGETS)
    loader = await start(pool)
    await loader.reconcile(
        (
            Entry(
                id="shell",
                name=GROUP,
                group=(Entry(id="kid", name="alpha", config={"id": "kid", "v": 0}),),
            ),
        )
    )
    before = fibers(loader)

    report = await loader.remount(("shell",), between=lambda: pool.update(alpha=gamma))

    assert set(report.disposed) == {"shell", "shell.kid"}
    assert set(report.mounted) == {"shell", "shell.kid"}
    assert fibers(loader)["shell.kid"] != before["shell.kid"]
    assert live(loader) == {"shell": (GROUP, None), "shell.kid": ("gamma", 0)}


async def test_remounting_nothing_is_quiet() -> None:
    """A path the loader never mounted is not an error, and not a change."""
    loader = await start()
    await loader.reconcile((Entry(id="one", name="alpha", config={"id": "one"}),))
    before = fibers(loader)

    report = await loader.remount(("nowhere",))

    assert report.quiet
    assert report.unchanged == ("one",)
    assert fibers(loader) == before


# --------------------------------------------------------------------------
# PROP-LOADER-009
# --------------------------------------------------------------------------

VOCABULARY = {
    "config": st.just({"k": 1}),
    # `disabled: false` is the default, and a default is what `as_mapping`
    # omits -- so only the meaningful value is written out.
    "disabled": st.just(True),
    "inject": st.just(["db"]),
    "isolate": st.just(["shell"]),
    "intercept": st.just({"shell": {"timeout": 5}}),
}

CORRUPTIONS = ("unknown key", "no id", "no name", "id is not a string", "not a mapping")


@st.composite
def envelopes(draw: st.DrawFn) -> tuple[list[object], str | None, tuple[object, ...]]:
    count = draw(st.integers(min_value=1, max_value=3))
    raw: list[object] = []
    for index in range(count):
        row: dict[str, object] = {"id": f"e{index}", "name": "alpha"}
        row |= {
            field: draw(strategy)
            for field, strategy in VOCABULARY.items()
            if draw(st.booleans())
        }
        raw.append(row)
    if not draw(st.booleans()):
        return raw, None, ()
    fault = draw(st.sampled_from(CORRUPTIONS))
    at = draw(st.integers(min_value=0, max_value=count - 1))
    return _corrupt(raw, fault, at)


def _corrupt(
    raw: list[object], fault: str, at: int
) -> tuple[list[object], str, tuple[object, ...]]:
    original = raw[at]
    assert isinstance(original, dict)
    row: dict[str, object] = dict(original)
    if fault == "unknown key":
        row["enabled"] = True
        raw[at] = row
        return raw, fault, (at, "enabled")
    if fault == "no id":
        del row["id"]
        raw[at] = row
        return raw, fault, (at, "id")
    if fault == "no name":
        del row["name"]
        raw[at] = row
        return raw, fault, (at, "name")
    if fault == "id is not a string":
        row["id"] = 7
        raw[at] = row
        return raw, fault, (at, "id")
    raw[at] = ["not", "a", "mapping"]
    return raw, fault, (at,)


@pytest.mark.tier_local
@settings(max_examples=200, deadline=None)
@given(case=envelopes())
async def test_the_entry_vocabulary_is_closed_and_round_trips(
    case: tuple[list[object], str | None, tuple[object, ...]],
) -> None:
    """PROP-LOADER-009: exactly the vocabulary, and nothing lost on the way.

    Failure value: a parser that ignores keys it does not recognise, so
    `disabled: ture` leaves the entry running and the config file and the
    running system disagree with nothing to say so.
    """
    raw, fault, where = case
    if fault is None:
        parsed = read_entries(raw)
        assert [as_mapping(entry) for entry in parsed] == raw
        assert read_entries([as_mapping(entry) for entry in parsed]) == parsed
        return

    with pytest.raises(ConfigValidationError) as caught:
        read_entries(raw)
    assert {tuple(issue.path) for issue in caught.value.issues} == {where}


@pytest.mark.tier_local
async def test_two_entries_may_not_share_an_id() -> None:
    """Ids are how the diff finds a row; two rows with one id has no answer."""
    with pytest.raises(ConfigValidationError) as caught:
        read_entries([{"id": "same", "name": "alpha"}, {"id": "same", "name": "beta"}])

    assert [tuple(issue.path) for issue in caught.value.issues] == [(1, "id")]
    # Two ids that are equal in different lists are two different entries.
    parsed = read_entries(
        [
            {"id": "same", "name": "alpha"},
            {"id": "g", "name": GROUP, "config": [{"id": "same", "name": "beta"}]},
        ]
    )
    assert parsed[1].group is not None


# --------------------------------------------------------------------------
# Seams: sources, resolution and dry runs
# --------------------------------------------------------------------------


@pytest.mark.tier_local
async def test_a_dry_run_reports_what_a_real_one_would_do() -> None:
    """The same code path, with the apply step skipped."""
    loader = await start()
    entries = entries_of((Row("one", "alpha", 0, disabled=False),))

    planned = await loader.reconcile(entries, dry_run=True)

    assert planned.mounted == ("one",)
    assert loader.live() == ()

    done = await loader.reconcile(entries)
    assert (done.mounted, done.updated, done.disposed) == (planned.mounted, (), ())
    assert loader.live() == ("one",)


@pytest.mark.tier_local
async def test_a_dry_run_finds_the_bad_row_before_anything_is_mounted() -> None:
    loader = await start()
    report = await loader.reconcile(
        (Entry(id="one", name="nowhere"), Entry(id="two", name="alpha")),
        dry_run=True,
    )

    assert [failure.id for failure in report.failed] == ["one"]
    assert report.mounted == ("two",)
    assert loader.live() == ()


@pytest.mark.tier_local
async def test_targets_resolve_by_import_path() -> None:
    """`pkg.module:attr`, and a bare module with a module-level `apply`."""
    host = PluginHost()
    targets = ImportTargets(host.root.context)

    assert targets.resolve("tests.test_loader:alpha") is alpha

    module = targets.resolve("tests.support.applied")
    assert getattr(module, "apply", None) is not None


@pytest.mark.tier_local
async def test_an_unresolvable_target_says_what_it_tried() -> None:
    host = PluginHost()
    targets = ImportTargets(host.root.context)

    with pytest.raises(LookupError) as caught:
        targets.resolve("cordis.loader:no_such_attribute")
    assert "no_such_attribute" in str(caught.value)

    with pytest.raises(LookupError):
        targets.resolve("no.such.module.anywhere")


@pytest.mark.tier_local
async def test_a_json_file_is_read_as_entries(tmp_path: Any) -> None:
    path = tmp_path / "app.json"
    path.write_text(json.dumps([{"id": "one", "name": "alpha", "config": {"v": 1}}]))

    assert JsonSource(path).read() == (Entry(id="one", name="alpha", config={"v": 1}),)


@pytest.mark.tier_local
async def test_a_mapping_source_is_the_same_reader() -> None:
    source: MappingSource = MappingSource([{"id": "one", "name": "alpha"}])
    assert source.read() == (Entry(id="one", name="alpha"),)


@pytest.mark.tier_local
async def test_a_report_of_nothing_is_quiet() -> None:
    assert ReconcileReport().quiet
    assert not ReconcileReport(mounted=("one",)).quiet


@pytest.mark.tier_local
async def test_the_loader_falls_back_to_importing_when_nothing_is_bound() -> None:
    """No `TargetSource` in the tree means the importing one, not a failure."""
    host = PluginHost()
    handle = host.root.plugin(LoaderService)
    await host.runtime.quiesce()
    loader = host.root.context.require(LoaderService)

    report = await loader.reconcile(
        (Entry(id="one", name="tests.test_loader:alpha", config={"id": "one", "v": 0}),)
    )
    await host.runtime.quiesce()

    assert report.mounted == ("one",)
    assert [leaf(node.label) for node in inspect(handle).children] == ["alpha"]
