"""Two Consumers. They import the Definition and never a Provider.

Each declares what it needs with `@inject`, so neither runs before a store
exists and both unwind when one goes away. That is the whole of the swap: no
consumer here knows a swap can happen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from cordis import config_schema, inject, scope_of
from examples.notes.journal import Journal
from examples.notes.store import Store

if TYPE_CHECKING:
    from collections.abc import Callable

    from cordis import Context


@dataclass(frozen=True, slots=True)
class NoteConfig:
    """One note to write, named in the config file."""

    key: str
    text: str


@inject("store", "journal")
@config_schema(NoteConfig)
def writer(ctx: Context, config: NoteConfig) -> None:
    """Write one note, and say which implementation took it."""
    store = Store.of(ctx)
    ctx.require(Journal).record(f"writer wrote {config.key} to {_kind(store)}")
    store.put(config.key, config.text)


@dataclass(frozen=True, slots=True)
class ReportConfig:
    """How a report labels itself, so two reporters are distinguishable."""

    label: str = "report"


@inject("store", "journal")
@config_schema(ReportConfig)
def reporter(ctx: Context, config: ReportConfig) -> None:
    """Report what the store holds, and unwind loudly when it is taken away."""
    store = Store.of(ctx)
    journal = ctx.require(Journal)
    journal.record(f"{config.label} read {len(store.all())} from {_kind(store)}")

    def opened() -> Callable[[], None]:
        return lambda: journal.record(f"{config.label} released {_kind(store)}")

    scope_of(ctx).effect(opened)


def _kind(store: Store) -> str:
    """The provider class behind the contract -- for the journal, not for logic."""
    return type(store).__name__
