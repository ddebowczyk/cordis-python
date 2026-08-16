"""Properties of the error taxonomy and of mount-site attribution.

The attribution properties are the primitive half of PROP-DIAG-005: they hold
the annotate-don't-wrap contract at the level of the context manager, before
there is a plugin system to mount anything with. The capability-level property
re-tests it through real mounts once tier 1 lands.
"""

from __future__ import annotations

import pickle
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

import cordis
from cordis.errors import (
    AsyncValidationError,
    ConfigValidationError,
    CordisError,
    DependencyCycleError,
    EventModeError,
    ExpressionError,
    InactiveScopeError,
    InvalidEffectError,
    InvalidPluginError,
    NextCalledTwiceError,
    PatchTargetError,
    RegistryConflictError,
    ServiceConflictError,
    ServiceNotFoundError,
    mount_attribution,
    mount_sites,
)

if TYPE_CHECKING:
    from types import TracebackType

# The classes the specification names. Listed literally rather than discovered
# by walking the module, so deleting one is a test failure instead of a smaller
# test run.
TAXONOMY: tuple[type[CordisError], ...] = (
    AsyncValidationError,
    ConfigValidationError,
    DependencyCycleError,
    EventModeError,
    ExpressionError,
    InactiveScopeError,
    InvalidEffectError,
    InvalidPluginError,
    NextCalledTwiceError,
    PatchTargetError,
    RegistryConflictError,
    ServiceConflictError,
    ServiceNotFoundError,
)


class UserDefinedError(Exception):
    """Stands in for an exception a plugin author defined and catches by type."""


#: Exception types a plugin body might raise, including one the framework has
#: never heard of -- the case wrapping would break.
RAISABLE: tuple[type[Exception], ...] = (
    ValueError,
    KeyError,
    RuntimeError,
    ZeroDivisionError,
    UserDefinedError,
    ServiceNotFoundError,
)

sites = st.text(
    st.characters(min_codepoint=33, max_codepoint=126, exclude_characters="\n"),
    min_size=1,
    max_size=24,
)


def _deepest_frame(tb: TracebackType | None) -> str:
    name = "<none>"
    while tb is not None:
        name = tb.tb_frame.f_code.co_name
        tb = tb.tb_next
    return name


def _boom(exc_type: type[Exception]) -> None:
    raise exc_type("original")


# --------------------------------------------------------------------------
# Taxonomy
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@pytest.mark.parametrize("cls", TAXONOMY, ids=lambda c: c.__name__)
def test_every_error_is_exported_and_rooted(cls: type[CordisError]) -> None:
    assert issubclass(cls, CordisError)
    assert cls.__name__ in cordis.__all__
    assert getattr(cordis, cls.__name__) is cls


@pytest.mark.tier_local
def test_codes_are_unique_and_specific() -> None:
    codes = [cls.code for cls in TAXONOMY]
    assert all(codes)
    assert CordisError.code not in codes, "a subclass left `code` at the default"
    assert len(set(codes)) == len(codes)


@pytest.mark.tier_local
@pytest.mark.parametrize(
    ("cls", "builtin"),
    [
        (ServiceNotFoundError, AttributeError),
        (InvalidEffectError, TypeError),
        (InvalidPluginError, TypeError),
        (AsyncValidationError, TypeError),
        (EventModeError, TypeError),
        (InactiveScopeError, RuntimeError),
        (NextCalledTwiceError, RuntimeError),
        (ConfigValidationError, ValueError),
        (ExpressionError, ValueError),
        (PatchTargetError, ValueError),
    ],
    ids=lambda x: getattr(x, "__name__", str(x)),
)
def test_errors_are_also_the_builtin_callers_already_catch(
    cls: type[CordisError], builtin: type[Exception]
) -> None:
    assert issubclass(cls, builtin)


@pytest.mark.tier_local
@given(name=st.from_regex(r"[a-z][a-z0-9_]{0,19}", fullmatch=True))
def test_missing_service_behaves_like_a_missing_attribute(name: str) -> None:
    """`hasattr` must answer False, not propagate a framework error.

    Failure value: `copy.deepcopy(ctx)` or a debugger's `inspect.getmembers`
    walking a Context and blowing up instead of skipping absent names.

    The name is constructed rather than filtered, and lowercase for a reason:
    every attribute an object already has is a dunder, so a generated
    `__dict__` makes `hasattr` answer True without `__getattr__` ever being
    consulted -- a hole in the test rather than a service that is missing.
    """

    class Probe:
        def __getattr__(self, attr: str) -> object:
            raise ServiceNotFoundError(attr, searched=("root",))

    assert not hasattr(Probe(), name)
    assert getattr(Probe(), "db", "fallback") == "fallback"


# --------------------------------------------------------------------------
# Payload survives a round trip
# --------------------------------------------------------------------------

instances = st.one_of(
    st.builds(ServiceNotFoundError, st.text(min_size=1), st.lists(st.text())),
    st.builds(ServiceConflictError, st.text(), st.text(), st.text()),
    st.builds(DependencyCycleError, st.lists(st.text(min_size=1), min_size=1)),
    st.builds(InvalidEffectError, st.one_of(st.integers(), st.text(), st.none())),
    st.builds(InactiveScopeError, st.text(), st.text()),
    st.builds(InvalidPluginError, st.text(), st.text()),
    st.builds(AsyncValidationError, st.text()),
    st.builds(EventModeError, st.text(), st.text(), st.text()),
    st.builds(NextCalledTwiceError, st.text(), st.text()),
    st.builds(ExpressionError, st.text(), st.text(), st.text(), st.text()),
    st.builds(PatchTargetError, st.text(), st.text(), st.text(), st.text()),
    st.builds(RegistryConflictError, st.text(), st.text()),
)


@pytest.mark.tier_local
@given(error=instances)
def test_errors_survive_pickling(error: CordisError) -> None:
    """Type, message and structured payload are preserved by a pickle round trip.

    Failure value: a worker process in a pytest-xdist or ProcessPool run
    reporting `PicklingError` instead of the defect that actually occurred.
    """
    restored = pickle.loads(pickle.dumps(error))

    assert type(restored) is type(error)
    assert str(restored) == str(error)
    assert restored.details() == error.details()


@pytest.mark.tier_local
@given(error=instances)
def test_details_cover_the_declared_fields(error: CordisError) -> None:
    assert set(error.details()) == set(type(error).fields)
    assert error.message in str(error)


# --------------------------------------------------------------------------
# Mount-site attribution (PROP-DIAG-005, primitive level)
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(
    trail=st.lists(sites, min_size=1, max_size=8),
    exc_type=st.sampled_from(RAISABLE),
)
def test_attribution_records_the_trail_innermost_first(
    trail: list[str], exc_type: type[Exception]
) -> None:
    """Notes name every enclosing mount site, ordered innermost to outermost.

    The oracle is the generated trail, reversed -- built from the nesting the
    test issued, never read back from the implementation.

    Failure value: an outermost-first trail, which points a reader at the
    application root instead of at the plugin that actually failed.
    """

    def enter(depth: int) -> None:
        if depth == len(trail):
            _boom(exc_type)
            return
        with mount_attribution(trail[depth]):
            enter(depth + 1)

    with pytest.raises(exc_type) as caught:
        enter(0)

    assert mount_sites(caught.value) == tuple(reversed(trail))


@pytest.mark.tier_local
@given(
    trail=st.lists(sites, min_size=1, max_size=6),
    exc_type=st.sampled_from(RAISABLE),
)
def test_attribution_preserves_type_identity_and_traceback(
    trail: list[str], exc_type: type[Exception]
) -> None:
    """The exception that arrives is the one that was raised.

    Failure value: wrapping the exception in a framework class, so a plugin
    author's `except MyError` stops working the moment the loader mounts the
    plugin rather than the test calling it directly.
    """
    raised: BaseException | None = None

    def enter(depth: int) -> None:
        nonlocal raised
        if depth == len(trail):
            try:
                _boom(exc_type)
            except exc_type as exc:
                raised = exc
                raise
            return
        with mount_attribution(trail[depth]):
            enter(depth + 1)

    with pytest.raises(exc_type) as caught:
        enter(0)

    assert caught.value is raised
    assert type(caught.value) is exc_type
    # The traceback still ends where the exception was raised, not in the
    # attribution helper.
    assert _deepest_frame(caught.value.__traceback__) == "_boom"


# Notes some other library attached. Built with a foreign prefix rather than
# filtered out of arbitrary text: a note that happens to carry the mount prefix
# is indistinguishable from a real one by construction, so it is excluded from
# the domain deliberately rather than generated and discarded.
foreign_notes = st.lists(st.text(max_size=20).map(lambda s: f"other library: {s}"))


@pytest.mark.tier_local
@given(trail=st.lists(sites, min_size=1, max_size=6), foreign=foreign_notes)
def test_mount_sites_ignores_notes_from_elsewhere(
    trail: list[str], foreign: list[str]
) -> None:
    """Notes attached by other libraries are not read as mount sites.

    Failure value: a trail polluted by an unrelated library's note, so the
    reported mount chain names a site that does not exist.
    """
    error = RuntimeError("boom")
    for note in foreign:
        error.add_note(note)

    def enter(depth: int) -> None:
        if depth == len(trail):
            raise error
        with mount_attribution(trail[depth]):
            enter(depth + 1)

    with pytest.raises(RuntimeError) as caught:
        enter(0)

    assert mount_sites(caught.value) == tuple(reversed(trail))
    assert set(foreign) <= set(caught.value.__notes__)
