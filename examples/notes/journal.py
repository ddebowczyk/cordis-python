"""A second service, so the scenario has something to assert on.

Consumers record what they saw here instead of printing it. Printing would
make the demonstration unverifiable, and a journal is what an application
would have anyway.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cordis import Service

if TYPE_CHECKING:
    from cordis import Context


class Journal(Service):
    """An append-only record of who ran, and against which implementation."""

    name = "journal"

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self._lines: list[str] = []

    def record(self, line: str) -> None:
        self._lines.append(line)

    def lines(self) -> tuple[str, ...]:
        return tuple(self._lines)
