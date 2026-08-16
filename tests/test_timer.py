"""PROP-TIMER-001..005, from spec/capabilities/13-scheduling.yaml.

Every timing statement here is a statement about *logical* time. The clock is a
service, so a test binds `VirtualClock` the same way an application would bind a
real one, and time moves only when the test moves it. Nothing sleeps, nothing is
monkeypatched, and no assertion depends on how fast the machine is.

Two models carry most of the weight. `simulate` computes what a repeating
schedule should have completed and skipped from the generated durations alone,
and `model_throttle`/`model_debounce` are folds over the generated call events
written from the definitions in SEM-006. All three are arithmetic over the
generator's own numbers, sharing no code with `cordis.timer`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cordis.context import Context
from cordis.effect import EffectScope
from cordis.logging import Level, LoggerService, Record
from cordis.plugin import SCOPE_KEY
from cordis.registry import ServiceRegistry
from cordis.timer import (
    Clock,
    SystemClock,
    TimerFailure,
    clock_of,
    debounce,
    interval,
    spawn,
    throttle,
    timeout,
)
from tests.support.clock import VirtualClock

if TYPE_CHECKING:
    from collections.abc import Sequence

#: The entry the disposal itself writes into PROP-TIMER-001's timeline.
DISPOSE = "dispose"

KINDS = ("timeout", "interval", "spawn")


# --------------------------------------------------------------------------
# A world: a scope, a context that resolves to it, and time under test control
# --------------------------------------------------------------------------


@dataclass
class World:
    ctx: Context
    scope: EffectScope
    clock: VirtualClock
    host: EffectScope


async def open_world() -> World:
    """A plugin-shaped context: its own scope, inside a host that outlives it.

    The clock is provided on the *host* scope, so disposing the plugin's scope
    -- the event most of these properties are about -- does not also take away
    the clock the assertions still need to drive.
    """
    registry = ServiceRegistry()
    root = Context(resolver=registry, label="root")
    host = EffectScope("host")
    clock = VirtualClock()
    await registry.provide("clock", clock, scope=host, ctx=root)
    scope = host.child("plugin")
    ctx = root.extend(**{SCOPE_KEY: scope})
    return World(ctx=ctx, scope=scope, clock=clock, host=host)


# --------------------------------------------------------------------------
# PROP-TIMER-001
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Job:
    """One piece of scheduled work, and when it first comes due."""

    kind: Literal["timeout", "interval", "spawn"]
    delay: int


@st.composite
def runs(draw: st.DrawFn) -> tuple[tuple[Job, ...], int]:
    """Jobs plus a disposal time drawn *at* a deadline and one tick either side.

    The card's strategy hint asks for the boundary explicitly rather than
    hoping the generator wanders onto it, so the disposal time is built from a
    deadline the jobs actually have.
    """
    jobs = tuple(
        draw(
            st.lists(
                st.builds(
                    Job,
                    kind=st.sampled_from(KINDS),
                    delay=st.integers(min_value=1, max_value=6),
                ),
                min_size=1,
                max_size=4,
            )
        )
    )
    deadlines = sorted(
        {job.delay for job in jobs}
        | {job.delay * 2 for job in jobs if job.kind == "interval"}
    )
    at = draw(st.sampled_from(deadlines))
    offset = draw(st.sampled_from((-1, 0, 1)))
    return jobs, max(0, at + offset)


async def sleeper(
    clock: VirtualClock, delay: int, timeline: list[str], tag: str
) -> None:
    await clock.sleep(delay)
    timeline.append(tag)


@pytest.mark.tier_local
@settings(max_examples=200, deadline=None)
@given(plan=runs())
async def test_nothing_scheduled_runs_after_its_scope_is_gone(
    plan: tuple[tuple[Job, ...], int],
) -> None:
    """PROP-TIMER-001: every callback entry precedes the disposal entry.

    Failure value: a due-but-not-yet-run callback firing after disposal,
    calling into a service whose connection was already closed -- the
    intermittent "connection closed" error that appears only when a reload
    lands on a timer boundary.
    """
    jobs, dispose_at = plan
    world = await open_world()
    timeline: list[str] = []

    for index, job in enumerate(jobs):
        tag = f"{job.kind}-{index}"
        if job.kind == "timeout":
            timeout(world.ctx, job.delay, lambda tag=tag: timeline.append(tag))  # type: ignore[misc]
        elif job.kind == "interval":
            interval(world.ctx, job.delay, lambda tag=tag: timeline.append(tag))  # type: ignore[misc]
        else:
            spawn(world.ctx, sleeper(world.clock, job.delay, timeline, tag))

    # Let each task reach its first sleep: a schedule times from when its body
    # starts running, and nothing has run yet at the moment the call returns.
    await world.clock.drain()
    assert len(world.clock.pending) == len(jobs)

    await world.clock.advance(dispose_at)
    timeline.append(DISPOSE)
    await world.scope.dispose()

    # Far past every deadline any of these jobs could have had.
    await world.clock.advance(100)

    assert timeline.count(DISPOSE) == 1
    assert timeline[-1] == DISPOSE
    assert world.clock.pending == ()


# --------------------------------------------------------------------------
# PROP-TIMER-002
# --------------------------------------------------------------------------


@pytest.mark.tier_pr
@settings(max_examples=200, deadline=None)
@given(holds=st.lists(st.integers(min_value=0, max_value=4), min_size=1, max_size=5))
async def test_disposing_awaits_every_task_it_spawned(holds: list[int]) -> None:
    """PROP-TIMER-002: at the instant dispose returns, every task has finished.

    Failure value: cancelling without awaiting, so dispose returns while a
    task's cleanup is still writing to a file the next reload is about to
    overwrite.
    """
    world = await open_world()
    tasks: dict[int, asyncio.Task[None]] = {}
    cleaned: list[int] = []

    async def body(index: int, passes: int) -> None:
        # The task records *itself*: task state is asyncio's, not the
        # scheduler's bookkeeping, which is what makes it evidence.
        current = asyncio.current_task()
        assert current is not None
        tasks[index] = current
        try:
            await world.clock.sleep(1_000)
        finally:
            # Cleanup that takes a while: the point of awaiting cancellation
            # is that dispose does not return in the middle of this.
            for _ in range(passes):
                await asyncio.sleep(0)
            cleaned.append(index)

    for index, passes in enumerate(holds):
        spawn(world.ctx, body(index, passes), label=f"job-{index}")

    await world.clock.drain()
    assert len(tasks) == len(holds)

    await world.scope.dispose()

    assert all(task.done() for task in tasks.values())
    assert sorted(cleaned) == list(range(len(holds)))
    assert world.clock.pending == ()


# --------------------------------------------------------------------------
# PROP-TIMER-003
# --------------------------------------------------------------------------


def simulate(period: int, durations: Sequence[int], horizon: int) -> tuple[int, int]:
    """Completed and skipped iterations on a start-based grid, by arithmetic.

    Deadlines sit at `period, 2*period, ...`; an iteration that is still
    running when one arrives skips it, and an iteration that finished exactly
    on it is not late (the third deviation in the record).
    """
    completed = skipped = 0
    now = 0
    tick = 0
    while True:
        tick += 1
        deadline = tick * period
        if now > deadline:
            skipped += 1
            continue
        if deadline > horizon:
            break  # the iteration never starts inside the observed window
        end = deadline + durations[completed % len(durations)]
        if end > horizon:
            break  # started, still running when the observation stopped
        now = end
        completed += 1
    return completed, skipped


@pytest.mark.tier_pr
@settings(max_examples=200, deadline=None)
@given(
    period=st.integers(min_value=1, max_value=4),
    durations=st.lists(st.integers(min_value=1, max_value=9), min_size=1, max_size=6),
    horizon=st.integers(min_value=1, max_value=30),
)
async def test_a_schedule_never_overlaps_and_loses_no_deadline(
    period: int, durations: list[int], horizon: int
) -> None:
    """PROP-TIMER-003: completed + skipped accounts for every elapsed deadline.

    Failure value: scheduling the next iteration on a fixed wall-clock grid
    without an overlap guard, so a poller whose backend slows down builds up an
    unbounded pile of concurrent requests and finishes the job of taking the
    backend down.
    """
    world = await open_world()
    inflight = 0
    peak = 0
    started = 0

    async def body() -> None:
        nonlocal inflight, peak, started
        inflight += 1
        peak = max(peak, inflight)
        duration = durations[started % len(durations)]
        started += 1
        try:
            await world.clock.sleep(duration)
        finally:
            inflight -= 1

    schedule = interval(world.ctx, period, body)
    await world.clock.drain()  # the grid starts when the loop does
    await world.clock.advance(horizon)

    expected_completed, expected_skipped = simulate(period, durations, horizon)
    assert (schedule.completed, schedule.skipped) == (
        expected_completed,
        expected_skipped,
    )
    assert peak <= 1

    await world.scope.dispose()


# --------------------------------------------------------------------------
# PROP-TIMER-004
# --------------------------------------------------------------------------

Event = tuple[int, int]


def model_throttle(period: int, events: Sequence[Event]) -> list[Event]:
    """At most one call per window, at its end, with the window's last argument."""
    fires: list[Event] = []
    end: int | None = None
    pending: int | None = None
    for at, arg in events:
        while end is not None and end <= at:
            assert pending is not None
            fires.append((end, pending))
            end = pending = None
        if end is None:
            end = at + period
        pending = arg
    if end is not None:
        assert pending is not None
        fires.append((end, pending))
    return fires


def model_debounce(delay: int, events: Sequence[Event]) -> list[Event]:
    """One call per quiet period, `delay` after the last call in it."""
    fires: list[Event] = []
    pending: Event | None = None
    for at, arg in events:
        if pending is not None and pending[0] <= at:
            fires.append(pending)
        pending = (at + delay, arg)
    if pending is not None:
        fires.append(pending)
    return fires


@st.composite
def call_plans(draw: st.DrawFn) -> tuple[int, tuple[Event, ...]]:
    """Calls whose gaps sit below, at, and above the configured interval."""
    period = draw(st.integers(min_value=2, max_value=5))
    gaps = draw(
        st.lists(
            st.sampled_from((1, period - 1, period, period + 1, 2 * period)),
            min_size=1,
            max_size=6,
        )
    )
    args = draw(
        st.lists(
            st.integers(min_value=0, max_value=99),
            min_size=len(gaps),
            max_size=len(gaps),
        )
    )
    at = 0
    events: list[Event] = []
    for gap, arg in zip(gaps, args, strict=True):
        at += gap
        events.append((at, arg))
    return period, tuple(events)


async def drive(world: World, call: object, events: Sequence[Event], tail: int) -> None:
    """Issue each call at its own instant, then let the tail settle."""
    assert callable(call)
    for at, arg in events:
        await world.clock.advance_to(at)
        call(arg)
        # The wrapper's task registers its sleep when it first runs, so it has
        # to run before time moves again or it would time from the wrong now.
        await world.clock.drain()
    await world.clock.advance(tail)


@pytest.mark.tier_local
@settings(max_examples=200, deadline=None)
@given(plan=call_plans())
async def test_throttle_fires_once_per_window_with_the_latest_arguments(
    plan: tuple[int, tuple[Event, ...]],
) -> None:
    """PROP-TIMER-004, throttle half.

    Failure value: a throttle that invokes with the *first* arguments of the
    interval rather than the last, so a progress indicator or a state-sync call
    always reports a stale value.
    """
    period, events = plan
    world = await open_world()
    fired: list[Event] = []
    call = throttle(
        world.ctx, period, lambda arg: fired.append((int(world.clock.now()), arg))
    )

    await drive(world, call, events, tail=period + 1)

    assert fired == model_throttle(period, events)
    await world.scope.dispose()


@pytest.mark.tier_local
@settings(max_examples=200, deadline=None)
@given(plan=call_plans())
async def test_debounce_fires_once_after_the_last_call_of_a_burst(
    plan: tuple[int, tuple[Event, ...]],
) -> None:
    """PROP-TIMER-004, debounce half: one invocation per quiet period."""
    delay, events = plan
    world = await open_world()
    fired: list[Event] = []
    call = debounce(
        world.ctx, delay, lambda arg: fired.append((int(world.clock.now()), arg))
    )

    await drive(world, call, events, tail=delay + 1)

    assert fired == model_debounce(delay, events)
    await world.scope.dispose()


# --------------------------------------------------------------------------
# PROP-TIMER-005
# --------------------------------------------------------------------------

#: How far PROP-TIMER-005 runs its schedules. Fixed rather than generated: the
#: card is about failures not stopping a schedule, and a generated horizon adds
#: nothing to that except cases with no iterations in them.
HORIZON = 6


@dataclass(frozen=True)
class SchedulePlan:
    period: int
    raising: frozenset[int]

    @property
    def iterations(self) -> int:
        return HORIZON // self.period

    def failures(self) -> list[int]:
        return [k for k in sorted(self.raising) if k <= self.iterations]


@st.composite
def failing_plans(draw: st.DrawFn) -> tuple[SchedulePlan, ...]:
    """Several schedules, at least one of which raises inside the horizon."""
    count = draw(st.integers(min_value=2, max_value=3))
    plans: list[SchedulePlan] = []
    for index in range(count):
        period = draw(st.integers(min_value=1, max_value=3))
        raising = draw(
            st.frozensets(st.integers(min_value=1, max_value=HORIZON), max_size=4)
        )
        if index == 0 and not raising:
            raising = frozenset({1})  # the card's precondition, by construction
        plans.append(SchedulePlan(period=period, raising=raising))
    return tuple(plans)


@dataclass
class Raiser:
    """A callback that fails on the iterations the generator chose."""

    index: int
    raising: frozenset[int]
    seen: int = 0
    tags: list[str] = field(default_factory=list)

    def __call__(self) -> None:
        self.seen += 1
        self.tags.append(f"{self.index}:{self.seen}")
        if self.seen in self.raising:
            msg = f"{self.index}:{self.seen}"
            raise RuntimeError(msg)


@pytest.mark.tier_local
@settings(max_examples=200, deadline=None)
@given(plans=failing_plans())
async def test_a_raising_iteration_stops_nothing(
    plans: tuple[SchedulePlan, ...],
) -> None:
    """PROP-TIMER-005: counts match elapsed deadlines whichever iterations raise.

    Failure value: a transient exception silently terminating a health-check
    loop, so the system stops monitoring itself and nothing reports that it
    stopped.
    """
    world = await open_world()
    reported: list[list[TimerFailure]] = [[] for _ in plans]
    bodies = [Raiser(index, plan.raising) for index, plan in enumerate(plans)]

    schedules = [
        interval(world.ctx, plan.period, bodies[index], on_error=reported[index].append)
        for index, plan in enumerate(plans)
    ]

    await world.clock.drain()  # every grid starts when its loop does
    await world.clock.advance(HORIZON)

    for index, plan in enumerate(plans):
        assert schedules[index].completed == plan.iterations
        assert schedules[index].skipped == 0
        assert bodies[index].tags == [
            f"{index}:{k}" for k in range(1, plan.iterations + 1)
        ]
        assert [str(failure.error) for failure in reported[index]] == [
            f"{index}:{k}" for k in plan.failures()
        ]

    await world.scope.dispose()


# --------------------------------------------------------------------------
# The seams the properties do not reach
# --------------------------------------------------------------------------


@pytest.mark.tier_local
def test_time_is_a_service_and_the_system_clock_is_the_fallback() -> None:
    """SEM-007: any object with `now` and `sleep` is a clock; absent one, real time."""
    assert isinstance(VirtualClock(), Clock)
    assert isinstance(clock_of(Context(label="bare")), SystemClock)


@pytest.mark.tier_local
async def test_a_bound_clock_wins() -> None:
    world = await open_world()
    assert clock_of(world.ctx) is world.clock


@pytest.mark.tier_local
@pytest.mark.parametrize("period", [0, -1])
async def test_a_schedule_with_no_period_is_rejected_at_the_call(period: int) -> None:
    """A period of zero is a busy loop, and the error belongs at the call site."""
    world = await open_world()
    with pytest.raises(ValueError, match="positive"):
        interval(world.ctx, period, lambda: None)
    with pytest.raises(ValueError, match="positive"):
        throttle(world.ctx, period, lambda: None)
    with pytest.raises(ValueError, match="positive"):
        debounce(world.ctx, period, lambda: None)


@pytest.mark.tier_local
async def test_a_wrapper_passes_through_whatever_it_was_called_with() -> None:
    world = await open_world()
    seen: list[tuple[tuple[int, ...], dict[str, str]]] = []

    def record(*args: int, **kwargs: str) -> None:
        seen.append((args, kwargs))

    call = debounce(world.ctx, 2, record)
    call(1, 2, tag="a")
    await world.clock.drain()
    await world.clock.advance(3)

    assert seen == [((1, 2), {"tag": "a"})]
    await world.scope.dispose()


@pytest.mark.tier_local
async def test_one_effect_per_wrapper_however_often_it_is_called() -> None:
    """The wrapper owns one task; a thousand calls must not leave a thousand records."""
    world = await open_world()
    throttled = throttle(world.ctx, 2, lambda: None)
    settled = debounce(world.ctx, 2, lambda: None)
    before = len(world.scope.effects())
    for _ in range(50):
        throttled()
        settled()
        await world.clock.drain()
    assert len(world.scope.effects()) == before

    await world.scope.dispose()
    # Both wrappers had a task in flight at disposal; neither deadline may
    # survive the scope that owns it.
    assert world.clock.pending == ()


@pytest.mark.tier_local
async def test_a_failure_with_nowhere_to_go_reaches_the_log() -> None:
    """The record's first deviation: no `on_error` means the bound logger."""
    registry = ServiceRegistry()
    root = Context(resolver=registry, label="root")
    host = EffectScope("host")
    clock = VirtualClock()
    await registry.provide("clock", clock, scope=host, ctx=root)
    service = LoggerService(root)
    await registry.provide("logger", service, scope=host, ctx=root)
    scope = host.child("plugin")
    ctx = root.extend(**{SCOPE_KEY: scope})

    seen: list[Record] = []

    class Sink:
        def export(self, record: Record, /) -> None:
            seen.append(record)

    service.add_exporter(Sink(), scope=host)

    def explode() -> None:
        msg = "boom"
        raise RuntimeError(msg)

    timeout(ctx, 1, explode)
    await clock.drain()
    await clock.advance(1)

    assert [record.level for record in seen] == [Level.ERROR]
    assert "timeout:1" in seen[0].render()

    await scope.dispose()
    await host.dispose()


@pytest.mark.tier_local
def test_the_models_agree_with_themselves() -> None:
    """The models, read once against hand-worked examples.

    A model that is wrong in the same direction as the implementation proves
    nothing, so it is worth checking the two folds against cases whose answers
    were worked out from SEM-006's wording rather than from either program.
    """
    # Three calls inside one window: one invocation, at the window's end,
    # carrying the last argument.
    assert model_throttle(5, [(0, 1), (1, 2), (2, 3)]) == [(5, 3)]
    # A call exactly at the window's end starts the next window.
    assert model_throttle(5, [(0, 1), (5, 2)]) == [(5, 1), (10, 2)]
    # A burst with no gap longer than the delay settles once, after the last.
    assert model_debounce(5, [(0, 1), (2, 2), (4, 3)]) == [(9, 3)]
    # A gap of exactly the delay lets the first one through.
    assert model_debounce(5, [(0, 1), (5, 2)]) == [(5, 1), (10, 2)]
