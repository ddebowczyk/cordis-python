"""PROP-HMR-001..006, from spec/capabilities/17-hot-reload.yaml.

Hot reload is the one capability whose subject is the Python module system
itself, so these tests write real `.py` files under a temporary root and
import them. A hand-supplied edge list would exercise the reachability walk
while assuming away the part that can be wrong -- whether the port can read a
Python program's import structure at all (PROP-HMR-002's note).

The generated modules reach the test through their *config*, never through an
import of the test package: an import of `tests.support` would put an edge in
the graph that no card asked about, and the graph is the thing under test.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import itertools
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cordis.diagnostics import inspect
from cordis.events import Emit, EventBus
from cordis.fiber import FiberState
from cordis.hmr import (
    HmrService,
    ReloadReport,
    affected,
    escalated,
    import_graph,
    imports_of,
    project_module,
    reload_order,
)
from cordis.loader import Entry, JsonSource, LoaderService
from cordis.plugin import PluginHost, scope_of
from tests.support import DagSpec, ResourceLedger
from tests.support import strategies as gen

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from cordis.context import Context
    from cordis.diagnostics import FiberSnapshot

# --------------------------------------------------------------------------
# A temporary package of generated plugin modules
# --------------------------------------------------------------------------

#: Distinct package names across examples. Two sandboxes sharing a package
#: name would share `sys.modules` entries, and a stale entry from a shrunk
#: example is a failure that reproduces only in the full run.
SERIAL = itertools.count()


def source(
    name: str,
    imports: Sequence[str],
    version: int,
    *,
    declines: bool = False,
    fail: str | None = None,
) -> str:
    """The text of one generated module.

    The `raise` for `fail="import"` goes above everything else on purpose:
    `importlib.reload` re-executes into the *existing* namespace, so a raise
    below the definitions would leave the old `plug` in place and the reimport
    failure would be invisible to everything downstream.
    """
    lines = [
        '"""A generated plugin module."""',
        "",
        "from __future__ import annotations",
        "",
    ]
    lines += [f"import {module}" for module in imports]
    if imports:
        lines.append("")
    if fail == "import":
        lines += ["raise RuntimeError('broken on import')", ""]
    lines += [f"NAME = {name!r}", f"VERSION = {version}", "HELD = {}"]
    if declines:
        lines.append("__cordis_reload__ = False")
    lines += [
        "",
        "",
        # The undo is reached through a *global* lookup, so it finds whichever
        # `HELD` the module has when it runs. Reimporting before disposal
        # rebinds `HELD` to a fresh dict, and the release then has nothing to
        # release -- which is the harm SEM-002's ordering exists to prevent,
        # made observable.
        "def keep(key, disposer):",
        "    HELD[key] = disposer",
        "",
        "    def close():",
        "        HELD.pop(key)()",
        "",
        "    return close",
        "",
        "",
        "async def plug(ctx):",
        "    from cordis.plugin import config_of",
        "",
    ]
    if fail == "apply":
        lines.append("    raise RuntimeError('broken on apply')")
    lines += [
        "    config = config_of(ctx)",
        "    enter = config.get('enter') if isinstance(config, dict) else None",
        "    if enter is not None:",
        "        await enter(ctx, NAME, VERSION)",
    ]
    return "\n".join(lines) + "\n"


class Sandbox:
    """A temporary importable package, and the mess it leaves behind removed.

    Cleanup is not hygiene here, it is correctness: a module left in
    `sys.modules` is a module the next example's `import_graph` would find.
    """

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cordis-hmr-"))
        self.pkg = f"hmrgen{next(SERIAL)}"
        (self.root / self.pkg).mkdir()
        (self.root / self.pkg / "__init__.py").write_text("", encoding="utf-8")
        sys.path.insert(0, str(self.root))

    def module(self, stem: str) -> str:
        return f"{self.pkg}.{stem}"

    def write(
        self,
        stem: str,
        *,
        imports: Sequence[str] = (),
        version: int = 0,
        declines: bool = False,
        fail: str | None = None,
    ) -> str:
        """Write one module and return its dotted name."""
        name = self.module(stem)
        text = source(
            name,
            [self.module(other) for other in imports],
            version,
            declines=declines,
            fail=fail,
        )
        (self.root / self.pkg / f"{stem}.py").write_text(text, encoding="utf-8")
        return name

    def path(self, stem: str) -> Path:
        return self.root / self.pkg / f"{stem}.py"

    def load(self, stems: Iterable[str]) -> tuple[str, ...]:
        importlib.invalidate_caches()
        return tuple(
            importlib.import_module(self.module(stem)).__name__ for stem in stems
        )

    def close(self) -> None:
        for name in [
            found
            for found in sys.modules
            if found == self.pkg or found.startswith(f"{self.pkg}.")
        ]:
            del sys.modules[name]
        with contextlib.suppress(ValueError):
            sys.path.remove(str(self.root))
        shutil.rmtree(self.root, ignore_errors=True)
        importlib.invalidate_caches()

    def __enter__(self) -> Sandbox:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# --------------------------------------------------------------------------
# What a generated body registers
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Recipe:
    """How much a plugin registers: the quantity SEM-003 is about."""

    listeners: int = 0
    services: int = 0
    resources: int = 0


class World:
    """Everything a generated body reaches for, handed to it as its config.

    Listeners go on a real bus, services into the host's real registry and
    resources into the ledger, so "what this fiber registered" is counted by
    three witnesses the reload path never writes to.
    """

    def __init__(self, host: PluginHost) -> None:
        self.host = host
        self.bus = EventBus()
        self.topic: Emit[[]] = Emit("hmr/probe")
        self.ledger = ResourceLedger()
        self.recipes: dict[str, Recipe] = {}
        self.loaded: list[tuple[str, int]] = []
        self.hold: str | None = None
        self.gate = asyncio.Event()
        self.holding = asyncio.Event()
        self._serial = itertools.count()

    @property
    def config(self) -> dict[str, object]:
        """The config every generated entry carries."""
        return {"enter": self.enter}

    async def enter(self, ctx: Context, name: str, version: int) -> None:
        if self.hold == name:
            self.holding.set()
            await self.gate.wait()
        self.loaded.append((name, version))
        recipe = self.recipes.get(name, Recipe())
        scope = scope_of(ctx)
        for _ in range(recipe.listeners):
            self.bus.through(ctx).on(self.topic, _idle, scope=scope)
        for index in range(recipe.services):
            self.host.registry.provide(
                f"{name}#{index}", object(), scope=scope, ctx=ctx
            )
        module = sys.modules[name]
        for index in range(recipe.resources):
            # A fresh id per acquisition: the ledger refuses to hold the same
            # resource twice, which is exactly the leak this card looks for.
            # The resource is parked in the plugin's own module, so its undo
            # goes through module state the way a real one's would.
            key = f"{name}:{index}:{next(self._serial)}"
            scope.effect(self._resource(module, key), label=f"res:{index}")

    def _resource(self, module: Any, key: str) -> Callable[[], object]:
        keep = module.keep  # generated: the module parks its own disposers

        def setup() -> object:
            return keep(key, self.ledger.disposer(key))

        return setup

    # -- the counts SEM-003 conserves --------------------------------------

    def listeners(self) -> int:
        return len(self.bus.listeners(self.topic))

    def services(self, name: str) -> int:
        return sum(
            1
            for key, _realm in self.host.registry.bindings()
            if key.startswith(f"{name}#")
        )

    def resources(self, name: str) -> int:
        return sum(1 for key in self.ledger.live if key.startswith(f"{name}:"))

    def counts(self, names: Iterable[str]) -> dict[str, tuple[int, int]]:
        return {name: (self.services(name), self.resources(name)) for name in names}


def _idle() -> None:
    """A listener that exists to be counted."""


@dataclass
class Rig:
    """A host with a loader and a reloader on it, and the world they see."""

    host: PluginHost
    world: World
    loader: LoaderService
    hmr: HmrService
    reports: list[ReloadReport] = field(default_factory=list)


async def rig(box: Sandbox) -> Rig:
    host = PluginHost()
    host.root.plugin(LoaderService)
    host.root.plugin(HmrService, {"root": str(box.root)})
    await host.runtime.quiesce()
    loader = host.root.context.require(LoaderService)
    hmr = host.root.context.require(HmrService)
    assert isinstance(loader, LoaderService)
    assert isinstance(hmr, HmrService)
    built = Rig(host, World(host), loader, hmr)
    hmr.on_reload(built.reports.append)
    return built


def entries_for(box: Sandbox, world: World, stems: Iterable[str]) -> tuple[Entry, ...]:
    return tuple(
        Entry(id=stem, name=f"{box.module(stem)}:plug", config=world.config)
        for stem in stems
    )


def uids(loader: LoaderService) -> dict[str, int]:
    """Every live entry's fiber identity, by path."""
    out: dict[str, int] = {}
    for path in loader.live():
        handle = loader.handle_for(path)
        assert handle is not None
        out[path] = handle.uid
    return out


def shape(node: FiberSnapshot) -> object:
    """A snapshot with identities removed, for comparing two trees."""
    return (
        node.label.rsplit("/", 1)[-1].split("#", 1)[0],
        node.state,
        tuple(shape(child) for child in node.children),
    )


# --------------------------------------------------------------------------
# PROP-HMR-002
# --------------------------------------------------------------------------


@pytest.mark.tier_pr
@settings(max_examples=25)
@given(
    spec=gen.dag_specs(min_size=1, max_size=5),
    pick=st.integers(min_value=0, max_value=4),
)
async def test_only_what_imports_the_change_is_rebuilt(
    spec: DagSpec, pick: int
) -> None:
    """PROP-HMR-002: the affected set is reverse reachability, and nothing more.

    Reloading everything on any change is correct and provides none of the
    value; the claim is that the port reads a real program's import structure
    and rebuilds exactly the fibers that structure implicates.
    """
    index = pick % len(spec.names)
    with Sandbox() as box:
        for position, stem in enumerate(spec.names):
            box.write(stem, imports=spec.dependencies(position))
        box.load(spec.names)
        built = await rig(box)
        await built.loader.reconcile(entries_for(box, built.world, spec.names))
        before = uids(built.loader)

        report = await built.hmr.reload([box.module(spec.names[index])])

        expected = {box.module(name) for name in spec.dependents(index)}
        expected.add(box.module(spec.names[index]))
        assert set(report.reloaded) == expected
        assert not report.failed
        moved = {
            stem for stem, uid in uids(built.loader).items() if before[stem] != uid
        }
        assert {box.module(stem) for stem in moved} == expected
        await built.host.dispose()


@pytest.mark.tier_local
async def test_the_graph_reads_both_import_forms() -> None:
    """`import a.b` and `from a.b import c` are the same edge to the reader."""
    with Sandbox() as box:
        box.write("base")
        plain = box.path("plain")
        plain.write_text(
            f"import {box.module('base')}\n\n\ndef apply(ctx):\n    pass\n",
            encoding="utf-8",
        )
        froms = box.path("froms")
        froms.write_text(
            f"from {box.pkg} import base\n\n\ndef apply(ctx):\n    pass\n",
            encoding="utf-8",
        )
        box.load(("base", "plain", "froms"))

        graph = import_graph(root=box.root)

        assert box.module("base") in graph[box.module("plain")]
        assert box.module("base") in graph[box.module("froms")]
        assert affected([box.module("base")], graph) == {
            box.module("base"),
            box.module("plain"),
            box.module("froms"),
        }


@pytest.mark.tier_local
async def test_a_file_outside_the_root_is_not_a_project_module() -> None:
    """SEM-001's exclusion, as a path test rather than a list of prefixes."""
    with Sandbox() as box:
        box.write("only")
        box.load(("only",))

        assert project_module(box.path("only"), root=box.root) == box.module("only")
        assert project_module(Path(json.__file__), root=box.root) is None
        assert imports_of("json", root=box.root) == frozenset()


@pytest.mark.tier_local
async def test_dependencies_are_reimported_before_their_importers() -> None:
    """A module reloaded before its dependency would capture the old one."""
    with Sandbox() as box:
        box.write("low")
        box.write("mid", imports=("low",))
        box.write("high", imports=("mid",))
        box.load(("low", "mid", "high"))
        graph = import_graph(root=box.root)

        order = reload_order(
            [box.module(stem) for stem in ("high", "mid", "low")], graph
        )

        assert order.index(box.module("low")) < order.index(box.module("mid"))
        assert order.index(box.module("mid")) < order.index(box.module("high"))


# --------------------------------------------------------------------------
# PROP-HMR-001
# --------------------------------------------------------------------------


@pytest.mark.tier_pr
@settings(max_examples=25, deadline=None)
@given(
    recipes=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=3),
            st.integers(min_value=0, max_value=2),
            st.integers(min_value=0, max_value=2),
        ),
        min_size=1,
        max_size=3,
    ),
    rounds=st.lists(
        st.sets(st.integers(min_value=0, max_value=2), min_size=1, max_size=3),
        min_size=1,
        max_size=5,
    ),
)
async def test_reloading_any_number_of_times_conserves_what_was_registered(
    recipes: list[tuple[int, int, int]], rounds: list[set[int]]
) -> None:
    """PROP-HMR-001: the tenth save registers as much as the first, not ten times.

    Accumulation across reloads is the defining failure of every hand-rolled
    reload system, and it is silent until the tenth save -- so the oracle is
    the counts after the first load, compared after every reload in a
    generated sequence.
    """
    stems = tuple(f"m{index}" for index in range(len(recipes)))
    with Sandbox() as box:
        for stem in stems:
            box.write(stem)
        box.load(stems)
        built = await rig(box)
        names = [box.module(stem) for stem in stems]
        for name, (listeners, services, resources) in zip(names, recipes, strict=True):
            built.world.recipes[name] = Recipe(listeners, services, resources)
        await built.loader.reconcile(entries_for(box, built.world, stems))
        first = (built.world.listeners(), built.world.counts(names))

        for version, chosen in enumerate(rounds, start=1):
            picked = [names[index] for index in chosen if index < len(names)]
            if not picked:
                continue
            for name in picked:
                box.write(name.rsplit(".", 1)[-1], version=version)
            report = await built.hmr.reload(picked)
            assert not report.failed
            assert (built.world.listeners(), built.world.counts(names)) == first
            assert set(built.world.loaded[-len(picked) :]) == {
                (name, version) for name in picked
            }
        await built.host.dispose()
        assert built.world.ledger.balanced


# --------------------------------------------------------------------------
# PROP-HMR-003
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@settings(max_examples=25)
@given(
    breaks=st.sets(st.integers(min_value=0, max_value=2), min_size=1, max_size=2),
    mode=st.sampled_from(("import", "apply")),
)
async def test_a_reload_that_fails_takes_only_its_own_fibers(
    breaks: set[int], mode: str
) -> None:
    """PROP-HMR-003: a syntax error must not take down the session.

    The two shapes a failure takes are both here: code that will not import
    leaves no fiber and an unresolvable entry, code that raises when applied
    leaves a mounted FAILED one. Neither may touch a fiber it has nothing to
    do with, which is checked by identity against handles captured before.
    """
    stems = ("m0", "m1", "m2")
    with Sandbox() as box:
        for stem in stems:
            box.write(stem)
        box.load(stems)
        built = await rig(box)
        await built.loader.reconcile(entries_for(box, built.world, stems))
        handles = {stem: built.loader.handle_for(stem) for stem in stems}
        broken = sorted(stems[index] for index in breaks)
        for stem in broken:
            box.write(stem, fail=mode)

        report = await built.hmr.reload([box.module(stem) for stem in broken])

        assert report.reloaded == tuple(sorted(box.module(stem) for stem in broken))
        for stem in stems:
            if stem in broken:
                continue
            standing = handles[stem]
            assert standing is not None
            assert built.loader.handle_for(stem) is standing
            assert standing.state is FiberState.ACTIVE
        named = (
            {failure.id for failure in report.entries.failed}
            if report.entries
            else set()
        )
        assert named == set(broken)
        for stem in broken:
            handle = built.loader.handle_for(stem)
            assert handle is None or handle.state is FiberState.FAILED
        await built.host.dispose()


# --------------------------------------------------------------------------
# PROP-HMR-005
# --------------------------------------------------------------------------


@pytest.mark.tier_pr
@settings(max_examples=25)
@given(
    spec=gen.dag_specs(min_size=1, max_size=4),
    pick=st.integers(min_value=0, max_value=3),
    mask=st.integers(min_value=1, max_value=15),
)
async def test_a_declining_module_escalates_or_refuses(
    spec: DagSpec, pick: int, mask: int
) -> None:
    """PROP-HMR-005: an author who said no is not overruled, and a refusal is total.

    The model walks the generated graph rather than the runtime's: escalation
    that reaches a module nothing imports is a refusal, and a refusal must
    leave every fiber identical -- not most of them.
    """
    index = pick % len(spec.names)
    declining = {
        name for position, name in enumerate(spec.names) if mask >> position & 1
    }
    with Sandbox() as box:
        for position, stem in enumerate(spec.names):
            box.write(
                stem,
                imports=spec.dependencies(position),
                declines=stem in declining,
            )
        box.load(spec.names)
        built = await rig(box)
        await built.loader.reconcile(entries_for(box, built.world, spec.names))
        before = uids(built.loader)

        report = await built.hmr.reload([box.module(spec.names[index])])

        hit = set(spec.dependents(index)) | {spec.names[index]}
        refused = {
            stem
            for stem in hit & declining
            if not spec.dependents(spec.names.index(stem))
        }
        if refused:
            assert report.refused_reload
            assert set(report.refused) == {box.module(stem) for stem in refused}
            assert report.reloaded == ()
            assert uids(built.loader) == before
        else:
            assert set(report.reloaded) == {
                box.module(stem) for stem in hit - declining
            }
            moved = {
                stem for stem, uid in uids(built.loader).items() if before[stem] != uid
            }
            assert moved == hit - declining
        await built.host.dispose()


# --------------------------------------------------------------------------
# PROP-HMR-004
# --------------------------------------------------------------------------


@pytest.mark.tier_pr
@settings(max_examples=25, deadline=None)
@given(
    bursts=st.lists(
        st.sets(st.integers(min_value=0, max_value=2), min_size=1, max_size=3),
        min_size=1,
        max_size=4,
    )
)
async def test_a_burst_during_a_reload_becomes_exactly_one_more_reload(
    bursts: list[set[int]],
) -> None:
    """PROP-HMR-004: editors write in bursts; the tree is rebuilt once for them.

    One reload is held open inside a mounted body, which is what makes the
    overlap window real rather than timed. Nothing else may run while it is
    held, and what arrives while it is held is one further reload of the
    union, not one per caller.
    """
    stems = ("m0", "m1", "m2")
    with Sandbox() as box:
        for stem in stems:
            box.write(stem)
        box.load(stems)
        built = await rig(box)
        names = [box.module(stem) for stem in stems]
        await built.loader.reconcile(entries_for(box, built.world, stems))
        built.world.hold = names[0]

        first = asyncio.create_task(built.hmr.reload([names[0]]))
        await asyncio.wait_for(built.world.holding.wait(), timeout=5)
        held = list(built.world.loaded)
        waiting = [
            asyncio.create_task(built.hmr.reload([names[index] for index in burst]))
            for burst in bursts
        ]
        for _ in range(len(waiting) + 2):
            await asyncio.sleep(0)
        assert built.world.loaded == held, "a second reload ran while one was open"

        built.world.hold = None
        built.world.gate.set()
        reports = await asyncio.gather(first, *waiting)

        union = {names[index] for burst in bursts for index in burst}
        assert set(reports[0].reloaded) == {names[0]}
        assert set().union(*(set(one.reloaded) for one in reports)) == union | {
            names[0]
        }
        assert len(built.reports) == 2
        await built.host.dispose()


# --------------------------------------------------------------------------
# PROP-HMR-006
# --------------------------------------------------------------------------


@pytest.mark.tier_pr
@settings(max_examples=25)
@given(
    before=st.lists(
        st.integers(min_value=0, max_value=2), min_size=1, max_size=3, unique=True
    ),
    after=st.lists(
        st.integers(min_value=0, max_value=2), min_size=1, max_size=3, unique=True
    ),
)
async def test_a_config_file_change_is_the_loaders_own_reconcile(
    before: list[int], after: list[int]
) -> None:
    """PROP-HMR-006: saving the file and calling reconcile produce one tree.

    A watcher with a diff of its own drifts from the loader's, so the edit
    applied by saving stops matching the edit applied programmatically. Two
    fresh roots, one entry list, and the snapshots must agree.
    """
    stems = ("m0", "m1", "m2")
    with Sandbox() as box:
        for stem in stems:
            box.write(stem)
        box.load(stems)

        def rows(chosen: Sequence[int]) -> list[dict[str, Any]]:
            return [
                {"id": stems[index], "name": f"{box.module(stems[index])}:plug"}
                for index in chosen
            ]

        config = box.root / "plugins.json"
        config.write_text(json.dumps(rows(before)), encoding="utf-8")

        watched = await rig(box)
        watched.hmr.follow(config, JsonSource(config))
        await watched.hmr.reload([config])
        config.write_text(json.dumps(rows(after)), encoding="utf-8")
        await watched.hmr.reload([config])

        direct = await rig(box)
        await direct.loader.reconcile(JsonSource(config).read())

        assert shape(inspect(watched.host)) == shape(inspect(direct.host))
        await watched.host.dispose()
        await direct.host.dispose()


@pytest.mark.tier_local
async def test_a_reload_of_nothing_is_quiet() -> None:
    """No change, no work, and a report that says so."""
    with Sandbox() as box:
        box.write("m0")
        box.load(("m0",))
        built = await rig(box)
        await built.loader.reconcile(entries_for(box, built.world, ("m0",)))
        before = uids(built.loader)

        report = await built.hmr.reload([])

        assert report.quiet
        assert uids(built.loader) == before
        assert built.reports == []
        await built.host.dispose()


@pytest.mark.tier_local
async def test_a_change_to_a_module_nothing_mounted_reloads_nothing() -> None:
    """A module the loader has no recipe for is left alone, not disposed."""
    with Sandbox() as box:
        box.write("mounted")
        box.write("stray")
        box.load(("mounted", "stray"))
        built = await rig(box)
        await built.loader.reconcile(entries_for(box, built.world, ("mounted",)))
        before = uids(built.loader)

        report = await built.hmr.reload([box.module("stray")])

        assert report.reloaded == (box.module("stray"),)
        assert built.hmr.entries_for({box.module("stray")}) == ()
        assert uids(built.loader) == before
        await built.host.dispose()


@pytest.mark.tier_local
async def test_escalation_stops_at_the_first_importer_that_accepts() -> None:
    """A declining leaf is not reloaded; what imports it is."""
    with Sandbox() as box:
        box.write("held", declines=True)
        box.write("user", imports=("held",))
        box.load(("held", "user"))
        graph = import_graph(root=box.root)

        reloadable, refused = escalated([box.module("held")], graph)

        assert refused == frozenset()
        assert reloadable == frozenset({box.module("user")})
