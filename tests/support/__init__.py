"""Shared instrumentation for the property suite.

Built once, before any capability, because every property card assumes it:
a ledger to witness resource conservation, a virtual clock so timing claims
are claims about logical time, a transition recorder so state-machine
properties can assert on the path rather than the destination, and a generator
library that constructs valid values instead of filtering for them.

Nothing here imports `cordis`. The oracles have to be able to disagree with
the implementation, which they cannot do if they are built out of it.
"""

from __future__ import annotations

from tests.support.clock import VirtualClock
from tests.support.ledger import LedgerViolationError, ResourceLedger
from tests.support.recorder import (
    FIBER_INITIAL,
    FIBER_SETTLED,
    FIBER_TRANSITIONS,
    IllegalTransitionError,
    TransitionRecorder,
)
from tests.support.specs import (
    VALID_SHAPES,
    DagSpec,
    DisposeOp,
    EffectShape,
    EffectSpec,
    EntryDiff,
    EntrySpec,
    ProvideOp,
    RegistryOp,
    TreeSpec,
    diff_entries,
)

__all__ = [
    "FIBER_INITIAL",
    "FIBER_SETTLED",
    "FIBER_TRANSITIONS",
    "VALID_SHAPES",
    "DagSpec",
    "DisposeOp",
    "EffectShape",
    "EffectSpec",
    "EntryDiff",
    "EntrySpec",
    "IllegalTransitionError",
    "LedgerViolationError",
    "ProvideOp",
    "RegistryOp",
    "ResourceLedger",
    "TransitionRecorder",
    "TreeSpec",
    "VirtualClock",
    "diff_entries",
]
