"""PROP-LOG-001..006, from spec/capabilities/12-logging.yaml.

The model is written once, in `Plan`: a step issues one record, an exporter is
alive between its registration step and its disposal step, and the exporter that
registers first also receives whatever was buffered before it arrived. What each
exporter *should* see is computed from the generated plan alone -- indices and
levels the generator chose -- so agreement with what it did see is evidence
rather than a second copy of the delivery loop.
"""

from __future__ import annotations

import asyncio
import io
import itertools
import logging as stdlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from cordis.context import Context
from cordis.effect import EffectScope
from cordis.exporters import ConsoleExporter, StdlibExporter
from cordis.logging import (
    DETACHED,
    Exporter,
    ExportFailure,
    Level,
    LoggerService,
    Record,
    logger,
)
from cordis.plugin import PluginHost, fiber_of
from cordis.registry import ServiceRegistry

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from cordis.effect import EffectHandle

LEVELS = (Level.DEBUG, Level.INFO, Level.WARNING, Level.ERROR)

#: The name the test's own records carry. The service's own notices (the
#: overflow report) carry `logger`, and are collected separately.
APP = "app"


# --------------------------------------------------------------------------
# The model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SinkSpec:
    """One exporter: when it arrives, when it leaves, and what it wants."""

    at: int
    until: int | None
    level: Level
    #: Steps whose record this exporter raises on, having received it.
    raising: frozenset[int] = frozenset()


@dataclass(frozen=True)
class Plan:
    """One record per step, at the level the generator chose, plus exporters."""

    calls: tuple[Level, ...]
    sinks: tuple[SinkSpec, ...]

    @property
    def first(self) -> int:
        """The exporter that registers first: the one the buffer is replayed to."""
        order = sorted(range(len(self.sinks)), key=lambda i: (self.sinks[i].at, i))
        return order[0]

    def live(self, index: int, step: int) -> bool:
        sink = self.sinks[index]
        return sink.at <= step and (sink.until is None or step < sink.until)

    def expected(self, index: int) -> list[int]:
        """The steps whose record exporter ``index`` should receive, in order."""
        sink = self.sinks[index]
        replayed = (
            [step for step in range(sink.at) if self.calls[step] >= sink.level]
            if index == self.first
            else []
        )
        live = [
            step
            for step in range(sink.at, len(self.calls))
            if self.live(index, step) and self.calls[step] >= sink.level
        ]
        return replayed + live

    def failures(self) -> int:
        """How many deliveries should raise, over the whole run."""
        return sum(
            1
            for index, sink in enumerate(self.sinks)
            for step in self.expected(index)
            if step in sink.raising
        )


@st.composite
def plans(draw: st.DrawFn, *, raising: bool = False, sinks: int = 3) -> Plan:
    count = draw(st.integers(min_value=1, max_value=8))
    calls = tuple(
        draw(st.lists(st.sampled_from(LEVELS), min_size=count, max_size=count))
    )
    total = draw(st.integers(min_value=2 if raising else 1, max_value=sinks))
    specs: list[SinkSpec] = []
    for index in range(total):
        at = draw(st.integers(min_value=0, max_value=count - 1))
        until = draw(
            st.one_of(st.none(), st.integers(min_value=at, max_value=count - 1))
        )
        level = draw(st.sampled_from(LEVELS))
        # The first exporter always raises and the last never does, so the card
        # has both a witness and a control without filtering anything away.
        breaks = frozenset(
            draw(st.frozensets(st.integers(min_value=0, max_value=count - 1)))
            if raising and index < total - 1
            else ()
        )
        if raising and index == 0 and not breaks:
            breaks = frozenset(range(count))
        specs.append(SinkSpec(at=at, until=until, level=level, raising=breaks))
    return Plan(calls, tuple(specs))


# --------------------------------------------------------------------------
# Building a plan
# --------------------------------------------------------------------------


@dataclass
class World:
    """A plan, run: what each exporter saw, and what the service reported."""

    service: LoggerService
    scope: EffectScope
    root: Context
    received: list[list[int]] = field(default_factory=list)
    seqs: list[list[int]] = field(default_factory=list)
    notices: list[Record] = field(default_factory=list)
    failures: list[ExportFailure] = field(default_factory=list)
    handles: list[EffectHandle | None] = field(default_factory=list)


class Collector:
    """An exporter that records the step of every record it is handed.

    Renders each record, because rendering is what a real exporter does and
    what makes PROP-LOG-004's formatting spies observable.
    """

    def __init__(self, world: World, index: int, raising: frozenset[int]) -> None:
        self._world = world
        self._index = index
        self._raising = raising

    def export(self, record: Record, /) -> None:
        record.render()
        # Every arrival is recorded, notices included, and in arrival order:
        # "the sequence numbers this exporter saw" is only a statement about
        # ordering if nothing is filtered out of it first.
        self._world.seqs[self._index].append(record.seq)
        if record.name != APP:  # the service's own overflow notice
            self._world.notices.append(record)
            return
        step = int(record.args[0])  # type: ignore[call-overload]
        self._world.received[self._index].append(step)
        if step in self._raising:
            msg = f"exporter {self._index} is broken"
            raise RuntimeError(msg)


def open_world(*, buffer: int | None = None) -> World:
    registry = ServiceRegistry()
    root = Context(resolver=registry, label="root")
    service = LoggerService(root, buffer=buffer)
    world = World(service=service, scope=EffectScope("test"), root=root)
    service.on_error(world.failures.append)
    return world


def run(plan: Plan) -> World:
    """Register, dispose and log in step order; nothing here inspects delivery."""
    world = open_world()
    for _ in plan.sinks:
        world.received.append([])
        world.seqs.append([])
        world.handles.append(None)
    log = world.service(APP, fiber="test")
    for step, level in enumerate(plan.calls):
        for index, sink in enumerate(plan.sinks):
            if sink.at == step:
                world.handles[index] = world.service.add_exporter(
                    Collector(world, index, sink.raising),
                    scope=world.scope,
                    level=sink.level,
                )
        for index, sink in enumerate(plan.sinks):
            if sink.until == step:
                handle = world.handles[index]
                assert handle is not None
                handle()
        log.log(level, "call %s", step)
    return world


def ascending(values: Sequence[int]) -> bool:
    return all(earlier < later for earlier, later in itertools.pairwise(values))


# --------------------------------------------------------------------------
# PROP-LOG-001
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@settings(max_examples=200, deadline=None)
@given(plan=plans())
def test_every_record_reaches_every_live_exporter_once_in_order(plan: Plan) -> None:
    """PROP-LOG-001: delivery is exactly the live window, in sequence order.

    Failure value: delivering through a queue with no ordering guarantee, so
    interleaved records appear shuffled and a causal sequence in the log no
    longer reflects the real one.
    """
    world = run(plan)
    for index in range(len(plan.sinks)):
        assert world.received[index] == plan.expected(index)
        assert ascending(world.seqs[index])


@pytest.mark.tier_local
def test_a_record_carries_everything_a_reader_needs() -> None:
    """SEM-001, stated as a test: the fields are not optional."""
    world = open_world()
    seen: list[Record] = []
    world.service.add_exporter(_into(seen), scope=world.scope, level=Level.DEBUG)
    world.service(APP, fiber="root/tools#0").warning("hi %s", "there", where="here")
    (record,) = seen
    assert record.seq == 1
    assert record.name == APP
    assert record.level is Level.WARNING
    assert record.fiber == "root/tools#0"
    assert record.render() == "hi there"
    assert record.extra == {"where": "here"}


@pytest.mark.tier_local
def test_the_clock_is_a_service_when_one_is_bound() -> None:
    """The timestamp is not `time.time` by fiat -- a bound clock wins."""
    registry = ServiceRegistry()
    root = Context(resolver=registry, label="root")
    scope = EffectScope("test")
    registry.provide("clock", _FixedClock(41.5), scope=scope, ctx=root)
    seen: list[Record] = []
    service = LoggerService(root)
    service.add_exporter(_into(seen), scope=scope, level=Level.DEBUG)
    service(APP, fiber="t").info("tick")
    assert [record.ts for record in seen] == [41.5]


# --------------------------------------------------------------------------
# PROP-LOG-002
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@settings(max_examples=200, deadline=None)
@given(plan=plans(raising=True))
def test_a_raising_exporter_costs_nobody_else_anything(plan: Plan) -> None:
    """PROP-LOG-002: containment, and the report that proves it was contained.

    Failure value: a file exporter failing on a full disk taking the console
    exporter down with it, so the operator loses visibility exactly when the
    system is degrading.
    """
    world = run(plan)
    for index, sink in enumerate(plan.sinks):
        if not sink.raising:
            assert world.received[index] == plan.expected(index)
    assert len(world.failures) == plan.failures()
    assert all(isinstance(report.error, RuntimeError) for report in world.failures)


# --------------------------------------------------------------------------
# PROP-LOG-003
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@settings(max_examples=200, deadline=None)
@given(bound=st.sampled_from((0, 1, 2, 5)), factor=st.sampled_from((-1, 0, 1, 3)))
def test_the_buffer_replays_its_tail_and_owns_up_to_the_rest(
    bound: int, factor: int
) -> None:
    """PROP-LOG-003: what survives the wait, and what is admitted as lost.

    Failure value: an unbounded buffer, so a process that never mounts an
    exporter -- the normal case for a library embedding the framework -- grows
    until it is killed.
    """
    issued = max(0, bound + factor if factor <= 1 else bound * factor)
    world = open_world(buffer=bound)
    log = world.service(APP, fiber="test")
    for step in range(issued):
        log.info("call %s", step)
    dropped = max(0, issued - bound)
    assert world.service.dropped == dropped

    world.received.append([])
    world.seqs.append([])
    world.service.add_exporter(
        Collector(world, 0, frozenset()), scope=world.scope, level=Level.DEBUG
    )
    kept = min(bound, issued)
    assert world.received[0] == list(range(issued - kept, issued))
    assert [notice.extra["dropped"] for notice in world.notices] == (
        [dropped] if dropped else []
    )
    # After the replay, never before it. A notice built at replay time carries a
    # higher sequence number than everything it describes, so delivering it
    # first would hand this exporter a descending pair on its first two records.
    assert ascending(world.seqs[0])


# --------------------------------------------------------------------------
# PROP-LOG-004
# --------------------------------------------------------------------------


class Spy:
    """A log argument that counts how many times it was rendered."""

    def __init__(self) -> None:
        self.count = 0

    def __str__(self) -> str:
        self.count += 1
        return "spy"


@pytest.mark.tier_local
@settings(max_examples=200, deadline=None)
@given(plan=plans())
def test_nothing_is_formatted_for_a_reader_that_does_not_exist(plan: Plan) -> None:
    """PROP-LOG-004: formatting happens once per delivery, and never otherwise.

    Failure value: eager f-string style formatting inside the logger, making a
    debug call in a per-token hot path cost a string build per token even when
    debug output is off.
    """
    world = open_world()
    for _ in plan.sinks:
        world.received.append([])
        world.seqs.append([])
        world.handles.append(None)
    spies = [Spy() for _ in plan.calls]
    log = world.service(APP, fiber="test")
    for step, level in enumerate(plan.calls):
        for index, sink in enumerate(plan.sinks):
            if sink.at == step:
                world.handles[index] = world.service.add_exporter(
                    Collector(world, index, sink.raising),
                    scope=world.scope,
                    level=sink.level,
                )
        for index, sink in enumerate(plan.sinks):
            if sink.until == step:
                handle = world.handles[index]
                assert handle is not None
                handle()
        log.log(level, "call %s %s", step, spies[step])

    for step, spy in enumerate(spies):
        deliveries = sum(
            1 for index in range(len(plan.sinks)) if step in plan.expected(index)
        )
        assert spy.count == deliveries


# --------------------------------------------------------------------------
# PROP-LOG-005
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@settings(max_examples=100, deadline=None)
@given(counts=st.lists(st.integers(min_value=1, max_value=3), min_size=1, max_size=4))
def test_every_record_names_the_instance_that_wrote_it(counts: list[int]) -> None:
    """PROP-LOG-005: attribution follows the asking context, not the service.

    Failure value: attributing records to the fiber that *provides* the logger
    service, so every record in a forty-plugin tree says `root`.
    """
    labels: dict[int, str] = {}
    collected: list[Record] = []

    def make(index: int, total: int) -> Callable[[Context], None]:
        def body(ctx: Context) -> None:
            owner = fiber_of(ctx)
            assert owner is not None
            labels[index] = owner.label
            log = logger(ctx, "work")
            for _ in range(total):
                log.info("from %s", index)

        body.__name__ = f"plugin{index}"
        return body

    async def build() -> None:
        host = PluginHost()
        await host.root.plugin(LoggerService)
        service = host.root.context.require(LoggerService)
        service.add_exporter(_into(collected), scope=host.root.scope)
        for index, total in enumerate(counts):
            await host.root.plugin(make(index, total))
        await host.dispose()

    asyncio.run(build())

    assert len(collected) == sum(counts)
    assert len(set(labels.values())) == len(counts), (
        "distinct instances, distinct names"
    )
    for record in collected:
        assert record.fiber == labels[int(record.args[0])]  # type: ignore[call-overload]


@pytest.mark.tier_local
def test_a_logger_made_outside_a_mount_says_so() -> None:
    """There is no fiber to name, and inventing one would be worse than saying so."""
    registry = ServiceRegistry()
    root = Context(resolver=registry, label="root")
    scope = EffectScope("test")
    registry.provide("logger", LoggerService(root), scope=scope, ctx=root)
    assert logger(root, "loose").fiber == DETACHED


# --------------------------------------------------------------------------
# PROP-LOG-006
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@settings(max_examples=200, deadline=None)
@given(plan=plans())
def test_no_record_outlives_the_scope_that_wanted_it(plan: Plan) -> None:
    """PROP-LOG-006: disposal is immediate, measured in sequence numbers.

    Failure value: a file exporter still receiving records after its plugin
    unloads, writing to a handle a later reload has already reopened and
    truncated.
    """
    assume(any(sink.until is not None for sink in plan.sinks))
    world = open_world()
    scopes: list[EffectScope] = []
    marks: dict[int, int] = {}
    for index in range(len(plan.sinks)):
        world.received.append([])
        world.seqs.append([])
        world.handles.append(None)
        scopes.append(world.scope.child(f"sink{index}"))

    async def drive() -> None:
        log = world.service(APP, fiber="test")
        for step, level in enumerate(plan.calls):
            for index, sink in enumerate(plan.sinks):
                if sink.at == step:
                    world.service.add_exporter(
                        Collector(world, index, sink.raising),
                        scope=scopes[index],
                        level=sink.level,
                    )
            for index, sink in enumerate(plan.sinks):
                if sink.until == step:
                    marks[index] = world.service.sequence
                    await scopes[index].dispose()
            log.log(level, "call %s", step)

    asyncio.run(drive())

    for index, mark in marks.items():
        assert all(seq <= mark for seq in world.seqs[index])
    for index in range(len(plan.sinks)):
        assert world.received[index] == plan.expected(index)


# --------------------------------------------------------------------------
# Seams
# --------------------------------------------------------------------------


@pytest.mark.tier_local
def test_an_exporter_may_carry_its_own_level() -> None:
    """The keyword is the override, not the only way to say it."""

    class Picky:
        level = Level.ERROR

        def __init__(self) -> None:
            self.seen: list[Record] = []

        def export(self, record: Record, /) -> None:
            self.seen.append(record)

    world = open_world()
    picky = Picky()
    world.service.add_exporter(picky, scope=world.scope)
    log = world.service(APP, fiber="test")
    log.info("quiet")
    log.error("loud")
    assert [record.render() for record in picky.seen] == ["loud"]


@pytest.mark.tier_local
def test_the_threshold_closes_when_the_last_exporter_leaves() -> None:
    """Nothing is buffered a second time: the boot window happens once."""
    world = open_world()
    seen: list[Record] = []
    handle = world.service.add_exporter(_into(seen), scope=world.scope)
    log = world.service(APP, fiber="test")
    log.info("during")
    handle()
    log.info("after")
    assert world.service.sequence == 1, "no record is built with nobody to read it"
    world.service.add_exporter(_into(seen), scope=world.scope)
    assert [record.render() for record in seen] == ["during"], "no second replay"


@pytest.mark.tier_local
def test_the_console_exporter_writes_one_line_per_record() -> None:
    """The shipped default, checked on the stream it was handed."""
    stream = io.StringIO()
    world = open_world()
    world.service.add_exporter(ConsoleExporter(stream), scope=world.scope)
    log = world.service(APP, fiber="root/tools#0")
    log.debug("invisible at INFO")
    log.warning("disk at %s%%", 91)
    assert stream.getvalue() == "WARNING root/tools#0 app: disk at 91%\n"


@pytest.mark.tier_local
def test_the_stdlib_bridge_hands_over_the_message_unrendered() -> None:
    """Lazy stays lazy across the bridge: stdlib does its own formatting."""
    seen: list[stdlib.LogRecord] = []

    class Capture(stdlib.Handler):
        def emit(self, record: stdlib.LogRecord) -> None:
            seen.append(record)

    bridged = stdlib.getLogger("cordis-test.app")
    bridged.setLevel(stdlib.DEBUG)
    handler = Capture()
    bridged.addHandler(handler)
    try:
        world = open_world()
        world.service.add_exporter(
            StdlibExporter("cordis-test"), scope=world.scope, level=Level.DEBUG
        )
        world.service(APP, fiber="root/tools#0").info("hello %s", "world")
    finally:
        bridged.removeHandler(handler)

    (record,) = seen
    assert record.msg == "hello %s", "unrendered: stdlib formats when it must"
    assert record.getMessage() == "hello world"
    assert record.levelno == int(Level.INFO)
    assert record.cordis_fiber == "root/tools#0"  # type: ignore[attr-defined]


@pytest.mark.tier_local
def test_the_plan_agrees_with_itself() -> None:
    """The generator's own arithmetic, on a shape written by hand."""
    plan = Plan(
        calls=(Level.DEBUG, Level.ERROR, Level.INFO),
        sinks=(
            SinkSpec(at=1, until=None, level=Level.INFO),
            SinkSpec(at=0, until=2, level=Level.DEBUG),
        ),
    )
    assert plan.first == 1, "sink 1 registers at step 0"
    assert plan.expected(1) == [0, 1]
    # Sink 0 arrives at step 1, is not the first, and wants INFO and above.
    assert plan.expected(0) == [1, 2]


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


class _FixedClock:
    def __init__(self, when: float) -> None:
        self._when = when

    def now(self) -> float:
        return self._when


def _into(sink: list[Record]) -> Exporter:
    """An exporter that appends every record it renders."""

    class _Appender:
        def export(self, record: Record, /) -> None:
            record.render()
            sink.append(record)

    return _Appender()
