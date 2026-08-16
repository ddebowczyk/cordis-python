"""Boot the application from YAML, and swap its store while it runs.

Two things are worth reading here. The first is :func:`boot`, which is the
whole launcher: mount the loader, hand it an entry list, wait. The second is
:func:`swap`, which changes the running application by reconciling against a
*different entry list* -- the same call the first boot made. There is no
special path for reconfiguration, which is why there is no consumer code in
this example that knows a swap is possible.

::

    python -m examples.notes.app                # run the scenario
    python -m examples.notes.app --dump-config  # what the layers resolve to
    python -m examples.notes.app --dump-config --layer swap.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from cordis import (
    LoaderService,
    PluginHost,
    Resolution,
    YamlSource,
    read_layer,
    resolve,
    wrote,
)
from examples.notes.journal import Journal

if TYPE_CHECKING:
    from collections.abc import Sequence

HERE = Path(__file__).resolve().parent
BASE = HERE / "cordis.yml"
SWAP = HERE / "swap.yaml"


# --------------------------------------------------------------------------
# What to run
# --------------------------------------------------------------------------


def plan(layers: Sequence[Path] = ()) -> Resolution:
    """Fold the patch layers onto the base file, remembering who wrote what.

    Pure, and no host in sight: a deployment can render its own configuration
    and diff it against production without starting anything.
    """
    read = [
        read_layer(yaml.safe_load(path.read_text(encoding="utf-8")), path.name)
        for path in layers
    ]
    return resolve(YamlSource(BASE).read(), read)


# --------------------------------------------------------------------------
# The launcher
# --------------------------------------------------------------------------


async def boot(layers: Sequence[Path] = ()) -> tuple[PluginHost, LoaderService]:
    """The whole of starting an application: a host, a loader, an entry list."""
    host = PluginHost()
    host.root.plugin(LoaderService)
    await host.runtime.quiesce()
    loader = host.root.context.require(LoaderService)
    await loader.reconcile(plan(layers).entries)
    await host.runtime.quiesce()
    return host, loader


async def swap() -> tuple[str, ...]:
    """Boot, replace the store underneath the running consumers, report.

    The journal survives the swap because nothing in the new entry list
    changed its row; the consumers do not, because the service they injected
    went away and came back as something else. Neither of them was told.
    """
    host, loader = await boot()
    journal = host.root.context.require(Journal)
    journal.record("--- swapping the store ---")

    await loader.reconcile(plan([SWAP]).entries)
    await host.runtime.quiesce()

    lines = journal.lines()
    await host.dispose()
    return lines


# --------------------------------------------------------------------------
# What the layers resolved to
# --------------------------------------------------------------------------


def dump(layers: Sequence[Path] = ()) -> str:
    """Every entry, every field, and the file responsible for its value."""
    resolution = plan(layers)
    width = max(
        (len(f"{path}.{field}") for path, field in resolution.provenance), default=0
    )
    lines = [f"{'entry.field':<{width}}  source"]
    for (path, field), source in sorted(resolution.provenance.items()):
        lines.append(f"{f'{path}.{field}':<{width}}  {source}")
    for layer in layers:
        written = wrote(resolution, layer.name)
        lines.append(f"\n{layer.name} accounts for {len(written)} field(s):")
        lines += [f"  {path}.{field}" for path, field in written]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dump-config",
        action="store_true",
        help="print the resolved entry list with the source of every field",
    )
    parser.add_argument(
        "--layer",
        action="append",
        type=Path,
        default=[],
        metavar="FILE",
        help="a patch layer to fold on; repeatable, applied in order",
    )
    options = parser.parse_args(argv)
    layers = [path if path.is_file() else HERE / path for path in options.layer]

    if options.dump_config:
        print(dump(layers))
        return 0
    for line in asyncio.run(swap()):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
