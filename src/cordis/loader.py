"""Declarative loader: the application is a config file (capability 14).

A list of entries in, a plugin tree out -- and, on every subsequent read, the
smallest set of changes that turns the tree it left into the tree the new list
describes. The whole capability is that diff. Mounting a list is easy; mounting
the *difference* between two lists is what lets an operator edit one row of a
forty-row file without restarting the other thirty-nine.

Three decisions shape the module:

* Entries are validated as a whole before anything is imported, and every
  problem in the list is reported at once, in the same currency as every other
  config failure (:class:`~cordis.errors.ConfigValidationError`). SEM-003's
  "naming its position" is then free -- the issue path *is* the position.
* Targets are resolved through a capability seam, not a method. Binding a
  different :class:`TargetSource` is how a test supplies synthetic targets and
  how a locked-down deployment refuses to import anything off its allow-list.
* A group is an ordinary plugin with an empty body whose children mount on its
  own handle, so disposal, isolation and interception compose (SEM-007) rather
  than being reimplemented for groups.

The diff keys on the entry id, so an id is required (SEM-003) and the order of
the file means nothing (SEM-004).
"""

from __future__ import annotations

import abc
import asyncio
import importlib
import importlib.util
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Protocol, TypeGuard, runtime_checkable

from cordis.config import ConfigIssue, resolve_config, schema_of
from cordis.errors import ConfigValidationError, ExpressionError, InvalidPluginError
from cordis.expr import (
    FUNCTIONS,
    Expr,
    FunctionSource,
    evaluate,
    expression_paths,
    is_envelope,
    opaque,
    parse_expressions,
    substitute,
    unparse_expressions,
    yaml_loader,
)
from cordis.plugin import normalise
from cordis.registry import Service
from cordis.seam import Definition

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from cordis.context import Context
    from cordis.intercept import Interception
    from cordis.plugin import ConfigPreparer, PluginHandle, PluginTarget
    from cordis.realm import Isolation

__all__ = [
    "GROUP",
    "Entry",
    "EntryFailure",
    "FileSource",
    "ImportTargets",
    "JsonSource",
    "LoaderService",
    "MappingSource",
    "ReconcileReport",
    "TargetSource",
    "TomlSource",
    "YamlSource",
    "as_mapping",
    "read_entries",
]

#: The reserved target name of a group entry. A group is not a fourth plugin
#: form; it is a name the loader recognises and mounts an empty body for.
GROUP: Final = "group"

#: The closed entry vocabulary (SEM-001). A key outside it is an error, not
#: something to ignore: `disabled: ture` must not leave an entry running.
FIELDS: Final = frozenset(
    {"id", "name", "config", "disabled", "inject", "isolate", "intercept"}
)

#: The two fields an expression may stand in (config-expressions SEM-001). A
#: closed set is what makes the security surface auditable; in particular
#: `name` must never be computed, or the set of loadable code becomes dynamic.
_COMPUTABLE: Final = frozenset({"config", "disabled"})

_MOUNT: Final = "mount"
_UPDATE: Final = "update"
_KEEP: Final = "keep"


# --------------------------------------------------------------------------
# What a config file says
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Entry:
    """One row of the file: what to mount, how, and under what id.

    ``config`` is deliberately ``object``. What a plugin's config must look
    like is the plugin's own declaration, checked by the config capability;
    the loader validates the *envelope* and nothing inside it.

    ``isolate`` and ``intercept`` are the mount API's own aliases, so an entry
    field is the mount keyword rather than a third spelling of either.
    """

    id: str
    name: str
    config: object = None
    disabled: bool | Expr = False
    inject: tuple[str, ...] | None = None
    isolate: Isolation = ()
    intercept: Interception | None = None
    group: tuple[Entry, ...] | None = None

    @property
    def is_group(self) -> bool:
        return self.group is not None


@dataclass(frozen=True, slots=True)
class EntryFailure:
    """An entry that did not reach a running state, and why.

    ``id`` is the dotted path (``group.child``), which is what identifies a
    nested entry; a bare id does not.
    """

    id: str
    reason: str
    error: BaseException


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """What one reconcile did, by dotted entry path.

    ``disposed`` lists every entry that went away *at any depth*, so a group
    replaced by a plugin names each child it took with it -- a structural
    answer rather than a log line, which holds whether or not a logger is
    bound.
    """

    mounted: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    disposed: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    failed: tuple[EntryFailure, ...] = ()

    @property
    def quiet(self) -> bool:
        """Whether the tree was left exactly as it was found."""
        return not (self.mounted or self.updated or self.disposed or self.failed)


# --------------------------------------------------------------------------
# Reading the envelope (SEM-001, SEM-003)
# --------------------------------------------------------------------------


def read_entries(raw: object, /) -> tuple[Entry, ...]:
    """Validate a raw entry list in full, or raise naming every problem.

    In full, and before returning any of it: a file with a mistake halfway
    down must not leave the application in a state that matches no version of
    the file.
    """
    issues: list[ConfigIssue] = []
    entries = _read_list(raw, (), issues)
    if issues:
        raise ConfigValidationError("loader", issues)
    return entries


def as_mapping(entry: Entry, /) -> dict[str, object]:
    """The inverse of :func:`read_entries` for one entry, defaults omitted.

    Round trips: what an operator wrote comes back out, which is what makes a
    file-watching loader's "did anything change" answer trustworthy.
    """
    out: dict[str, object] = {"id": entry.id, "name": entry.name}
    if entry.config is not None:
        # One writer for all three formats: an expression comes back out as
        # the portable envelope, which is the text that went in (SEM-006).
        out["config"] = unparse_expressions(entry.config)
    if entry.disabled is not False:
        out["disabled"] = unparse_expressions(entry.disabled)
    if entry.inject is not None:
        out["inject"] = list(entry.inject)
    if entry.isolate:
        out["isolate"] = (
            dict(entry.isolate)
            if isinstance(entry.isolate, Mapping)
            else list(entry.isolate)
        )
    if entry.intercept is not None:
        out["intercept"] = dict(entry.intercept)
    return out


def _read_list(
    raw: object, path: tuple[str | int, ...], issues: list[ConfigIssue]
) -> tuple[Entry, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        issues.append(ConfigIssue(path, "expected a list of entries"))
        return ()
    seen: set[str] = set()
    out: list[Entry] = []
    for index, row in enumerate(raw):
        here = (*path, index)
        entry = _read_entry(row, here, issues)
        if entry is None:
            continue
        if entry.id in seen:
            issues.append(
                ConfigIssue((*here, "id"), f"duplicate entry id {entry.id!r}")
            )
            continue
        seen.add(entry.id)
        out.append(entry)
    return tuple(out)


def _read_entry(
    row: object, here: tuple[str | int, ...], issues: list[ConfigIssue]
) -> Entry | None:
    if not isinstance(row, Mapping):
        issues.append(ConfigIssue(here, "expected a mapping"))
        return None
    if _computed(row):
        # An entry list is not a place an expression can stand: what to mount
        # must never be computed (SEM-001).
        issues.append(ConfigIssue(here, "an expression is not permitted here"))
        return None

    before = len(issues)
    issues.extend(
        ConfigIssue(
            (*here, key if isinstance(key, str | int) else str(key)),
            "not an entry field",
        )
        for key in row
        if key not in FIELDS
    )
    _reject_expressions(row, here, issues)

    entry_id = _text(row, "id", here, issues)
    name = _text(row, "name", here, issues)
    config = row.get("config")
    group = None
    if name == GROUP:
        group = _read_list(
            config if config is not None else (), (*here, "config"), issues
        )
    else:
        config = _read_config(config, (*here, "config"), issues)
    parsed = Entry(
        id=entry_id or "",
        name=name or "",
        config=config,
        disabled=_flag(row, here, issues),
        inject=_names(row, here, issues),
        isolate=_isolation(row, here, issues),
        intercept=_interception(row, here, issues),
        group=group,
    )
    return None if len(issues) != before else parsed


def _text(
    row: Mapping[object, object],
    key: str,
    here: tuple[str | int, ...],
    issues: list[ConfigIssue],
) -> str | None:
    value = row.get(key)
    if value is None:
        issues.append(ConfigIssue((*here, key), "required"))
        return None
    if not isinstance(value, str) or not value:
        issues.append(ConfigIssue((*here, key), "must be a non-empty string"))
        return None
    return value


def _flag(
    row: Mapping[object, object],
    here: tuple[str | int, ...],
    issues: list[ConfigIssue],
) -> bool | Expr:
    parsed = _read_config(row.get("disabled", False), (*here, "disabled"), issues)
    if isinstance(parsed, Expr):
        return parsed
    if not isinstance(parsed, bool):
        issues.append(ConfigIssue((*here, "disabled"), "must be true or false"))
        return False
    return parsed


def _computed(value: object) -> bool:
    """Whether ``value`` is itself an expression, written either way."""
    return isinstance(value, Expr) or is_envelope(value)


def _read_config(
    value: object, here: tuple[str | int, ...], issues: list[ConfigIssue]
) -> object:
    """Turn every envelope in ``value`` into an :class:`Expr`, compiled.

    A source that does not compile is a mistake in the document, reported
    beside the document's other mistakes rather than saved for the mount:
    nothing about the file can make a bad parse succeed later.
    """
    parsed, problems = parse_expressions(value)
    issues.extend(
        ConfigIssue((*here, *where), f"invalid expression: {reason}")
        for where, _source, reason in problems
    )
    return parsed


def _reject_expressions(
    row: Mapping[object, object],
    here: tuple[str | int, ...],
    issues: list[ConfigIssue],
) -> None:
    """SEM-001: two fields may hold an expression, and every other one may not.

    Checked at any depth, because ``isolate`` and ``intercept`` are structures:
    an expression buried in one of them is the same escape as an expression in
    ``name``, arrived at more slowly.
    """
    for key, value in row.items():
        if key in _COMPUTABLE:
            continue
        label = key if isinstance(key, str | int) else str(key)
        issues.extend(
            ConfigIssue(
                (*here, label, *found), "an expression is not permitted in this field"
            )
            for found in expression_paths(value)
        )


def _names(
    row: Mapping[object, object],
    here: tuple[str | int, ...],
    issues: list[ConfigIssue],
) -> tuple[str, ...] | None:
    value = row.get("inject")
    if value is None:
        return None  # "read the target's own declaration", which is not ()
    if not _string_list(value):
        issues.append(ConfigIssue((*here, "inject"), "must be a list of service names"))
        return None
    return tuple(value)


def _isolation(
    row: Mapping[object, object],
    here: tuple[str | int, ...],
    issues: list[ConfigIssue],
) -> Isolation:
    value = row.get("isolate")
    if value is None:
        return ()
    if isinstance(value, Mapping) and all(
        isinstance(key, str) and (label is None or isinstance(label, str))
        for key, label in value.items()
    ):
        return {str(key): label for key, label in value.items()}
    if _string_list(value):
        return tuple(value)
    issues.append(
        ConfigIssue(
            (*here, "isolate"),
            "must be a list of service names or a mapping of name to realm label",
        )
    )
    return ()


def _interception(
    row: Mapping[object, object],
    here: tuple[str | int, ...],
    issues: list[ConfigIssue],
) -> Interception | None:
    value = row.get("intercept")
    if value is None:
        return None
    if isinstance(value, Mapping) and all(
        isinstance(key, str) and isinstance(config, Mapping)
        for key, config in value.items()
    ):
        return {str(key): dict(config) for key, config in value.items()}
    issues.append(
        ConfigIssue(
            (*here, "intercept"),
            "must be a mapping of service name to config",
        )
    )
    return None


def _string_list(value: object) -> TypeGuard[Sequence[str]]:
    return (
        isinstance(value, Sequence)
        and not isinstance(value, str | bytes)
        and all(isinstance(item, str) for item in value)
    )


# --------------------------------------------------------------------------
# Where a target name comes from (the seam)
# --------------------------------------------------------------------------


class TargetSource(Definition):
    """The contract for turning an entry's ``name`` into something mountable.

    A seam rather than a method on the loader: a test binds one that hands
    back synthetic targets, and a locked-down deployment binds one that
    refuses anything off an allow-list. Neither is reachable if resolution is
    a method.
    """

    name = "loader.targets"

    @abc.abstractmethod
    def resolve(self, name: str, /) -> PluginTarget:
        """The target ``name`` stands for, or raise :class:`LookupError`."""


class ImportTargets(TargetSource):
    """The default source: ``pkg.mod:attr``, a bare module, or a file path.

    A module object is already a valid plugin target -- mounting looks for
    ``apply`` structurally -- so the bare-module form needs no unwrapping.
    """

    def resolve(self, name: str, /) -> PluginTarget:
        if ":" in name:
            module_name, _, attribute = name.partition(":")
            module = self._import(module_name)
            found = getattr(module, attribute, None)
            if found is None:
                msg = f"{module_name!r} has no attribute {attribute!r}"
                raise LookupError(msg)
            return found
        if name.endswith(".py") or "/" in name:
            return self._from_file(Path(name))
        return self._import(name)

    def _import(self, name: str) -> PluginTarget:
        try:
            return importlib.import_module(name)
        except ImportError as exc:
            msg = f"could not import {name!r}: {exc}"
            raise LookupError(msg) from exc

    def _from_file(self, path: Path) -> PluginTarget:
        if not path.is_file():
            msg = f"no such plugin file: {path}"
            raise LookupError(msg)
        stem = f"cordis_loader_{path.stem}"
        spec = importlib.util.spec_from_file_location(stem, path)
        if spec is None or spec.loader is None:
            msg = f"{path} is not importable as a module"
            raise LookupError(msg)
        module = importlib.util.module_from_spec(spec)
        sys.modules[stem] = module
        spec.loader.exec_module(module)
        return module


# --------------------------------------------------------------------------
# Where a config file comes from
# --------------------------------------------------------------------------


@runtime_checkable
class FileSource(Protocol):
    """Anything that can produce an entry list.

    Each implementation imports its own parser inside :meth:`read`, so the
    loader never imports one and the YAML source costs nothing on an
    installation without PyYAML.
    """

    def read(self) -> tuple[Entry, ...]: ...


@dataclass(frozen=True, slots=True)
class MappingSource:
    """Entries already in memory, still validated like anything else."""

    raw: object

    def read(self) -> tuple[Entry, ...]:
        return read_entries(self.raw)


@dataclass(frozen=True, slots=True)
class JsonSource:
    """A JSON file holding a list, or a mapping with ``key`` in it."""

    path: Path | str
    key: str | None = None

    def read(self) -> tuple[Entry, ...]:
        import json

        text = Path(self.path).read_text(encoding="utf-8")
        return read_entries(_select(json.loads(text), self.key))


@dataclass(frozen=True, slots=True)
class TomlSource:
    """A TOML file. Its top level is a table, so the list lives under a key."""

    path: Path | str
    key: str | None = "plugins"

    def read(self) -> tuple[Entry, ...]:
        import tomllib

        loaded = tomllib.loads(Path(self.path).read_text(encoding="utf-8"))
        return read_entries(_select(loaded, self.key))


@dataclass(frozen=True, slots=True)
class YamlSource:
    """A YAML file. Needs PyYAML, which nothing else here does.

    Read with a ``SafeLoader`` that knows one extra tag, ``!expr``. Everything
    it refused before, it still refuses; the tag is the one YAML-native way to
    write an expression, and it reads to the same :class:`~cordis.expr.Expr`
    the portable ``{'$expr': ...}`` form does.
    """

    path: Path | str
    key: str | None = None

    def read(self) -> tuple[Entry, ...]:
        import yaml

        text = Path(self.path).read_text(encoding="utf-8")
        loaded = yaml.load(text, Loader=yaml_loader())  # a SafeLoader subclass
        return read_entries(_select(loaded, self.key))


def _select(loaded: object, key: str | None) -> object:
    if key is None or not isinstance(loaded, Mapping):
        return loaded
    return loaded.get(key, ())


# --------------------------------------------------------------------------
# The loader
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Mounted:
    """What the loader remembers about one live entry."""

    entry: Entry
    handle: PluginHandle


@dataclass(frozen=True, slots=True)
class _Step:
    """One planned change, and the changes that belong underneath it."""

    path: str
    entry: Entry
    action: str
    target: PluginTarget | None = None
    children: tuple[_Step, ...] = ()
    prepare: ConfigPreparer | None = None


@dataclass(slots=True)
class _Plan:
    """A whole reconcile, decided before any of it is applied."""

    steps: tuple[_Step, ...] = ()
    kill: list[str] = field(default_factory=list)
    mounted: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    disposed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    failed: list[EntryFailure] = field(default_factory=list)
    doomed: set[str] = field(default_factory=set)

    def report(self) -> ReconcileReport:
        return ReconcileReport(
            mounted=tuple(self.mounted),
            updated=tuple(self.updated),
            disposed=tuple(self.disposed),
            unchanged=tuple(self.unchanged),
            failed=tuple(self.failed),
        )


class LoaderService(Service):
    """Mounts an entry list, and reconciles it against the live tree.

    Mounted as a plugin like anything else; its children *are* the entries, so
    disposing the loader disposes the application it loaded.
    """

    name = "loader"

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self._live: dict[str, _Mounted] = {}
        self._lock = asyncio.Lock()
        self._default: TargetSource | None = None

    # -- what is running ---------------------------------------------------

    def live(self) -> tuple[str, ...]:
        """Every mounted entry's dotted path, in mount order."""
        return tuple(self._live)

    def handle_for(self, path: str, /) -> PluginHandle | None:
        """The instance mounted for ``path``, if one is."""
        found = self._live.get(path)
        return None if found is None else found.handle

    # -- reconciling -------------------------------------------------------

    async def reconcile(
        self, entries: Sequence[Entry], /, *, dry_run: bool = False
    ) -> ReconcileReport:
        """Make the live tree match ``entries``, and say what that took.

        The whole diff is decided synchronously, before anything moves. Then
        the removals are awaited, then the updates, then every mount lands in
        one synchronous section -- a monotone transition rather than an
        interleaving. ``quiesce()`` remains the supported way to ask for a
        settled tree; what is guaranteed here is that no observer sees the
        additions of this reconcile before the removals of it (SEM-008).
        """
        async with self._lock:
            plan = self._plan(entries)
            if not dry_run:
                await self._apply(plan)
            return plan.report()

    async def remount(
        self,
        paths: Sequence[str],
        /,
        *,
        between: Callable[[], object] | None = None,
    ) -> ReconcileReport:
        """Rebuild the named entries, leaving every other one alone.

        What no edit to the entry list can express: the file says the same
        thing and means something different, because the code behind it
        changed. ``between`` runs after every named instance is gone and
        before any is rebuilt -- the moment a reimport belongs in (hot-reload
        SEM-002) -- and targets are resolved again afterwards, so what gets
        mounted is the code that was just loaded rather than the code the plan
        was made against.
        """
        async with self._lock:
            plan = _Plan()
            for path in paths:
                if path in self._live:
                    self._doom(path, plan)
            wanted: set[str] = set()
            desired = tuple(
                found.entry for path, found in self._live.items() if "." not in path
            )
            plan.steps = self._walk(desired, "", plan, wanted)
            await self._apply(plan, between=between)
            return plan.report()

    # -- deciding ----------------------------------------------------------

    def _plan(self, entries: Sequence[Entry], /) -> _Plan:
        plan = _Plan()
        wanted: set[str] = set()
        plan.steps = self._walk(entries, "", plan, wanted)
        for path in tuple(self._live):
            if path not in wanted:
                self._doom(path, plan)
        return plan

    def _walk(
        self, entries: Sequence[Entry], prefix: str, plan: _Plan, wanted: set[str]
    ) -> tuple[_Step, ...]:
        steps: list[_Step] = []
        # Built once per level: what one entry's expressions may read of its
        # siblings is the level it was written at, not the whole document.
        view = _siblings(entries)
        for entry in entries:
            path = f"{prefix}{entry.id}"
            if self._hidden(entry, path, plan, view):
                continue  # preserved by the file, absent from the tree (SEM-005)
            wanted.add(path)
            step = self._step(entry, path, plan, wanted, view)
            if step is not None:
                steps.append(step)
        return tuple(steps)

    def _hidden(
        self, entry: Entry, path: str, plan: _Plan, view: Mapping[str, object]
    ) -> bool:
        """Whether the file says this entry should not be in the tree.

        A `disabled` expression is decided here, against the loader's own
        context, at every mount decision (config-expressions SEM-004):
        evaluating it in the plugin's context would mean mounting the plugin to
        find out whether to mount it. An expression that cannot be decided
        leaves the entry out and records the failure -- "cannot decide"
        resolves to "not mounted" rather than to "left as it was", so the tree
        after a reconcile is always a tree the file could have described.
        """
        flag = entry.disabled
        if not isinstance(flag, Expr):
            return flag
        where = ExpressionError(path, "disabled", flag.source, "must be true or false")
        try:
            decided = evaluate(
                flag,
                self._environment(view),
                functions=self._functions(self.ctx),
                entry_id=path,
                field="disabled",
            )
        except ExpressionError as exc:
            plan.failed.append(EntryFailure(path, "disabled expression", exc))
            return True
        if not isinstance(decided, bool):
            plan.failed.append(EntryFailure(path, "disabled expression", where))
            return True
        return decided

    def _step(
        self,
        entry: Entry,
        path: str,
        plan: _Plan,
        wanted: set[str],
        view: Mapping[str, object],
    ) -> _Step | None:
        live = self._current(path, plan)
        if live is not None and _shape(live.entry) != _shape(entry):
            # A changed target, injection, isolation or interception cannot be
            # adopted in place: all four are decided when the instance is
            # built. Disposing first keeps the transition monotone.
            self._doom(path, plan)
            live = None

        if live is None:
            target = self._target(entry, path, plan)
            if target is None:
                return None
            plan.mounted.append(path)
            children = self._walk(entry.group or (), f"{path}.", plan, wanted)
            prepare = self._preparer(path, view) if _computes(entry) else None
            return _Step(path, entry, _MOUNT, target, children, prepare)

        if not entry.is_group and live.entry.config != entry.config:
            plan.updated.append(path)
            return _Step(path, entry, _UPDATE)

        plan.unchanged.append(path)
        children = self._walk(entry.group or (), f"{path}.", plan, wanted)
        # A group whose own settings are unchanged is still the place its
        # children mount, so it stays in the plan when any of them moved.
        return _Step(path, entry, _KEEP, None, children) if children else None

    def _current(self, path: str, plan: _Plan) -> _Mounted | None:
        """What is live at ``path``, ignoring what this plan already removed."""
        found = self._live.get(path)
        return None if found is None or path in plan.doomed else found

    def _doom(self, path: str, plan: _Plan) -> None:
        """Mark ``path`` and everything under it for disposal.

        Only the top of the subtree is disposed -- a fiber disposes its own
        descendants -- but every path that goes away is reported, so a group
        replaced by a plugin names each child it took with it.
        """
        if path in plan.doomed:
            return
        plan.kill.append(path)
        for step in _subtree(self._live, path):
            plan.doomed.add(step)
            plan.disposed.append(step)

    def _target(self, entry: Entry, path: str, plan: _Plan) -> PluginTarget | None:
        """Resolve and pre-validate, or record why this entry cannot mount.

        Both halves of SEM-006 happen here, before anything is created, which
        is also what makes ``dry_run=True`` able to find a bad row without
        touching the tree. The config half is skipped for an entry whose config
        is computed: there is nothing to validate until the instance's own
        environment exists, so that entry fails at load instead, through the
        path a failing body already uses (config-expressions SEM-004).
        """
        if entry.is_group:
            return _group_body(entry.id)
        try:
            target = self._targets().resolve(entry.name)
        except Exception as exc:
            plan.failed.append(EntryFailure(path, "unresolvable target", exc))
            return None
        try:
            normalise(target)
            if not _computes(entry):
                resolve_config(schema_of(target), entry.config, plugin=path)
        except InvalidPluginError as exc:
            plan.failed.append(EntryFailure(path, "not a plugin", exc))
            return None
        except ConfigValidationError as exc:
            plan.failed.append(EntryFailure(path, "invalid config", exc))
            return None
        return target

    # -- expressions (config-expressions SEM-004) --------------------------

    def _environment(self, view: Mapping[str, object]) -> dict[str, object]:
        """What an expression can see: the process environment and its siblings.

        Read fresh each time rather than captured once, because "the port from
        the environment" is a question about the environment now. Determinism
        (SEM-003) is determinism *given the inputs*, and this is one of them.
        """
        return {"env": MappingProxyType(dict(os.environ)), "entries": view}

    def _functions(self, ctx: Context) -> dict[str, Callable[..., object]]:
        """The allow-list, plus two readers bound to ``ctx``.

        ``service`` and ``has`` are functions rather than names in the
        environment because a function is the one shape the grammar lets a
        config file call, and calls on what they *return* are a compile-time
        rejection: an expression can read a service, never work one.
        """
        out: dict[str, Callable[..., object]] = dict(FUNCTIONS)
        source: FunctionSource | None = self.ctx.get(FunctionSource.name)
        if source is not None:
            out.update(source.functions())
        out["service"] = lambda name: ctx.get(str(name))
        out["has"] = lambda name: ctx.get(str(name)) is not None
        return out

    def _preparer(self, path: str, view: Mapping[str, object]) -> ConfigPreparer:
        """The closure the kernel calls while building this entry's context.

        It is handed the instance's own context, so `service('shell')` inside a
        `config` expression resolves what the *instance* resolves -- through
        its isolation and its interceptions -- rather than what the loader
        does. That difference is the whole of SEM-004.
        """

        def prepare(ctx: Context, config: object) -> object:
            return substitute(
                config,
                self._environment(view),
                functions=self._functions(ctx),
                entry_id=path,
                field="config",
            )

        return prepare

    def _targets(self) -> TargetSource:
        # Asked for by name rather than by class: the Definition is abstract,
        # and what matters is what is bound under the seam's name, exactly as
        # for every other capability seam.
        bound: TargetSource | None = self.ctx.get(TargetSource.name)
        if bound is not None:
            return bound
        if self._default is None:
            self._default = ImportTargets(self.ctx)
        return self._default

    # -- applying ----------------------------------------------------------

    async def _apply(
        self, plan: _Plan, *, between: Callable[[], object] | None = None
    ) -> None:
        for path in plan.kill:
            found = self._live.get(path)
            if found is not None:
                await found.handle.dispose()
        for path in plan.doomed:
            self._live.pop(path, None)

        if between is not None:
            between()
            # Whatever `between` did, it may have replaced the objects the plan
            # points at; ask for them again rather than mounting what is by now
            # the previous version.
            plan.steps = self._refresh(plan.steps, plan)

        touched: list[str] = []
        for path in plan.updated:
            found = self._live[path]
            entry = _entry_at(plan.steps, path)
            if entry is None:  # pragma: no cover -- the plan named it
                continue
            found.entry = entry
            await found.handle.update(entry.config)
            touched.append(path)

        self._mount(plan.steps, self.ctx.plugin, touched)

        for path in touched:
            found = self._live.get(path)
            if found is None:
                continue
            try:
                await found.handle
            except Exception as exc:
                # The instance is mounted and FAILED; the report says which
                # entry it was, because "which row is broken" is a question
                # only the loader can answer (SEM-006).
                plan.failed.append(EntryFailure(path, "start failed", exc))

    def _refresh(self, steps: Sequence[_Step], plan: _Plan) -> tuple[_Step, ...]:
        """Resolve every planned mount again, dropping the ones that no longer do.

        A target that resolved when the plan was made and does not now is the
        shape a broken edit takes: the entry fails, alone, exactly as it would
        have on a first mount (SEM-006).
        """
        out: list[_Step] = []
        for step in steps:
            if step.action != _MOUNT:
                out.append(replace(step, children=self._refresh(step.children, plan)))
                continue
            target = self._target(step.entry, step.path, plan)
            if target is None:
                for gone in _paths_of(step):
                    plan.mounted.remove(gone)
                continue
            out.append(
                replace(
                    step, target=target, children=self._refresh(step.children, plan)
                )
            )
        return tuple(out)

    def _mount(
        self,
        steps: Sequence[_Step],
        mount: Callable[..., PluginHandle],
        touched: list[str],
    ) -> None:
        """Every mount of one reconcile, in one synchronous section.

        Mounting arms a fiber rather than awaiting it, so the concurrency
        SEM-004 asks for is the fibers' own and file order cannot become load
        order.
        """
        for step in steps:
            under = mount
            if step.action == _MOUNT:
                handle = mount(
                    step.target,
                    None if step.entry.is_group else step.entry.config,
                    requires=step.entry.inject,
                    isolate=step.entry.isolate,
                    intercept=step.entry.intercept,
                    prepare=step.prepare,
                )
                self._live[step.path] = _Mounted(step.entry, handle)
                touched.append(step.path)
                under = handle.plugin
            elif step.children:
                found = self._live.get(step.path)
                if found is None:  # pragma: no cover -- KEEP means it is live
                    continue
                found.entry = step.entry
                under = found.handle.plugin
            self._mount(step.children, under, touched)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _computes(entry: Entry) -> bool:
    """Whether this entry's config has to be computed before it can be used."""
    return not entry.is_group and bool(expression_paths(entry.config))


def _siblings(entries: Sequence[Entry]) -> Mapping[str, object]:
    """What one entry's expressions may read of the entries beside it.

    The literal part only: a field that is itself computed comes back as
    :class:`~cordis.expr.Opaque` and fails the expression that reads it. That
    removes cycles from the language by construction, rather than detecting
    them, and it removes evaluation order from the answer -- two entries that
    each computed from the other would otherwise mean different things
    depending on which the planner reached first.
    """
    return MappingProxyType(
        {
            entry.id: opaque(entry.config, f"{entry.id}.config is computed")
            for entry in entries
        }
    )


def _shape(entry: Entry) -> tuple[object, ...]:
    """What about an entry can only be changed by remounting it.

    Config is not in it: config is what :meth:`PluginHandle.update` exists
    for, and comparing it here would turn every edit into a remount.
    """
    return (entry.name, entry.inject, entry.isolate, entry.intercept)


def _subtree(live: Mapping[str, _Mounted], path: str) -> Iterator[str]:
    """``path`` and every live entry beneath it, deepest last."""
    under = f"{path}."
    for step in live:
        if step == path or step.startswith(under):
            yield step


def _paths_of(step: _Step) -> Iterator[str]:
    """``step``'s path and every path planned beneath it."""
    yield step.path
    for child in step.children:
        yield from _paths_of(child)


def _entry_at(steps: Sequence[_Step], path: str) -> Entry | None:
    for step in steps:
        if step.path == path:
            return step.entry
        found = _entry_at(step.children, path)
        if found is not None:
            return found
    return None


def _group_body(entry_id: str) -> PluginTarget:
    """A plugin whose whole job is to be a place in the tree (SEM-007).

    Named after the entry so a group is legible in a diagnostics snapshot --
    which is also how the tests read the shape of the tree back without
    consulting the loader's own bookkeeping.
    """

    def body(ctx: Context) -> None:
        """A group holds children; it has no behaviour of its own."""

    body.__name__ = f"{GROUP}:{entry_id}"
    body.__qualname__ = body.__name__
    return body
