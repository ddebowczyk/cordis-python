#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Can this machine run what the manifests declare, and does CI still agree?

Two checks that both come from the same idea: a requirement should be stated
once and read from there, never restated.

``doctor`` reports every tool listed under `requires.tools` in any
`capability.yaml`, which capability needs it, and whether it is on PATH. The
list is derived, so a capability that starts needing `gh` is reported the
moment its manifest says so -- there is no second list of prerequisites to
forget.

``--ci`` compares the local CI lane with the GitHub workflow. `just ci` used to
claim it was "everything CI runs, in CI's order" while omitting a whole job,
because the two descriptions were written separately and nothing compared them.
Now the workflow invokes the same capability commands the local lane does, and
this check fails if the two lists stop matching.

Run: ``ops/workflow/bin/doctor.py [--ci]``.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent.parent
OPS = ROOT / "ops"
LOCAL_CI = OPS / "workflow" / "justfile"
GITHUB_CI = ROOT / ".github" / "workflows" / "ci.yml"

#: How the local lane spells a delegation, and how the workflow spells the same
#: thing. Two renderings of one list; the point of `--ci` is that they agree.
DELEGATION = re.compile(
    r"--justfile ops/([a-z][a-z0-9-]*)/justfile\s+"
    r"--working-directory \.\s+([a-z][a-z0-9-]*)"
)
INVOCATION = re.compile(r"just ops ([a-z][a-z0-9-]*) ([a-z][a-z0-9-]*)")


def manifests() -> dict[str, dict[str, object]]:
    found: dict[str, dict[str, object]] = {}
    for path in sorted(OPS.glob("*/capability.yaml")):
        record = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(record, dict):
            found[str(record.get("id", path.parent.name))] = record
    return found


def _commands(record: dict[str, object]) -> set[str]:
    value = record.get("commands")
    if not isinstance(value, list):
        return set()
    return {str(item.get("name")) for item in value if isinstance(item, dict)}


def doctor() -> int:
    """Every declared tool, who needs it, and whether it is here."""
    wanted: dict[str, list[str]] = {}
    for capability_id, record in manifests().items():
        requires = record.get("requires")
        tools = requires.get("tools") if isinstance(requires, dict) else None
        for tool in tools if isinstance(tools, list) else []:
            wanted.setdefault(str(tool), []).append(capability_id)

    missing: list[str] = []
    width = max((len(tool) for tool in wanted), default=0)
    for tool in sorted(wanted):
        location = shutil.which(tool)
        mark = "ok " if location else "MISSING"
        print(f"{mark:<8}{tool:<{width}}  {location or 'not on PATH'}")
        print(f"{'':8}{'':<{width}}  needed by: {', '.join(sorted(wanted[tool]))}")
        if location is None:
            missing.append(tool)

    if missing:
        print(
            f"\ndoctor: {len(missing)} tool(s) missing: {', '.join(missing)}",
            file=sys.stderr,
        )
        return 1
    print(f"\ndoctor: {len(wanted)} tools, all present")
    return 0


def _uncommented(text: str) -> str:
    """The file with its comment lines removed.

    Both files are prose-heavy, and prose about CI quotes CI commands: the
    sentence explaining that `ci.yml` mirrors the local lane reads far better
    with the command in it. A comment is not a step, and counting one as a step
    made this check fail on its own documentation.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _steps(text: str, pattern: re.Pattern[str]) -> list[tuple[str, str]]:
    return [
        (match.group(1), match.group(2))
        for match in pattern.finditer(_uncommented(text))
    ]


def ci_check() -> int:
    """The local lane and the workflow are one list rendered twice."""
    local = _steps(LOCAL_CI.read_text(encoding="utf-8"), DELEGATION)
    remote = _steps(GITHUB_CI.read_text(encoding="utf-8"), INVOCATION)
    problems: list[str] = []

    if not local:
        here = LOCAL_CI.relative_to(ROOT)
        problems.append(f"{here}: no capability delegations found")
    if not remote:
        there = GITHUB_CI.relative_to(ROOT)
        problems.append(f"{there}: no `just ops` invocations found")

    known = manifests()
    for capability_id, command in {*local, *remote}:
        record = known.get(capability_id)
        if record is None:
            problems.append(f"CI runs unknown capability {capability_id}")
        elif command not in _commands(record):
            problems.append(
                f"CI runs {capability_id} {command}, "
                f"which the manifest does not declare"
            )

    problems += [
        f"{capability} {command} runs locally but not in {GITHUB_CI.name}"
        for capability, command in local
        if (capability, command) not in remote
    ]
    problems += [
        f"{capability} {command} runs in {GITHUB_CI.name} but not locally"
        for capability, command in remote
        if (capability, command) not in local
    ]

    if problems:
        print("\n".join(f"ci: {problem}" for problem in problems), file=sys.stderr)
        return 1
    ordered = ", ".join(f"{capability} {command}" for capability, command in local)
    print(f"local CI and {GITHUB_CI.name} agree: {ordered}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Toolchain and CI-description checks.")
    parser.add_argument(
        "--ci", action="store_true", help="compare the local CI lane with the workflow"
    )
    arguments = parser.parse_args(argv)
    return ci_check() if arguments.ci else doctor()


if __name__ == "__main__":
    sys.exit(main())
