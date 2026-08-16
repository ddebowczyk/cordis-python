"""PROP-FIBER-001..007, transcribed from spec/capabilities/04-fiber-lifecycle.yaml.

Three cards (001, 002, 006) are driven by one ``RuleBasedStateMachine``, as
their domains specify. It is defined once and subclassed per card, with each
subclass switching on exactly the check its card owns: a shared machine that
asserted everything would make every card fail for every defect, and a card
that cannot fail alone is not evidence about anything in particular.

The transition oracle is :class:`tests.support.TransitionRecorder`, whose
allowed-edge table is transcribed from SEM-002 rather than imported from
``cordis.fiber``. An oracle that reads the table the implementation enforces
cannot detect a wrong table.

PROP-FIBER-007 is the one card that cannot run under the async test plugin:
its whole subject is what happens with no loop running, so it drives its own
``asyncio.run`` and compares the two mount sites.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any, TypeVar

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    consumes,
    invariant,
    rule,
    run_state_machine_as_test,
)

from cordis.fiber import SETTLED, TRANSITIONS, FiberState, StatusChange
from cordis.plugin import PluginHost, scope_of
from tests.support import FIBER_TRANSITIONS, ResourceLedger, TransitionRecorder

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from cordis.context import Context
    from cordis.effect import EffectScope
    from cordis.fiber import Fiber

#: A small pool keeps availability flipping often; three names is enough for a
#: fiber to depend on a strict subset and for removals to bite.
POOL = ("s0", "s1", "s2")

T = TypeVar("T")


class BodyError(RuntimeError):
    """Raised by a body the generator marked as failing."""


def _identity(value: T) -> T:
    """Setup that hands back a disposer prepared by the caller.

    ``scope.effect`` calls its argument and keeps what it returns, so binding
    the disposer here -- rather than in a closure over a loop variable --
    keeps each effect holding the disposer for its own resource.
    """
    return value


# --------------------------------------------------------------------------
# Shared harness
# --------------------------------------------------------------------------


@dataclass
class Providers:
    """Services provided from outside the fiber tree, and how to withdraw them.

    Each provider gets its own scope, so "remove s1" is a scope disposal --
    the same path a plugin's unload would take -- rather than a registry poke
    no real application would make.
    """

    host: PluginHost
    scopes: dict[str, EffectScope] = field(default_factory=dict)

    async def add(self, name: str) -> None:
        """Bind ``name``, from inside the loop -- where a plugin body would.

        Asynchronous although nothing here awaits: binding a service notifies
        the fibers that need it, and they schedule their reload. Doing that
        from outside the loop is a different situation with its own test.
        """
        if name in self.scopes:
            return
        scope = self.host.root.scope.child(f"provider/{name}")
        self.host.registry.provide(name, f"impl-{name}", scope=scope)
        self.scopes[name] = scope

    async def remove(self, name: str) -> None:
        scope = self.scopes.pop(name, None)
        if scope is not None:
            await scope.dispose()

    @property
    def live(self) -> frozenset[str]:
        return frozenset(self.scopes)


def recording_body(
    log: list[str], name: str, *, fails: bool = False
) -> Callable[[Context], None]:
    """A body that leaves a trace and can be told to raise."""

    def apply(ctx: Context) -> None:
        log.append(name)
        if fails:
            raise BodyError(name)

    return apply


# --------------------------------------------------------------------------
# The state machine behind PROP-FIBER-001, 002 and 006
# --------------------------------------------------------------------------


class FiberMachine(RuleBasedStateMachine):
    """Mount, dispose, restart, update, provide and remove, against a live root."""

    #: PROP-FIBER-006: assert the new state is readable when the notification
    #: arrives. Off in the base machine so each card fails for its own reason.
    check_visibility = False

    #: PROP-FIBER-002: assert the ACTIVE/PENDING biconditional at quiescence.
    check_readiness = False

    fibers = Bundle("fibers")

    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.host = PluginHost()
        self.providers = Providers(self.host)
        self.recorder = TransitionRecorder()
        self.declared: dict[int, tuple[str, ...]] = {}
        self.labels: dict[int, str] = {}
        self.failing: set[int] = set()
        self.log: list[str] = []
        self.errors: list[BaseException] = []
        self.minted = 0
        self.unobserve = self.host.runtime.observe(self._on_status)

    # -- observation -------------------------------------------------------

    def _on_status(self, change: StatusChange) -> None:
        subject = self.labels.setdefault(id(change.fiber), change.fiber.label)
        try:
            self.recorder.record(subject, change.new.name)
            if self.check_visibility:
                # The payload and the attribute are two surfaces the runtime
                # must keep in sync; publishing before assigning shows up here.
                assert change.fiber.state is change.new
        except BaseException as exc:  # re-raised by an invariant
            # A status listener runs deep inside the runtime, sometimes within
            # a task nobody awaits. Recording the failure and re-raising it
            # from an invariant is what keeps it from being swallowed.
            self.errors.append(exc)
            raise

    def _run(self, coro: Any) -> Any:
        return self.loop.run_until_complete(coro)

    # -- rules -------------------------------------------------------------

    @rule(
        target=fibers,
        requires=st.frozensets(st.sampled_from(POOL), max_size=3),
        fails=st.booleans(),
        config=st.integers(min_value=0, max_value=3),
    )
    def mount(self, requires: frozenset[str], fails: bool, config: int) -> Fiber:
        self.minted += 1
        name = f"p{self.minted}"
        fiber = self.host.root.plugin(
            recording_body(self.log, name, fails=fails),
            config,
            requires=tuple(sorted(requires)),
        )
        self.declared[id(fiber)] = tuple(sorted(requires))
        self.labels[id(fiber)] = fiber.label
        if fails:
            self.failing.add(id(fiber))
        return fiber

    @rule(fiber=fibers)
    def restart(self, fiber: Fiber) -> None:
        self._settling(fiber, fiber.restart())

    @rule(fiber=fibers, config=st.integers(min_value=0, max_value=3))
    def update(self, fiber: Fiber, config: int) -> None:
        self._settling(fiber, fiber.update(config))

    def _settling(self, fiber: Fiber, coro: Any) -> None:
        """Run an operation that settles the fiber, tolerating a failed body.

        Restarting or updating waits for the fiber to settle, and settling on
        FAILED re-raises what the body raised (SEM-006). That is the intended
        behaviour, so the rule absorbs it -- but only for a body the test told
        to fail. An unexpected BodyError still ends the run.
        """
        try:
            self._run(coro)
        except BodyError:
            assert id(fiber) in self.failing, f"{fiber.label} raised unbidden"

    @rule(fiber=consumes(fibers))
    def dispose(self, fiber: Fiber) -> None:
        self._run(fiber.dispose())

    @rule(name=st.sampled_from(POOL))
    def provide(self, name: str) -> None:
        self._run(self.providers.add(name))
        self._run(self.host.runtime.quiesce())

    @rule(name=st.sampled_from(POOL))
    def withdraw(self, name: str) -> None:
        self._run(self.providers.remove(name))
        self._run(self.host.runtime.quiesce())

    # -- invariants --------------------------------------------------------

    @invariant()
    def no_listener_failed(self) -> None:
        if self.errors:
            raise self.errors[0]

    @invariant()
    def disposed_is_terminal(self) -> None:
        self.recorder.assert_terminal_is_final()

    @invariant()
    def active_iff_dependencies_bound(self) -> None:
        if not self.check_readiness:
            return
        self._run(self.host.runtime.quiesce())
        live = self.providers.live
        for fiber in self._live_fibers():
            deps = self.declared[id(fiber)]
            # The test's own record of what it provided -- never the registry,
            # so a registry bug cannot make both sides agree.
            satisfied = all(dep in live for dep in deps)
            if fiber.state is FiberState.ACTIVE:
                assert satisfied, f"{fiber.label} ACTIVE, missing from {deps}"
            if fiber.state is FiberState.PENDING:
                assert not satisfied, f"{fiber.label} PENDING with {deps} all bound"

    def _live_fibers(self) -> Iterator[Fiber]:
        for fiber in self.host.root.children:
            if fiber.state is not FiberState.DISPOSED and id(fiber) in self.declared:
                yield fiber

    def teardown(self) -> None:
        try:
            self.unobserve()
            # Bounded: disposing an application must terminate, so a hang here
            # is a defect to report, not a reason for the suite to stop.
            self._run(asyncio.wait_for(self.host.dispose(), timeout=5))
            if self.errors:
                raise self.errors[0]
        finally:
            self.loop.close()


class TransitionMachine(FiberMachine):
    """PROP-FIBER-001."""


class ReadinessMachine(FiberMachine):
    """PROP-FIBER-002."""

    check_readiness = True


class VisibilityMachine(FiberMachine):
    """PROP-FIBER-006."""

    check_visibility = True


# --------------------------------------------------------------------------
# PROP-FIBER-001
# --------------------------------------------------------------------------


@pytest.mark.tier_pr
def test_every_transition_is_permitted() -> None:
    """Failure value: a dependency notification arriving after disposal and
    driving a DISPOSED->LOADING transition, re-running a plugin body whose
    owner is gone and re-registering listeners into a dead scope."""
    run_state_machine_as_test(TransitionMachine)  # type: ignore[no-untyped-call]


# --------------------------------------------------------------------------
# PROP-FIBER-002
# --------------------------------------------------------------------------


@pytest.mark.tier_pr
def test_active_exactly_when_dependencies_are_bound() -> None:
    """Failure value: recomputing readiness only on provide and not on
    removal, leaving a fiber ACTIVE and happily calling a service that was
    unloaded."""
    run_state_machine_as_test(ReadinessMachine)  # type: ignore[no-untyped-call]


# --------------------------------------------------------------------------
# PROP-FIBER-006
# --------------------------------------------------------------------------


@pytest.mark.tier_local
def test_the_new_state_is_readable_when_the_notification_arrives() -> None:
    """Failure value: emitting the notification before assigning the new
    state, so a dashboard built on the event stream is always one transition
    behind and reacts to conditions that no longer hold."""
    run_state_machine_as_test(  # type: ignore[no-untyped-call]
        VisibilityMachine, settings=settings(max_examples=25, deadline=None)
    )


# --------------------------------------------------------------------------
# PROP-FIBER-003
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(
    resources=st.integers(min_value=0, max_value=3),
    listeners=st.integers(min_value=0, max_value=3),
    restarts=st.integers(min_value=1, max_value=5),
)
async def test_restart_conserves_resources_and_shares_nothing(
    resources: int, listeners: int, restarts: int
) -> None:
    """Failure value: a restart that reuses the existing effect scope to "save
    allocation", so each reload doubles the listener count -- the classic HMR
    leak where the tenth save handles every event ten times."""
    host = PluginHost()
    ledger = ResourceLedger()
    loads = 0
    # Held for the whole test: an id is only unique while its object is alive,
    # so the disjointness claim below needs the disposers kept from the GC.
    disposers: list[list[object]] = []

    def body(ctx: Context) -> None:
        nonlocal loads
        loads += 1
        generation = loads
        scope = scope_of(ctx)
        mine: list[object] = []
        for index in range(resources):
            resource = f"r{generation}-{index}"
            ledger.acquire(resource)
            release = partial(ledger.release, resource)
            scope.effect(partial(_identity, release), label=resource)
            mine.append(release)
        for index in range(listeners):
            noop = partial(seen.append, f"l{generation}-{index}")
            scope.effect(partial(_identity, noop), label=f"l{generation}-{index}")
            mine.append(noop)
        disposers.append(mine)

    seen: list[str] = []
    fiber = await host.root.plugin(body)
    baseline = len(ledger.live)

    for _ in range(restarts):
        await fiber.restart()
        assert fiber.state is FiberState.ACTIVE
        # Net live count is what it was after the first load: every reload
        # released exactly what the previous one acquired.
        assert len(ledger.live) == baseline

    # Nothing survives a restart: the identities held before and after are
    # disjoint, which no implementation reusing the scope can arrange.
    if resources + listeners:
        first = {id(obj) for obj in disposers[0]}
        latest = {id(obj) for obj in disposers[-1]}
        assert first.isdisjoint(latest)
    assert loads == restarts + 1

    await host.dispose()
    assert ledger.balanced


# --------------------------------------------------------------------------
# PROP-FIBER-004
# --------------------------------------------------------------------------


EQUAL_PAIRS: tuple[tuple[object, object], ...] = (
    ({"a": 1, "b": 2}, {"b": 2, "a": 1}),
    ([1, 2, 3], [1, 2, 3]),
    ("dsn", "ds" + "n"),
    (7, 7.0),
    ((1, ("x",)), (1, ("x",))),
    (None, None),
)


@pytest.mark.tier_local
@given(pair=st.sampled_from(EQUAL_PAIRS))
async def test_updating_with_an_equal_config_changes_nothing(
    pair: tuple[object, object],
) -> None:
    """Failure value: comparing configs by object identity rather than value,
    so re-reading an unchanged YAML file restarts every plugin in the tree on
    every save."""
    original, equal = pair
    host = PluginHost()
    seen: list[str] = []
    changes: list[StatusChange] = []

    def body(ctx: Context, config: object) -> None:
        seen.append(f"load-{len(seen)}")

    fiber = await host.root.plugin(body, original)
    marker = fiber.context
    host.runtime.observe(changes.append)

    await fiber.update(equal)

    assert changes == []
    assert seen == ["load-0"]
    assert fiber.state is FiberState.ACTIVE
    assert fiber.context is marker

    # ...and a config that differs does restart, so the test cannot pass by
    # making update a no-op.
    await fiber.update(("different", original))
    assert [change.new for change in changes] == [
        FiberState.UNLOADING,
        FiberState.PENDING,
        FiberState.LOADING,
        FiberState.ACTIVE,
    ]
    assert seen == ["load-0", "load-1"]
    await host.dispose()


# --------------------------------------------------------------------------
# PROP-FIBER-005
# --------------------------------------------------------------------------


@pytest.mark.tier_pr
@given(
    plan=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=3),  # turns before completing
            st.sampled_from(("ok", "fails", "waits")),
        ),
        min_size=1,
        max_size=5,
    )
)
async def test_awaiting_a_fiber_always_settles(plan: list[tuple[int, str]]) -> None:
    """Failure value: awaiting a fiber whose dependency never arrives hanging
    forever, so a startup script that awaits its plugins blocks indefinitely on
    a misconfigured deployment instead of reporting which service is missing."""
    host = PluginHost()
    resumed: dict[str, FiberState] = {}
    raised: dict[str, BaseException] = {}

    def slow(name: str, *, turns: int, fails: bool) -> Callable[[Context], Any]:
        async def apply(ctx: Context) -> None:
            for _ in range(turns):
                await asyncio.sleep(0)
            if fails:
                error = BodyError(name)
                raised[name] = error
                raise error

        return apply

    async def watch(name: str, fiber: Fiber) -> None:
        try:
            await fiber
        except BaseException as exc:  # compared by identity below
            raised.setdefault(f"{name}!", exc)
        # The state at the moment the await resumed, read by a task other than
        # the one that decides when waiters wake.
        resumed[name] = fiber.state

    watchers = []
    for index, (turns, kind) in enumerate(plan):
        name = f"p{index}"
        fiber = host.root.plugin(
            slow(name, turns=turns, fails=kind == "fails"),
            requires=("never",) if kind == "waits" else (),
        )
        watchers.append(asyncio.ensure_future(watch(name, fiber)))

    await asyncio.wait_for(asyncio.gather(*watchers), timeout=5)

    for index, (_turns, kind) in enumerate(plan):
        name = f"p{index}"
        assert resumed[name].name in SETTLED_NAMES
        if kind == "waits":
            assert resumed[name] is FiberState.PENDING
        elif kind == "fails":
            assert resumed[name] is FiberState.FAILED
            assert raised[f"{name}!"] is raised[name]
        else:
            assert resumed[name] is FiberState.ACTIVE

    await host.dispose()


SETTLED_NAMES = frozenset(state.name for state in SETTLED)


# --------------------------------------------------------------------------
# PROP-FIBER-007
# --------------------------------------------------------------------------


#: (final state, exception type name or None, ledger event trace).
Outcome = tuple[str, str | None, tuple[str, ...]]


def _bootstrap(
    host: PluginHost, ledger: ResourceLedger, *, kind: str, deps: tuple[str, ...]
) -> Fiber:
    """Mount a body that takes a resource, then bind what it declared.

    Everything here is synchronous and is run twice by the test: once with a
    loop running and once without. Nothing in it mentions the loop.
    """

    def body(ctx: Context) -> Any:
        ledger.acquire("r")
        scope_of(ctx).effect(lambda: partial(ledger.release, "r"), label="r")
        if kind == "raises":
            raise BodyError("r")
        if kind == "awaits":
            return asyncio.sleep(0)
        return None

    fiber = host.root.plugin(body, requires=deps)
    for name in deps:
        # Bound after the mount, so it is the fiber that has to notice.
        host.registry.provide(name, f"impl-{name}", scope=host.root.scope)
    return fiber


@pytest.mark.tier_local
@given(
    kind=st.sampled_from(("returns", "awaits", "raises")),
    deps=st.frozensets(st.sampled_from(POOL), max_size=2),
)
def test_mounting_before_the_loop_starts_settles_the_same_way(
    kind: str, deps: frozenset[str]
) -> None:
    """Failure value: scheduling through `asyncio.ensure_future`, which falls
    back to a policy loop nobody runs, so a failing body's resources are never
    released and the fiber never settles -- a program that mounts during
    bootstrap then hangs on its first await."""
    names = tuple(sorted(deps))

    def run(*, inside: bool) -> Outcome:
        host = PluginHost()
        ledger = ResourceLedger()
        early = None if inside else _bootstrap(host, ledger, kind=kind, deps=names)

        async def settle() -> Outcome:
            fiber = (
                early
                if early is not None
                else _bootstrap(host, ledger, kind=kind, deps=names)
            )
            failure: str | None = None
            try:
                await fiber
            except BodyError as exc:
                failure = type(exc).__name__
            state = fiber.state.name
            await host.dispose()
            return (
                state,
                failure,
                tuple(f"{e.action}:{e.resource}" for e in ledger.events),
            )

        return asyncio.run(settle())

    assert run(inside=False) == run(inside=True)


def test_the_transition_table_is_the_one_the_specification_states() -> None:
    """The implementation's table, edge for edge, against the recorder's copy."""
    mirrored = {
        source.name: frozenset(target.name for target in targets)
        for source, targets in TRANSITIONS.items()
    }
    assert mirrored == dict(FIBER_TRANSITIONS)
