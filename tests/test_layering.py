"""PROP-LAYER-001..005, from spec/capabilities/16-config-layering.yaml.

The oracles are a handful of dict and list assignments the test keeps while
walking the same generated plan the fold walks. That is the whole point of a
pure fold over frozen values: the model can be written out in five lines and
share nothing with the implementation.
"""

from __future__ import annotations

import copy
from dataclasses import fields as fields_of
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cordis.errors import ConfigValidationError, PatchTargetError
from cordis.expr import Expr
from cordis.layering import BASE, Layer, Patch, Resolution, read_layer, resolve, wrote
from cordis.loader import GROUP, Entry

if TYPE_CHECKING:
    from collections.abc import Sequence

# The alphabet is deliberately tiny: overlap between layers is the interesting
# case, and a wide namespace makes it rare.
LEAVES = ("a", "b", "c")
GROUP_ID = "g"
CHILDREN = ("x", "y")
PATHS = ("a", "b", "c", "g.x", "g.y")
NAMES = ("alpha", "beta")
FIELDS = ("config", "disabled", "inject")

Key = tuple[str, str]


def _value(field: str, seed: int) -> object:
    """One generated value per field, kept comparable to what `Entry` stores."""
    if field == "config":
        return {"v": seed}
    if field == "disabled":
        return bool(seed % 2)
    return None if seed % 3 == 0 else tuple(f"s{index}" for index in range(seed % 3))


def _base() -> tuple[Entry, ...]:
    leaves = tuple(
        Entry(id=found, name=NAMES[index % len(NAMES)], config={"v": 0})
        for index, found in enumerate(LEAVES)
    )
    children = tuple(
        Entry(id=found, name=NAMES[index % len(NAMES)], config={"v": 0})
        for index, found in enumerate(CHILDREN)
    )
    return (*leaves, Entry(id=GROUP_ID, name=GROUP, group=children))


BASE_ENTRIES = _base()

NAME_OF = {
    "a": NAMES[0],
    "b": NAMES[1],
    "c": NAMES[0],
    "g": GROUP,
    "g.x": NAMES[0],
    "g.y": NAMES[1],
}


def _lookup(entries: Sequence[Entry], path: str) -> Entry:
    """The entry at a dotted path, or an assertion -- the test's own walk."""
    head, _, rest = path.partition(".")
    for entry in entries:
        if entry.id == head:
            if not rest:
                return entry
            assert entry.group is not None
            return _lookup(entry.group, rest)
    raise AssertionError(path)


# --------------------------------------------------------------------------
# PROP-LAYER-001: resolution is a pure function
# --------------------------------------------------------------------------


@st.composite
def _plan(draw: st.DrawFn, *, max_layers: int = 6) -> tuple[Layer, ...]:
    """Layers of field patches over paths that exist, and nothing else."""
    layers = []
    for index in range(draw(st.integers(0, max_layers))):
        patches = tuple(
            Patch(
                id=draw(st.sampled_from(PATHS)),
                fields={
                    field: _value(field, draw(st.integers(0, 5)))
                    for field in draw(
                        st.lists(
                            st.sampled_from(FIELDS), min_size=1, max_size=3, unique=True
                        )
                    )
                },
            )
            for _ in range(draw(st.integers(0, 3)))
        )
        layers.append(Layer(source=f"layer{index}", patches=patches))
    return tuple(layers)


@pytest.mark.tier_local
@settings(max_examples=50)
@given(layers=_plan())
def test_resolution_is_a_pure_function_of_its_inputs(layers: tuple[Layer, ...]) -> None:
    """PROP-LAYER-001: same inputs, same answer, and the inputs come back whole."""
    base_before = copy.deepcopy(BASE_ENTRIES)
    layers_before = copy.deepcopy(layers)

    first = resolve(BASE_ENTRIES, layers)
    second = resolve(BASE_ENTRIES, layers)

    assert first.entries == second.entries
    assert dict(first.provenance) == dict(second.provenance)
    assert base_before == BASE_ENTRIES
    assert layers == layers_before


# --------------------------------------------------------------------------
# PROP-LAYER-002: last write wins, and provenance says who wrote it
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@settings(max_examples=50)
@given(layers=_plan())
def test_every_field_holds_what_the_last_layer_to_write_it_wrote(
    layers: tuple[Layer, ...],
) -> None:
    """PROP-LAYER-002: the model is a dict, walked in the declared order."""
    written: dict[Key, tuple[object, str]] = {}
    for layer in layers:
        for patch in layer.patches:
            assert patch.id is not None
            for field, value in patch.fields.items():
                written[patch.id, field] = (value, layer.source)

    found = resolve(BASE_ENTRIES, layers)
    for path in PATHS:
        entry = _lookup(found.entries, path)
        for field in FIELDS:
            here = written.get((path, field))
            if here is None:
                # Nobody wrote it, so it still has a provenance and it is base.
                assert found.provenance[path, field] == BASE, (path, field)
                continue
            value, source = here
            assert getattr(entry, field) == value, (path, field)
            assert found.provenance[path, field] == source, (path, field)


# --------------------------------------------------------------------------
# PROP-LAYER-003: a neutral layer is neutral
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@settings(max_examples=50)
@given(
    layers=_plan(max_layers=4),
    at=st.integers(0, 4),
    picks=st.lists(
        st.tuples(st.sampled_from(PATHS), st.sampled_from(FIELDS)),
        max_size=4,
        unique=True,
    ),
)
def test_a_layer_that_restates_what_is_already_there_changes_nothing(
    layers: tuple[Layer, ...], at: int, picks: list[tuple[str, str]]
) -> None:
    """PROP-LAYER-003: built from the resolution it will be inserted after."""
    cut = min(at, len(layers))
    so_far = resolve(BASE_ENTRIES, layers[:cut])
    neutral = Layer(
        source="neutral",
        patches=tuple(
            Patch(
                id=path,
                fields={field: getattr(_lookup(so_far.entries, path), field)},
            )
            for path, field in picks
        ),
    )
    with_it = resolve(BASE_ENTRIES, (*layers[:cut], neutral, *layers[cut:]))
    without = resolve(BASE_ENTRIES, layers)
    assert with_it.entries == without.entries


@pytest.mark.tier_local
@settings(max_examples=50)
@given(layers=_plan(max_layers=4), at=st.integers(0, 4))
def test_an_empty_layer_leaves_every_entry_untouched(
    layers: tuple[Layer, ...], at: int
) -> None:
    """PROP-LAYER-003, the other neutral element."""
    cut = min(at, len(layers))
    empty = Layer(source="empty")
    with_it = resolve(BASE_ENTRIES, (*layers[:cut], empty, *layers[cut:]))
    without = resolve(BASE_ENTRIES, layers)
    assert with_it.entries == without.entries
    assert dict(with_it.provenance) == dict(without.provenance)


# --------------------------------------------------------------------------
# PROP-LAYER-004: a patch that cannot apply says so, and resolves nothing
# --------------------------------------------------------------------------

ABSENT = ("zz", "qq", "g.zz", "nope.x")
WRONG = "not-the-name"


@st.composite
def _broken(draw: st.DrawFn) -> tuple[tuple[Layer, ...], tuple[str, str, str]]:
    """Layers with at least one unapplicable patch, and the first of them."""
    plan: list[tuple[str, tuple[Patch, ...]]] = []
    offences: list[tuple[str, str, str]] = []
    for index in range(draw(st.integers(1, 4))):
        source = f"layer{index}"
        patches: list[Patch] = []
        for position in range(draw(st.integers(1, 3))):
            kind = draw(st.sampled_from(("valid", "absent", "misnamed")))
            if kind == "absent":
                target = draw(st.sampled_from(ABSENT))
                patches.append(Patch(id=target, fields={"config": {"v": 1}}))
                offences.append((f"patches[{position}]", source, target))
            elif kind == "misnamed":
                target = draw(st.sampled_from(PATHS))
                patches.append(
                    Patch(id=target, name=WRONG, fields={"config": {"v": 1}})
                )
                offences.append((f"patches[{position}]", source, target))
            else:
                patches.append(
                    Patch(id=draw(st.sampled_from(PATHS)), fields={"config": {"v": 1}})
                )
        plan.append((source, tuple(patches)))
    if not offences:
        first, rows = plan[0]
        plan[0] = (first, (Patch(id=ABSENT[0], fields={"config": {"v": 1}}), *rows))
        offences.append(("patches[0]", first, ABSENT[0]))
    layers = tuple(Layer(source=where, patches=rows) for where, rows in plan)
    return layers, offences[0]


@pytest.mark.tier_local
@settings(max_examples=50)
@given(case=_broken())
def test_a_patch_that_cannot_apply_names_itself_its_layer_and_its_target(
    case: tuple[tuple[Layer, ...], tuple[str, str, str]],
) -> None:
    """PROP-LAYER-004: the first offender, and no partial resolution."""
    layers, (patch_id, source, target) = case
    with pytest.raises(PatchTargetError) as caught:
        resolve(BASE_ENTRIES, layers)
    assert caught.value.patch_id == patch_id
    assert caught.value.layer_source == source
    assert caught.value.target == target


# --------------------------------------------------------------------------
# PROP-LAYER-005: inserts add, and only add
# --------------------------------------------------------------------------


@st.composite
def _inserts(draw: st.DrawFn) -> tuple[tuple[Layer, ...], dict[str, list[str]]]:
    """Insert patches, and the id lists the test expects them to produce."""
    expected = {
        "": [entry.id for entry in BASE_ENTRIES],
        GROUP_ID: list(CHILDREN),
    }
    minted = 0
    layers = []
    for index in range(draw(st.integers(1, 4))):
        patches = []
        for _ in range(draw(st.integers(0, 2))):
            into = draw(st.sampled_from(("", GROUP_ID)))
            rows = tuple(
                Entry(id=f"n{minted + offset}", name=NAMES[0], config={"v": offset})
                for offset in range(draw(st.integers(1, 2)))
            )
            minted += len(rows)
            patches.append(Patch(id=into or None, insert=rows))
            expected[into].extend(row.id for row in rows)
        layers.append(Layer(source=f"layer{index}", patches=tuple(patches)))
    return tuple(layers), expected


@pytest.mark.tier_local
@settings(max_examples=50)
@given(case=_inserts())
def test_inserts_append_where_named_and_move_nothing(
    case: tuple[tuple[Layer, ...], dict[str, list[str]]],
) -> None:
    """PROP-LAYER-005: order and multiplicity, both by construction."""
    layers, expected = case
    found = resolve(BASE_ENTRIES, layers)
    assert [entry.id for entry in found.entries] == expected[""]
    group = _lookup(found.entries, GROUP_ID)
    assert group.group is not None
    assert [entry.id for entry in group.group] == expected[GROUP_ID]

    # Nothing that was there before moved, changed or multiplied.
    for entry in BASE_ENTRIES:
        if entry.id != GROUP_ID:
            assert _lookup(found.entries, entry.id) is entry


# --------------------------------------------------------------------------
# The rules a generator would only reach by accident
# --------------------------------------------------------------------------


def test_setting_a_field_replaces_the_whole_value() -> None:
    """SEM-002: no deep merge, and the omitted key is gone."""
    base = (Entry(id="a", name="alpha", config={"host": "x", "port": 1}),)
    layer = Layer("one", (Patch(id="a", fields={"config": {"port": 2}}),))
    found = resolve(base, (layer,))
    assert _lookup(found.entries, "a").config == {"port": 2}


def test_a_patch_may_declare_the_name_it_expects() -> None:
    """SEM-005, from the side that has to keep working."""
    base = (Entry(id="a", name="alpha"),)
    layer = Layer("one", (Patch(id="a", name="alpha", fields={"disabled": True}),))
    assert _lookup(resolve(base, (layer,)).entries, "a").disabled is True


def test_a_name_that_does_not_match_says_which_name_it_found() -> None:
    base = (Entry(id="a", name="alpha"),)
    layer = Layer("one", (Patch(id="a", name="beta", fields={"disabled": True}),))
    with pytest.raises(PatchTargetError, match="named 'alpha'"):
        resolve(base, (layer,))


def test_inserting_into_something_that_is_not_a_group_is_an_error() -> None:
    """SEM-003: the failure the alternative would silently delete rows for."""
    base = (Entry(id="a", name="alpha", config=[{"id": "x"}]),)
    layer = Layer("one", (Patch(id="a", insert=(Entry(id="n", name="alpha"),)),))
    with pytest.raises(PatchTargetError, match="not a group"):
        resolve(base, (layer,))


def test_inserting_an_id_that_is_already_there_is_an_error() -> None:
    """Two rows with one path would make every later patch ambiguous."""
    layer = Layer("one", (Patch(insert=(Entry(id="a", name="alpha"),)),))
    with pytest.raises(PatchTargetError, match="already"):
        resolve(BASE_ENTRIES, (layer,))


def test_a_patch_that_targets_nothing_cannot_be_built() -> None:
    """Illegal states, made unrepresentable rather than checked in the fold."""
    with pytest.raises(ValueError, match="needs an id"):
        Patch(fields={"config": None})
    with pytest.raises(ValueError, match="cannot set 'id'"):
        Patch(id="a", fields={"id": "b"})
    with pytest.raises(ValueError, match="cannot set 'group'"):
        Patch(id="a", fields={"group": ()})


def test_provenance_covers_every_field_of_every_entry() -> None:
    """SEM-006: a table with a hole in it is a table nobody can read."""
    found = resolve(BASE_ENTRIES, ())
    names = tuple(field.name for field in fields_of(Entry))
    for path in (*LEAVES, GROUP_ID, "g.x", "g.y"):
        for name in names:
            assert found.provenance[path, name] == BASE


def test_an_inserted_entry_carries_the_layer_that_inserted_it() -> None:
    layer = Layer("vendor", (Patch(insert=(Entry(id="n", name="alpha"),)),))
    found = resolve(BASE_ENTRIES, (layer,))
    assert found.provenance["n", "name"] == "vendor"
    assert found.provenance["a", "name"] == BASE
    assert wrote(found, "vendor") == tuple(
        sorted(key for key in found.provenance if key[0] == "n")
    )


def test_a_patched_entry_is_rebuilt_and_its_neighbours_are_not() -> None:
    """The identity claim SEM-007 is easiest to observe through."""
    layer = Layer("one", (Patch(id="g.x", fields={"disabled": True}),))
    found = resolve(BASE_ENTRIES, (layer,))
    assert _lookup(found.entries, "a") is _lookup(BASE_ENTRIES, "a")
    assert _lookup(found.entries, "g.y") is _lookup(BASE_ENTRIES, "g.y")
    assert _lookup(found.entries, "g.x") is not _lookup(BASE_ENTRIES, "g.x")


# --------------------------------------------------------------------------
# Reading a patch document
# --------------------------------------------------------------------------


def test_a_patch_document_reads_the_same_fields_an_entry_does() -> None:
    """One reader for both, so a field cannot mean two things."""
    raw = {
        "patches": [
            {
                "id": "g.x",
                "name": "alpha",
                "fields": {
                    "config": {"level": {"$expr": "env['LEVEL']"}},
                    "inject": ["shell"],
                    "isolate": {"shell": "test"},
                },
            },
            {"insert": [{"id": "n", "name": "alpha"}]},
        ]
    }
    layer = read_layer(raw, "profiles/dev.yaml")
    assert layer.source == "profiles/dev.yaml"
    first, second = layer.patches
    assert first.fields["config"] == {"level": Expr("env['LEVEL']")}
    assert first.fields["inject"] == ("shell",)
    assert first.fields["isolate"] == {"shell": "test"}
    assert second.insert == (Entry(id="n", name="alpha"),)


def test_a_bare_list_is_a_patch_document_too() -> None:
    layer = read_layer([{"id": "a", "fields": {"disabled": True}}], "cli")
    assert layer.patches[0].fields == {"disabled": True}


def test_a_patch_document_reports_every_problem_at_once() -> None:
    raw = {
        "patches": [
            {"id": "a", "fields": {"inject": "shell"}},
            {"id": 3},
            {"id": "b", "wat": 1},
        ]
    }
    with pytest.raises(ConfigValidationError) as caught:
        read_layer(raw, "bad.yaml")
    paths = {tuple(issue.path) for issue in caught.value.issues}
    assert (0, "fields", "inject") in paths
    assert (1, "id") in paths
    assert (2, "wat") in paths


def test_a_config_expression_survives_the_fold_unevaluated() -> None:
    """Layering composes with config expressions because it never looks inside."""
    base = (Entry(id="a", name="alpha", config={"v": 1}),)
    layer = read_layer(
        [{"id": "a", "fields": {"config": {"v": {"$expr": "1 + 1"}}}}], "one"
    )
    found = resolve(base, (layer,))
    assert _lookup(found.entries, "a").config == {"v": Expr("1 + 1")}


def test_a_resolution_is_what_the_loader_takes() -> None:
    """The one integration claim: no second representation in between.

    Handed a list, because a `Sequence` is what the signature accepts and a
    tuple is what `reconcile` compares -- resolving must not pass the caller's
    own mutable list straight through.
    """
    found: Resolution = resolve(list(BASE_ENTRIES), ())
    assert isinstance(found.entries, tuple)
    assert all(isinstance(entry, Entry) for entry in found.entries)
