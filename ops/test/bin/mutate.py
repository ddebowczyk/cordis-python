#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Break the implementation on purpose and require the suite to notice.

A green suite says the tests pass. It does not say the tests would fail if the
code were wrong, and those are different claims -- a property that never
constrains anything is green forever. This harness makes the second claim
checkable: for each mutation declared in `mutations.yaml` it edits the source
into a defect, runs the tests that are supposed to catch it, and expects them
to fail.

Three outcomes:

* **CAUGHT** -- the tests failed, which is the result being asked for.
* **SURVIVED** -- the tests passed with a real bug in place. That is a hole in
  the suite and the reason this command exits non-zero.
* **CAUGHT (hung)** -- the mutation made the tests hang rather than fail. It
  counts as caught (the defect is visible) but the timeout is reported, since a
  hang is a worse failure mode than an assertion.

The originals are restored in a `finally`, and a mutation whose `before` text
no longer occurs exactly once aborts the run rather than testing nothing.

Run: ``ops/test/bin/mutate.py run <campaign|all>`` or ``... list``.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent.parent
DECLARED = Path(__file__).resolve().parent.parent / "mutations.yaml"

#: Long enough for the mutation profile on the slowest module, short enough
#: that a hang is reported in the same run rather than the next morning.
TIMEOUT = 300


@dataclass(frozen=True, slots=True)
class Edit:
    """One textual substitution, and the file it applies to."""

    file: Path
    before: str
    after: str


@dataclass(frozen=True, slots=True)
class Mutation:
    """A defect to introduce, and the tests that must object to it."""

    campaign: str
    title: str
    test: str
    edits: tuple[Edit, ...]


@dataclass(frozen=True, slots=True)
class Outcome:
    """What the suite did about one mutation."""

    mutation: Mutation
    caught: bool
    hung: bool

    def render(self) -> str:
        caught = "CAUGHT" if self.caught else "SURVIVED"
        verdict = "CAUGHT (hung)" if self.hung else caught
        return f"{verdict:<14}{self.mutation.campaign}: {self.mutation.title}"


def declared() -> list[Mutation]:
    record = yaml.safe_load(DECLARED.read_text(encoding="utf-8"))
    found: list[Mutation] = []
    for campaign in record["campaigns"]:
        found += [
            Mutation(
                campaign=campaign["name"],
                title=mutation["title"],
                test=mutation["test"],
                edits=tuple(
                    Edit(ROOT / edit["file"], edit["before"], edit["after"])
                    for edit in mutation["edits"]
                ),
            )
            for mutation in campaign["mutations"]
        ]
    return found


def _apply(mutation: Mutation) -> dict[Path, str]:
    """Write every edit, returning the originals so they can be put back."""
    originals: dict[Path, str] = {}
    for edit in mutation.edits:
        text = edit.file.read_text(encoding="utf-8")
        occurrences = text.count(edit.before)
        if occurrences != 1:
            for path, original in originals.items():
                path.write_text(original, encoding="utf-8")
            raise SystemExit(
                f"mutate: {edit.file.relative_to(ROOT)} contains the text for "
                f"{mutation.title!r} {occurrences} times, not once -- the code moved, "
                f"so this mutation would have tested nothing. Update mutations.yaml."
            )
        originals.setdefault(edit.file, text)
        edit.file.write_text(text.replace(edit.before, edit.after), encoding="utf-8")
    return originals


def _run_tests(selector: str) -> tuple[int | None, bool]:
    """Run one selector under the mutation profile. Returns (code, hung)."""
    environment = {
        key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"
    }
    environment["HYPOTHESIS_PROFILE"] = "mutation"
    process = subprocess.Popen(
        ["uv", "run", "pytest", selector, "-x", "-q", "--no-header"],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        # Its own process group, so a hung run can be killed whole. Hypothesis
        # and pytest both spawn children; signalling the leader alone leaves
        # them behind.
        start_new_session=True,
    )
    try:
        process.communicate(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        process.communicate()
        return None, True
    return process.returncode, False


def run(campaign: str) -> int:
    wanted = [
        mutation for mutation in declared() if campaign in {"all", mutation.campaign}
    ]
    if not wanted:
        names = sorted({mutation.campaign for mutation in declared()})
        print(
            f"mutate: no campaign named {campaign!r}; "
            f"try one of {', '.join(names)}, or all",
            file=sys.stderr,
        )
        return 1

    outcomes: list[Outcome] = []
    for index, mutation in enumerate(wanted, start=1):
        heading = f"[{index}/{len(wanted)}] {mutation.campaign}: {mutation.title}"
        print(heading, flush=True)
        originals = _apply(mutation)
        try:
            code, hung = _run_tests(mutation.test)
        finally:
            for path, text in originals.items():
                path.write_text(text, encoding="utf-8")
        outcomes.append(Outcome(mutation, caught=hung or code != 0, hung=hung))
        print(f"        {outcomes[-1].render().strip()}", flush=True)

    print()
    print("\n".join(outcome.render() for outcome in outcomes))
    survivors = [outcome for outcome in outcomes if not outcome.caught]
    if survivors:
        print(
            f"\nmutate: {len(survivors)} of {len(outcomes)} mutations survived -- "
            f"the suite does not hold what it claims to",
            file=sys.stderr,
        )
        return 1
    print(f"\nmutate: {len(outcomes)}/{len(outcomes)} caught")
    return 0


def render_list() -> int:
    record = yaml.safe_load(DECLARED.read_text(encoding="utf-8"))
    for campaign in record["campaigns"]:
        print(f"{campaign['name']}  ({len(campaign['mutations'])} mutations)")
        print(f"  {' '.join(campaign['summary'].split())}")
        for mutation in campaign["mutations"]:
            print(f"    - {mutation['title']}")
            print(f"      {mutation['test']}")
        print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mutation verification of the test suite."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    running = sub.add_parser("run", help="introduce each defect and require a failure")
    running.add_argument("campaign", help="a campaign name, or `all`")
    sub.add_parser("list", help="the declared campaigns and what they cover")
    arguments = parser.parse_args(argv)
    return render_list() if arguments.command == "list" else run(arguments.campaign)


if __name__ == "__main__":
    sys.exit(main())
