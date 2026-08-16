"""A transition recorder: the witness for state-machine properties.

A property that asserts only on the final state passes for any implementation
that happens to end up there, including one that got there through a
transition the specification forbids. The recorder makes the *path* an
observable, so "a disposed fiber never activates again" is a claim a test can
actually falsify.

The permitted transitions are transcribed from fiber-lifecycle SEM-002 and
SEM-003 and live here rather than being imported from the implementation. An
oracle that reads the table the implementation enforces cannot detect a wrong
table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

#: fiber-lifecycle SEM-002: the complete set of legal edges.
FIBER_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "PENDING": frozenset({"LOADING", "UNLOADING"}),
    "LOADING": frozenset({"ACTIVE", "FAILED"}),
    "ACTIVE": frozenset({"UNLOADING"}),
    "FAILED": frozenset({"UNLOADING"}),
    "UNLOADING": frozenset({"PENDING", "DISPOSED"}),
    # SEM-003: DISPOSED is terminal.
    "DISPOSED": frozenset(),
}

FIBER_INITIAL = "PENDING"

#: States at which a fiber is quiescent -- no transition is in flight, so an
#: invariant may be asserted. Asserting during LOADING or UNLOADING tests a
#: half-applied state the specification says nothing about.
FIBER_SETTLED = frozenset({"PENDING", "ACTIVE", "FAILED", "DISPOSED"})


class IllegalTransitionError(AssertionError):
    """A transition outside the permitted set was recorded."""


@dataclass(frozen=True)
class Transition:
    subject: str
    source: str
    target: str
    seq: int


class TransitionRecorder:
    """Records observed transitions and rejects illegal ones at the source."""

    def __init__(
        self,
        allowed: Mapping[str, frozenset[str]] = FIBER_TRANSITIONS,
        initial: str = FIBER_INITIAL,
    ) -> None:
        self._allowed = allowed
        self._initial = initial
        self._state: dict[str, str] = {}
        self._log: list[Transition] = []
        self._seq = 0

    def state(self, subject: str) -> str:
        return self._state.get(subject, self._initial)

    def record(self, subject: str, target: str) -> None:
        source = self.state(subject)
        if target not in self._allowed.get(source, frozenset()):
            raise IllegalTransitionError(
                f"{subject}: {source} -> {target} is not a permitted "
                f"transition (permitted: {sorted(self._allowed.get(source, ()))})"
            )
        self._seq += 1
        self._log.append(Transition(subject, source, target, self._seq))
        self._state[subject] = target

    # -- reading -----------------------------------------------------------

    @property
    def log(self) -> Sequence[Transition]:
        return tuple(self._log)

    def path(self, subject: str) -> tuple[str, ...]:
        """Every state ``subject`` has occupied, in order."""
        steps = [t.target for t in self._log if t.subject == subject]
        return (self._initial, *steps)

    def reached(self, subject: str, state: str) -> bool:
        return state in self.path(subject)

    def assert_terminal_is_final(self, terminal: str = "DISPOSED") -> None:
        """No subject transitioned after reaching a terminal state."""
        for subject in self._state:
            path = self.path(subject)
            if terminal in path and path[-1] != terminal:
                raise IllegalTransitionError(
                    f"{subject} left the terminal state {terminal}: {path}"
                )

    def assert_visited(self, subject: str, states: Iterable[str]) -> None:
        """The path contains these states, in this order (gaps allowed).

        Subsequence rather than equality, so a property about ordering does not
        also accidentally assert how many intermediate reloads occurred.
        """
        wanted = list(states)
        remaining = iter(self.path(subject))
        missing = [s for s in wanted if s not in remaining]
        if missing:
            raise IllegalTransitionError(
                f"{subject} path {self.path(subject)} does not contain "
                f"{wanted} in order (missing from {missing[0]!r} on)"
            )

    def __repr__(self) -> str:
        return f"TransitionRecorder(subjects={len(self._state)}, steps={self._seq})"
