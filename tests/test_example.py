"""The example application, run as the integration test it was built to be.

`examples/notes` exists to demonstrate the claim the architecture rests on:
an application is a config file, and any implementation in it can be replaced
while it runs without a line of consumer code changing. A demonstration nobody
checks decays into a story, so the same scenario the CLI prints is asserted
here -- what booted, what the swap rebound, what stayed isolated, and which
file each field of the resolved config came from.

Everything runs in `tmp_path`: the file-backed provider writes where its config
says, and the config says `notes.json`, relative to the working directory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from examples.notes import app, consumers, providers
from examples.notes.journal import Journal
from examples.notes.providers import FileStore, MemoryStore
from examples.notes.store import Store

from cordis import PatchTargetError, imports_of

if TYPE_CHECKING:
    from pathlib import Path

pytest.importorskip("yaml", reason="the example is configured in YAML")

ROOT = app.HERE.parent.parent

#: Held so `imports_of` -- which reads `sys.modules` -- has both sides of the
#: seam to look at, whichever order the tests happen to run in.
SEAM_SIDES = (consumers, providers)


@pytest.fixture(autouse=True)
def _in_tmp_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The file-backed store writes `notes.json` into the working directory."""
    monkeypatch.chdir(tmp_path)


# --------------------------------------------------------------------------
# The seam itself
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@pytest.mark.parametrize(
    ("module", "forbidden"),
    [
        ("examples.notes.consumers", "examples.notes.providers"),
        ("examples.notes.providers", "examples.notes.consumers"),
    ],
)
def test_neither_side_of_the_seam_imports_the_other(
    module: str, forbidden: str
) -> None:
    """Capability 19 SEM-002, checked the only way it can be: on the imports.

    Behaviour cannot show this. Swapping providers and watching the answers
    change passes just as happily on the arrangement the rule forbids.

    `imports_of` reads `sys.modules`, so both sides are imported at the top of
    this file and the edges are asserted to be non-empty: a module nobody
    imported has no edges, and "no edges" would pass this on any arrangement
    at all.
    """
    edges = imports_of(module, root=ROOT)
    assert "examples.notes.store" in edges, f"{module} was never imported"
    assert forbidden not in edges


@pytest.mark.tier_local
def test_a_consumer_reaches_its_provider_through_the_definition() -> None:
    """`Store.of(ctx)` is what the consumers call, and it is typed as `Store`."""
    assert Store in providers.MemoryStore.__mro__
    assert issubclass(MemoryStore, Store)
    assert issubclass(FileStore, Store)
    assert MemoryStore.name == FileStore.name == "store"


# --------------------------------------------------------------------------
# Booting
# --------------------------------------------------------------------------


@pytest.mark.tier_local
async def test_the_application_boots_from_yaml() -> None:
    """Twelve lines of launcher, and the file decides what is running."""
    host, loader = await app.boot()
    try:
        assert set(loader.live()) == {
            "journal",
            "store",
            "writer",
            "report",
            "sandbox",
            "sandbox.store",
            "sandbox.report",
        }
    finally:
        await host.dispose()


@pytest.mark.tier_local
async def test_the_group_resolves_its_own_store() -> None:
    """Isolation: the sandbox reads the store mounted inside it, and only it."""
    host, _ = await app.boot()
    try:
        journal = host.root.context.require(Journal)
        assert "sandbox read 0 from MemoryStore" in journal.lines()
        # The outer reporter saw the note the writer left, or was there first;
        # what it cannot have seen is the sandbox's store, which stays empty.
        assert any(line.startswith("main read ") for line in journal.lines())
    finally:
        await host.dispose()


# --------------------------------------------------------------------------
# The swap
# --------------------------------------------------------------------------


@pytest.mark.tier_local
async def test_the_swap_rebinds_every_consumer_and_leaves_the_sandbox_alone() -> None:
    """The claim, asserted: new provider, same consumers, no consumer edits."""
    lines = await app.swap()
    before, after = _split(lines)

    assert any("MemoryStore" in line for line in before)
    assert not any("FileStore" in line for line in before)

    # Both consumers ran again, against the implementation the layer named.
    assert "writer wrote hello to FileStore" in after
    assert any(line.startswith("main read ") and "FileStore" in line for line in after)

    # And the isolated subtree, which the patch did not name, never moved.
    assert "main released MemoryStore" in after
    assert not any("sandbox released" in line for line in after)
    assert not any(line.startswith("sandbox read") for line in after)


@pytest.mark.tier_local
async def test_the_swap_leaves_the_notes_where_the_new_provider_puts_them(
    tmp_path: Path,
) -> None:
    """The new provider is really the one running, not a renamed old one."""
    await app.swap()
    assert (tmp_path / "notes.json").is_file()
    assert "the first note" in (tmp_path / "notes.json").read_text(encoding="utf-8")


def _split(lines: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    marker = lines.index("--- swapping the store ---")
    return lines[:marker], lines[marker + 1 :]


# --------------------------------------------------------------------------
# What the layers resolved to
# --------------------------------------------------------------------------


@pytest.mark.tier_local
def test_every_field_of_the_resolved_config_names_its_source() -> None:
    """`--dump-config`: the question a layered deployment is debugged with."""
    resolution = app.plan([app.SWAP])
    written = {
        key for key, source in resolution.provenance.items() if source != "<base>"
    }

    assert written == {("store", "name"), ("store", "config")}
    assert all(
        source == "swap.yaml"
        for key, source in resolution.provenance.items()
        if key in written
    )


@pytest.mark.tier_local
def test_the_dump_is_readable_and_complete() -> None:
    text = app.dump([app.SWAP])
    assert "store.name" in text
    assert "swap.yaml accounts for 2 field(s):" in text
    assert "sandbox.store.name" in text  # the untouched half is shown too


@pytest.mark.tier_local
def test_a_layer_that_names_the_wrong_provider_is_refused(tmp_path: Path) -> None:
    """The `name` guard: a patch applied to the row it did not mean is a bug.

    Written as a file the same way the shipped layer is, because the check
    being tested lives in the reader, not in a hand-built patch.
    """
    layer = tmp_path / "wrong.yaml"
    layer.write_text(
        "patches:\n"
        "  - id: store\n"
        "    name: examples.notes.providers:FileStore\n"
        "    fields:\n"
        "      disabled: true\n",
        encoding="utf-8",
    )
    with pytest.raises(PatchTargetError, match="store"):
        app.plan([layer])
