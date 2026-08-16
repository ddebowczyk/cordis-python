"""Two Providers of one Definition. Nothing imports this module.

The config file names them, the loader resolves the name to the class, and the
class binds under `Store.name` because that is what mounting a `Service`
subclass does. Swapping one for the other is therefore an edit to a file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from examples.notes.store import Store

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cordis import Context


class MemoryStore(Store):
    """The default: notes live as long as the plugin does."""

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self._notes: dict[str, str] = {}

    def put(self, key: str, text: str) -> None:
        self._notes[key] = text

    def all(self) -> Mapping[str, str]:
        return dict(self._notes)


@dataclass(frozen=True, slots=True)
class FileConfig:
    """What the file-backed provider needs. Validated before it is constructed."""

    path: str = "notes.json"


class FileStore(Store):
    """The same capability, backed by a JSON file.

    Takes a config, which a `Service` plugin does by taking a second parameter;
    `Config` is the schema it is validated against, so a bad `path` is a mount
    failure with a field path in it rather than a `TypeError` later on.
    """

    Config = FileConfig

    def __init__(self, ctx: Context, config: FileConfig) -> None:
        super().__init__(ctx)
        self._path = Path(config.path)
        if not self._path.exists():
            self._path.write_text("{}", encoding="utf-8")

    def put(self, key: str, text: str) -> None:
        notes = dict(self.all())
        notes[key] = text
        self._path.write_text(json.dumps(notes, indent=2), encoding="utf-8")

    def all(self) -> Mapping[str, str]:
        loaded: object = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):  # pragma: no cover -- a corrupted file
            return {}
        return {str(key): str(value) for key, value in loaded.items()}
