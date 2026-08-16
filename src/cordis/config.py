"""Config validation: a plugin never starts half-configured.

Implements ``spec/capabilities/07-config-validation.yaml``.

A plugin declares a schema by naming it ``Config`` -- the same name it uses as
the annotation on ``apply(ctx, config: Config)`` -- or by wearing
:func:`config_schema`. The mount resolves the config against that schema
*before* the body runs, so a mistyped field fails one fiber cleanly instead of
surfacing three layers down as an ``AttributeError`` on ``None``.

Three things shape this module.

**The core knows a protocol, not a library** (SEM-007). Anything with a
``validate(raw) -> ConfigResult`` is a schema; there is nothing to register and
nothing to subclass, so an adapter for pydantic or jsonschema is a shim that
lives with whatever package takes that dependency. :func:`from_dataclass` is
the one adapter shipped here, because the standard library is the one
dependency this package already has.

**Validation is synchronous** (SEM-004). A schema that wants to await is
refused where it is declared. Deciding a fiber's fate is on the synchronous
path that runs between "mount was called" and "the body ran"; an await there
would make startup order depend on how fast a remote schema service answered.

**The adapter checks; it does not coerce.** ``"30"`` is not an ``int``. A
coercion table is a validation library, and SEM-007 exists so this package does
not grow one. What it will not check, it refuses at declaration time -- an
author learns their annotation is unsupported when they write it, not when a
config arrives in production.
"""

from __future__ import annotations

import dataclasses
import inspect
import types
import typing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Generic, Protocol, TypeVar, get_args

from cordis.errors import (
    AsyncValidationError,
    ConfigValidationError,
    InvalidPluginError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

__all__ = [
    "CONFIG_SCHEMA_ATTR",
    "DECLARED_SCHEMA_ATTR",
    "ConfigIssue",
    "ConfigResult",
    "ConfigSchema",
    "config_schema",
    "from_dataclass",
    "render_path",
    "resolve_config",
    "schema_of",
]

#: What a plugin declares its schema under. The same name it annotates with,
#: which is the point: one name, both roles.
CONFIG_SCHEMA_ATTR: Final = "Config"

#: What :func:`config_schema` writes, so a decorated function does not have to
#: carry a public attribute it never uses.
DECLARED_SCHEMA_ATTR: Final = "__cordis_config__"

T = TypeVar("T")
F = TypeVar("F")


# --------------------------------------------------------------------------
# What a schema answers with
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConfigIssue:
    """One thing wrong, and where.

    The path is a tuple of keys and indices rather than a rendered string, so
    it can be walked back into the structure the operator submitted. Rendering
    is a presentation decision made once, in :meth:`__str__`.
    """

    path: tuple[str | int, ...]
    message: str

    def __str__(self) -> str:
        return f"{render_path(self.path)}: {self.message}"


def render_path(path: Sequence[str | int]) -> str:
    """``("plugins", 2, "timeout_ms")`` -> ``plugins[2].timeout_ms``."""
    if not path:
        return "<config>"
    rendered = str(path[0])
    for step in path[1:]:
        rendered += f"[{step}]" if isinstance(step, int) else f".{step}"
    return rendered


@dataclass(frozen=True, slots=True)
class ConfigResult(Generic[T]):
    """Either a resolved value or the reasons there isn't one.

    Both halves are present rather than one being a union, because a schema
    that reports issues has no value and a schema that resolved has no issues
    -- and a caller that has to unpack a union to find out which is a caller
    that will forget to.
    """

    value: T | None
    issues: tuple[ConfigIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.issues

    @classmethod
    def accepted(cls, value: T) -> ConfigResult[T]:
        return cls(value, ())

    @classmethod
    def rejected(cls, *issues: ConfigIssue) -> ConfigResult[T]:
        if not issues:  # a rejection with no reason is a defect in the schema
            msg = "a rejected config must carry at least one issue"
            raise ValueError(msg)
        return cls(None, issues)


class ConfigSchema(Protocol[T]):
    """Anything that can say whether a config is acceptable.

    Structural on purpose (SEM-007): a schema written for another framework, or
    one written this afternoon as a class with a method, is a schema here
    without importing this module.

    Invariant in ``T``, not covariant. The value flows outward, which is the
    shape covariance is for, but it flows out inside a :class:`ConfigResult`,
    and that class takes ``T`` in the parameters of :meth:`ConfigResult.accepted`
    -- a covariant variable is not allowed there. Nothing in the runtime is
    generic over a schema anyway: it holds ``ConfigSchema[Any]`` and hands the
    value to a body that annotated it itself.
    """

    def validate(self, raw: object, /) -> ConfigResult[T]: ...


# --------------------------------------------------------------------------
# Declaring a schema, and reading the declaration
# --------------------------------------------------------------------------


def config_schema(schema: ConfigSchema[Any] | type) -> Callable[[F], F]:
    """Attach ``schema`` to a plugin that cannot carry a ``Config`` attribute.

    The decorator spelling. A class or module sets ``Config``; a function that
    would rather not grow an attribute in the reader's line of sight wears
    this. Both are read by :func:`schema_of`, exactly as capability 05 has two
    spellings and one :func:`~cordis.inject.dependencies_of`.
    """
    adapted = _adapt(schema)

    def declare(target: F) -> F:
        try:
            setattr(target, DECLARED_SCHEMA_ATTR, adapted)
        except (AttributeError, TypeError) as exc:
            raise InvalidPluginError(target, "cannot carry a config schema") from exc
        return target

    return declare


def schema_of(target: object) -> ConfigSchema[Any] | None:
    """The schema ``target`` declared, adapted, or ``None`` if it declared none.

    ``None`` is the common case and stays free: a plugin with no schema is
    handed its config untouched (SEM-005).
    """
    declared = getattr(target, DECLARED_SCHEMA_ATTR, None)
    if declared is not None:
        return _adapt(declared)
    found = getattr(target, CONFIG_SCHEMA_ATTR, None)
    if found is None:
        return None
    return _adapt(found)


def _adapt(schema: object) -> ConfigSchema[Any]:
    """Take a declaration to something with a synchronous ``validate``."""
    validate = getattr(schema, "validate", None)
    if callable(validate):
        _refuse_async(schema, validate)
        return typing.cast("ConfigSchema[Any]", schema)
    if isinstance(schema, type) and dataclasses.is_dataclass(schema):
        return from_dataclass(schema)
    raise InvalidPluginError(
        schema,
        "a config schema is a dataclass or an object with a "
        "`validate(raw) -> ConfigResult`",
    )


def _refuse_async(schema: object, validate: Callable[..., object]) -> None:
    """Reject an asynchronous validator where it is declared (SEM-004).

    Only the statically visible half is caught here -- a ``validate`` that is
    itself a coroutine function. A validator that merely *returns* an awaitable
    cannot be recognised without calling it, so that case is caught by
    :func:`resolve_config` on the first resolution, which for a declared schema
    is still the mount call. Both raise from the same place; only the reason
    differs.
    """
    if inspect.iscoroutinefunction(validate) or inspect.isasyncgenfunction(validate):
        raise AsyncValidationError(schema)


# --------------------------------------------------------------------------
# Resolving
# --------------------------------------------------------------------------


def resolve_config(
    schema: ConfigSchema[T] | None, raw: object, /, *, plugin: str
) -> T | object:
    """The value a body should be handed, or the reason it may not run.

    Raises rather than returning a union: every caller inside the runtime
    treats an invalid config as a failure, and a caller that had to unpack the
    good case would eventually forget to. :class:`ConfigResult` is the
    protocol's currency; the exception is the runtime's.
    """
    if schema is None:
        return raw  # SEM-005: unchanged, by identity, including None
    result = schema.validate(raw)
    if inspect.isawaitable(result):
        # Closed rather than abandoned: an un-awaited coroutine warns at
        # collection time, in whatever test happened to run next.
        close = getattr(result, "close", None)
        if callable(close):
            close()
        raise AsyncValidationError(schema)
    if not result.ok:
        raise ConfigValidationError(plugin, result.issues)
    return result.value


# --------------------------------------------------------------------------
# The stdlib adapter
# --------------------------------------------------------------------------

_NONE = type(None)
_PRIMITIVES: Final = (bool, int, float, str, bytes)


class _DataclassSchema(Generic[T]):
    """A dataclass, read as a schema: check the types, apply the defaults."""

    __slots__ = ("_cls", "_hints")

    def __init__(self, cls: type[T], hints: Mapping[str, object]) -> None:
        self._cls = cls
        self._hints = hints

    def __repr__(self) -> str:
        return f"from_dataclass({self._cls.__name__})"

    def validate(self, raw: object, /) -> ConfigResult[T]:
        value, issues = _resolve_dataclass(self._cls, self._hints, raw, ())
        if issues:
            return ConfigResult.rejected(*issues)
        return ConfigResult.accepted(typing.cast("T", value))


_ADAPTED: dict[type, _DataclassSchema[Any]] = {}


def from_dataclass(cls: type[T]) -> ConfigSchema[T]:
    """Read ``cls`` as a schema, refusing annotations it cannot check.

    Annotations are resolved with :func:`typing.get_type_hints` rather than
    read off ``__annotations__``: every module in this project uses
    ``from __future__ import annotations``, so the raw form is strings, and an
    adapter that compared strings would check nothing at all.
    """
    # Checked through a deliberately untyped alias: narrowing `cls` itself with
    # `is_dataclass` turns it into an intersection type that no longer matches
    # `ConfigSchema[T]` structurally, and the cast to silence that would be a
    # bigger lie than this one.
    subject: Any = cls
    if not (dataclasses.is_dataclass(subject) and isinstance(subject, type)):
        msg = f"{cls!r} is not a dataclass"
        raise TypeError(msg)
    cached = _ADAPTED.get(subject)
    if cached is not None:
        return cached
    hints = typing.get_type_hints(subject)
    for field in dataclasses.fields(subject):
        _require_supported(hints[field.name], (subject, field.name))
    schema = _DataclassSchema(cls, hints)
    _ADAPTED[subject] = schema
    return schema


def _require_supported(annotation: object, where: tuple[type, str]) -> None:
    """Refuse at declaration time what cannot be checked at validation time.

    Accepting an unknown annotation and waving it through would be worse than
    refusing it: the author would believe their config was validated.
    """
    if annotation is Any or annotation in _PRIMITIVES or annotation is _NONE:
        return
    origin = typing.get_origin(annotation)
    if origin in (types.UnionType, typing.Union):
        for member in get_args(annotation):
            _require_supported(member, where)
        return
    if origin is list:
        (member,) = get_args(annotation) or (Any,)
        _require_supported(member, where)
        return
    if origin is dict:
        key, member = get_args(annotation) or (str, Any)
        if key is not str:
            owner, name = where
            msg = (
                f"{owner.__name__}.{name}: a config mapping must be keyed by "
                f"str, not {_name_of(key)}"
            )
            raise TypeError(msg)
        _require_supported(member, where)
        return
    if isinstance(annotation, type) and dataclasses.is_dataclass(annotation):
        from_dataclass(annotation)  # checks it, and caches it for the resolve
        return
    owner, name = where
    msg = (
        f"{owner.__name__}.{name}: {_name_of(annotation)} is not a type this "
        f"adapter can check; supply a schema object instead"
    )
    raise TypeError(msg)


def _name_of(annotation: object) -> str:
    return getattr(annotation, "__name__", None) or str(annotation)


def _resolve_dataclass(
    cls: type,
    hints: Mapping[str, object],
    raw: object,
    path: tuple[str | int, ...],
) -> tuple[object, list[ConfigIssue]]:
    if isinstance(raw, cls):
        # Config does not always come from a file: a test, or a parent plugin
        # composing a child, has the typed object in hand already, and making
        # it turn that back into a mapping first would be a chore with no
        # purpose.
        return raw, []
    if not _is_mapping(raw):
        return None, [ConfigIssue(path, f"must be a mapping for {cls.__name__}")]

    mapping = typing.cast("Mapping[str, object]", raw)
    known = {field.name: field for field in dataclasses.fields(cls)}
    # Unknown keys are reported, not ignored: a misspelled key is the most
    # common config mistake there is, and dropping it silently means the
    # operator's edit had no effect and nothing said so.
    issues: list[ConfigIssue] = [
        ConfigIssue((*path, key), "unknown field")
        for key in mapping
        if key not in known
    ]

    arguments: dict[str, object] = {}
    for name, field in known.items():
        if name not in mapping:
            if _has_default(field):
                continue
            issues.append(ConfigIssue((*path, name), "required field is missing"))
            continue
        value, found = _resolve(hints[name], mapping[name], (*path, name))
        issues.extend(found)
        if not found:
            arguments[name] = value

    if issues:
        return None, issues
    return cls(**arguments), []


def _resolve(
    annotation: object, raw: object, path: tuple[str | int, ...]
) -> tuple[object, list[ConfigIssue]]:
    if annotation is Any:
        return raw, []

    origin = typing.get_origin(annotation)
    if origin in (types.UnionType, typing.Union):
        return _resolve_union(get_args(annotation), raw, path)
    if origin is list:
        return _resolve_list(get_args(annotation), raw, path)
    if origin is dict:
        return _resolve_dict(get_args(annotation), raw, path)
    if isinstance(annotation, type) and dataclasses.is_dataclass(annotation):
        hints = typing.get_type_hints(annotation)
        return _resolve_dataclass(annotation, hints, raw, path)
    return _resolve_primitive(typing.cast("type", annotation), raw, path)


def _resolve_primitive(
    annotation: type, raw: object, path: tuple[str | int, ...]
) -> tuple[object, list[ConfigIssue]]:
    if annotation is _NONE:
        if raw is None:
            return None, []
        return None, [ConfigIssue(path, "must be null")]
    # `isinstance(True, int)` is True, and a timeout of `true` is not a
    # timeout. This is the one place the adapter is deliberately stricter than
    # Python: a bool arriving where a number belongs is a config error every
    # time, and never a deliberate one.
    if annotation is bool:
        ok = isinstance(raw, bool)
    elif annotation is int:
        ok = isinstance(raw, int) and not isinstance(raw, bool)
    elif annotation is float:
        ok = isinstance(raw, (int, float)) and not isinstance(raw, bool)
    else:
        ok = isinstance(raw, annotation)
    if ok:
        return raw, []
    wanted = _name_of(annotation)
    return None, [ConfigIssue(path, f"must be {wanted}, got {_kind(raw)}")]


def _resolve_union(
    members: tuple[object, ...], raw: object, path: tuple[str | int, ...]
) -> tuple[object, list[ConfigIssue]]:
    for member in members:
        value, issues = _resolve(member, raw, path)
        if not issues:
            return value, []
    wanted = " or ".join(_name_of(member) for member in members)
    return None, [ConfigIssue(path, f"must be {wanted}, got {_kind(raw)}")]


def _resolve_list(
    members: tuple[object, ...], raw: object, path: tuple[str | int, ...]
) -> tuple[object, list[ConfigIssue]]:
    if not isinstance(raw, list):
        return None, [ConfigIssue(path, f"must be a list, got {_kind(raw)}")]
    member = members[0] if members else Any
    values: list[object] = []
    issues: list[ConfigIssue] = []
    for index, element in enumerate(raw):
        # The index goes into the path, which is why paths are tuples: an
        # operator with forty rows needs to know which one is wrong.
        value, found = _resolve(member, element, (*path, index))
        issues.extend(found)
        values.append(value)
    return (None, issues) if issues else (values, [])


def _resolve_dict(
    members: tuple[object, ...], raw: object, path: tuple[str | int, ...]
) -> tuple[object, list[ConfigIssue]]:
    if not _is_mapping(raw):
        return None, [ConfigIssue(path, f"must be a mapping, got {_kind(raw)}")]
    mapping = typing.cast("Mapping[object, object]", raw)
    member = members[1] if len(members) == 2 else Any
    values: dict[str, object] = {}
    issues: list[ConfigIssue] = []
    for key, element in mapping.items():
        if not isinstance(key, str):
            issues.append(ConfigIssue(path, f"key {key!r} must be a str"))
            continue
        value, found = _resolve(member, element, (*path, key))
        issues.extend(found)
        values[key] = value
    return (None, issues) if issues else (values, [])


def _is_mapping(raw: object) -> bool:
    """Duck-typed rather than ``isinstance(raw, Mapping)``.

    A config loader may hand back its own mapping type that never registered
    with the ABC, and a schema that rejected it would be rejecting a perfectly
    ordinary config for a reason the operator cannot act on.
    """
    return hasattr(raw, "keys") and hasattr(raw, "__getitem__")


def _has_default(field: dataclasses.Field[Any]) -> bool:
    return (
        field.default is not dataclasses.MISSING
        or field.default_factory is not dataclasses.MISSING
    )


def _kind(raw: object) -> str:
    return type(raw).__name__
