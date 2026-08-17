#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""The repository-operations catalogue: validate it, list it, run its lanes.

A directory per capability is only a longer path to the same tangle unless
something holds the manifests to what they claim. That is this script's whole
job. It answers three questions:

* ``validate`` -- do the manifests describe the repository that exists? Every
  invariant below has a name, and a failure prints that name, because "ops is
  broken" is not a diagnosis.
* ``list`` -- what capabilities exist, what do they provide, what do they run?
* ``aggregate`` -- which commands belong to a repository-wide lane? The lanes
  are assembled from the manifests, so `just ops check` cannot fall out of step
  with the capabilities it is supposed to cover.

Run: ``ops/bin/ops.py <command>`` (or ``just ops control validate``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
OPS = ROOT / "ops"
SCHEMA = OPS / "capabilities" / "schema"

#: Directories under `ops/` that are not capabilities. `bin` is the catalogue's
#: own tooling and `capabilities` holds the schemas both are validated against.
NOT_A_CAPABILITY = frozenset({"bin", "capabilities"})

#: Extensions worth reading when looking for a script that writes outside its
#: own boundary. A capability may hold data files of any kind; only executable
#: text is scanned.
EXECUTABLE_SUFFIXES = frozenset({".py", ".sh", ".mjs", ".js", ".ts"})


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One thing that is wrong, named by the rule that noticed it."""

    rule: str
    message: str
    file: str = ""

    def render(self) -> str:
        return (
            f"{self.file}: [{self.rule}] {self.message}"
            if self.file
            else (f"[{self.rule}] {self.message}")
        )


@dataclass(frozen=True, slots=True)
class Capability:
    """A manifest and where it was found."""

    directory: str
    manifest: dict[str, Any]

    @property
    def id(self) -> str:
        found = self.manifest.get("id")
        return found if isinstance(found, str) else self.directory

    @property
    def root(self) -> Path:
        return OPS / self.directory

    @property
    def manifest_path(self) -> Path:
        return self.root / "capability.yaml"

    def paths(self, *keys: str) -> list[str]:
        found: list[str] = []
        for key in keys:
            value = self.manifest.get(key)
            if isinstance(value, list):
                found += [item for item in value if isinstance(item, str)]
        return found

    def commands(self) -> list[dict[str, Any]]:
        value = self.manifest.get("commands")
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    def skills(self) -> list[dict[str, Any]]:
        value = self.manifest.get("skills")
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    def requires(self, key: str) -> list[str]:
        block = self.manifest.get("requires")
        if not isinstance(block, dict):
            return []
        value = block.get(key)
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, str)]


# --------------------------------------------------------------------------
# Reading the catalogue
# --------------------------------------------------------------------------


def _load(path: Path) -> dict[str, Any]:
    found = yaml.safe_load(path.read_text(encoding="utf-8"))
    return found if isinstance(found, dict) else {}


def capabilities() -> list[Capability]:
    """Every capability directory that carries a manifest, sorted by id."""
    found = [
        Capability(entry.name, _load(entry / "capability.yaml"))
        for entry in sorted(OPS.iterdir())
        if entry.is_dir()
        and entry.name not in NOT_A_CAPABILITY
        and (entry / "capability.yaml").is_file()
    ]
    return sorted(found, key=lambda capability: capability.id)


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _files_under(directory: Path) -> list[Path]:
    return [path for path in sorted(directory.rglob("*")) if path.is_file()]


# --------------------------------------------------------------------------
# Glob claims
#
# A claim is a repository-relative glob. Two questions get asked of them: does
# this path match a claim, and do two claims describe overlapping territory.
# The second cannot be answered exactly for arbitrary globs, so it is answered
# conservatively -- by comparing the fixed prefix each pattern starts with,
# which is enough for the `dir/**` claims a manifest actually writes.
# --------------------------------------------------------------------------


def _as_regex(pattern: str) -> re.Pattern[str]:
    source = ""
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*" and pattern[index : index + 2] == "**":
            source += ".*"
            index += 2
            continue
        source += "[^/]*" if char == "*" else re.escape(char)
        index += 1
    return re.compile(f"^{source}$")


def _matches(pattern: str, path: str) -> bool:
    return _as_regex(pattern).search(path) is not None


def _fixed_prefix(pattern: str) -> str:
    wildcard = re.search(r"[*?]", pattern)
    head = pattern if wildcard is None else pattern[: wildcard.start()]
    return head.rstrip("/")


def _overlap(left: str, right: str) -> bool:
    """Whether two claims can describe the same file."""
    if left == right:
        return True
    left_prefix, right_prefix = _fixed_prefix(left), _fixed_prefix(right)
    return (
        left.endswith("/**")
        and (right_prefix == left_prefix or right_prefix.startswith(f"{left_prefix}/"))
    ) or (
        right.endswith("/**")
        and (left_prefix == right_prefix or left_prefix.startswith(f"{right_prefix}/"))
    )


# --------------------------------------------------------------------------
# Invariants
# --------------------------------------------------------------------------


def _check_schemas(found: list[Capability]) -> list[Diagnostic]:
    """Every manifest against the versioned schema, with one external tool.

    `check-jsonschema` is what `just ops spec check` already uses for the
    library's own catalog, so the operations catalogue is held to its schema by
    the same tool in the same way.
    """
    targets = [(SCHEMA / "capability.v1.yaml", [c.manifest_path for c in found])]
    targets += [(SCHEMA / "ops.v1.yaml", [OPS / "ops.yaml"])]
    diagnostics: list[Diagnostic] = []
    for schema, files in targets:
        if not files:
            continue
        result = subprocess.run(
            ["uvx", "check-jsonschema", "--schemafile", str(schema), *map(str, files)],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
        )
        if result.returncode != 0:
            reported = (result.stdout + result.stderr).strip()
            diagnostics.append(Diagnostic("schema", reported, _relative(schema)))
    return diagnostics


def _check_layout(found: list[Capability]) -> list[Diagnostic]:
    """A capability is its directory: same name, and the files that make it one."""
    diagnostics: list[Diagnostic] = []
    for capability in found:
        if capability.id != capability.directory:
            diagnostics.append(
                Diagnostic(
                    "id-directory-mismatch",
                    f"manifest id {capability.id!r} in directory "
                    f"{capability.directory!r}",
                    _relative(capability.manifest_path),
                )
            )
        diagnostics += [
            Diagnostic(
                "missing-file",
                f"{capability.id} has no {required}",
                _relative(capability.root / required),
            )
            for required in ("justfile", "README.md")
            if not (capability.root / required).is_file()
        ]
    return diagnostics


def _check_ownership(found: list[Capability]) -> list[Diagnostic]:
    """No shared territory, no writing into someone else's, no file unowned."""
    diagnostics: list[Diagnostic] = []
    claims = [
        (capability.id, pattern)
        for capability in found
        for pattern in capability.paths("owns", "generates")
    ]

    for index, (owner, pattern) in enumerate(claims):
        diagnostics += [
            Diagnostic(
                "overlapping-claim",
                f"{owner}:{pattern} overlaps {other_owner}:{other_pattern}",
            )
            for other_owner, other_pattern in claims[index + 1 :]
            if owner != other_owner and _overlap(pattern, other_pattern)
        ]

    for capability in found:
        writes = capability.paths("owns", "generates")
        diagnostics += [
            Diagnostic(
                "read-write-overlap",
                f"{capability.id} reads {read} and writes {write}",
                _relative(capability.manifest_path),
            )
            for read in capability.paths("reads")
            for write in writes
            if _overlap(read, write)
        ]

    for path in _files_under(OPS):
        relative = _relative(path)
        owners = {owner for owner, pattern in claims if _matches(pattern, relative)}
        if len(owners) != 1:
            named = f" ({', '.join(sorted(owners))})" if owners else ""
            diagnostics.append(
                Diagnostic(
                    "unowned-path",
                    f"{len(owners)} capabilities claim it{named}",
                    relative,
                )
            )
    return diagnostics


def _check_graph(found: list[Capability]) -> list[Diagnostic]:
    """Dependencies resolve and do not loop."""
    diagnostics: list[Diagnostic] = []
    graph = {capability.id: capability.requires("capabilities") for capability in found}
    for capability_id, dependencies in graph.items():
        diagnostics += [
            Diagnostic(
                "missing-capability",
                f"{capability_id} requires unknown capability {dependency}",
            )
            for dependency in dependencies
            if dependency not in graph
        ]

    visiting: set[str] = set()
    settled: set[str] = set()

    def visit(node: str, trail: tuple[str, ...]) -> None:
        if node in visiting:
            cycle = " -> ".join([*trail, node])
            diagnostics.append(Diagnostic("capability-cycle", cycle))
            return
        if node in settled:
            return
        visiting.add(node)
        for dependency in graph.get(node, ()):
            if dependency in graph:
                visit(dependency, (*trail, node))
        visiting.discard(node)
        settled.add(node)

    for capability_id in graph:
        visit(capability_id, ())
    return diagnostics


def _recipes(capability: Capability) -> tuple[dict[str, Any], Diagnostic | None]:
    justfile = capability.root / "justfile"
    result = subprocess.run(
        ["just", "--justfile", str(justfile), "--dump", "--dump-format", "json"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    if result.returncode != 0:
        reported = result.stderr.strip()
        return {}, Diagnostic("invalid-justfile", reported, _relative(justfile))
    found = json.loads(result.stdout).get("recipes")
    return (found if isinstance(found, dict) else {}), None


def _check_commands(found: list[Capability]) -> list[Diagnostic]:
    """Every declared command is a recipe, with the parameters it declares."""
    diagnostics: list[Diagnostic] = []
    for capability in found:
        recipes, failure = _recipes(capability)
        if failure is not None:
            diagnostics.append(failure)
            continue
        justfile = _relative(capability.root / "justfile")
        for command in capability.commands():
            name = command.get("name")
            if not isinstance(name, str):
                continue
            recipe = recipes.get(name)
            if recipe is None:
                diagnostics.append(
                    Diagnostic(
                        "missing-recipe",
                        f"{capability.id}:{name} has no recipe",
                        justfile,
                    )
                )
                continue
            declared = command.get("args") or []
            actual = [
                parameter.get("name")
                for parameter in recipe.get("parameters", [])
                if isinstance(parameter, dict)
            ]
            if len(declared) != len(actual):
                diagnostics.append(
                    Diagnostic(
                        "argument-mismatch",
                        f"{capability.id}:{name} declares {len(declared)} args, "
                        f"the recipe takes {len(actual)}",
                        justfile,
                    )
                )
        documented = {command.get("name") for command in capability.commands()}
        diagnostics += [
            Diagnostic(
                "undeclared-recipe",
                f"{capability.id}:{name} is a recipe no manifest mentions",
                justfile,
            )
            for name in recipes
            if not name.startswith("_") and name not in documented
        ]
    return diagnostics


def _check_skills(found: list[Capability]) -> list[Diagnostic]:
    """A declared skill has a SKILL.md, and a SKILL.md is declared."""
    diagnostics: list[Diagnostic] = []
    frontmatter = re.compile(r"\A---\n(.*?)\n---", re.DOTALL)
    for capability in found:
        root = capability.root / "skills"
        declared = {str(skill.get("name")) for skill in capability.skills()}
        present: set[str] = set()
        if root.is_dir():
            present = {entry.name for entry in root.iterdir() if entry.is_dir()}
        diagnostics += [
            Diagnostic(
                "undeclared-skill",
                f"{capability.id}:{name} is not in the manifest",
            )
            for name in sorted(present - declared)
        ]
        diagnostics += [
            Diagnostic(
                "missing-skill",
                f"{capability.id}:{name} has no skills/{name}/",
            )
            for name in sorted(declared - present)
        ]
        for name in sorted(declared & present):
            skill = root / str(name) / "SKILL.md"
            if not skill.is_file():
                diagnostics.append(
                    Diagnostic(
                        "missing-skill",
                        f"{capability.id}:{name} has no SKILL.md",
                    )
                )
                continue
            head = frontmatter.match(skill.read_text(encoding="utf-8"))
            block = yaml.safe_load(head.group(1)) if head else None
            if not isinstance(block, dict) or not (
                block.get("name") and block.get("description")
            ):
                diagnostics.append(
                    Diagnostic(
                        "invalid-skill-frontmatter",
                        f"{capability.id}:{name} needs name and description",
                        _relative(skill),
                    )
                )
    return diagnostics


def _check_providers(found: list[Capability]) -> list[Diagnostic]:
    """`ops.yaml` selects a real provider for each interface, and none is orphaned."""
    diagnostics: list[Diagnostic] = []
    active = _load(OPS / "ops.yaml").get("active")
    if not isinstance(active, dict):
        return [
            Diagnostic(
                "missing-provider", "ops.yaml declares no active map", "ops/ops.yaml"
            )
        ]
    by_id = {capability.id: capability for capability in found}
    for interface, provider in active.items():
        capability = by_id.get(str(provider))
        if capability is None:
            diagnostics.append(
                Diagnostic(
                    "missing-provider",
                    f"{interface} selects unknown capability {provider}",
                    "ops/ops.yaml",
                )
            )
        elif capability.manifest.get("provides") != interface:
            provides = capability.manifest.get("provides")
            diagnostics.append(
                Diagnostic(
                    "provider-interface",
                    f"{provider} provides {provides}, not {interface}",
                    "ops/ops.yaml",
                )
            )
    selected = {str(value) for value in active.values()}
    diagnostics += [
        Diagnostic(
            "unselected-capability",
            f"{capability_id} is in the tree but no interface selects it",
            "ops/ops.yaml",
        )
        for capability_id in sorted(set(by_id) - selected)
    ]
    return diagnostics


def _check_boundaries(found: list[Capability]) -> list[Diagnostic]:
    """No capability reaches into another's `bin/`.

    Capabilities compose through commands, which are a declared surface, rather
    than through each other's scripts, which are not. A shared helper that two
    capabilities need belongs to one of them, invoked as a command -- or in
    `ops/bin/`, which belongs to the catalogue itself.
    """
    diagnostics: list[Diagnostic] = []
    for capability in found:
        for path in _files_under(capability.root):
            if path.suffix not in EXECUTABLE_SUFFIXES:
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
            for peer in found:
                if peer.id == capability.id:
                    continue
                if f"ops/{peer.directory}/bin" in source:
                    diagnostics.append(
                        Diagnostic(
                            "peer-bin-reach",
                            f"{capability.id} reaches into {peer.id}/bin",
                            _relative(path),
                        )
                    )
    return diagnostics


def validate() -> list[Diagnostic]:
    found = capabilities()
    if not found:
        return [Diagnostic("empty-catalogue", "no capability.yaml found under ops/")]
    return [
        *_check_schemas(found),
        *_check_layout(found),
        *_check_ownership(found),
        *_check_graph(found),
        *_check_commands(found),
        *_check_skills(found),
        *_check_providers(found),
        *_check_boundaries(found),
    ]


# --------------------------------------------------------------------------
# Reporting and lanes
# --------------------------------------------------------------------------


def render_list() -> str:
    rows = [("capability", "provides", "status", "commands")]
    rows += [
        (
            capability.id,
            str(capability.manifest.get("provides", "")),
            str(capability.manifest.get("status", "")),
            " ".join(str(command.get("name")) for command in capability.commands()),
        )
        for capability in capabilities()
    ]
    widths = [max(len(row[column]) for row in rows) for column in range(3)]
    return "\n".join(
        f"{row[0]:<{widths[0]}}  {row[1]:<{widths[1]}}  {row[2]:<{widths[2]}}  {row[3]}"
        for row in rows
    )


@dataclass(frozen=True, slots=True)
class Step:
    capability: str
    command: str
    lane: str = field(default="fast")


def lane(name: str) -> list[Step]:
    """The commands that declare themselves part of a repository-wide lane."""
    return [
        Step(capability.id, str(command.get("name")), str(command.get("lane", "fast")))
        for capability in capabilities()
        for command in capability.commands()
        if command.get("aggregate") == name
    ]


def _environment() -> dict[str, str]:
    """The environment to hand a delegated command.

    `VIRTUAL_ENV` is dropped. This script runs as a PEP 723 command, so `uv`
    puts it in an environment of its own; passing that down makes every nested
    `uv run` warn that the active environment is not the project's and then
    ignore it anyway. The warning is noise about a decision already made
    correctly.
    """
    return {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}


def run_lane(name: str) -> int:
    steps = lane(name)
    if not steps:
        print(f"no command declares `aggregate: {name}`", file=sys.stderr)
        return 1
    for step in steps:
        print(f"==> {step.capability} {step.command}", flush=True)
        result = subprocess.run(
            [
                "just",
                "--justfile",
                str(OPS / step.capability / "justfile"),
                "--working-directory",
                str(ROOT),
                step.command,
            ],
            cwd=ROOT,
            env=_environment(),
            check=False,
        )
        if result.returncode != 0:
            print(f"{step.capability} {step.command}: failed", file=sys.stderr)
            return result.returncode
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate", help="hold every manifest to what it claims")
    sub.add_parser("list", help="what the catalogue contains")
    aggregate = sub.add_parser("aggregate", help="run a repository-wide lane")
    aggregate.add_argument("lane", help="the value of `aggregate:` to collect")
    arguments = parser.parse_args(argv)

    if arguments.command == "list":
        print(render_list())
        return 0
    if arguments.command == "aggregate":
        return run_lane(arguments.lane)

    diagnostics = validate()
    if diagnostics:
        report = "\n".join(diagnostic.render() for diagnostic in diagnostics)
        print(report, file=sys.stderr)
        print(f"\n{len(diagnostics)} problem(s)", file=sys.stderr)
        return 1
    print(f"repository operations: {len(capabilities())} capabilities, OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
