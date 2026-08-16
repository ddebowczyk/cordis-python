"""Diagnostics: the runtime, as a value you can read.

Implements ``spec/capabilities/11-diagnostics.yaml``.

The defining behaviour of this architecture is that a plugin missing a
dependency does nothing -- quietly, and legitimately. That makes "my plugin
isn't running" the most common question anyone will ask of an application built
on it, and it is unanswerable without a way to enumerate instances and see why
each one is where it is.

Three surfaces, all read-only:

* :func:`inspect` returns a :class:`FiberSnapshot` tree -- a value, not a view.
  Nothing in it can be mutated into the runtime, and nothing the runtime does
  afterwards changes it (SEM-002).
* :func:`pending` answers the question directly: every instance that is
  waiting, each unmet name, and -- when the name's would-be provider is itself
  waiting -- the instance at the end of that chain, which is the one to fix
  (SEM-006).
* :func:`render_tree` turns a snapshot into text or JSON. It is a pure function
  of the snapshot, so the operator-facing output is testable without a running
  system.

Three of this capability's rules were already true before it was opened:
effects carry a label and a source location (SEM-003) because
:class:`~cordis.effect.EffectScope` records both, that capture is switchable
(SEM-005) through :data:`~cordis.effect.CAPTURE_LOCATIONS`, and an exception
escaping a plugin body is annotated with its mount chain (SEM-004) by
:func:`~cordis.errors.mount_attribution`. This module adds the surface that
reads them, not a second mechanism.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from cordis.fiber import Fiber, FiberState
from cordis.plugin import PluginHost
from cordis.registry import realm_for

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from cordis.effect import EffectNode

__all__ = [
    "Blockage",
    "FiberSnapshot",
    "PendingReport",
    "inspect",
    "pending",
    "render_tree",
    "walk",
]

Subject = Fiber | PluginHost


@dataclass(frozen=True, slots=True)
class FiberSnapshot:
    """One instance, frozen at a point in the event loop.

    Every field is either immutable or a copy: a caller can hold one of these
    for as long as it likes, and the runtime it came from will neither see the
    caller's changes nor impose its own (SEM-002).
    """

    #: The instance's identity. A serial, not an address: this snapshot may
    #: well outlive the instance, and an address gets reused.
    uid: int
    label: str
    state: FiberState
    #: The config the instance was mounted with, with its containers copied.
    config: object
    requires: tuple[str, ...]
    provides: tuple[str, ...]
    #: Which of ``requires`` is unbound right now -- why it is PENDING.
    unmet: tuple[str, ...]
    #: The failure, as text. Deliberately not the exception: an exception owns
    #: its traceback, a traceback owns every frame it passed through, and a
    #: snapshot is exactly the object a diagnostic tool keeps hold of.
    error: str | None
    #: What the instance registered, excluding what the instances it mounted
    #: registered -- those are children here, with trees of their own.
    effects: EffectNode
    children: tuple[FiberSnapshot, ...]


@dataclass(frozen=True, slots=True)
class Blockage:
    """One unmet dependency, and who owes it."""

    #: The name that is unbound.
    name: str
    #: The pending instance that declared it would provide ``name``, if there
    #: is one. ``None`` means nothing in the tree ever intended to.
    provider: str | None
    #: The instance at the end of the chain: the one whose own dependencies are
    #: the reason nobody further down ever loaded. ``None`` when there is no
    #: chain to follow.
    root: str | None


@dataclass(frozen=True, slots=True)
class PendingReport:
    """One waiting instance, with a cause per unmet name."""

    uid: int
    label: str
    blocked: tuple[Blockage, ...]

    @property
    def names(self) -> tuple[str, ...]:
        """The unmet names, in declaration order."""
        return tuple(item.name for item in self.blocked)


# --------------------------------------------------------------------------
# Reading the tree
# --------------------------------------------------------------------------


def inspect(subject: Subject, /) -> FiberSnapshot:
    """Snapshot ``subject`` and everything it mounted.

    Walks without awaiting, so what comes back is consistent with respect to a
    single point in the event loop rather than smeared across several.
    """
    return _snapshot(_fiber(subject))


def pending(subject: Subject, /) -> tuple[PendingReport, ...]:
    """Every waiting instance under ``subject``, with each unmet name's cause.

    A cascade of forty pending instances has one cause, and reporting forty is
    not a diagnosis: each name is traced through the instances that declared
    they would provide it until one is reached whose own missing dependency
    nobody in the tree is going to bind (SEM-006).
    """
    waiting = tuple(
        fiber for fiber in _fibers(_fiber(subject)) if fiber.state is FiberState.PENDING
    )
    return tuple(
        PendingReport(
            uid=fiber.uid,
            label=fiber.label,
            blocked=tuple(_trace(fiber, name, waiting) for name in fiber.missing),
        )
        for fiber in waiting
    )


def walk(snapshot: FiberSnapshot, /) -> Iterator[FiberSnapshot]:
    """Depth-first over a snapshot tree, the node itself first."""
    yield snapshot
    for child in snapshot.children:
        yield from walk(child)


def _fiber(subject: Subject) -> Fiber:
    return subject.root if isinstance(subject, PluginHost) else subject


def _fibers(root: Fiber) -> Iterator[Fiber]:
    yield root
    for child in root.children:
        yield from _fibers(child)


def _snapshot(fiber: Fiber) -> FiberSnapshot:
    return FiberSnapshot(
        uid=fiber.uid,
        label=fiber.label,
        state=fiber.state,
        config=_copy(fiber.config),
        requires=fiber.requires,
        provides=fiber.provides,
        unmet=fiber.missing,
        error=None if fiber.error is None else repr(fiber.error),
        effects=fiber.effects(),
        children=tuple(_snapshot(child) for child in fiber.children),
    )


def _copy(value: object) -> object:
    """Copy the containers of a config, leaving anything else alone.

    Copying the spine is what keeps a caller's edit out of the runtime. Going
    further -- a `deepcopy` of whatever the config happens to hold -- is not
    something a framework can promise to do correctly: config values include
    connections, clients and file handles, and duplicating one of those is a
    worse bug than the aliasing it would prevent.
    """
    if isinstance(value, dict):
        return {key: _copy(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return tuple(_copy(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(value)
    return value


# --------------------------------------------------------------------------
# Attribution
# --------------------------------------------------------------------------


def _trace(fiber: Fiber, name: str, waiting: Sequence[Fiber]) -> Blockage:
    """Follow ``name`` through the instances that promised it, to the far end."""
    provider = _provider(fiber, name, waiting)
    if provider is None:
        return Blockage(name=name, provider=None, root=None)
    seen = {fiber.uid, provider.uid}
    end = provider
    while (nxt := _next(end, waiting, seen)) is not None:
        seen.add(nxt.uid)
        end = nxt
    return Blockage(name=name, provider=provider.label, root=end.label)


def _next(fiber: Fiber, waiting: Sequence[Fiber], seen: set[int]) -> Fiber | None:
    """The next link: the first of ``fiber``'s own unmet names with a provider.

    ``seen`` is what stops a cycle of mutually-pending providers from becoming
    an infinite walk -- and a cycle is not a hypothetical here, it is what a
    pair of plugins that each declare the other's service looks like.
    """
    for name in fiber.missing:
        candidate = _provider(fiber, name, waiting)
        if candidate is not None and candidate.uid not in seen:
            return candidate
    return None


def _provider(consumer: Fiber, name: str, waiting: Sequence[Fiber]) -> Fiber | None:
    """The pending instance whose loading would bind ``name`` *for this consumer*.

    Realm-aware, and deliberately strict about it: the realm is computed with
    the same :func:`~cordis.registry.realm_for` a lookup uses, on both sides.
    An isolated provider is not the answer to a consumer looking in the global
    realm, and naming it would send an operator to a plugin that was never
    going to help.
    """
    target = realm_for(consumer.context, name)
    for other in waiting:
        if other is consumer or name not in other.provides:
            continue
        if realm_for(other.context, name) is target:
            return other
    return None


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

#: The three prefixes a tree needs, and nothing else.
_LAST = "`- "
_MORE = "|- "
_PIPE = "|  "
_GAP = "   "


def render_tree(
    snapshot: FiberSnapshot,
    /,
    *,
    style: Literal["text", "json"] = "text",
    effects: bool = False,
) -> str:
    """Render a snapshot for a reader.

    ``effects`` answers a different question from the fiber tree -- "what is
    still registered" rather than "what is running" -- and is off by default so
    the small tree an operator usually wants is not buried under the large one
    they occasionally do.
    """
    if style == "json":
        return json.dumps(_as_dict(snapshot, effects=effects), indent=2, default=repr)
    return "\n".join(_lines(snapshot, prefix="", head="", effects=effects))


def _lines(
    snapshot: FiberSnapshot, *, prefix: str, head: str, effects: bool
) -> Iterator[str]:
    yield prefix + head + _describe(snapshot)
    body = prefix + ("" if not head else _GAP if head == _LAST else _PIPE)
    if effects:
        yield from _effect_lines(snapshot.effects, prefix=body, head=_MORE)
    for index, child in enumerate(snapshot.children):
        last = index == len(snapshot.children) - 1
        yield from _lines(
            child, prefix=body, head=_LAST if last else _MORE, effects=effects
        )


def _effect_lines(node: EffectNode, *, prefix: str, head: str) -> Iterator[str]:
    yield f"{prefix}{head}[{node.label or '?'}] {node.location}"
    body = prefix + (_GAP if head == _LAST else _PIPE)
    for index, child in enumerate(node.children):
        last = index == len(node.children) - 1
        yield from _effect_lines(child, prefix=body, head=_LAST if last else _MORE)


def _describe(snapshot: FiberSnapshot) -> str:
    parts = [f"{snapshot.label} [{snapshot.state.name}] #{snapshot.uid}"]
    if snapshot.unmet:
        parts.append(f"waiting on {', '.join(snapshot.unmet)}")
    if snapshot.error is not None:
        parts.append(snapshot.error)
    return " -- ".join(parts)


def _as_dict(snapshot: FiberSnapshot, *, effects: bool) -> dict[str, object]:
    body: dict[str, object] = {
        "uid": snapshot.uid,
        "label": snapshot.label,
        "state": snapshot.state.name,
        "config": snapshot.config,
        "requires": list(snapshot.requires),
        "provides": list(snapshot.provides),
        "unmet": list(snapshot.unmet),
        "error": snapshot.error,
    }
    if effects:
        body["effects"] = _effect_dict(snapshot.effects)
    body["children"] = [_as_dict(child, effects=effects) for child in snapshot.children]
    return body


def _effect_dict(node: EffectNode) -> dict[str, object]:
    return {
        "label": node.label,
        "location": node.location,
        "children": [_effect_dict(child) for child in node.children],
    }
