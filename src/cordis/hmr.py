"""Hot reload: a consequence of lifetime discipline, not a feature (capability 17).

Nothing here reloads anything itself. Reimporting a module is one line of
stdlib; the reason hot reload is normally a research project is that the old
objects, listeners and tasks survive the reimport, and a process running two
versions at once is worse than a process that restarted. This module is
therefore mostly about *which* fibers to take down and *when* -- the taking
down and the building back up belong to the loader, which owns the tree.

Three decisions shape it:

* The affected set is read from the program's real import structure, by
  parsing each project module's source with :mod:`ast`. What imports what is a
  question about the files, and the files are the thing the developer just
  edited.
* Disposal, reimport and remount are a single call --
  ``loader.remount(paths, between=...)``. SEM-002's ordering is expressed
  where the tree is, not in a second place that would have to agree with it.
* Only entries the loader mounted are reloadable. A plugin mounted by hand
  from application code carries no recipe anything can replay, so hot reload
  leaves it alone rather than disposing a fiber it cannot rebuild.

A module that cannot be reloaded safely says so with
``__cordis_reload__ = False``; a change to it escalates to whatever imports
it, and if nothing does, the whole reload is refused rather than performed
against the author's declaration.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import importlib
import importlib.util
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Final

from cordis.inject import inject
from cordis.loader import LoaderService, ReconcileReport
from cordis.plugin import config_of
from cordis.registry import Service
from cordis.timer import interval

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable, Sequence

    from cordis.context import Context
    from cordis.loader import Entry, FileSource
    from cordis.timer import Report, Schedule

__all__ = [
    "RELOAD_FLAG",
    "HmrService",
    "ReloadFailure",
    "ReloadReport",
    "affected",
    "declines",
    "escalated",
    "import_graph",
    "imports_of",
    "project_module",
    "reload_order",
]

#: What a module sets to decline reload (SEM-005). A module attribute rather
#: than a decorator: the declaration is about the module, and a module has no
#: other place to put one.
RELOAD_FLAG: Final = "__cordis_reload__"

_MODULE: Final = "module"
_FILE: Final = "file"


# --------------------------------------------------------------------------
# Reading the program
# --------------------------------------------------------------------------


def project_module(path: Path | str, /, *, root: Path | None = None) -> str | None:
    """The loaded module ``path`` is, or ``None`` if it is not one.

    ``None`` covers three different things on purpose -- outside the project,
    not a Python file, never imported -- because the answer to all three is
    the same: there is nothing here to reload.
    """
    base = _base(root)
    target = Path(path).resolve()
    if not target.is_relative_to(base):
        return None
    for name, module in list(sys.modules.items()):
        if _file_of(module) == target:
            return name
    return None


def imports_of(name: str, /, *, root: Path | None = None) -> frozenset[str]:
    """The project modules ``name``'s source imports, at any nesting depth.

    Read from the file rather than from an import hook. A hook records exactly
    what was imported, but only for modules imported *after* it is installed,
    which for a plugin mounted into a running application is nearly none of
    them. Function-level imports count: a body that imports on first call
    still holds the old module object after a reimport.
    """
    base = _base(root)
    module = sys.modules.get(name)
    file = _file_of(module)
    if module is None or file is None or not file.is_relative_to(base):
        return frozenset()
    try:
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
    except (OSError, SyntaxError, ValueError):
        # A module whose source cannot be read has no readable edges. Silence
        # would be wrong for a *reload*; here it only means "no in-edges
        # found", and a module that depends on one such declines instead.
        return frozenset()
    package = getattr(module, "__package__", None) or name.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.update(_prefixes(alias.name))
        elif isinstance(node, ast.ImportFrom):
            found.update(_from_import(node, package))
    found.discard(name)
    return frozenset(other for other in found if _is_project(other, base))


def import_graph(
    names: Iterable[str] | None = None, /, *, root: Path | None = None
) -> dict[str, frozenset[str]]:
    """Every project module mapped to what it imports.

    Built from ``sys.modules``, so it describes the program that is running
    rather than the one on disk: a file nothing ever imported has no fiber to
    rebuild, and a file that was deleted still has whatever imported it.
    """
    base = _base(root)
    chosen = (
        tuple(names)
        if names is not None
        else tuple(name for name in list(sys.modules) if _is_project(name, base))
    )
    return {name: imports_of(name, root=base) for name in chosen}


def affected(
    changed: Iterable[str], graph: Mapping[str, frozenset[str]], /
) -> frozenset[str]:
    """``changed`` and everything that reaches it through the import graph.

    Reloading only the changed module leaves its importers holding the old
    objects; reloading everything discards the state hot reload exists to
    preserve. This closure is the set between the two (SEM-001).
    """
    out = set(changed)
    growing = True
    while growing:
        growing = False
        for name, imports in graph.items():
            if name not in out and imports & out:
                out.add(name)
                growing = True
    return frozenset(out)


def declines(name: str, /) -> bool:
    """Whether ``name`` declared itself non-reloadable."""
    module = sys.modules.get(name)
    return module is not None and getattr(module, RELOAD_FLAG, True) is False


def escalated(
    names: Iterable[str], graph: Mapping[str, frozenset[str]], /
) -> tuple[frozenset[str], frozenset[str]]:
    """Split the affected set into what may be reloaded and what refuses.

    A declining module is not reloaded; what imports it is, which is already
    in the closure. A declining module nothing imports has nowhere to escalate
    to, and that is the refusal SEM-005 describes -- reported rather than
    worked around, because the author who wrote the flag meant it.
    """
    hit = affected(names, graph)
    refused = frozenset(
        name for name in hit if declines(name) and not _importers(name, graph)
    )
    reloadable = frozenset(name for name in hit if not declines(name))
    return reloadable, refused


def reload_order(
    names: Iterable[str], graph: Mapping[str, frozenset[str]], /
) -> tuple[str, ...]:
    """``names`` ordered so that a module follows everything it imports.

    A module reimported before its dependency re-executes its own imports
    against the *old* dependency and captures it, which is the two-versions
    failure one level down. Cycles terminate in whatever order they are
    reached: there is no correct order for a cycle, and refusing to reload one
    would be a worse answer than picking.
    """
    wanted = set(names)
    order: list[str] = []
    seen: set[str] = set()

    def visit(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        for dependency in sorted(graph.get(name, frozenset()) & wanted):
            visit(dependency)
        order.append(name)

    for name in sorted(wanted):
        visit(name)
    return tuple(order)


# --------------------------------------------------------------------------
# What a reload did
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReloadFailure:
    """A module whose new code would not import, and why."""

    module: str
    error: BaseException


@dataclass(frozen=True, slots=True)
class ReloadReport:
    """What one reload did, in the same currency as a reconcile.

    ``entries`` carries the loader's own report when the reload went through
    it, so tooling that already understands a :class:`~cordis.loader.
    ReconcileReport` understands this too rather than learning a second
    vocabulary for the same tree.
    """

    #: What was asked about: module names, then the config files, as text.
    changed: tuple[str, ...] = ()
    #: The modules actually reimported.
    reloaded: tuple[str, ...] = ()
    #: The modules that declined and had nowhere to escalate to. Non-empty
    #: means nothing at all was done.
    refused: tuple[str, ...] = ()
    entries: ReconcileReport | None = None
    failed: tuple[ReloadFailure, ...] = ()

    @property
    def quiet(self) -> bool:
        """Whether the application was left exactly as it was found."""
        moved = self.entries is not None and not self.entries.quiet
        return not (self.reloaded or self.refused or self.failed or moved)

    @property
    def refused_reload(self) -> bool:
        """Whether a module declined and the whole run was abandoned."""
        return bool(self.refused)


# --------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------


@inject("loader")
class HmrService(Service):
    """Reimports changed modules and has the loader rebuild what they reach.

    Config is optional and holds one key, ``root``: the directory that counts
    as the project. Everything outside it is somebody else's code -- the
    standard library, site-packages, the interpreter -- and reloading it is
    not what the developer who just pressed save asked for.
    """

    name = "hmr"

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        loader = ctx.require(LoaderService)
        assert isinstance(loader, LoaderService)
        self._loader = loader
        self._root = _configured_root(ctx)
        self._lock = asyncio.Lock()
        self._pending: set[tuple[str, str]] = set()
        self._sources: dict[Path, FileSource] = {}
        self._handlers: list[Callable[[ReloadReport], object]] = []
        self._stamps: dict[Path, int] = {}
        self._last = ReloadReport()

    # -- the capability ----------------------------------------------------

    async def reload(self, changed: Sequence[str | Path], /) -> ReloadReport:
        """Rebuild whatever ``changed`` reaches, once, and say what happened.

        Callers arriving while a reload is running add their paths to a
        pending union and wait for the run that will include them, so a burst
        of saves becomes exactly one further reload (SEM-007) and every caller
        gets back the report of the run that carried its change -- not a
        report of a run that finished before its file was written.
        """
        mine = self._changes(changed)
        self._pending |= mine
        async with self._lock:
            if not (mine & self._pending):
                return self._last  # a run that already carried it
            batch = frozenset(self._pending)
            self._pending.clear()
            report = await self._run(batch)
            self._last = report
        self._announce(report)
        return report

    def entries_for(self, modules: Collection[str], /) -> tuple[str, ...]:
        """The loader entry paths whose plugin came out of ``modules``.

        The intersection that keeps hot reload from disposing something it
        cannot rebuild: an entry has a recipe, an ad hoc mount does not.
        """
        wanted = set(modules)
        return tuple(
            path
            for path in self._loader.live()
            if _origin(self._target_of(path)) in wanted
        )

    def follow(self, path: Path | str, source: FileSource, /) -> Callable[[], None]:
        """Treat ``path`` as a config file read by ``source``.

        A change to a followed file is handed to the loader's own reconcile
        (SEM-006) rather than to a diff of this module's own, which would
        drift and make an edit mean two different things depending on how it
        was applied. Returns the undo.
        """
        key = Path(path).resolve()
        self._sources[key] = source

        def forget() -> None:
            self._sources.pop(key, None)

        return forget

    def watch(
        self,
        paths: Sequence[Path | str],
        /,
        *,
        every: float = 1.0,
        on_error: Report | None = None,
    ) -> Schedule:
        """Poll ``paths`` and reload what changed.

        A directory is expanded to the ``.py`` files under it on every poll,
        so a new file counts as a change. This is a convenience, not the
        capability: :meth:`reload` is public precisely so a real watcher --
        watchdog, an editor plugin, a CI hook -- can drive it instead.
        """
        watched = tuple(Path(path).resolve() for path in paths)
        self._stamps = _stamps(watched)

        async def poll() -> None:
            fresh = _stamps(watched)
            changed = [
                path for path, stamp in fresh.items() if self._stamps.get(path) != stamp
            ]
            self._stamps = fresh
            if changed:
                await self.reload(changed)

        return interval(self.ctx, every, poll, on_error=on_error)

    def on_reload(
        self, handler: Callable[[ReloadReport], object], /
    ) -> Callable[[], None]:
        """Be told about every reload this service performs. Returns the undo.

        The service's own channel rather than the event bus: the port binds no
        ambient bus, and a watcher has no caller to return its report to.
        """
        self._handlers.append(handler)

        def forget() -> None:
            with contextlib.suppress(ValueError):
                self._handlers.remove(handler)

        return forget

    # -- one run -----------------------------------------------------------

    async def _run(self, batch: frozenset[tuple[str, str]]) -> ReloadReport:
        modules = frozenset(name for kind, name in batch if kind == _MODULE)
        files = tuple(sorted(name for kind, name in batch if kind == _FILE))
        changed = tuple(sorted(modules)) + files
        graph = import_graph(root=self._root)
        reloadable, refused = escalated(modules, graph)
        if refused:
            # Total, including the config half: PROP-HMR-005 asks for every
            # fiber to be identical afterwards, and a reconcile performed
            # alongside is a fiber that is not.
            return ReloadReport(changed=changed, refused=tuple(sorted(refused)))

        failed: list[ReloadFailure] = []
        entries: ReconcileReport | None = None
        if reloadable:
            order = reload_order(reloadable, graph)
            entries = await self._loader.remount(
                self.entries_for(reloadable),
                between=partial(self._reimport, order, failed),
            )
        if files:
            reconciled = await self._loader.reconcile(self._entries())
            entries = reconciled if entries is None else _merged(entries, reconciled)
        return ReloadReport(
            changed=changed,
            reloaded=tuple(sorted(reloadable)),
            entries=entries,
            failed=tuple(failed),
        )

    def _reimport(self, order: Sequence[str], failed: list[ReloadFailure]) -> None:
        """Reimport, dependencies first, between disposal and remounting."""
        importlib.invalidate_caches()
        for name in order:
            module = sys.modules.get(name)
            if module is None:
                continue
            _uncache(module)
            try:
                importlib.reload(module)
            except Exception as exc:
                # A half-executed module is worse than an absent one: dropping
                # it makes the next import re-run the file, so a corrected
                # save recovers, and makes the entry's target fail to resolve
                # now rather than mount whatever survived the crash.
                sys.modules.pop(name, None)
                failed.append(ReloadFailure(name, exc))

    def _entries(self) -> tuple[Entry, ...]:
        out: list[Entry] = []
        for source in self._sources.values():
            out.extend(source.read())
        return tuple(out)

    def _announce(self, report: ReloadReport) -> None:
        for handler in tuple(self._handlers):
            handler(report)

    # -- reading what the caller said --------------------------------------

    def _changes(self, changed: Sequence[str | Path]) -> frozenset[tuple[str, str]]:
        """Normalise module names and file paths into one comparable unit.

        Anything that is neither a loaded module nor a followed file is
        dropped: a change to a file nothing ever imported has no fiber behind
        it, and inventing one would be a reload of something that was never
        loaded.
        """
        out: set[tuple[str, str]] = set()
        for item in changed:
            if isinstance(item, str) and item in sys.modules:
                out.add((_MODULE, item))
                continue
            path = Path(item).resolve()
            if path in self._sources:
                out.add((_FILE, str(path)))
                continue
            name = project_module(path, root=self._root)
            if name is not None:
                out.add((_MODULE, name))
        return frozenset(out)

    def _target_of(self, path: str) -> object:
        handle = self._loader.handle_for(path)
        form = None if handle is None else handle.form
        return None if form is None else form.call


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _base(root: Path | None) -> Path:
    return Path.cwd().resolve() if root is None else Path(root).resolve()


def _configured_root(ctx: Context) -> Path:
    config = config_of(ctx)
    if isinstance(config, Mapping):
        found = config.get("root")
        if isinstance(found, str | Path):
            return Path(found).resolve()
    return Path.cwd().resolve()


def _file_of(module: object) -> Path | None:
    found = getattr(module, "__file__", None)
    if not isinstance(found, str):
        return None
    try:
        return Path(found).resolve()
    except OSError:  # pragma: no cover -- a path the platform refuses
        return None


def _uncache(module: ModuleType) -> None:
    """Drop ``module``'s cached bytecode before it is reimported.

    Python decides a ``.pyc`` is current by comparing the source's size and
    its modification time *in whole seconds*. Two saves within the same second
    that leave the file the same length -- flipping a constant, correcting a
    character -- are therefore indistinguishable to that check, and the
    reimport would silently re-execute the old bytecode. Removing the cache
    entry for a module we are deliberately reloading costs one compile and
    makes the reload mean what it says.
    """
    cached = getattr(module, "__cached__", None)
    if isinstance(cached, str):
        with contextlib.suppress(OSError):
            Path(cached).unlink()


def _is_project(name: str, root: Path) -> bool:
    file = _file_of(sys.modules.get(name))
    return file is not None and file.is_relative_to(root)


def _prefixes(dotted: str) -> frozenset[str]:
    """``a.b.c`` and its packages: importing a submodule imports its parents."""
    parts = dotted.split(".")
    return frozenset(".".join(parts[: index + 1]) for index in range(len(parts)))


def _from_import(node: ast.ImportFrom, package: str) -> frozenset[str]:
    """``from a.b import c``: an edge to ``a.b``, and to ``a.b.c`` if it is one.

    Both, because the statement does not say which ``c`` is -- a submodule or
    an attribute -- and the loaded-module filter downstream decides.
    """
    try:
        base = importlib.util.resolve_name(
            "." * node.level + (node.module or ""), package
        )
    except (ImportError, ValueError):
        return frozenset()
    return _prefixes(base) | {f"{base}.{alias.name}" for alias in node.names}


def _importers(name: str, graph: Mapping[str, frozenset[str]]) -> frozenset[str]:
    return frozenset(other for other, imports in graph.items() if name in imports)


def _origin(target: object) -> str | None:
    """The module a plugin target came out of."""
    if isinstance(target, ModuleType):
        return target.__name__
    found = getattr(target, "__module__", None)
    return found if isinstance(found, str) else None


def _stamps(paths: Iterable[Path]) -> dict[Path, int]:
    out: dict[Path, int] = {}
    for path in paths:
        for file in sorted(path.rglob("*.py")) if path.is_dir() else (path,):
            with contextlib.suppress(OSError):
                out[file] = file.stat().st_mtime_ns
    return out


def _merged(first: ReconcileReport, second: ReconcileReport) -> ReconcileReport:
    """One report out of the module half and the config half of a run."""
    return ReconcileReport(
        mounted=first.mounted + second.mounted,
        updated=first.updated + second.updated,
        disposed=first.disposed + second.disposed,
        unchanged=first.unchanged + second.unchanged,
        failed=first.failed + second.failed,
    )
