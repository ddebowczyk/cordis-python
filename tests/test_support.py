"""The harness tests itself.

An oracle nobody checked is a second implementation nobody checked. These
tests hold the instrumentation to the same standard as the code it will judge:
the ledger must detect the violations it exists to detect, the clock must be
deterministic, the models must agree with brute force, and the strategies must
construct rather than filter.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.support import (
    DagSpec,
    EffectSpec,
    EntrySpec,
    IllegalTransitionError,
    LedgerViolationError,
    ResourceLedger,
    TransitionRecorder,
    TreeSpec,
    VirtualClock,
    diff_entries,
)
from tests.support import strategies as gen

# --------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(resources=gen.unique_names(min_size=1, max_size=6))
def test_ledger_accepts_a_balanced_reverse_order_unwind(
    resources: tuple[str, ...],
) -> None:
    ledger = ResourceLedger()
    for resource in resources:
        ledger.acquire(resource)
    for resource in reversed(resources):
        ledger.release(resource)

    assert ledger.balanced
    ledger.assert_unwound(resources)
    assert set(ledger.counts().values()) <= {1}


@pytest.mark.tier_local
@given(resources=gen.unique_names(min_size=2, max_size=6))
def test_ledger_rejects_forward_order_unwind(resources: tuple[str, ...]) -> None:
    """Failure value: a scope disposing in registration order, so a resource is
    torn down while something registered later still depends on it."""
    ledger = ResourceLedger()
    for resource in resources:
        ledger.acquire(resource)
    for resource in resources:
        ledger.release(resource)

    with pytest.raises(LedgerViolationError):
        ledger.assert_unwound(resources)


@pytest.mark.tier_local
def test_ledger_detects_double_release_and_orphan_release() -> None:
    ledger = ResourceLedger()
    ledger.acquire("db")
    ledger.release("db")

    with pytest.raises(LedgerViolationError, match="twice"):
        ledger.release("db")
    with pytest.raises(LedgerViolationError, match="never acquired"):
        ledger.release("cache")


@pytest.mark.tier_local
@given(resources=gen.unique_names(min_size=1, max_size=5))
def test_ledger_reports_what_leaked(resources: tuple[str, ...]) -> None:
    ledger = ResourceLedger()
    for resource in resources:
        ledger.acquire(resource)
    ledger.release(resources[-1])

    assert ledger.live == frozenset(resources[:-1])
    assert ledger.balanced is (len(resources) == 1)


@pytest.mark.tier_local
def test_ledger_disposer_releases_exactly_what_it_acquired() -> None:
    ledger = ResourceLedger()
    dispose = ledger.disposer("conn")
    assert ledger.live == {"conn"}
    dispose()
    assert ledger.balanced


# --------------------------------------------------------------------------
# Virtual clock
# --------------------------------------------------------------------------


@pytest.mark.tier_local
async def test_clock_does_not_move_on_its_own() -> None:
    clock = VirtualClock()
    await clock.drain()
    await asyncio.sleep(0)
    assert clock.now() == 0.0


@pytest.mark.tier_local
@given(delays=gen.distinct_delays(max_size=6))
async def test_sleepers_wake_in_deadline_order_at_their_own_deadline(
    delays: tuple[float, ...],
) -> None:
    """Failure value: a clock that wakes every sleeper at the advanced-to time,
    which would make an interval property pass for an implementation that fires
    all its ticks in one batch."""
    clock = VirtualClock()
    woke: list[tuple[float, float]] = []

    async def sleeper(delay: float) -> None:
        await clock.sleep(delay)
        woke.append((delay, clock.now()))

    async with asyncio.TaskGroup() as group:
        for delay in delays:
            group.create_task(sleeper(delay))
        await clock.drain()
        await clock.advance(max(delays))

    assert [d for d, _ in woke] == sorted(delays)
    assert list(delays) == sorted(delays), "gaps are positive, so delays increase"
    assert all(observed == delay for delay, observed in woke)
    assert clock.pending == ()


@pytest.mark.tier_local
@given(delay=st.floats(1, 20), short=st.floats(0.1, 0.9))
async def test_advancing_short_of_a_deadline_leaves_the_sleeper_waiting(
    delay: float, short: float
) -> None:
    clock = VirtualClock()
    fired = False

    async def sleeper() -> None:
        nonlocal fired
        await clock.sleep(delay)
        fired = True

    task = asyncio.create_task(sleeper())
    await clock.drain()
    await clock.advance(delay * short)

    assert not fired
    assert clock.pending == (delay,)
    assert clock.now() == pytest.approx(delay * short)

    await clock.advance(delay)
    await task
    assert fired


@pytest.mark.tier_local
async def test_clock_refuses_to_run_backwards() -> None:
    clock = VirtualClock()
    with pytest.raises(ValueError, match="backwards"):
        await clock.advance(-1.0)


# --------------------------------------------------------------------------
# Transition recorder
# --------------------------------------------------------------------------


@pytest.mark.tier_local
def test_recorder_accepts_the_full_lifecycle_and_the_reload_edge() -> None:
    recorder = TransitionRecorder()
    for state in ("LOADING", "ACTIVE", "UNLOADING", "PENDING", "LOADING", "ACTIVE"):
        recorder.record("f", state)

    assert recorder.state("f") == "ACTIVE"
    assert recorder.reached("f", "UNLOADING")
    recorder.assert_visited("f", ["PENDING", "ACTIVE", "UNLOADING", "ACTIVE"])


@pytest.mark.tier_local
@pytest.mark.parametrize(
    ("path", "illegal"),
    [
        (("LOADING", "ACTIVE"), "PENDING"),  # ACTIVE may only unload
        (("LOADING",), "DISPOSED"),  # loading cannot skip to disposed
        (("UNLOADING", "DISPOSED"), "LOADING"),  # DISPOSED is terminal
    ],
)
def test_recorder_rejects_transitions_outside_the_permitted_set(
    path: tuple[str, ...], illegal: str
) -> None:
    recorder = TransitionRecorder()
    for state in path:
        recorder.record("f", state)

    with pytest.raises(IllegalTransitionError):
        recorder.record("f", illegal)


@pytest.mark.tier_local
def test_recorder_flags_a_resurrected_terminal_state() -> None:
    # A deliberately wrong table, to prove the check fires: this one lets a
    # disposed fiber load again.
    recorder = TransitionRecorder(
        allowed={
            "PENDING": frozenset({"UNLOADING"}),
            "UNLOADING": frozenset({"DISPOSED"}),
            "DISPOSED": frozenset({"LOADING"}),
            "LOADING": frozenset(),
        },
    )
    recorder.record("f", "UNLOADING")
    recorder.record("f", "DISPOSED")
    recorder.record("f", "LOADING")

    with pytest.raises(IllegalTransitionError, match="terminal"):
        recorder.assert_terminal_is_final()


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(data=st.data())
def test_tree_resolution_matches_brute_force(data: st.DataObject) -> None:
    """The inheritance model agrees with the most naive possible statement of it."""
    tree = data.draw(gen.tree_specs())
    path = data.draw(gen.tree_paths(tree))
    key = data.draw(st.sampled_from(["alpha", "beta", "gamma", "delta", "absent"]))

    lineage = tree.chain(path)
    expected = None
    for node in lineage:  # naive: overwrite going down, last writer wins
        if key in node.meta:
            expected = node.meta[key]

    assert tree.resolve(path, key) == expected


@pytest.mark.tier_local
@given(tree=gen.tree_specs())
def test_tree_labels_are_unique_and_positional(tree: TreeSpec) -> None:
    labels = [node.label for _, node in tree.walk()]
    assert len(set(labels)) == len(labels)
    assert tree.label == "root"
    assert tree.size() == len(labels)


@pytest.mark.tier_local
@given(dag=gen.dag_specs(max_size=7))
def test_generated_graphs_are_acyclic_by_construction(dag: DagSpec) -> None:
    for index, deps in enumerate(dag.edges):
        assert all(dep < index for dep in deps)
        assert dag.names[index] not in dag.transitive(index)


@pytest.mark.tier_local
@given(dag=gen.dag_specs(min_size=1, max_size=6))
def test_dependents_is_the_inverse_of_transitive_dependencies(dag: DagSpec) -> None:
    for index, name in enumerate(dag.names):
        for other in dag.dependents(index):
            assert name in dag.transitive(dag.names.index(other))


@pytest.mark.tier_local
@given(entries=gen.entry_lists())
def test_diff_of_a_list_with_itself_is_empty(entries: tuple[EntrySpec, ...]) -> None:
    """Failure value: reconciliation that remounts everything on every config
    re-read, which is how a loader turns a no-op save into a restart."""
    diff = diff_entries(entries, entries)

    assert diff.added == ()
    assert diff.removed == ()
    assert diff.reconfigured == ()
    assert set(diff.untouched) == {e.id for e in entries}


@pytest.mark.tier_local
@given(entries=gen.entry_lists(min_size=1))
def test_reordering_alone_changes_nothing(entries: tuple[EntrySpec, ...]) -> None:
    reversed_entries = tuple(reversed(entries))
    diff = diff_entries(entries, reversed_entries)

    assert (diff.added, diff.removed, diff.reconfigured) == ((), (), ())


@pytest.mark.tier_local
@given(data=st.data())
def test_diff_partitions_every_id_exactly_once(data: st.DataObject) -> None:
    before = data.draw(gen.entry_lists())
    after = data.draw(gen.reconfigurations(before))
    diff = diff_entries(before, after)

    buckets = [*diff.added, *diff.removed, *diff.reconfigured, *diff.untouched]
    assert len(buckets) == len(set(buckets))
    assert set(buckets) == {e.id for e in before} | {e.id for e in after}


# --------------------------------------------------------------------------
# Generator health
# --------------------------------------------------------------------------

STRATEGY_MODULE = Path(gen.__file__)


@pytest.mark.tier_local
def test_strategies_construct_rather_than_filter() -> None:
    """No `.filter(` or `assume(` in the generator library without a stated reason.

    Hypothesis's own `filter_too_much` health check catches a strategy that
    discards heavily, but only once the discards get bad enough to notice. This
    catches the habit, which is the thing that eventually produces a strategy
    whose valid region is a sliver.

    Failure value: a strategy that quietly stops covering its domain, so a
    green suite means "generated nothing interesting" rather than "found
    nothing wrong".
    """
    source = STRATEGY_MODULE.read_text()
    lines = source.splitlines()
    offenders: list[str] = []

    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id
            if isinstance(func, ast.Name)
            else ""
        )
        if name not in {"filter", "assume"}:
            continue
        line = lines[node.lineno - 1]
        if "# discard-ok:" not in line:
            offenders.append(f"{STRATEGY_MODULE.name}:{node.lineno}: {line.strip()}")

    assert not offenders, "filtering without a stated justification:\n" + "\n".join(
        offenders
    )


@pytest.mark.tier_local
@given(data=st.data())
def test_every_strategy_stays_within_the_generator_health_checks(
    data: st.DataObject,
) -> None:
    """Draw from everything the library exposes, under the active profile.

    The health checks are the enforcement, and they are deliberately left on
    in every profile: `filter_too_much` fails the run if a strategy starts
    rejecting most of what it generates, and `data_too_large` fails it if a
    strategy grows past what the engine will build.

    A note on measurement, because the obvious metric misleads. The "invalid
    test cases" count in `--hypothesis-show-statistics` is *not* a filter rate:
    a control strategy containing no filter at all (a mapped `st.lists`) still
    reports 10-20%, because the engine also counts its own overruns and
    duplicate draws. Judging strategies by that number would mean rewriting
    ones that never filter. So the rule enforced here is the one that is
    actually about filtering -- the AST check above -- with the health checks
    as the runtime backstop, and `just discards` for the raw numbers when a
    strategy is under suspicion.
    """
    assert data.draw(gen.identifiers)
    assert data.draw(gen.event_names)
    assert data.draw(gen.distinct_delays())
    data.draw(gen.metadata())
    data.draw(gen.unique_names())
    tree = data.draw(gen.tree_specs())
    data.draw(gen.tree_paths(tree))
    dag = data.draw(gen.dag_specs())
    assert len(dag.names) == len(dag.edges)
    entries = data.draw(gen.entry_lists())
    data.draw(gen.reconfigurations(entries))
    data.draw(gen.effect_specs(allow_invalid=True))
    data.draw(gen.effect_plans())


@pytest.mark.tier_local
@given(plan=gen.effect_plans(max_size=5))
def test_effect_plans_have_globally_unique_resources(
    plan: tuple[EffectSpec, ...],
) -> None:
    resources = [r for spec in plan for r in spec.resources]
    assert len(resources) == len(set(resources))


@pytest.mark.tier_local
@given(spec=gen.effect_specs(allow_invalid=True))
def test_a_rejected_or_failed_effect_holds_nothing(spec: EffectSpec) -> None:
    if spec.raises_on_setup or spec.shape.value == "invalid":
        assert spec.acquires == ()
