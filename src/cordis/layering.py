"""Config layering: bundles, profiles and patches fold into one entry list.

Three rules carry the design.

A patch replaces a field's whole value and never merges into it. Deep merge is
ambiguous the moment a list or a null appears, and "what is my effective
config" stops being answerable without running the merge; restating a whole
value is blunt, but it is visible in review and it is the same answer every
time.

A patch that cannot be applied is an error, not a warning. Upstream skips it,
which makes a renamed entry silently discard every downstream customisation of
it -- a failure that shows up as "my setting is ignored" and nowhere else.

The fold is pure and the provenance is accumulated as it goes. `--dump-config`
calls this function and nothing else, so the table it prints cannot disagree
with the tree that mounts.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, TypeAlias

from cordis.config import ConfigIssue
from cordis.errors import ConfigValidationError, PatchTargetError
from cordis.loader import Entry, read_entries

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = [
    "BASE",
    "Fields",
    "Layer",
    "Patch",
    "Resolution",
    "read_layer",
    "resolve",
    "wrote",
]

#: The layer name every field carries until something writes it. Spelled with
#: brackets so it cannot collide with a file path used as a layer source.
BASE: Final = "<base>"

Fields: TypeAlias = Mapping[str, object]

#: What a patch may not set, and why it is refused at construction rather than
#: in the fold: rewriting an id would rekey the entry every later layer
#: targets, and replacing a group's children wholesale is what `insert` is for.
_SEALED: Final = frozenset({"id", "group"})

_EMPTY: Final[Fields] = MappingProxyType({})

_NOTHING: Final[Mapping[tuple[str, str], str]] = MappingProxyType({})

_FIELD_NAMES: Final = tuple(found.name for found in fields(Entry))

_KEYS: Final = frozenset({"id", "name", "fields", "insert"})


# --------------------------------------------------------------------------
# What a layer says
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Patch:
    """One instruction from one layer.

    ``id`` is the dotted path the loader uses everywhere else it names a
    nested entry (``group.child``), not a bare id: a bare id is not unique
    across groups, and two spellings of "which entry" would be one too many.

    A patch with an ``id`` and neither fields nor rows is legal, and useful:
    it asserts that the entry exists and is named what the layer thinks it is.
    """

    id: str | None = None
    name: str | None = None
    # `field(default_factory=...)` rather than `= _EMPTY`: Python 3.11's
    # dataclasses refuse a `mappingproxy` default as mutable, and the port
    # targets 3.11. The factory hands back the one shared empty proxy, so
    # nothing is allocated per instance either.
    fields: Fields = field(default_factory=lambda: _EMPTY)
    insert: tuple[Entry, ...] = ()

    def __post_init__(self) -> None:
        if self.fields and self.id is None:
            msg = "a patch that sets fields needs an id to set them on"
            raise ValueError(msg)
        for sealed in sorted(_SEALED & set(self.fields)):
            msg = f"a patch cannot set {sealed!r}"
            raise ValueError(msg)
        unknown = sorted(set(self.fields) - set(_FIELD_NAMES))
        if unknown:
            msg = f"not entry fields: {', '.join(repr(key) for key in unknown)}"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class Layer:
    """One party's contribution, named by where it came from.

    ``source`` is what every failure and every provenance row reports, so it
    should be the artefact an operator can open: a file path, a bundle name,
    ``"cli"``.
    """

    source: str
    patches: tuple[Patch, ...] = ()


@dataclass(frozen=True, slots=True)
class Resolution:
    """The folded entry list, and who wrote each field of it.

    ``entries`` is exactly what :meth:`LoaderService.reconcile` takes; there is
    no intermediate representation, and nothing here needs a running host.
    """

    entries: tuple[Entry, ...]
    provenance: Mapping[tuple[str, str], str] = field(
        default_factory=lambda: _NOTHING  # a mappingproxy default; see `Patch`
    )


# --------------------------------------------------------------------------
# The fold (SEM-001 .. SEM-007)
# --------------------------------------------------------------------------


def resolve(base: Sequence[Entry], layers: Sequence[Layer], /) -> Resolution:
    """Fold ``layers`` onto ``base`` in order, or raise naming the offender.

    Pure in both arguments: entries are frozen and only the path from the root
    to a patched entry is rebuilt, so an entry no layer touched comes out as
    the same object it went in as. That is what makes an empty layer cost
    nothing and what keeps the loader's diff from seeing a change that is not
    one (SEM-007).
    """
    entries = tuple(base)
    provenance: dict[tuple[str, str], str] = {}
    _claim(entries, "", BASE, provenance)
    for layer in layers:
        for index, patch in enumerate(layer.patches):
            entries = _patch(entries, patch, layer, f"patches[{index}]", provenance)
    return Resolution(entries, MappingProxyType(provenance))


def wrote(resolution: Resolution, source: str, /) -> tuple[tuple[str, str], ...]:
    """Every ``(path, field)`` ``source`` is responsible for, sorted.

    The question a layered deployment is actually debugged with: not "what is
    the config" but "what is this layer accounting for".
    """
    return tuple(
        sorted(key for key, found in resolution.provenance.items() if found == source)
    )


def _claim(
    entries: Iterable[Entry],
    prefix: str,
    source: str,
    provenance: dict[tuple[str, str], str],
) -> None:
    """Record ``source`` as the writer of every field of every entry below."""
    for entry in entries:
        path = f"{prefix}{entry.id}"
        for name in _FIELD_NAMES:
            provenance[path, name] = source
        if entry.group is not None:
            _claim(entry.group, f"{path}.", source, provenance)


def _patch(
    entries: tuple[Entry, ...],
    patch: Patch,
    layer: Layer,
    label: str,
    provenance: dict[tuple[str, str], str],
) -> tuple[Entry, ...]:
    if patch.id is None:
        # No target means the root list, which is the one place an insert can
        # go without an entry to go into.
        return _insert(entries, patch, layer, label, "", provenance)
    return _descend(entries, patch.id.split("."), "", patch, layer, label, provenance)


def _descend(
    entries: tuple[Entry, ...],
    steps: Sequence[str],
    prefix: str,
    patch: Patch,
    layer: Layer,
    label: str,
    provenance: dict[tuple[str, str], str],
) -> tuple[Entry, ...]:
    """Rebuild only the branch that leads to the target (SEM-001, SEM-007)."""
    head, rest = steps[0], steps[1:]
    for index, entry in enumerate(entries):
        if entry.id != head:
            continue
        path = f"{prefix}{head}"
        if rest:
            children = _group(entry, patch, layer, label)
            found = _descend(
                children, rest, f"{path}.", patch, layer, label, provenance
            )
            return _swap(entries, index, replace(entry, group=found))
        return _swap(
            entries, index, _rewrite(entry, patch, layer, label, path, provenance)
        )
    raise _failure(patch, layer, label, "no earlier layer defines it")


def _rewrite(
    entry: Entry,
    patch: Patch,
    layer: Layer,
    label: str,
    path: str,
    provenance: dict[tuple[str, str], str],
) -> Entry:
    """Check the name, replace the named fields, then insert (SEM-002, SEM-005)."""
    if patch.name is not None and entry.name != patch.name:
        reason = f"it is named {entry.name!r}, not {patch.name!r}"
        raise _failure(patch, layer, label, reason)
    if patch.fields:
        # The one dynamic step in the fold. What a field's value may be is the
        # loader reader's question and it answered it when the layer was read;
        # a hand-built patch is trusted exactly as far as a hand-built entry.
        values: dict[str, Any] = dict(patch.fields)
        entry = replace(entry, **values)
        for name in patch.fields:
            provenance[path, name] = layer.source
    if not patch.insert:
        return entry
    children = _group(entry, patch, layer, label)
    found = _insert(children, patch, layer, label, f"{path}.", provenance)
    provenance[path, "group"] = layer.source
    return replace(entry, group=found)


def _insert(
    entries: tuple[Entry, ...],
    patch: Patch,
    layer: Layer,
    label: str,
    prefix: str,
    provenance: dict[tuple[str, str], str],
) -> tuple[Entry, ...]:
    """Append ``patch.insert``, refusing an id the target already has (SEM-003)."""
    taken = {entry.id for entry in entries}
    for row in patch.insert:
        if row.id in taken:
            reason = f"it already defines an entry with id {row.id!r}"
            raise _failure(patch, layer, label, reason)
        taken.add(row.id)
    _claim(patch.insert, prefix, layer.source, provenance)
    return (*entries, *patch.insert)


def _group(entry: Entry, patch: Patch, layer: Layer, label: str) -> tuple[Entry, ...]:
    """The children of a group, or the failure the alternative would delete."""
    if entry.group is None:
        raise _failure(patch, layer, label, "it is not a group")
    return entry.group


def _swap(entries: tuple[Entry, ...], index: int, entry: Entry) -> tuple[Entry, ...]:
    return (*entries[:index], entry, *entries[index + 1 :])


def _failure(patch: Patch, layer: Layer, label: str, reason: str) -> PatchTargetError:
    target = patch.id if patch.id is not None else ""
    return PatchTargetError(label, layer.source, target, reason)


# --------------------------------------------------------------------------
# Reading a patch document
# --------------------------------------------------------------------------


def read_layer(raw: object, source: str, /) -> Layer:
    """Validate one patch document in full, or raise naming every problem.

    A patch's fields are validated by handing them to the loader's own row
    reader as a synthetic entry row, so `config`, `inject`, `isolate`,
    `intercept` and a config expression mean in a patch exactly what they mean
    in an entry -- there is no second implementation to drift from the first.
    """
    rows = raw.get("patches", ()) if isinstance(raw, Mapping) else raw
    if not isinstance(rows, list | tuple):
        raise ConfigValidationError(source, [ConfigIssue((), "must be a list")])
    issues: list[ConfigIssue] = []
    patches = tuple(_read_patch(row, index, issues) for index, row in enumerate(rows))
    if issues:
        raise ConfigValidationError(source, issues)
    return Layer(source=source, patches=patches)


def _read_patch(row: object, index: int, issues: list[ConfigIssue]) -> Patch:
    if not isinstance(row, Mapping):
        issues.append(ConfigIssue((index,), "must be a mapping"))
        return Patch()
    issues.extend(
        ConfigIssue((index, key), "is not a patch field")
        for key in sorted(set(row) - _KEYS)
    )
    target = _text(row, "id", index, issues)
    name = _text(row, "name", index, issues)
    return Patch(
        id=target,
        name=name,
        fields=_read_fields(row.get("fields"), target, name, index, issues),
        insert=_read_insert(row.get("insert"), index, issues),
    )


def _text(
    row: Mapping[str, object], key: str, index: int, issues: list[ConfigIssue]
) -> str | None:
    found = row.get(key)
    if found is None:
        return None
    if not isinstance(found, str):
        issues.append(ConfigIssue((index, key), "must be a string"))
        return None
    return found


def _read_fields(
    raw: object,
    target: str | None,
    name: str | None,
    index: int,
    issues: list[ConfigIssue],
) -> Fields:
    """Read field values through the entry reader, then keep only what was set."""
    if raw is None:
        return _EMPTY
    if not isinstance(raw, Mapping):
        issues.append(ConfigIssue((index, "fields"), "must be a mapping"))
        return _EMPTY
    issues.extend(
        ConfigIssue((index, "fields", key), "cannot be patched")
        for key in sorted(_SEALED & set(raw))
    )
    wanted = {key: value for key, value in raw.items() if key not in _SEALED}
    if not wanted:
        return _EMPTY
    if target is None:
        issues.append(ConfigIssue((index, "fields"), "needs an id to be set on"))
        return _EMPTY
    # The synthetic row exists only to be validated: its id is a placeholder
    # the reader needs, and its name is the patch's own expectation when it
    # made one, so a name that is checked is also a name that is validated.
    row = {"id": target.rpartition(".")[2], "name": name or "x", **wanted}
    try:
        entry = read_entries([row])[0]
    except ConfigValidationError as exc:
        issues.extend(_reroot(exc, index, wanted))
        return _EMPTY
    return {key: getattr(entry, key) for key in wanted}


def _reroot(
    exc: ConfigValidationError, index: int, wanted: Mapping[str, object]
) -> list[ConfigIssue]:
    """Move issues from the synthetic row back onto the patch that caused them.

    An issue about a key the patch did not write belongs to the placeholder,
    not to the operator, and reporting it would send them looking for a field
    they never typed.
    """
    found: list[ConfigIssue] = []
    for issue in exc.issues:
        path = tuple(issue.path)[1:]  # drop the synthetic row's own index
        if path and path[0] not in wanted:
            continue
        found.append(ConfigIssue((index, "fields", *path), issue.message))
    return found


def _read_insert(
    raw: object, index: int, issues: list[ConfigIssue]
) -> tuple[Entry, ...]:
    if raw is None:
        return ()
    try:
        return read_entries(raw)
    except ConfigValidationError as exc:
        issues.extend(
            ConfigIssue((index, "insert", *tuple(issue.path)), issue.message)
            for issue in exc.issues
        )
        return ()
