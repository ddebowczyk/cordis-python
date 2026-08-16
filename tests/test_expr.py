"""PROP-EXPR-001..006, from spec/capabilities/15-config-expressions.yaml.

The oracles here are deliberately outside the evaluator. Reachability is
checked with sentinel objects that record what was asked of them; termination
is checked with a wall clock; placement is checked against the position the
generator chose; and the two evaluation *times* -- once per mount for `config`,
once per mount decision for `disabled` -- are counted by a probe function the
test binds as a service, which is also the demonstration that the allow-list
extends without a global registry.

Nothing here asserts against the whitelist by restating it. A test that said
"`Lambda` is rejected because `Lambda` is not in the permitted set" would pass
for a permitted set containing nothing at all.
"""

from __future__ import annotations

import copy
import json
import time
import tomllib
from typing import TYPE_CHECKING, TypedDict

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from cordis.errors import ConfigValidationError, ExpressionError
from cordis.expr import (
    BUDGET,
    Expr,
    FunctionSource,
    Opaque,
    compile_expr,
    evaluate,
    is_envelope,
    substitute,
)
from cordis.fiber import FiberState
from cordis.loader import (
    GROUP,
    Entry,
    LoaderService,
    TargetSource,
    YamlSource,
    as_mapping,
    read_entries,
)
from cordis.plugin import PluginHost, config_of
from cordis.realm import isolated_realm
from cordis.registry import Service

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from cordis.context import Context
    from cordis.plugin import PluginHandle, PluginTarget


class Where(TypedDict):
    """Where an expression was written. Every direct call has to say."""

    entry_id: str
    field: str


#: SEM-005 requires a failure to name its position, so nothing here evaluates
#: an expression that has no position.
WHERE: Where = {"entry_id": "e0", "field": "config"}


# --------------------------------------------------------------------------
# PROP-EXPR-001: what an expression can reach
# --------------------------------------------------------------------------


class Ledger:
    """Every sentinel this environment ever minted, and every name asked for."""

    def __init__(self) -> None:
        self.minted: list[Probe] = []
        self.reads: list[str] = []

    def mint(self, name: str) -> Probe:
        made = Probe(self, name)
        self.minted.append(made)
        return made

    def holds(self, value: object) -> bool:
        return any(value is made for made in self.minted)


class Probe:
    """An object that answers anything, and remembers what it was asked.

    Answering *anything* is the point: a whitelist that lets an attribute
    through has to be caught by what the attribute was, not by the object
    happening not to have it.
    """

    def __init__(self, ledger: Ledger, name: str) -> None:
        self._ledger = ledger
        self._name = name

    def __getattr__(self, name: str) -> Probe:
        self._ledger.reads.append(name)
        return self._ledger.mint(f"{self._name}.{name}")

    def __getitem__(self, key: object) -> Probe:
        return self._ledger.mint(f"{self._name}[{key!r}]")

    def __repr__(self) -> str:
        return f"Probe({self._name})"


#: The only attribute names the generator ever writes as an honest read. Any
#: other name in `Ledger.reads` is an attribute the compiler should have
#: refused before evaluation began.
OPEN: frozenset[str] = frozenset({"url", "port", "name"})

_LITERAL_TYPES = (str, bytes, int, float, complex, bool, type(None))


def _contained(value: object, ledger: Ledger) -> bool:
    """Whether ``value`` came out of ``ledger`` or out of the source text."""
    if ledger.holds(value) or isinstance(value, _LITERAL_TYPES):
        return True
    if isinstance(value, list | tuple | set | frozenset):
        return all(_contained(item, ledger) for item in value)
    if isinstance(value, dict):
        return all(
            _contained(key, ledger) and _contained(item, ledger)
            for key, item in value.items()
        )
    return False


#: Deliberate escape attempts. Random text almost never parses, so a generator
#: built from it would discard nearly everything and test nothing.
_ESCAPES = (
    "a.__class__",
    "a.__class__.__mro__",
    "a.__class__.__mro__[1].__subclasses__()",
    "a.__globals__",
    "a.__dict__",
    "a._private",
    "a.__init__.__builtins__",
    "(lambda: a)()",
    "[i for i in a]",
    "{i: i for i in a}",
    "(b := a)",
    "f'{a}'",
    "a.__class__(*a)",
    "open('/etc/passwd')",
    "eval('1')",
    "exec('x=1')",
    "__import__('os')",
    "type(a)",
    "getattr(a, '__class__')",
    "vars(a)",
    "globals()",
    "a ** a",
    "2 ** 4096",
)

_HONEST = ("a", "b", "c", "a.url", "b.port", "c.name", "a['k']", "a[0]", "a[1:2]")

_ATTRS = ("url", "port", "name", "__class__", "__mro__", "_secret")

ADVERSARIAL: st.SearchStrategy[str] = st.deferred(
    lambda: st.one_of(
        st.sampled_from((*_ESCAPES, *_HONEST)),
        st.tuples(ADVERSARIAL, st.sampled_from(_ATTRS)).map(
            lambda pair: f"{pair[0]}.{pair[1]}"
        ),
        st.tuples(ADVERSARIAL, st.sampled_from(_HONEST)).map(
            lambda pair: f"{pair[0]}[{pair[1]}]"
        ),
        st.tuples(ADVERSARIAL, st.sampled_from(("+", "*", "and", "or"))).map(
            lambda pair: f"({pair[0]} {pair[1]} {pair[0]})"
        ),
        ADVERSARIAL.map(lambda source: f"len({source})"),
    )
)


@pytest.mark.tier_pr
@settings(max_examples=200)
@given(source=ADVERSARIAL)
def test_no_expression_reaches_outside_its_environment(source: str) -> None:
    """PROP-EXPR-001: rejected at compile time, or confined to what it was given."""
    ledger = Ledger()
    env: dict[str, object] = {name: ledger.mint(name) for name in ("a", "b", "c")}
    try:
        compiled = compile_expr(source, **WHERE)
    except ExpressionError:
        assert not ledger.reads  # a rejected expression never ran
        return
    # The allow-list is empty on purpose: an object that is neither a probe nor
    # a literal from the source has no legitimate way to exist.
    try:
        result: object = evaluate(compiled, env, functions={}, **WHERE)
    except ExpressionError:
        result = None
    assert _contained(result, ledger), f"{source!r} produced {result!r}"
    assert set(ledger.reads) <= OPEN, f"{source!r} read {sorted(set(ledger.reads))}"


# --------------------------------------------------------------------------
# PROP-EXPR-002: evaluating twice
# --------------------------------------------------------------------------

_NAMES = ("a", "b", "c")

_VALUES = st.recursive(
    st.one_of(
        st.integers(-30, 30),
        st.text(alphabet="xyz", max_size=4),
        st.booleans(),
        st.none(),
    ),
    lambda inner: st.one_of(
        st.lists(inner, max_size=4),
        st.dictionaries(
            st.text(alphabet="kq", min_size=1, max_size=2), inner, max_size=4
        ),
    ),
    max_leaves=8,
)

_ATOM = st.one_of(
    st.sampled_from(_NAMES),
    st.integers(-9, 9).map(repr),
    st.text(alphabet="xyz", max_size=3).map(repr),
)

VALID: st.SearchStrategy[str] = st.deferred(
    lambda: st.one_of(
        _ATOM,
        st.tuples(VALID, st.sampled_from(("+", "-", "*")), VALID).map(
            lambda parts: f"({parts[0]} {parts[1]} {parts[2]})"
        ),
        st.tuples(VALID, st.sampled_from(("==", "<", ">", "in")), VALID).map(
            lambda parts: f"({parts[0]} {parts[1]} {parts[2]})"
        ),
        st.tuples(VALID, VALID).map(lambda pair: f"{pair[0]}[{pair[1]}]"),
        st.tuples(VALID, st.sampled_from(("len", "str", "sorted", "bool"))).map(
            lambda pair: f"{pair[1]}({pair[0]})"
        ),
        VALID.map(lambda source: f"[{source}]"),
        st.tuples(VALID, VALID).map(
            lambda pair: f"({pair[0]} if {pair[0]} else {pair[1]})"
        ),
    )
)


def _outcome(source: str, env: Mapping[str, object]) -> tuple[str, object]:
    try:
        return "value", evaluate(Expr(source), env, **WHERE)
    except ExpressionError as exc:
        return "error", exc.reason


@pytest.mark.tier_local
@settings(max_examples=50)
@given(
    source=VALID,
    values=st.lists(_VALUES, min_size=len(_NAMES), max_size=len(_NAMES)),
)
def test_evaluating_twice_yields_the_same_and_changes_nothing(
    source: str, values: list[object]
) -> None:
    """PROP-EXPR-002: deterministic given its inputs, and it leaves them alone."""
    env = dict(zip(_NAMES, values, strict=True))
    before = copy.deepcopy(env)
    first = _outcome(source, env)
    second = _outcome(source, env)
    assert first == second
    assert env == before


# --------------------------------------------------------------------------
# PROP-EXPR-003: the step budget
# --------------------------------------------------------------------------

_BIG = list(range(20_000))


@st.composite
def _costly(draw: st.DrawFn) -> tuple[str, bool]:
    """A source, and whether the budget is obliged to refuse it."""
    kind = draw(st.sampled_from(("repeat", "nest", "literal", "member", "calls")))
    if kind == "repeat":
        count = draw(st.integers(1, 2_000_000))
        return f"'ab' * {count}", 2 * count > BUDGET
    if kind == "nest":
        depth = draw(st.integers(1, 300))
        return "(" * depth + "1" + " + 1)" * depth, False
    if kind == "literal":
        size = draw(st.integers(1, 4_000))
        return "[" + ",".join("1" for _ in range(size)) + "]", size > BUDGET
    if kind == "member":
        return "1 in xs", len(_BIG) > BUDGET
    depth = draw(st.integers(1, 40))
    return "len(" * depth + "xs" + ")" * depth, False


@pytest.mark.tier_pr
@settings(max_examples=200, deadline=None)
@given(case=_costly())
def test_evaluation_terminates_inside_its_budget(case: tuple[str, bool]) -> None:
    """PROP-EXPR-003: it returns or it raises, and it does so promptly."""
    source, must_refuse = case
    started = time.perf_counter()
    try:
        evaluate(Expr(source), {"xs": _BIG}, **WHERE)
    except ExpressionError:
        refused = True
    else:
        refused = False
    elapsed = time.perf_counter() - started
    # An order of magnitude above what a 10_000-step budget can honestly cost.
    assert elapsed < 1.0, f"{source[:60]!r} took {elapsed:.3f}s"
    if must_refuse:
        assert refused, f"{source[:60]!r} was affordable after all"


def test_the_budget_is_charged_before_the_work_is_done() -> None:
    """A refusal that allocated the string first is not a refusal."""
    with pytest.raises(ExpressionError, match="step budget"):
        evaluate(Expr("'a' * 500000000"), {}, **WHERE)


# --------------------------------------------------------------------------
# PROP-EXPR-004: where an expression may stand
# --------------------------------------------------------------------------

_OUTSIDE = ("id", "name", "inject", "isolate", "intercept")


@st.composite
def _misplaced(draw: st.DrawFn) -> tuple[list[object], tuple[str | int, ...]]:
    """A document with one expression in a field that may not hold one."""
    field = draw(st.sampled_from(_OUTSIDE))
    source = draw(st.sampled_from(("1", "env['PORT']", "has('shell')")))
    written = {"$expr": source}
    row: dict[str, object] = {"id": "one", "name": "alpha"}
    sub: tuple[str | int, ...]
    if field in {"id", "name"}:
        row[field] = written
        sub = ()
    elif field == "inject":
        row["inject"] = ["shell", written]
        sub = (1,)
    elif field == "isolate":
        row["isolate"] = {"shell": written}
        sub = ("shell",)
    else:
        row["intercept"] = {"shell": {"timeout": written}}
        sub = ("shell", "timeout")
    if draw(st.booleans()):
        nested: list[object] = [{"id": "g", "name": GROUP, "config": [row]}]
        return nested, (0, "config", 0, field, *sub)
    return [row], (0, field, *sub)


@pytest.mark.tier_local
@settings(max_examples=50)
@given(case=_misplaced())
def test_an_expression_outside_config_or_disabled_is_rejected(
    case: tuple[list[object], tuple[str | int, ...]],
) -> None:
    """PROP-EXPR-004: the report names the position the generator chose."""
    raw, where = case
    with pytest.raises(ConfigValidationError) as caught:
        read_entries(raw)
    paths = [tuple(issue.path) for issue in caught.value.issues]
    assert where in paths, f"{where} not among {paths}"


@pytest.mark.tier_local
@settings(max_examples=50, deadline=None)
@given(case=_misplaced())
async def test_a_misplaced_expression_mounts_no_entry(
    case: tuple[list[object], tuple[str | int, ...]],
) -> None:
    """PROP-EXPR-004: and not one row of the list reaches the tree."""
    raw, _where = case
    async with Rig() as rig:
        with pytest.raises(ConfigValidationError):
            await rig.loader.reconcile(read_entries(raw))
        assert rig.loader.live() == ()


# --------------------------------------------------------------------------
# PROP-EXPR-005: reading and writing
# --------------------------------------------------------------------------

_SOURCES = ("1 + 1", "env['PORT']", "has('shell')", "len('abc')", "entries['e0']['k']")


@st.composite
def _document(draw: st.DrawFn) -> list[object]:
    rows: list[object] = []
    for index in range(draw(st.integers(1, 3))):
        row: dict[str, object] = {
            "id": f"e{index}",
            "name": "alpha",
            "config": {"k": index, "v": {"$expr": draw(st.sampled_from(_SOURCES))}},
        }
        if draw(st.booleans()):
            row["disabled"] = {"$expr": draw(st.sampled_from(_SOURCES))}
        rows.append(row)
    if draw(st.booleans()):
        return [{"id": "g", "name": GROUP, "config": rows}]
    return rows


@pytest.mark.tier_local
@settings(max_examples=50)
@given(raw=_document())
def test_a_document_with_expressions_round_trips_unevaluated(
    raw: list[object],
) -> None:
    """PROP-EXPR-005: what was written is what comes back, in every format."""
    entries = read_entries(raw)
    written = [as_mapping(entry) for entry in entries]
    assert read_entries(written) == entries

    as_json = json.dumps(written)
    assert read_entries(json.loads(as_json)) == entries
    as_yaml = yaml.safe_dump(written)
    assert read_entries(yaml.safe_load(as_yaml)) == entries

    for source in _sources_in(entries):
        assert source in as_json
        assert source in as_yaml


def _sources_in(entries: Sequence[Entry]) -> list[str]:
    found: list[str] = []
    for entry in entries:
        if isinstance(entry.disabled, Expr):
            found.append(entry.disabled.source)
        found.extend(_expr_sources(entry.config))
        found.extend(_sources_in(entry.group or ()))
    return found


def _expr_sources(value: object) -> list[str]:
    if isinstance(value, Expr):
        return [value.source]
    if isinstance(value, dict):
        return [source for item in value.values() for source in _expr_sources(item)]
    if isinstance(value, list | tuple):
        return [source for item in value for source in _expr_sources(item)]
    return []


def test_the_yaml_tag_and_the_envelope_read_the_same(tmp_path: Path) -> None:
    """The YAML-native spelling is a second reader, not a second meaning."""
    tagged = tmp_path / "tagged.yaml"
    tagged.write_text(
        "- id: one\n  name: alpha\n  config:\n    v: !expr \"env['PORT']\"\n",
        encoding="utf-8",
    )
    portable = tmp_path / "portable.yaml"
    portable.write_text(
        "- id: one\n  name: alpha\n  config:\n    v:\n      $expr: \"env['PORT']\"\n",
        encoding="utf-8",
    )
    assert YamlSource(tagged).read() == YamlSource(portable).read()


def test_a_toml_document_reads_the_portable_envelope() -> None:
    """TOML has no tags, which is why the envelope is what the writer emits."""
    text = (
        "[[plugins]]\n"
        'id = "one"\n'
        'name = "alpha"\n'
        "[plugins.config.v]\n"
        '"$expr" = "1 + 1"\n'
    )
    entries = read_entries(tomllib.loads(text)["plugins"])
    assert entries[0].config == {"v": Expr("1 + 1")}


# --------------------------------------------------------------------------
# The grammar, where the rule is not "some node type is missing"
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "db.execute('drop table t')",
        "db.query.run()",
        "(db.execute)('x')",
    ],
)
def test_a_call_on_anything_but_a_plain_name_is_rejected(source: str) -> None:
    """Reading a service is permitted; working one is not."""
    with pytest.raises(ExpressionError, match="plain function name"):
        compile_expr(source, **WHERE)


def test_reading_a_service_attribute_is_permitted() -> None:
    """The other half of the same rule, or the first half proves nothing."""

    class Db:
        url = "postgres://x"

    assert evaluate(Expr("db.url"), {"db": Db()}, **WHERE) == "postgres://x"


def test_exponentiation_is_not_in_the_grammar() -> None:
    """No step budget can see `2 ** 4096` coming; the grammar can."""
    with pytest.raises(ExpressionError, match="Pow is not permitted"):
        compile_expr("2 ** 4096", **WHERE)


def test_a_deeply_nested_source_is_refused_rather_than_walked() -> None:
    """The recursion guard is at compile time, where it can still say why."""
    source = "(" * 200 + "1" + " + 1)" * 200
    with pytest.raises(ExpressionError, match="nested more than"):
        compile_expr(source, **WHERE)


def test_an_unknown_function_is_not_reachable_through_the_environment() -> None:
    """A name in the environment is data. Calling it is not something it can be."""
    with pytest.raises(ExpressionError, match="unknown function"):
        evaluate(Expr("shout('hi')"), {"shout": str.upper}, **WHERE)


def test_a_computed_sibling_field_is_present_but_unreadable() -> None:
    """The cycle-free half of cross-entry references."""
    env = {"entries": {"one": {"port": 8080, "host": Opaque("one.config is computed")}}}
    assert evaluate(Expr("entries['one']['port']"), env, **WHERE) == 8080
    with pytest.raises(ExpressionError, match="is computed"):
        evaluate(Expr("entries['one']['host']"), env, **WHERE)


def test_substitution_leaves_a_structure_without_expressions_alone() -> None:
    """Reading a config file must not quietly copy what an operator wrote."""
    config = {"a": [1, {"b": 2}]}
    assert substitute(config, {}, **WHERE) is config


def test_an_envelope_is_exactly_one_key() -> None:
    """A mapping that merely mentions `$expr` is an ordinary mapping."""
    assert is_envelope({"$expr": "1"})
    assert not is_envelope({"$expr": "1", "other": 2})
    assert not is_envelope({"$expr": 1})


# --------------------------------------------------------------------------
# PROP-EXPR-006: when each field is evaluated, and against what
# --------------------------------------------------------------------------


class Tag:
    """A service whose only content is which realm it was bound in."""

    def __init__(self, realm: str) -> None:
        self.realm = realm


def tagged(ctx: Context) -> None:
    """A leaf whose config is whatever the expressions computed."""


TARGETS: dict[str, PluginTarget] = {"tagged": tagged, "alpha": tagged}


class Fakes(TargetSource):
    """The target seam, so nothing here is resolved by import."""

    def resolve(self, name: str, /) -> PluginTarget:
        found = TARGETS.get(name)
        if found is None:
            msg = f"no target named {name!r}"
            raise LookupError(msg)
        return found


class Probes(Service):
    """A `FunctionSource`: the allow-list, extended by a binding."""

    name = FunctionSource.name

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        config = config_of(ctx)
        assert isinstance(config, dict)
        self._log: list[tuple[str, str]] = config["log"]

    def functions(self) -> Mapping[str, Callable[..., object]]:
        return {"seen": self._seen}

    def _seen(self, kind: object, value: object) -> object:
        self._log.append((str(kind), str(value)))
        return value


class Rig:
    """A host with a target seam, a probe source and a loader."""

    def __init__(self) -> None:
        self.host = PluginHost()
        self.log: list[tuple[str, str]] = []
        self.loader: LoaderService

    async def __aenter__(self) -> Rig:
        self.host.root.plugin(Fakes)
        self.host.root.plugin(Probes, {"log": self.log})
        await self.host.runtime.quiesce()
        self.host.root.plugin(LoaderService)
        await self.host.runtime.quiesce()
        found = self.host.root.context.require(LoaderService)
        assert isinstance(found, LoaderService)
        self.loader = found
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.host.dispose()

    def kinds(self, kind: str) -> list[str]:
        return [seen for found, seen in self.log if found == kind]

    def handles(self) -> list[PluginHandle]:
        found = (self.loader.handle_for(path) for path in self.loader.live())
        return [handle for handle in found if handle is not None]


def computed(index: int, *, label: str = "alpha") -> Entry:
    """One entry whose config and whose presence are both computed."""
    return Entry(
        id=f"e{index}",
        name="tagged",
        config={"tag": Expr("seen('config', service('scoped').realm)")},
        disabled=Expr("seen('disabled', service('scoped').realm) == 'never'"),
        inject=("late",),
        isolate={"scoped": label},
    )


@pytest.mark.tier_pr
@settings(max_examples=25, deadline=None)
@given(
    count=st.integers(1, 3),
    rounds=st.integers(1, 4),
    bind_at=st.integers(0, 4),
)
async def test_config_is_computed_once_per_mount_and_disabled_at_every_decision(
    count: int, rounds: int, bind_at: int
) -> None:
    """PROP-EXPR-006: two fields, two times, two contexts."""
    async with Rig() as rig:
        scope = rig.host.root.scope
        rig.host.registry.provide("scoped", Tag("root"), scope=scope)
        rig.host.registry.provide(
            "scoped", Tag("alpha"), scope=scope, realm=isolated_realm("scoped", "alpha")
        )
        entries = tuple(computed(index) for index in range(count))
        for round_ in range(rounds):
            if round_ == bind_at:
                rig.host.registry.provide("late", object(), scope=scope)
            await rig.loader.reconcile(entries)
            await rig.host.runtime.quiesce()

        # `disabled` is a decision, taken once per entry per reconcile, in the
        # loader's own context -- the one with no isolation, which is why it
        # sees the root binding and never the entry's.
        assert rig.kinds("disabled") == ["root"] * (count * rounds)
        # `config` is computed once per mount, and not until the entry's
        # declared injection is available: before that there is no instance.
        mounted = count if bind_at < rounds else 0
        assert rig.kinds("config") == ["alpha"] * mounted

        states = [handle.state for handle in rig.handles()]
        assert len(states) == count
        assert states.count(FiberState.ACTIVE) == mounted


async def test_a_config_expression_that_fails_takes_only_its_own_entry() -> None:
    """SEM-005's blast radius, at the one place a computed config can fail."""
    async with Rig() as rig:
        good = Entry(id="good", name="tagged", config={"v": Expr("1 + 1")})
        bad = Entry(id="bad", name="tagged", config={"v": Expr("nope['x']")})
        await rig.loader.reconcile((good, bad))
        await rig.host.runtime.quiesce()

        assert set(rig.loader.live()) == {"good", "bad"}
        healthy = rig.loader.handle_for("good")
        broken = rig.loader.handle_for("bad")
        assert healthy is not None
        assert broken is not None
        assert healthy.state is FiberState.ACTIVE
        assert healthy.config == {"v": 2}
        assert broken.state is FiberState.FAILED
        assert isinstance(broken.error, ExpressionError)
        assert broken.error.field == "config.v"


async def test_a_disabled_expression_that_cannot_be_decided_leaves_the_entry_out() -> (
    None
):
    """ "Cannot decide" resolves to "not mounted", and names the row it was."""
    async with Rig() as rig:
        entries = (
            Entry(id="one", name="tagged", disabled=Expr("has('shell')")),
            Entry(id="two", name="tagged", disabled=Expr("nope")),
        )
        report = await rig.loader.reconcile(entries)
        await rig.host.runtime.quiesce()

        assert rig.loader.live() == ("one",)
        assert [failure.id for failure in report.failed] == ["two"]
        assert report.failed[0].reason == "disabled expression"


async def test_a_disabled_expression_that_is_not_a_boolean_fails_its_entry() -> None:
    """The strictness the literal field already has: `disabled: 3` is not falsey."""
    async with Rig() as rig:
        report = await rig.loader.reconcile(
            (Entry(id="one", name="tagged", disabled=Expr("1 + 1")),)
        )
        assert rig.loader.live() == ()
        failure = report.failed[0].error
        assert isinstance(failure, ExpressionError)
        assert failure.reason == "must be true or false"


async def test_a_computed_config_is_recomputed_when_the_expression_changes() -> None:
    """An edit to the source is an edit to the config, so the instance restarts."""
    async with Rig() as rig:
        first = Entry(id="one", name="tagged", config={"v": Expr("1 + 1")})
        await rig.loader.reconcile((first,))
        await rig.host.runtime.quiesce()
        before = rig.loader.handle_for("one")
        assert before is not None
        assert before.config == {"v": 2}

        second = Entry(id="one", name="tagged", config={"v": Expr("2 + 2")})
        report = await rig.loader.reconcile((second,))
        await rig.host.runtime.quiesce()
        after = rig.loader.handle_for("one")
        assert after is not None
        assert report.updated == ("one",)
        assert after.config == {"v": 4}

        # The identical document again computes nothing and moves nothing: two
        # identical expressions are one config.
        assert (await rig.loader.reconcile((second,))).quiet


async def test_an_entry_reads_a_literal_field_of_the_entry_beside_it() -> None:
    """The motivating case: two plugins that must agree on one value."""
    async with Rig() as rig:
        entries = (
            Entry(id="base", name="tagged", config={"root": "/srv"}),
            Entry(
                id="user",
                name="tagged",
                config={"path": Expr("entries['base']['root'] + '/user'")},
            ),
        )
        await rig.loader.reconcile(entries)
        await rig.host.runtime.quiesce()
        handle = rig.loader.handle_for("user")
        assert handle is not None
        assert handle.config == {"path": "/srv/user"}


async def test_an_entry_cannot_read_a_computed_field_of_the_entry_beside_it() -> None:
    """And the cycle the motivating case would otherwise open, refused."""
    async with Rig() as rig:
        entries = (
            Entry(id="base", name="tagged", config={"root": Expr("'/srv'")}),
            Entry(
                id="user",
                name="tagged",
                config={"path": Expr("entries['base']['root']")},
            ),
        )
        await rig.loader.reconcile(entries)
        await rig.host.runtime.quiesce()
        handle = rig.loader.handle_for("user")
        assert handle is not None
        assert handle.state is FiberState.FAILED
        assert isinstance(handle.error, ExpressionError)
        assert "base.config is computed" in handle.error.reason
