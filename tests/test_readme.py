"""The README's quickstart, executed.

A quickstart that does not run is worse than none: it costs a reader the time
to find out. The acceptance criterion for the public surface is that a plugin
author can write a working plugin from the README alone, so the README's Python
blocks are concatenated in order into one file and run in a clean interpreter,
exactly as a reader would if they pasted them one after another.

The blocks assert their own claims -- states before and after a dependency
arrives, what a reconcile reports -- so a failure here is either a broken
example or a changed contract the README has not caught up with.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"

#: ```lang ... ``` at the start of a line, with its language and its body.
FENCE = re.compile(r"^```(\w*)\n(.*?)^```", re.MULTILINE | re.DOTALL)


def _blocks(language: str) -> list[str]:
    text = README.read_text(encoding="utf-8")
    return [body for lang, body in FENCE.findall(text) if lang == language]


PYTHON = _blocks("python")


def test_the_readme_has_a_quickstart_to_run() -> None:
    """Guard against a regex that silently matches nothing."""
    assert len(PYTHON) >= 3, f"only {len(PYTHON)} python blocks found"


@pytest.mark.parametrize("block", PYTHON, ids=range(len(PYTHON)))
def test_a_python_block_is_a_whole_snippet(block: str) -> None:
    """No block ends mid-thought: a reader can paste any one of them."""
    compile(block, "<readme>", "exec")


def test_the_quickstart_runs(tmp_path: Path) -> None:
    """Every Python block, in order, in one file, in a clean interpreter."""
    script = tmp_path / "quickstart.py"
    script.write_text("\n".join(PYTHON), encoding="utf-8")
    found = subprocess.run(
        [sys.executable, script.name],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": ""},
    )
    assert found.returncode == 0, found.stderr
    assert "hello, world" in found.stdout, found.stdout
    assert "consumer: down" in found.stdout, found.stdout


def test_the_yaml_block_is_the_python_one() -> None:
    """The entry list shown as YAML and the one that runs must agree.

    They are written twice because a README that only shows the mapping form
    hides the thing the loader exists for. Written twice, they drift; so the
    ids and configs are compared, and only the module part of a name differs.
    """
    yaml = pytest.importorskip("yaml")
    rows = yaml.safe_load(_blocks("yaml")[0])
    shown = [(row["id"], row.get("config")) for row in rows]
    run = re.search(r"^ROWS = \[(.*?)^\]", "\n".join(PYTHON), re.MULTILINE | re.DOTALL)
    assert run is not None, "the loader block no longer defines ROWS"
    for entry_id, config in shown:
        assert f'"id": "{entry_id}"' in run.group(1)
        if config is not None:
            for key, value in config.items():
                assert f'"{key}": "{value}"' in run.group(1)
