"""PROP-EVENT-001..009, transcribed from spec/capabilities/06-event-bus.yaml.

Every expectation here is computed from the plan the generator produced, before
the bus runs: the live listener set from the test's own register/dispose log,
the invocation order by list arithmetic over that log, the serial result from
the list of planned returns, the waterfall result by composing the
transformations with plain function calls. What the listeners record while they
run is then compared against it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cordis.effect import EffectScope
from cordis.errors import EventModeError, NextCalledTwiceError
from cordis.events import Bail, Emit, EventBus, Parallel, Serial, Waterfall
from tests.support import strategies as gen

if TYPE_CHECKING:
    from collections.abc import Callable

    from cordis.effect import EffectHandle
    from cordis.events import ErrorReport, Event, Next


class MarkerError(RuntimeError):
    """Carries the index of the listener that raised it."""

    def __init__(self, index: int) -> None:
        super().__init__(f"listener {index} failed")
        self.index = index


@dataclass
class Calls:
    """What actually happened, written by the listeners themselves."""

    order: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def record(self, label: str) -> None:
        self.order.append(label)
        self.counts[label] = self.counts.get(label, 0) + 1


#: Falsy-but-present answers, listed explicitly (PROP-EVENT-004 strategy_hint).
#: Sampling from ``st.integers() | st.none()`` would make the interesting case
#: -- an answer that is false -- vanishingly rare, and that case is the whole
#: point of the card.
ANSWERS: tuple[object, ...] = (None, False, 0, "", (), "yes", 7, True)


# --------------------------------------------------------------------------
# PROP-EVENT-001
# --------------------------------------------------------------------------

Step = tuple[str, bool]


@pytest.mark.tier_local
@given(
    plan=st.lists(
        st.tuples(st.sampled_from(["register", "dispose", "dispatch"]), st.booleans()),
        min_size=1,
        max_size=25,
    )
)
async def test_dispatch_invokes_exactly_the_listeners_live_when_it_began(
    plan: list[Step],
) -> None:
    """Failure value: a listener that disposes itself during dispatch causing
    the iteration to skip the following listener -- an off-by-one that only
    appears when a one-shot handler is registered next to a permanent one."""
    bus = EventBus()
    scope = EffectScope("root")
    event: Emit[[]] = Emit("probe")
    calls = Calls()

    live: list[str] = []  # the model: written by the test, never read back
    handles: dict[str, EffectHandle] = {}
    issued = 0

    for action, flag in plan:
        if action == "register":
            label = f"l{issued}"
            issued += 1

            def listener(label: str = label, *, suicidal: bool = flag) -> None:
                calls.record(label)
                if suicidal:
                    handles[label]()  # disposal from inside a live dispatch
                    if label in live:
                        live.remove(label)

            handles[label] = bus.on(event, listener, scope=scope)
            live.append(label)
        elif action == "dispose" and live:
            handles[live.pop(0 if flag else -1)]()
        else:
            expected = list(live)
            before = dict(calls.counts)
            await bus.emit(event)
            for label in expected:
                assert calls.counts.get(label, 0) == before.get(label, 0) + 1
            for label in set(calls.counts) - set(expected):
                assert calls.counts[label] == before.get(label, 0)


# --------------------------------------------------------------------------
# PROP-EVENT-002
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(flags=st.lists(st.booleans(), min_size=1, max_size=20))
async def test_invocation_order_is_registration_order_with_prepends_reversed(
    flags: list[bool],
) -> None:
    """Failure value: implementing prepend with ``list.insert(0, ...)`` inside
    a shared list, so two prepending plugins end up in registration order
    rather than reverse -- making "get in front of everyone" depend on load
    order."""
    bus = EventBus()
    scope = EffectScope("root")
    event: Emit[[]] = Emit("ordered")
    calls = Calls()

    prepended: list[str] = []
    normal: list[str] = []

    for index, prepend in enumerate(flags):
        label = f"l{index}"
        bus.on(event, partial(calls.record, label), scope=scope, prepend=prepend)
        (prepended if prepend else normal).append(label)

    await bus.emit(event)

    assert calls.order == list(reversed(prepended)) + normal
    assert len(bus.listeners(event)) == len(flags)


# --------------------------------------------------------------------------
# PROP-EVENT-003
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(
    shape=st.lists(st.tuples(st.booleans(), st.booleans()), min_size=1, max_size=10),
    concurrent=st.booleans(),
)
async def test_every_listener_runs_once_however_many_raise(
    shape: list[tuple[bool, bool]], *, concurrent: bool
) -> None:
    """Failure value: iterating listeners with a single try/except around the
    loop, so the first raising listener silently cancels delivery to every
    later one -- an audit-log listener registered last never seeing an event
    because an unrelated metrics listener threw."""
    bus = EventBus()
    scope = EffectScope("root")
    calls = Calls()
    reports: list[ErrorReport] = []
    bus.on_error(reports.append)

    event: Any = Parallel("p") if concurrent else Emit("e")
    failing = {index for index, (fails, _) in enumerate(shape) if fails}

    for index, (fails, is_async) in enumerate(shape):
        label = f"l{index}"

        def sync(
            label: str = label, *, fails: bool = fails, index: int = index
        ) -> None:
            calls.record(label)
            if fails:
                raise MarkerError(index)

        async def coro(
            label: str = label, *, fails: bool = fails, index: int = index
        ) -> None:
            await asyncio.sleep(0)
            calls.record(label)
            if fails:
                raise MarkerError(index)

        bus.on(event, coro if is_async else sync, scope=scope)

    if not concurrent:
        # SEM-004: failures reach the error channel, never the caller.
        await bus.emit(event)
        raised = {
            report.error.index
            for report in reports
            if isinstance(report.error, MarkerError)
        }
    elif failing:
        # SEM-005: every listener settles first, then one group carries them.
        with pytest.raises(ExceptionGroup) as caught:
            await bus.parallel(event)
        raised = {
            exc.index for exc in caught.value.exceptions if isinstance(exc, MarkerError)
        }
    else:
        await bus.parallel(event)
        raised = set()

    assert raised == failing
    assert calls.counts == {f"l{index}": 1 for index in range(len(shape))}


# --------------------------------------------------------------------------
# PROP-EVENT-004
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(
    answers=st.lists(st.sampled_from(ANSWERS), min_size=1, max_size=8),
    synchronous=st.booleans(),
)
async def test_the_first_non_none_answer_wins_and_stops_the_chain(
    answers: list[object], *, synchronous: bool
) -> None:
    """Failure value: using truthiness instead of an ``is not None`` check, so
    a policy listener returning ``False`` ("deny") is treated as abstention and
    the next listener silently allows the operation."""
    bus = EventBus()
    scope = EffectScope("root")
    calls = Calls()

    # Computed from the generated plan, before anything runs.
    cutoff = next(
        (index for index, answer in enumerate(answers) if answer is not None),
        len(answers),
    )
    expected = answers[cutoff] if cutoff < len(answers) else None
    invoked = [f"l{index}" for index in range(min(cutoff + 1, len(answers)))]

    event: Any = Bail("b") if synchronous else Serial("s")
    for index, answer in enumerate(answers):

        def listener(index: int = index, answer: object = answer) -> object:
            calls.record(f"l{index}")
            return answer

        bus.on(event, listener, scope=scope)

    result = bus.bail(event) if synchronous else await bus.serial(event)

    # Identity, not equality: ``0 == False`` would let a truthiness bug pass
    # for two of the sampled answers.
    assert result is expected
    assert calls.order == invoked


# --------------------------------------------------------------------------
# PROP-EVENT-010
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(count=st.integers(min_value=1, max_value=6), data=st.data())
async def test_an_async_listener_on_a_bail_event_is_rejected(
    count: int, data: st.DataObject
) -> None:
    """Failure value: returning the un-awaited coroutine as the answer, so a
    bail chain "gets an answer" that is a coroutine object -- truthy, never
    executed, and reported by asyncio as "coroutine was never awaited" from a
    line unrelated to the listener that produced it."""
    bus = EventBus()
    scope = EffectScope("root")
    event: Bail[[], str] = Bail("sync-only")
    calls = Calls()

    offender = data.draw(st.integers(min_value=0, max_value=count - 1))

    for index in range(count):
        if index == offender:

            async def listener(index: int = index) -> str | None:
                calls.record(f"l{index}")
                return None

            # The cast is the point of the card: mypy rejects an async listener
            # on a Bail event, so the only way to reach SEM-011 is a call site
            # that is not type-checked -- a dynamically registered listener, or
            # a plugin shipped without a type checker.
            bus.on(event, cast("Callable[[], str | None]", listener), scope=scope)
        else:

            def sync(index: int = index) -> str | None:
                calls.record(f"l{index}")
                return None  # everyone before the offender abstains

            bus.on(event, sync, scope=scope)

    with pytest.raises(EventModeError) as caught:
        bus.bail(event)

    assert caught.value.event == "sync-only"
    assert caught.value.declared == "bail"
    assert "awaitable" in caught.value.attempted
    # The offender's own body never runs -- calling a coroutine function only
    # builds the coroutine -- so the last listener to leave a mark is the one
    # before it, and nothing after it was reached.
    assert calls.counts == {f"l{index}": 1 for index in range(offender)}


# --------------------------------------------------------------------------
# PROP-EVENT-005
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(marks=gen.unique_names(min_size=1, max_size=12), start=gen.identifiers)
async def test_a_full_waterfall_composes_in_onion_order(
    marks: tuple[str, ...], start: str
) -> None:
    """Failure value: building the chain by reducing in the wrong direction, so
    listeners wrap in registration order on the way out instead of the way in
    -- reversing the nesting of every interceptor without changing any single
    result in an obvious way."""
    bus = EventBus()
    scope = EffectScope("root")
    event: Waterfall[[], str] = Waterfall("wrap")

    for mark in marks:
        # Non-commutative by construction: the mark lands on both sides of the
        # downstream result, so a reversed nesting cannot produce the same
        # string for any set of marks.
        async def listener(nxt: Next[str], mark: str = mark) -> str:
            return f"{mark}<{await nxt()}>{mark}"

        bus.on(event, listener, scope=scope)

    result = await bus.waterfall(event, lambda: start)

    expected = start
    for mark in reversed(marks):  # the last listener wraps the default first
        expected = f"{mark}<{expected}>{mark}"
    assert result == expected


# --------------------------------------------------------------------------
# PROP-EVENT-006
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(count=st.integers(min_value=1, max_value=8), data=st.data())
async def test_a_listener_that_skips_next_short_circuits_the_chain(
    count: int, data: st.DataObject
) -> None:
    """Failure value: running the default unconditionally after the chain, so
    an approval listener that denies an operation returns "denied" while the
    operation still executes."""
    bus = EventBus()
    scope = EffectScope("root")
    event: Waterfall[[], str] = Waterfall("gate")
    calls = Calls()

    cut = data.draw(st.integers(min_value=0, max_value=count - 1))
    verdict = data.draw(st.text(min_size=1, max_size=8))

    for index in range(count):

        async def listener(
            nxt: Next[str], index: int = index, cut: int = cut, verdict: str = verdict
        ) -> str:
            calls.record(f"l{index}")
            if index == cut:
                return verdict
            return await nxt()

        bus.on(event, listener, scope=scope)

    def default() -> str:
        calls.record("default")
        return "default"

    assert await bus.waterfall(event, default) == verdict
    assert calls.order == [f"l{index}" for index in range(cut + 1)]
    assert "default" not in calls.counts


# --------------------------------------------------------------------------
# PROP-EVENT-007
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(count=st.integers(min_value=1, max_value=6), data=st.data())
async def test_calling_next_twice_raises_and_does_not_re_enter(
    count: int, data: st.DataObject
) -> None:
    """Failure value: a retry-style listener that calls next again on failure,
    silently re-executing a non-idempotent downstream tool call and producing
    duplicate writes."""
    bus = EventBus()
    scope = EffectScope("root")
    event: Waterfall[[], str] = Waterfall("retry")
    calls = Calls()

    offender = data.draw(st.integers(min_value=0, max_value=count - 1))

    for index in range(count):

        async def listener(
            nxt: Next[str], index: int = index, offender: int = offender
        ) -> str:
            calls.record(f"l{index}")
            first = await nxt()
            if index == offender:
                await nxt()  # the defect this property exists to catch
            return first

        bus.on(event, listener, scope=scope)

    def default() -> str:
        calls.record("default")
        return "base"

    with pytest.raises(NextCalledTwiceError) as caught:
        await bus.waterfall(event, default)

    assert caught.value.event == "retry"
    # Both halves of the oracle: the guard produced the error, *and* the
    # downstream chain ran exactly once. A guard that raises only after
    # re-entering would satisfy the first and fail this.
    assert calls.counts == {
        **{f"l{index}": 1 for index in range(count)},
        "default": 1,
    }


# --------------------------------------------------------------------------
# PROP-EVENT-008
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(
    parents=st.lists(st.integers(min_value=0, max_value=8), min_size=1, max_size=6),
    sizes=st.lists(st.integers(min_value=0, max_value=3), min_size=1, max_size=6),
    data=st.data(),
)
async def test_disposing_a_scope_unsubscribes_its_listeners(
    parents: list[int], sizes: list[int], data: st.DataObject
) -> None:
    """Failure value: listener registration bypassing the effect scope for
    performance, so unloading a plugin leaves its handlers subscribed and every
    reload multiplies handling."""
    bus = EventBus()
    root = EffectScope("root")
    event: Emit[[]] = Emit("scoped")
    calls = Calls()

    count = min(len(parents), len(sizes))
    # owner[i] is an *earlier* scope, or -1 for the root: a generated tree.
    owner = [
        -1 if parents[index] >= index else parents[index] for index in range(count)
    ]

    scopes = [root]
    for index in range(count):
        scopes.append(scopes[owner[index] + 1].child(f"plugin{index}"))

    membership: dict[int, list[str]] = {}
    for index in range(count):
        membership[index] = []
        for n in range(sizes[index]):
            label = f"s{index}.{n}"
            bus.on(event, partial(calls.record, label), scope=scopes[index + 1])
            membership[index].append(label)

    def subtree(index: int) -> set[int]:
        """The scopes a disposal takes with it -- the test's own bookkeeping."""
        out = {index}
        for other in range(index + 1, count):
            if owner[other] in out:
                out.add(other)
        return out

    gone: set[str] = set()
    for index in data.draw(st.permutations(range(count))):
        await scopes[index + 1].dispose()
        for fallen in subtree(index):
            gone.update(membership[fallen])

        before = dict(calls.counts)
        await bus.emit(event)
        for label in {*calls.counts, *gone}:
            increment = 0 if label in gone else 1
            assert calls.counts.get(label, 0) == before.get(label, 0) + increment


# --------------------------------------------------------------------------
# PROP-EVENT-009
# --------------------------------------------------------------------------

MODES: dict[str, Callable[[str], Event]] = {
    "emit": Emit,
    "parallel": Parallel,
    "serial": Serial,
    "bail": Bail,
    "waterfall": Waterfall,
}


@pytest.mark.tier_local
@given(declared=st.sampled_from(sorted(MODES)))
async def test_dispatching_under_the_wrong_mode_raises_and_delivers_nothing(
    declared: str,
) -> None:
    """Failure value: a dynamic dispatch site emitting a waterfall event, so
    every interceptor runs with a ``next`` that is never supplied and the
    pipeline's default behaviour is silently replaced by None."""
    bus = EventBus()
    scope = EffectScope("root")
    calls = Calls()
    event: Any = MODES[declared]("subject")

    bus.on(event, lambda *_a, **_kw: calls.record("listener"), scope=scope)

    async def dispatch(mode: str) -> None:
        if mode == "emit":
            await bus.emit(event)
        elif mode == "parallel":
            await bus.parallel(event)
        elif mode == "serial":
            await bus.serial(event)
        elif mode == "bail":
            bus.bail(event)
        else:
            await bus.waterfall(event, lambda: "default")

    for mode in MODES:
        if mode == declared:
            continue
        with pytest.raises(EventModeError) as caught:
            await dispatch(mode)
        assert caught.value.event == "subject"
        assert caught.value.declared == declared
        assert caught.value.attempted == mode

    assert calls.counts == {}
