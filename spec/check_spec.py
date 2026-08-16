#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Cross-file consistency checks for the capability catalog.

`ys` validates each file against the schema in isolation. This checks the
things a per-file schema cannot: that references resolve, that the tier
ordering is a real build order, and that every normative MUST is covered by at
least one property card.

Run: ``spec/check_spec.py`` (or ``just spec-check``). Exits non-zero on any
error; coverage gaps are reported as warnings and do not fail the build,
because a MUST may legitimately be enforced structurally rather than by a test
-- but each one should be a deliberate decision.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

SPEC_DIR = Path(__file__).parent
CAPABILITIES = SPEC_DIR / "capabilities"

PROPERTY_SHAPES = {
    "roundtrip",
    "differential",
    "idempotency",
    "safety",
    "conservation",
    "monotonicity",
    "state-machine",
    "metamorphic",
}


def load() -> dict[str, dict[str, Any]]:
    caps: dict[str, dict[str, Any]] = {}
    for path in sorted(CAPABILITIES.glob("*.yaml")):
        data = yaml.safe_load(path.read_text())
        data["__path__"] = path.name
        caps[data["id"]] = data
    return caps


def check(caps: dict[str, dict[str, Any]]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    seen_property_ids: Counter[str] = Counter()

    for cap_id, cap in caps.items():
        where = cap["__path__"]

        # Dependencies must exist and must not point upward in tier.
        for dep in cap.get("depends_on", []):
            if dep not in caps:
                errors.append(f"{where}: depends_on unknown capability {dep!r}")
            elif caps[dep]["tier"] > cap["tier"]:
                errors.append(
                    f"{where}: {cap_id} (tier {cap['tier']}) depends on "
                    f"{dep} (tier {caps[dep]['tier']}) -- dependencies may "
                    f"only point at the same tier or lower"
                )

        # No cycles via a simple reachability walk from each capability.
        stack, seen = list(cap.get("depends_on", [])), set()
        while stack:
            node = stack.pop()
            if node == cap_id:
                errors.append(f"{where}: {cap_id} is in a dependency cycle")
                break
            if node in seen or node not in caps:
                continue
            seen.add(node)
            stack.extend(caps[node].get("depends_on", []))

        sem_ids = {s["id"] for s in cap["semantics"]}
        if len(sem_ids) != len(cap["semantics"]):
            errors.append(f"{where}: duplicate semantics ids")

        cited: set[str] = set()
        for prop in cap["properties"]:
            seen_property_ids[prop["id"]] += 1
            for ref in prop["evidence"]:
                if ref not in sem_ids:
                    errors.append(
                        f"{where}: {prop['id']} cites {ref}, which this "
                        f"capability does not define"
                    )
                cited.add(ref)

        # Every MUST / MUST-NOT should be covered by a property card.
        warnings.extend(
            f"{where}: {sem['id']} ({sem['kind']}) is not cited by any property card"
            for sem in cap["semantics"]
            if sem["kind"] in {"MUST", "MUST-NOT"} and sem["id"] not in cited
        )

    for prop_id, count in seen_property_ids.items():
        if count > 1:
            errors.append(f"property id {prop_id} used {count} times across files")

    return errors, warnings


def main() -> int:
    caps = load()
    errors, warnings = check(caps)

    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    for error in errors:
        print(f"error: {error}", file=sys.stderr)

    props = sum(len(c["properties"]) for c in caps.values())
    sems = sum(len(c["semantics"]) for c in caps.values())
    print(
        f"{len(caps)} capabilities, {sems} normative rules, {props} property "
        f"cards, {len(warnings)} coverage warnings"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
