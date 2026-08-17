"""The validator, tested against the catalogue it validates.

Two kinds of test, and the first is the one that matters day to day: the live
`ops/` tree must validate cleanly, so a manifest that drifts from the
repository fails here as well as in the gate.

The rest check that the rules would actually fire. A validator whose rules
never fire is indistinguishable from one with no rules, and the only way to
tell the two apart is to hand it something wrong.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

SCRIPT = Path(__file__).resolve().parent.parent.parent / "bin" / "ops.py"


def _validator() -> ModuleType:
    """Import the PEP 723 script by path -- it is a command, not a package."""
    spec = importlib.util.spec_from_file_location("ops_validator", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


OPS = _validator()


def test_the_live_catalogue_validates() -> None:
    diagnostics = OPS.validate()
    assert diagnostics == [], "\n".join(
        diagnostic.render() for diagnostic in diagnostics
    )


def test_every_capability_is_selected_and_runnable() -> None:
    found = OPS.capabilities()
    assert found, "no capabilities found"
    for capability in found:
        assert (capability.root / "justfile").is_file()
        assert (capability.root / "README.md").is_file()
        assert capability.commands(), f"{capability.id} declares no commands"


def test_the_lanes_are_assembled_from_the_manifests() -> None:
    """A lane is what the manifests say it is, not a list kept somewhere else."""
    for name in ("check", "test"):
        steps = OPS.lane(name)
        assert steps, f"no command declares aggregate: {name}"
        for step in steps:
            capability = next(
                found for found in OPS.capabilities() if found.id == step.capability
            )
            declared = {command.get("name") for command in capability.commands()}
            assert step.command in declared


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("ops/docs/**", "ops/docs/**", True),
        # Not a path into a peer's `bin/`, deliberately: `peer-bin-reach` scans
        # source text, and it is right to fire on one even here.
        ("ops/docs/**", "ops/docs/schema/thing.yaml", True),
        ("ops/docs/**", "ops/version/**", False),
        # The trap this is guarding: a prefix that is a prefix of the *string*
        # but not of the *path*. `ops/doc` is not inside `ops/docs`.
        ("ops/docs/**", "ops/docsets/thing.md", False),
        ("justfile", "ops/justfile", False),
        ("docs/reference.md", "docs/**", True),
    ],
)
def test_claims_overlap_exactly_when_they_can_describe_one_file(
    left: str, right: str, *, expected: bool
) -> None:
    assert OPS._overlap(left, right) is expected
    assert OPS._overlap(right, left) is expected, "overlap must be symmetric"


def test_a_file_under_ops_that_nobody_claims_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule that makes ownership a fact rather than an intention."""
    root = tmp_path
    ops = root / "ops"
    (ops / "example").mkdir(parents=True)
    (ops / "example" / "capability.yaml").write_text("id: example\n", encoding="utf-8")
    (ops / "example" / "stray.txt").write_text("nobody claims me\n", encoding="utf-8")
    monkeypatch.setattr(OPS, "ROOT", root)
    monkeypatch.setattr(OPS, "OPS", ops)

    capability = OPS.Capability(
        "example", {"id": "example", "owns": ["ops/example/capability.yaml"]}
    )
    diagnostics = OPS._check_ownership([capability])

    rules = {diagnostic.rule for diagnostic in diagnostics}
    assert rules == {"unowned-path"}
    assert any("stray.txt" in diagnostic.file for diagnostic in diagnostics)


def test_two_capabilities_cannot_claim_the_same_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ops = tmp_path / "ops"
    ops.mkdir(parents=True)
    monkeypatch.setattr(OPS, "ROOT", tmp_path)
    monkeypatch.setattr(OPS, "OPS", ops)

    diagnostics = OPS._check_ownership(
        [
            OPS.Capability("one", {"id": "one", "owns": ["docs/**"]}),
            OPS.Capability("two", {"id": "two", "generates": ["docs/reference.md"]}),
        ]
    )

    assert [diagnostic.rule for diagnostic in diagnostics] == ["overlapping-claim"]


def test_a_dependency_cycle_is_reported() -> None:
    diagnostics = OPS._check_graph(
        [
            OPS.Capability("one", {"id": "one", "requires": {"capabilities": ["two"]}}),
            OPS.Capability("two", {"id": "two", "requires": {"capabilities": ["one"]}}),
        ]
    )
    assert {diagnostic.rule for diagnostic in diagnostics} == {"capability-cycle"}


def test_a_dependency_that_does_not_exist_is_reported() -> None:
    diagnostics = OPS._check_graph(
        [
            OPS.Capability(
                "one", {"id": "one", "requires": {"capabilities": ["nowhere"]}}
            )
        ]
    )
    assert [diagnostic.rule for diagnostic in diagnostics] == ["missing-capability"]
