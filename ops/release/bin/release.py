#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Cut a GitHub Release, or say exactly why one cannot be cut yet.

A release is a promise about a commit: these gates passed, this changelog
describes it, these artefacts came from it. Every step here exists to keep one
part of that promise checkable before it is made.

`status` answers each precondition yes or no and never changes anything.
`notes` reads the release body out of `CHANGELOG.md`, so the notes are the
changelog rather than a second account of the same work. `publish` is the only
command that reaches outside the checkout, and it prints its plan and stops
unless `--confirm` is given -- a tag and a release are not things to create by
typo.

The version itself is read from the package, not re-parsed here: `release` and
`version` compose through the public surface rather than through each other's
files.

Run: ``ops/release/bin/release.py <command>``.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"
DIST = ROOT / "dist"


@dataclass(frozen=True, slots=True)
class Check:
    """One precondition, and what it found."""

    name: str
    ok: bool
    detail: str

    def render(self) -> str:
        mark = "ok " if self.ok else "NO "
        return f"{mark}  {self.name:<26}{self.detail}"


def _run(*command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command), capture_output=True, text=True, cwd=ROOT, check=False
    )


def version() -> str:
    """The version the package itself reports, which is the one hatch will publish."""
    reading = "import cordis; print(cordis.__version__)"
    found = _run("uv", "run", "python", "-c", reading)
    if found.returncode != 0:
        raise SystemExit(f"release: cannot import cordis: {found.stderr.strip()}")
    return found.stdout.strip()


def notes(requested: str) -> str:
    """The changelog section for a version, without its heading."""
    text = CHANGELOG.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"^##\s*\[{re.escape(requested)}\][^\n]*\n(.*?)(?=^##\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    found = pattern.search(text)
    if found is None:
        raise SystemExit(f"release: CHANGELOG.md has no `## [{requested}]` section")
    body = found.group(1).strip()
    if not body:
        raise SystemExit(f"release: the `## [{requested}]` section is empty")
    return body


def _artefacts(requested: str) -> list[Path]:
    if not DIST.is_dir():
        return []
    return sorted(path for path in DIST.iterdir() if requested in path.name)


def _release_view(tag: str) -> dict[str, object] | None:
    found = _run("gh", "release", "view", tag, "--json", "isDraft,assets,tagName")
    if found.returncode != 0:
        return None
    parsed = json.loads(found.stdout)
    return parsed if isinstance(parsed, dict) else None


def status() -> int:
    requested = version()
    tag = f"v{requested}"
    checks: list[Check] = [Check("version", ok=True, detail=requested)]

    try:
        notes(requested)
        checks.append(
            Check("changelog section", ok=True, detail=f"[{requested}] has a body")
        )
    except SystemExit as failure:
        checks.append(Check("changelog section", ok=False, detail=str(failure)))

    dirty = _run("git", "status", "--porcelain").stdout.strip()
    changes = f"{len(dirty.splitlines())} changes" if dirty else "clean"
    checks.append(Check("working tree", ok=not dirty, detail=changes))

    kind = _run("git", "cat-file", "-t", f"refs/tags/{tag}")
    if kind.returncode != 0:
        checks.append(Check("tag", ok=False, detail=f"{tag} does not exist yet"))
    else:
        annotated = kind.stdout.strip() == "tag"
        on_head = (
            _run("git", "rev-parse", f"{tag}^{{commit}}").stdout.strip()
            == _run("git", "rev-parse", "HEAD").stdout.strip()
        )
        checks.append(
            Check(
                "tag",
                ok=annotated and on_head,
                detail=f"{tag} {'annotated' if annotated else 'LIGHTWEIGHT'}, "
                f"{'on HEAD' if on_head else 'NOT on HEAD'}",
            )
        )

    built = _artefacts(requested)
    checks.append(
        Check(
            "artefacts",
            ok=len(built) == 2,
            detail=", ".join(path.name for path in built)
            or "none built for this version",
        )
    )

    authenticated = _run("gh", "auth", "status").returncode == 0
    checks.append(
        Check(
            "github",
            ok=authenticated,
            detail="authenticated" if authenticated else "gh is not logged in",
        )
    )

    existing = _release_view(tag)
    checks.append(
        Check(
            "release",
            ok=existing is None,
            detail="not published yet" if existing is None else f"{tag} already exists",
        )
    )

    print("\n".join(check.render() for check in checks))
    blocked = [check for check in checks if not check.ok]
    if blocked:
        unmet = f"\nrelease: {len(blocked)} precondition(s) unmet"
        print(unmet, file=sys.stderr)
        return 1
    print(f"\nrelease: {tag} is ready to publish")
    return 0


def publish(*, confirm: bool) -> int:
    requested = version()
    tag = f"v{requested}"
    body = notes(requested)
    built = _artefacts(requested)

    plan = [
        f"git tag -a {tag} -m 'cordis-python {requested}'  (if absent)",
        f"git push origin {tag}",
        f"gh release create {tag} --title 'cordis-python {requested}' "
        f"--notes-file - " + " ".join(path.name for path in built),
    ]
    if not confirm:
        print("release: this would run\n")
        print("\n".join(f"  {line}" for line in plan))
        lines = len(body.splitlines())
        print(f"\nnotes ({lines} lines) come from CHANGELOG.md [{requested}]")
        print("\nre-run with --confirm to do it")
        return 0

    if status() != 0:
        return 1

    if _run("git", "cat-file", "-t", f"refs/tags/{tag}").returncode != 0:
        created = _run("git", "tag", "-a", tag, "-m", f"cordis-python {requested}")
        if created.returncode != 0:
            print(f"release: {created.stderr.strip()}", file=sys.stderr)
            return 1
    pushed = _run("git", "push", "origin", tag)
    if pushed.returncode != 0:
        print(f"release: {pushed.stderr.strip()}", file=sys.stderr)
        return 1

    result = subprocess.run(
        [
            "gh",
            "release",
            "create",
            tag,
            "--title",
            f"cordis-python {requested}",
            "--notes-file",
            "-",
            *[str(path) for path in built],
        ],
        input=body,
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    if result.returncode != 0:
        print(f"release: {(result.stdout + result.stderr).strip()}", file=sys.stderr)
        return 1
    print(result.stdout.strip())
    return 0


def verify() -> int:
    requested = version()
    tag = f"v{requested}"
    found = _release_view(tag)
    if found is None:
        print(f"release: {tag} is not published", file=sys.stderr)
        return 1
    assets = found.get("assets")
    names: list[str] = []
    if isinstance(assets, list):
        names = [str(asset.get("name")) for asset in assets]
    problems: list[str] = []
    if found.get("isDraft"):
        problems.append(f"{tag} is still a draft")
    if not any(name.endswith(".whl") for name in names):
        problems.append(f"{tag} carries no wheel")
    if not any(name.endswith(".tar.gz") for name in names):
        problems.append(f"{tag} carries no sdist")
    if problems:
        print("\n".join(f"release: {problem}" for problem in problems), file=sys.stderr)
        return 1
    print(f"{tag}: published, {len(names)} asset(s): {', '.join(names)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GitHub Release management.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="every precondition, answered")
    body = sub.add_parser("notes", help="the changelog section for a version")
    body.add_argument("version")
    publishing = sub.add_parser("publish", help="tag and create the release")
    publishing.add_argument("--confirm", action="store_true", help="actually do it")
    sub.add_parser("verify", help="the published release is real and complete")
    arguments = parser.parse_args(argv)

    if arguments.command == "status":
        return status()
    if arguments.command == "notes":
        print(notes(arguments.version))
        return 0
    if arguments.command == "publish":
        return publish(confirm=arguments.confirm)
    return verify()


if __name__ == "__main__":
    sys.exit(main())
