#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""What the built artefacts are, and whether they work.

Two different questions, and the second is the one that pays. `verify` reads
the wheel and the sdist: is the wheel pure Python, does the sdist carry the
capability catalog, does the metadata render on an index. `smoke` installs the
wheel into a fresh environment for every supported interpreter and imports it.

The distinction is not academic. Version 0.1.0 passed every gate that ran
against the source tree and still could not be imported on the oldest
interpreter it claimed to support: Python 3.11 rejects a `mappingproxy`
dataclass default that 3.12 accepts. A second defect -- a CPython-only
attribute read on every plugin mount -- broke PyPy the same way. Both were
found by installing the artefact rather than testing the checkout, which is the
whole reason this command exists.

Run: ``ops/packaging/bin/artefacts.py <command>``.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent.parent
DIST = ROOT / "dist"

#: Every interpreter the package claims to support, which is what the
#: classifiers in `pyproject.toml` say and what CI's matrix runs.
INTERPRETERS = ("3.11", "3.12", "3.13", "pypy3.11")

#: A file the sdist must carry. The catalog is the contract the tests are
#: generated from, so a downstream fork must get the spec and not just the code.
SDIST_WITNESS = "spec/capabilities/00-context-tree.yaml"


def _artefacts() -> tuple[Path, Path]:
    wheels = sorted(DIST.glob("*.whl")) if DIST.is_dir() else []
    sdists = sorted(DIST.glob("*.tar.gz")) if DIST.is_dir() else []
    if len(wheels) != 1 or len(sdists) != 1:
        print(
            f"packaging: expected one wheel and one sdist in {DIST.relative_to(ROOT)}, "
            f"found {len(wheels)} and {len(sdists)}; run `just ops packaging build`",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return wheels[0], sdists[0]


def verify() -> int:
    wheel, sdist = _artefacts()
    problems: list[str] = []

    if not wheel.name.endswith("-py3-none-any.whl"):
        problems.append(
            f"{wheel.name} is not py3-none-any; the package must stay pure Python"
        )

    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
    if not any(name.endswith("cordis/py.typed") for name in names):
        problems.append(f"{wheel.name} does not ship py.typed")

    with tarfile.open(sdist) as archive:
        carried = {
            Path(*Path(name).parts[1:]).as_posix() for name in archive.getnames()
        }
    if SDIST_WITNESS not in carried:
        problems.append(f"{sdist.name} does not carry {SDIST_WITNESS}")

    rendered = subprocess.run(
        ["uvx", "twine", "check", str(wheel), str(sdist)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    if rendered.returncode != 0:
        reported = (rendered.stdout + rendered.stderr).strip()
        problems.append(f"twine check failed:\n{reported}")

    if problems:
        report = "\n".join(f"packaging: {problem}" for problem in problems)
        print(report, file=sys.stderr)
        return 1
    print(f"{wheel.name}: pure Python, typed, catalog in the sdist, metadata renders")
    return 0


def smoke() -> int:
    """Install the wheel into each supported interpreter and use it.

    The check is deliberately more than `import cordis`: it mounts a plugin,
    because normalising a plugin target is where the PyPy defect lived and an
    import alone would have sailed straight past it.
    """
    wheel, _ = _artefacts()
    script = (
        "import cordis;"
        "from cordis import normalise;"
        "print(cordis.__version__, len(cordis.__all__), "
        "normalise(lambda ctx: None).arity)"
    )
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as scratch:
        for interpreter in INTERPRETERS:
            environment = Path(scratch) / interpreter
            steps = [
                ["uv", "venv", "--python", interpreter, str(environment)],
                ["uv", "pip", "install", "--python", str(environment), str(wheel)],
            ]
            broken = False
            for step in steps:
                result = subprocess.run(
                    step, capture_output=True, text=True, check=False
                )
                if result.returncode != 0:
                    detail = result.stderr.strip().splitlines()[-1]
                    failures.append(f"{interpreter}: {detail}")
                    broken = True
                    break
            if broken:
                continue
            binary = environment / "bin" / "python"
            result = subprocess.run(
                [str(binary), "-c", script], capture_output=True, text=True, check=False
            )
            if result.returncode != 0:
                tail = result.stderr.strip().splitlines()
                failures.append(f"{interpreter}: {tail[-1] if tail else 'failed'}")
            else:
                print(f"{interpreter:<10} {result.stdout.strip()}")

    if failures:
        report = "\n".join(f"packaging: {failure}" for failure in failures)
        print(report, file=sys.stderr)
        return 1
    print(f"{wheel.name}: imports and mounts on all {len(INTERPRETERS)} interpreters")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wheel and sdist integrity.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("verify", help="what the artefacts are")
    sub.add_parser("smoke", help="whether they work, on every supported interpreter")
    arguments = parser.parse_args(argv)
    return verify() if arguments.command == "verify" else smoke()


if __name__ == "__main__":
    sys.exit(main())
