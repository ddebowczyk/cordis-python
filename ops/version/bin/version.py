#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""The one authoritative version, and the rules that keep it trustworthy.

`cordis.__version__` is where the version is written; hatch reads it from there
at build time, so there is nothing to keep in sync and nothing that can
disagree with what was published. This script is what makes that single
statement safe to change:

* a version moves **forward only**. A repeat or a step backwards would publish
  a different artefact under a name that already means something else, and no
  index lets you take that back.
* a release **tag must agree** with it, be annotated, and point at the commit
  that was tested.
* the **changelog must have a section** for it before it can be released,
  because a version with no entry is a version nobody can read.

Run: ``ops/version/bin/version.py <command>`` (or ``just ops version <command>``).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent.parent
SOURCE = ROOT / "src" / "cordis" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"

#: The assignment this script reads and writes. Anchored to the start of a line
#: so a mention inside a docstring cannot be mistaken for the declaration.
ASSIGNMENT = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)

SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

PARTS = ("major", "minor", "patch")


class VersionError(Exception):
    """Something is wrong with the version, stated in a way that says what to do."""


def _git(*arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise VersionError(f"git {' '.join(arguments)}: {result.stderr.strip()}")
    return result.stdout.strip()


def _parse(version: str) -> tuple[int, int, int]:
    found = SEMVER.match(version)
    if found is None:
        raise VersionError(f"{version!r} is not a three-part version like 1.2.3")
    major, minor, patch = found.groups()
    return int(major), int(minor), int(patch)


def current() -> str:
    """The version as written, insisting there is exactly one place it is written."""
    found = ASSIGNMENT.findall(SOURCE.read_text(encoding="utf-8"))
    if len(found) != 1:
        raise VersionError(
            f"{SOURCE.relative_to(ROOT)} declares __version__ {len(found)} times; "
            f"exactly one is the point"
        )
    version: str = found[0]
    _parse(version)
    return version


def release_tags() -> list[tuple[int, int, int]]:
    """Every `vX.Y.Z` tag this checkout knows about, sorted."""
    lines = _git("tag", "--list", "v*").splitlines()
    found = [
        SEMVER.match(line.strip().removeprefix("v")) for line in lines if line.strip()
    ]
    return sorted(
        (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        for match in found
        if match is not None
    )


def changelog_sections() -> set[str]:
    """Every version the changelog has a heading for."""
    heading = re.compile(r"^##\s*\[(\d+\.\d+\.\d+)\]", re.MULTILINE)
    return set(heading.findall(CHANGELOG.read_text(encoding="utf-8")))


def bump(version: str, part: str) -> str:
    if part not in PARTS:
        raise VersionError(f"{part!r} is not one of {', '.join(PARTS)}")
    major, minor, patch = _parse(version)
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def show() -> int:
    version = current()
    tags = release_tags()
    latest = ".".join(str(part) for part in tags[-1]) if tags else "(none)"
    print(f"version        {version}")
    print(f"declared in    {SOURCE.relative_to(ROOT)}")
    print(f"latest tag     {'v' + latest if tags else latest}")
    print(f"tag for this   {'present' if _parse(version) in tags else 'absent'}")
    recorded = "present" if version in changelog_sections() else "MISSING"
    print(f"changelog      {recorded}")
    return 0


def check() -> int:
    """Everything that must be true of the version at any moment, released or not."""
    version = current()
    problems: list[str] = []

    if version not in changelog_sections():
        problems.append(f"CHANGELOG.md has no `## [{version}]` section")

    tags = release_tags()
    ahead = [tag for tag in tags if tag > _parse(version)]
    if ahead:
        published = ", ".join(
            "v" + ".".join(str(part) for part in tag) for tag in ahead
        )
        problems.append(f"tags ahead of the declared version: {published}")

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    if 'path = "src/cordis/__init__.py"' not in pyproject:
        problems.append("pyproject.toml no longer reads the version from the module")
    if 'dynamic = ["version"]' not in pyproject:
        problems.append("pyproject.toml declares a static version; there would be two")

    if problems:
        print("\n".join(f"version: {problem}" for problem in problems), file=sys.stderr)
        return 1
    print(f"version {version}: OK")
    return 0


def preview(part: str) -> int:
    print(bump(current(), part))
    return 0


def write(requested: str) -> int:
    version = current()
    if _parse(requested) <= _parse(version):
        raise VersionError(
            f"{requested} does not move forward from {version}; "
            f"a published version cannot be reissued"
        )
    tags = release_tags()
    if tags and _parse(requested) <= tags[-1]:
        latest = ".".join(str(part) for part in tags[-1])
        raise VersionError(
            f"{requested} is not ahead of the latest release tag v{latest}"
        )

    text = SOURCE.read_text(encoding="utf-8")
    SOURCE.write_text(
        ASSIGNMENT.sub(f'__version__ = "{requested}"', text, count=1), encoding="utf-8"
    )
    print(f"{version} -> {requested} in {SOURCE.relative_to(ROOT)}")
    if requested not in changelog_sections():
        print(f"next: add a `## [{requested}]` section to CHANGELOG.md")
    return 0


def verify_release() -> int:
    """What must hold at the moment a tag is cut."""
    version = current()
    tag = f"v{version}"
    problems: list[str] = []

    if _git("status", "--porcelain"):
        problems.append("the working tree is dirty")

    kind = subprocess.run(
        ["git", "-C", str(ROOT), "cat-file", "-t", f"refs/tags/{tag}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if kind.returncode != 0:
        problems.append(f"{tag} does not exist")
    else:
        if kind.stdout.strip() != "tag":
            problems.append(f"{tag} is lightweight; a release tag must be annotated")
        if _git("rev-parse", f"{tag}^{{commit}}") != _git("rev-parse", "HEAD"):
            problems.append(f"{tag} does not point at HEAD")

    if problems:
        print("\n".join(f"release: {problem}" for problem in problems), file=sys.stderr)
        return 1
    print(f"{tag}: annotated, on HEAD, clean tree")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The authoritative product version.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show", help="the version and what the tags say about it")
    sub.add_parser("check", help="everything that must be true right now")
    following = sub.add_parser("next", help="preview a bump without writing it")
    following.add_argument("part", choices=PARTS)
    setting = sub.add_parser("set", help="move the version forward")
    setting.add_argument("version")
    sub.add_parser(
        "verify-release", help="HEAD carries the annotated tag for this version"
    )
    arguments = parser.parse_args(argv)

    try:
        if arguments.command == "show":
            return show()
        if arguments.command == "check":
            return check()
        if arguments.command == "next":
            return preview(arguments.part)
        if arguments.command == "set":
            return write(arguments.version)
        return verify_release()
    except VersionError as failure:
        print(f"version: {failure}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
