#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Check every number the documentation claims against the thing it counts.

The README opens with a status line and states a current state: so many
capabilities, so many normative rules, so many property cards. Those numbers
are the first thing a reader trusts and the first thing to go stale, because
nothing about adding a capability record forces anyone to edit prose.

So the prose is checked rather than maintained. Each claim below is read out of
the documentation with a pattern, recomputed from `spec/capabilities/`, and
compared. A mismatch names both numbers and the file to fix.

Run: ``ops/docs/bin/counters.py`` (or ``just ops docs counters``).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent.parent
CAPABILITIES = ROOT / "spec" / "capabilities"
README = ROOT / "README.md"


def catalog() -> dict[str, int]:
    """The three numbers the documentation is allowed to claim."""
    records = [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted(CAPABILITIES.glob("*.yaml"))
    ]
    return {
        "capabilities": len(records),
        "rules": sum(len(record.get("semantics") or ()) for record in records),
        "cards": sum(len(record.get("properties") or ()) for record in records),
    }


#: Each claim: the file, a pattern with one capturing group, and which count it
#: must equal. The patterns are deliberately narrow -- a claim that moves should
#: fail loudly here rather than quietly stop being checked.
CLAIMS: tuple[tuple[Path, str, str], ...] = (
    (README, r"Status: all (\d+) capabilities implemented", "capabilities"),
    (README, r"Current state: \*\*(\d+) capabilities,", "capabilities"),
    (README, r"Current state: \*\*\d+ capabilities, (\d+) normative rules,", "rules"),
    (
        README,
        r"Current state: \*\*\d+ capabilities, \d+ normative rules, "
        r"(\d+) property cards",
        "cards",
    ),
)


def main() -> int:
    counts = catalog()
    problems: list[str] = []
    for path, pattern, key in CLAIMS:
        found = re.search(pattern, path.read_text(encoding="utf-8"))
        if found is None:
            problems.append(
                f"{path.relative_to(ROOT)}: no claim matching /{pattern}/ -- the "
                f"sentence "
                f"moved, so this check silently stopped covering it"
            )
            continue
        claimed = int(found.group(1))
        if claimed != counts[key]:
            problems.append(
                f"{path.relative_to(ROOT)}: claims {claimed} {key}, the catalog has "
                f"{counts[key]}"
            )

    if problems:
        print("\n".join(f"docs: {problem}" for problem in problems), file=sys.stderr)
        return 1
    summary = ", ".join(f"{value} {key}" for key, value in counts.items())
    print(f"documentation counters agree with the catalog: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
