"""Config expressions: computed values that are not code.

Implements ``spec/capabilities/15-config-expressions.yaml``.

A configuration file is data that arrives from bundles, profiles, home
directories and command-line overlays. The moment it can execute code, every
one of those layers can execute code with the process's full authority. So the
useful part of "the port comes from the environment" is kept and the dangerous
part is removed: expressions are parsed with :mod:`ast`, checked against a
whitelist of node types, and then *interpreted* -- there is no path from a
config file to :func:`eval`, :func:`compile` or :func:`exec` at all.

Three rules carry most of the weight.

**A call must name a plain function.** ``Call`` is permitted only when its
callee is a bare ``Name`` that resolves in the supplied ``functions`` mapping.
``db.url`` is therefore an ordinary read while ``db.execute(...)`` is rejected
before evaluation begins, which is the whole of "expose live services without
exposing their behaviour" and needs no per-object policy.

**The allow-list is an argument, not a registry.** Whoever builds the
environment decides what may be called on it. That removes the only mutable
global state this capability would otherwise have, and with it the test
isolation problem a process-wide registry brings.

**Cost is charged before it is paid.** ``'a' * 1_000_000`` is refused by
charging ``len(left) * right`` *before* the multiplication happens, not by
noticing afterwards that a large string exists. Exponentiation has no such
bound inside a single step, so it is not in the grammar.
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Final, Protocol, runtime_checkable

from cordis.errors import ExpressionError

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "BUDGET",
    "ENVELOPE",
    "FUNCTIONS",
    "CompiledExpr",
    "Expr",
    "FunctionSource",
    "Opaque",
    "compile_expr",
    "envelope_of",
    "evaluate",
    "expression_paths",
    "is_envelope",
    "parse_expressions",
    "substitute",
    "unparse_expressions",
    "yaml_loader",
]

#: The portable tag key. Valid in YAML, JSON and TOML alike, which is why it is
#: what the writer emits even though YAML has a tag of its own.
ENVELOPE: Final = "$expr"

#: The YAML tag accepted in addition to the envelope.
TAG: Final = "!expr"

#: Default evaluation step budget. Generous for anything an operator writes by
#: hand, and far below what it takes to notice a hung startup.
BUDGET: Final = 10_000

#: Maximum syntactic nesting. Enforced at compile time, which is what bounds
#: the interpreter's own recursion: a tree that passed cannot overflow a stack.
MAX_DEPTH: Final = 64

_LITERALS: Final = (str, bytes, int, float, complex, bool, type(None))


# --------------------------------------------------------------------------
# What an expression is
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Expr:
    """An expression as it appears in a document: its source, and nothing else.

    Equality is equality of source, so a document that round-trips through
    :func:`parse_expressions` and :func:`unparse_expressions` compares equal to
    itself (SEM-006) without the compiled form having to be comparable.
    """

    source: str

    def __str__(self) -> str:
        return self.source


@dataclass(frozen=True, slots=True)
class CompiledExpr:
    """A source string that has already been checked against the grammar.

    Held separately from :class:`Expr` because compilation is cached by source
    and a document may hold the same expression in many places; the tree is
    read-only to the interpreter, which keeps sharing it safe.
    """

    source: str
    tree: ast.Expression


@dataclass(frozen=True, slots=True)
class Opaque:
    """A value an expression is not permitted to read, and why.

    Placed in an environment where something exists but must not be reachable
    -- a sibling entry's field that is itself computed. Reading it fails the
    expression with ``why`` rather than returning a marker object that would
    travel onwards into a plugin's config.
    """

    why: str


@runtime_checkable
class FunctionSource(Protocol):
    """Extra allow-listed functions, bound as a service.

    Extending the language is a binding in a context: scoped, reversible, and
    visible in the registry -- unlike a module-level ``register_function``,
    which is none of those things.
    """

    name: ClassVar[str] = "expr.functions"

    def functions(self) -> Mapping[str, Callable[..., object]]: ...


class _GrammarError(Exception):
    """Grammar violation, raised during compilation and cached as a reason."""


class _EvalError(Exception):
    """Evaluation failure, converted to :class:`ExpressionError` by the caller."""


# --------------------------------------------------------------------------
# The grammar (SEM-002)
# --------------------------------------------------------------------------

_NODES: Final = frozenset(
    {
        ast.Expression,
        ast.Constant,
        ast.Name,
        ast.Load,
        ast.Attribute,
        ast.Subscript,
        ast.Slice,
        ast.Tuple,
        ast.List,
        ast.Dict,
        ast.Set,
        ast.BinOp,
        ast.UnaryOp,
        ast.BoolOp,
        ast.Compare,
        ast.IfExp,
        ast.Call,
        ast.keyword,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.FloorDiv,
        ast.Mod,
        ast.UAdd,
        ast.USub,
        ast.Not,
        ast.And,
        ast.Or,
        ast.Eq,
        ast.NotEq,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
        ast.In,
        ast.NotIn,
        ast.Is,
        ast.IsNot,
    }
)

_BINOPS: Final[Mapping[type[ast.AST], Callable[[Any, Any], object]]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}

_COMPARES: Final[Mapping[type[ast.AST], Callable[[Any, Any], object]]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda left, right: left in right,
    ast.NotIn: lambda left, right: left not in right,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}

#: The default allow-list: pure, total-enough, and deterministic given their
#: arguments (SEM-003). Nothing here reads the clock, the filesystem or the
#: environment; nothing here mutates what it is given.
FUNCTIONS: Final[Mapping[str, Callable[..., object]]] = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "float": float,
    "int": int,
    "len": len,
    "max": max,
    "min": min,
    "round": round,
    "sorted": sorted,
    "str": str,
    "sum": sum,
}


# --------------------------------------------------------------------------
# Compiling
# --------------------------------------------------------------------------


def compile_expr(source: str, /, *, entry_id: str, field: str) -> CompiledExpr:
    """Parse and check ``source``, or raise naming where it was written.

    ``entry_id`` and ``field`` are not part of the cache key: the same source
    is the same program wherever it appears, and only the message differs.
    """
    compiled, reason = _compile(source)
    if compiled is None:
        raise ExpressionError(entry_id, field, source, reason)
    return compiled


_CACHE: Final[dict[str, tuple[CompiledExpr | None, str]]] = {}
_CACHE_LIMIT: Final = 512


def _compile(source: str) -> tuple[CompiledExpr | None, str]:
    """Compile, cached by source -- rejections included.

    A rejection is cached too, because a document that repeats a mistake would
    otherwise re-parse it once per occurrence and once per reconcile.
    """
    found = _CACHE.get(source)
    if found is not None:
        return found
    try:
        tree = ast.parse(source, mode="eval")
        _validate(tree)
    except _GrammarError as exc:
        outcome: tuple[CompiledExpr | None, str] = (None, str(exc))
    except SyntaxError as exc:
        outcome = (None, f"syntax error: {exc.msg}")
    except (MemoryError, RecursionError, ValueError) as exc:
        outcome = (None, f"unparseable: {type(exc).__name__}")
    else:
        outcome = (CompiledExpr(source, tree), "")
    if len(_CACHE) >= _CACHE_LIMIT:
        _CACHE.clear()
    _CACHE[source] = outcome
    return outcome


def _validate(tree: ast.Expression) -> None:
    """Walk iteratively: the tree being checked is the reason to distrust it."""
    stack: list[tuple[ast.AST, int]] = [(tree, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_DEPTH:
            msg = f"nested more than {MAX_DEPTH} deep"
            raise _GrammarError(msg)
        kind = type(node)
        if kind not in _NODES:
            msg = f"{kind.__name__} is not permitted"
            raise _GrammarError(msg)
        _check(node)
        stack.extend((child, depth + 1) for child in ast.iter_child_nodes(node))


def _check(node: ast.AST) -> None:
    """The rules that are about a node's contents rather than its type."""
    if isinstance(node, ast.Attribute):
        if node.attr.startswith("_"):
            # Stricter than banning dunders, and short enough to defend: every
            # published escape from a Python sandbox starts with an underscore.
            msg = f"attribute {node.attr!r} is private"
            raise _GrammarError(msg)
    elif isinstance(node, ast.Name):
        if node.id.startswith("_"):
            msg = f"name {node.id!r} is private"
            raise _GrammarError(msg)
    elif isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            # `db.url` is a read; `db.execute(...)` is a capability. The
            # difference is exactly this node shape.
            msg = "only a plain function name may be called"
            raise _GrammarError(msg)
        if any(word.arg is None for word in node.keywords):
            msg = "** is not permitted"
            raise _GrammarError(msg)
    elif isinstance(node, ast.Constant) and not isinstance(node.value, _LITERALS):
        msg = f"{node.value!r} is not a permitted literal"
        raise _GrammarError(msg)


# --------------------------------------------------------------------------
# Evaluating (SEM-002, SEM-003, SEM-005)
# --------------------------------------------------------------------------


def evaluate(
    expr: Expr | CompiledExpr,
    env: Mapping[str, object],
    /,
    *,
    functions: Mapping[str, Callable[..., object]] = FUNCTIONS,
    budget: int = BUDGET,
    entry_id: str,
    field: str,
) -> object:
    """Evaluate ``expr`` against ``env``, or raise naming where it was written.

    Every failure mode -- a name that is not there, an object that raises when
    read, a budget that runs out -- arrives as one :class:`ExpressionError`, so
    a caller has one thing to catch and one thing to report (SEM-005).
    """
    compiled = (
        expr
        if isinstance(expr, CompiledExpr)
        else compile_expr(expr.source, entry_id=entry_id, field=field)
    )
    machine = _Machine(env, functions, budget)
    try:
        return machine.run(compiled.tree.body)
    except _EvalError as exc:
        raise ExpressionError(entry_id, field, compiled.source, str(exc)) from None
    except RecursionError:
        reason = "evaluation nested too deeply"
        raise ExpressionError(entry_id, field, compiled.source, reason) from None
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        raise ExpressionError(entry_id, field, compiled.source, reason) from exc


def _size(value: object) -> int:
    """What repeating or concatenating ``value`` costs. Zero for scalars."""
    if isinstance(value, str | bytes | list | tuple | set | frozenset | dict):
        return len(value)
    return 0


class _Machine:
    """A tree-walking interpreter over an already-validated expression.

    Holds no module state and no reference to anything but what it was handed,
    which is what makes "an expression can reach only its environment" a
    property of the code rather than a claim about it.
    """

    __slots__ = ("_env", "_fns", "_left")

    def __init__(
        self,
        env: Mapping[str, object],
        functions: Mapping[str, Callable[..., object]],
        budget: int,
    ) -> None:
        self._env = env
        self._fns = functions
        self._left = budget

    def run(self, node: ast.expr) -> object:
        return self._eval(node)

    # -- accounting --------------------------------------------------------

    def _spend(self, cost: int) -> None:
        self._left -= cost
        if self._left < 0:
            msg = "evaluation exceeded its step budget"
            raise _EvalError(msg)

    def _guard(self, value: object) -> object:
        if isinstance(value, Opaque):
            raise _EvalError(value.why)
        return value

    # -- the walk ----------------------------------------------------------

    def _eval(self, node: ast.expr) -> object:
        self._spend(1)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return self._name(node)
        if isinstance(node, ast.Attribute):
            return self._attribute(node)
        if isinstance(node, ast.Subscript):
            return self._subscript(node)
        if isinstance(node, ast.Slice):
            return self._slice(node)
        if isinstance(node, ast.BinOp):
            return self._binop(node)
        if isinstance(node, ast.UnaryOp):
            return self._unaryop(node)
        if isinstance(node, ast.BoolOp):
            return self._boolop(node)
        if isinstance(node, ast.Compare):
            return self._compare(node)
        if isinstance(node, ast.IfExp):
            body = node.body if self._eval(node.test) else node.orelse
            return self._eval(body)
        if isinstance(node, ast.Call):
            return self._call(node)
        return self._container(node)

    def _name(self, node: ast.Name) -> object:
        try:
            value = self._env[node.id]
        except KeyError:
            msg = f"unknown name {node.id!r}"
            raise _EvalError(msg) from None
        return self._guard(value)

    def _attribute(self, node: ast.Attribute) -> object:
        owner = self._eval(node.value)
        try:
            value = getattr(owner, node.attr)
        except AttributeError:
            msg = f"no attribute {node.attr!r}"
            raise _EvalError(msg) from None
        return self._guard(value)

    def _subscript(self, node: ast.Subscript) -> object:
        owner = self._eval(node.value)
        key = self._eval(node.slice)
        try:
            value = owner[key]  # type: ignore[index]  # the failure is the answer
        except (KeyError, IndexError, TypeError) as exc:
            msg = f"{type(exc).__name__}: {exc}"
            raise _EvalError(msg) from None
        return self._guard(value)

    def _slice(self, node: ast.Slice) -> object:
        return slice(
            None if node.lower is None else self._eval(node.lower),
            None if node.upper is None else self._eval(node.upper),
            None if node.step is None else self._eval(node.step),
        )

    def _binop(self, node: ast.BinOp) -> object:
        left = self._eval(node.left)
        right = self._eval(node.right)
        kind = type(node.op)
        # Charged before the operation, never after: a budget that notices a
        # ten-million-character string once it exists has already paid for it.
        if kind is ast.Mult:
            self._spend(_repeat(left, right))
        elif kind is ast.Add:
            self._spend(_size(left) + _size(right))
        return _BINOPS[kind](left, right)

    def _unaryop(self, node: ast.UnaryOp) -> object:
        value = self._eval(node.operand)
        if isinstance(node.op, ast.Not):
            return not value
        if isinstance(node.op, ast.USub):
            return -value  # type: ignore[operator]  # a TypeError is the answer
        return +value  # type: ignore[operator]  # likewise

    def _boolop(self, node: ast.BoolOp) -> object:
        wanted = isinstance(node.op, ast.And)
        value: object = wanted
        for operand in node.values:
            value = self._eval(operand)
            if bool(value) is not wanted:
                return value
        return value

    def _compare(self, node: ast.Compare) -> object:
        left = self._eval(node.left)
        for op, right_node in zip(node.ops, node.comparators, strict=True):
            right = self._eval(right_node)
            kind = type(op)
            if kind in (ast.In, ast.NotIn):
                self._spend(_size(right))
            if not _COMPARES[kind](left, right):
                return False
            left = right
        return True

    def _call(self, node: ast.Call) -> object:
        # The callee is a Name by construction (`_check`), and it is resolved
        # in `functions` alone: a name in the environment is data, never code.
        assert isinstance(node.func, ast.Name)
        found = self._fns.get(node.func.id)
        if found is None:
            msg = f"unknown function {node.func.id!r}"
            raise _EvalError(msg)
        args = [self._eval(arg) for arg in node.args]
        words = {
            word.arg: self._eval(word.value)
            for word in node.keywords
            if word.arg is not None
        }
        self._spend(sum(_size(arg) for arg in args))
        result = found(*args, **words)
        self._spend(_size(result))
        return self._guard(result)

    def _container(self, node: ast.expr) -> object:
        if isinstance(node, ast.Dict):
            pairs = [
                (self._eval(key), self._eval(value))
                for key, value in zip(node.keys, node.values, strict=True)
                if key is not None  # `{**x}` is not in the grammar
            ]
            self._spend(len(pairs))
            return dict(pairs)
        assert isinstance(node, ast.List | ast.Tuple | ast.Set)
        items = [self._eval(item) for item in node.elts]
        self._spend(len(items))
        if isinstance(node, ast.List):
            return items
        return tuple(items) if isinstance(node, ast.Tuple) else set(items)


def _repeat(left: object, right: object) -> int:
    """What ``left * right`` will cost, computed without doing it."""
    for sized, count in ((left, right), (right, left)):
        if isinstance(count, int) and not isinstance(count, bool):
            size = _size(sized)
            if size:
                return max(size * count, 0)
    return 0


# --------------------------------------------------------------------------
# Documents (SEM-001, SEM-006)
# --------------------------------------------------------------------------

#: Where an expression is, its source, and why it was rejected.
Problem = tuple[tuple[str | int, ...], str, str]


def is_envelope(value: object, /) -> bool:
    """Whether ``value`` is the portable form: a mapping of exactly one key."""
    return (
        isinstance(value, Mapping)
        and len(value) == 1
        and ENVELOPE in value
        and isinstance(value[ENVELOPE], str)
    )


def envelope_of(expr: Expr, /) -> dict[str, str]:
    """The portable form of ``expr``, valid in YAML, JSON and TOML alike."""
    return {ENVELOPE: expr.source}


def parse_expressions(value: object, /) -> tuple[object, tuple[Problem, ...]]:
    """Replace every envelope in ``value`` with an :class:`Expr`, compiling it.

    Returns the converted structure and every rejected source with its
    position. Rejections are returned rather than raised because they belong
    beside the document's other mistakes, in one report (SEM-001).

    A structure with no expressions in it comes back as the identical object:
    reading a config file must not quietly copy what an operator wrote.
    """
    problems: list[Problem] = []
    return _parse(value, (), problems), tuple(problems)


def _parse(
    value: object, path: tuple[str | int, ...], problems: list[Problem]
) -> object:
    if isinstance(value, Expr) or is_envelope(value):
        expr = value if isinstance(value, Expr) else Expr(str(_envelope_source(value)))
        compiled, reason = _compile(expr.source)
        if compiled is None:
            problems.append((path, expr.source, reason))
        return expr
    if isinstance(value, Mapping):
        out = {
            key: _parse(item, (*path, _step(key)), problems)
            for key, item in value.items()
        }
        return out if any(out[key] is not value[key] for key in out) else value
    if isinstance(value, list | tuple):
        items = [
            _parse(item, (*path, index), problems) for index, item in enumerate(value)
        ]
        same = all(new is old for new, old in zip(items, value, strict=True))
        return value if same else items
    return value


def unparse_expressions(value: object, /) -> object:
    """The inverse: every :class:`Expr` back to its portable envelope.

    One writer for all three formats. A document that survives a read-write
    round trip in JSON survives it in YAML and TOML for the same reason, and
    the expression comes back out as the text that went in (SEM-006).
    """
    if isinstance(value, Expr):
        return envelope_of(value)
    if isinstance(value, Mapping):
        out = {key: unparse_expressions(item) for key, item in value.items()}
        return out if any(out[key] is not value[key] for key in out) else value
    if isinstance(value, list | tuple):
        items = [unparse_expressions(item) for item in value]
        same = all(new is old for new, old in zip(items, value, strict=True))
        return value if same else items
    return value


def expression_paths(value: object, /) -> tuple[tuple[str | int, ...], ...]:
    """Every position in ``value`` holding an expression, envelope or parsed.

    What SEM-001 is enforced with: a field that may not contain one asks for
    this and reports each position it gets back.
    """
    return tuple(_paths(value, ()))


def _paths(value: object, path: tuple[str | int, ...]) -> list[tuple[str | int, ...]]:
    if isinstance(value, Expr) or is_envelope(value):
        return [path]
    if isinstance(value, Mapping):
        return [
            found
            for key, item in value.items()
            for found in _paths(item, (*path, _step(key)))
        ]
    if isinstance(value, list | tuple):
        return [
            found
            for index, item in enumerate(value)
            for found in _paths(item, (*path, index))
        ]
    return []


def substitute(
    value: object,
    env: Mapping[str, object],
    /,
    *,
    functions: Mapping[str, Callable[..., object]] = FUNCTIONS,
    budget: int = BUDGET,
    entry_id: str,
    field: str,
) -> object:
    """Evaluate every expression inside ``value``, in place of itself.

    Each expression gets its own budget: a config holding ten of them is ten
    small programs, not one large one, and one runaway must not be able to
    starve its neighbours out of an error message.
    """
    if isinstance(value, Expr):
        return evaluate(
            value,
            env,
            functions=functions,
            budget=budget,
            entry_id=entry_id,
            field=field,
        )
    if isinstance(value, Mapping):
        out = {
            key: substitute(
                item,
                env,
                functions=functions,
                budget=budget,
                entry_id=entry_id,
                field=f"{field}.{_step(key)}",
            )
            for key, item in value.items()
        }
        return out if any(out[key] is not value[key] for key in out) else value
    if isinstance(value, list | tuple):
        items = [
            substitute(
                item,
                env,
                functions=functions,
                budget=budget,
                entry_id=entry_id,
                field=f"{field}.{index}",
            )
            for index, item in enumerate(value)
        ]
        same = all(new is old for new, old in zip(items, value, strict=True))
        return value if same else items
    return value


def opaque(value: object, why: str, /) -> object:
    """``value`` with every expression in it replaced by :class:`Opaque`.

    How one entry is shown to another's expressions: the literal part is
    readable, the computed part fails with ``why`` rather than resolving to
    something that depends on evaluation order.
    """
    if isinstance(value, Expr):
        return Opaque(why)
    if isinstance(value, Mapping):
        return {key: opaque(item, why) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [opaque(item, why) for item in value]
    return value


def _envelope_source(value: object) -> object:
    assert isinstance(value, Mapping)
    return value[ENVELOPE]


def _step(key: object) -> str | int:
    return key if isinstance(key, str | int) else str(key)


# --------------------------------------------------------------------------
# YAML
# --------------------------------------------------------------------------

_LOADER: list[type] = []


def yaml_loader() -> type:
    """A ``SafeLoader`` subclass that reads ``!expr`` as an :class:`Expr`.

    Built once and reused: PyYAML keeps constructors on the class, so a fresh
    subclass per read would grow a new type per file. Imported lazily, because
    nothing else in the loader needs PyYAML installed.
    """
    if _LOADER:
        return _LOADER[0]
    import yaml

    class ExprLoader(yaml.SafeLoader):
        """SafeLoader plus one tag. Everything else it refuses, it still refuses."""

    def construct(loader: ExprLoader, node: yaml.ScalarNode) -> Expr:
        return Expr(str(loader.construct_scalar(node)))

    ExprLoader.add_constructor(TAG, construct)
    _LOADER.append(ExprLoader)
    return ExprLoader
