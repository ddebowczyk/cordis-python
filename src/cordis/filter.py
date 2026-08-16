"""Event filtering: a context decides which of its listeners a dispatch reaches.

Implements the admission half of ``spec/capabilities/10-event-filtering.yaml``;
the bus does the asking, in :mod:`cordis.events`.

One process often runs many instances of the same thing -- several agents,
several sessions, several tenants -- publishing to the same event names. Without
admission control every listener starts with a defensive "if this event is not
mine, return", and the one that forgets leaks across subjects. A filter moves
that decision to the registration site, where it can be read.

``with_filter(ctx, predicate)`` returns a child whose listeners run only for
dispatches whose carrier the predicate admits. It is an ``extend`` like every
other scoped value, so a descendant inherits the filter until it installs its
own -- and installing replaces rather than composes, which is what makes
"loosen the filter for this subtree" expressible at all.

A filter is a routing mechanism and not a security boundary (SEM-004): anything
reachable through the registry stays reachable. What it buys is that the routing
rule is written in one place instead of at the top of a hundred listeners.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, TypeAlias

if TYPE_CHECKING:
    from collections.abc import Callable

    from cordis.context import Context

__all__ = [
    "FILTER_KEY",
    "Filter",
    "filter_of",
    "with_filter",
]

#: The scoped-metadata key carrying a context's admission predicate.
FILTER_KEY: Final = "__filter__"

#: Asked once per dispatch about the carrier: may this registration's listeners
#: run? A predicate reads the carrier and nothing else, which is what lets one
#: answer serve every listener registered under it.
Filter: TypeAlias = "Callable[[Context], bool]"


def with_filter(ctx: Context, predicate: Filter, /) -> Context:
    """A child context admitting only the dispatches ``predicate`` accepts."""
    return ctx.extend(**{FILTER_KEY: predicate})


def filter_of(ctx: Context) -> Filter | None:
    """The filter ``ctx`` registers under, or ``None`` when it admits everything.

    Nearest wins, and a nearer filter *replaces* rather than intersects.
    Composition is one lambda away for a caller who wants it
    (``lambda c: outer(c) and mine(c)``); the opposite -- widening what an
    ancestor narrowed -- cannot be recovered from an intersecting rule at all.
    """
    for node in ctx.lineage():
        found = node.own_meta.get(FILTER_KEY)
        if callable(found):
            return found
    return None
