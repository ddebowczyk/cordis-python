"""The two exporters worth shipping: one to a stream, one to stdlib logging.

Kept out of :mod:`cordis.logging` so the core never reaches for the standard
library's ``logging`` on its own. Importing this module is harmless -- the
sharp edge in stdlib logging is global handler configuration, and neither
exporter here performs any.

Both are plain objects satisfying :class:`~cordis.logging.Exporter`
structurally, and both carry a ``level``, so mounting one is::

    logs = ctx.require(LoggerService)
    logs.add_exporter(ConsoleExporter(), scope=scope_of(ctx))
"""

from __future__ import annotations

import logging as stdlib
import sys
from typing import TYPE_CHECKING, Final

from cordis.logging import Level

if TYPE_CHECKING:
    from typing import TextIO

    from cordis.logging import Record

__all__ = [
    "ConsoleExporter",
    "StdlibExporter",
]

#: Where a record's own fields land on a stdlib ``LogRecord``. Prefixed,
#: because ``extra=`` silently corrupts a LogRecord when a key collides with
#: one of its own attributes.
FIBER_FIELD: Final = "cordis_fiber"
SEQ_FIELD: Final = "cordis_seq"


class ConsoleExporter:
    """One line per record, to a stream. The default destination for a shell app."""

    def __init__(
        self, stream: TextIO | None = None, *, level: Level = Level.INFO
    ) -> None:
        self._stream = stream
        self.level = level

    def export(self, record: Record, /) -> None:
        # Resolved per record rather than captured: a test that swaps
        # `sys.stderr` between calls means the swap, and capturing the stream
        # at construction would quietly ignore it.
        stream = self._stream if self._stream is not None else sys.stderr
        print(self.format(record), file=stream)

    def format(self, record: Record, /) -> str:
        """``LEVEL  fiber name: message``, in that order for a reason.

        Level first because it is what an eye scans a log for; the fiber before
        the logger name because "which instance" is the question a composed
        application asks and "which subsystem" is the one a single program does.
        """
        return f"{record.level.name:<7} {record.fiber} {record.name}: {record.render()}"

    def __repr__(self) -> str:
        return f"<ConsoleExporter level={self.level.name}>"


class StdlibExporter:
    """A bridge to ``logging``, for applications that already have opinions.

    Records go to ``logging.getLogger(f"{root}.{record.name}")`` unrendered, so
    stdlib's own lazy formatting stays lazy and its handlers, filters and
    levels apply exactly as the application configured them.
    """

    def __init__(self, root: str = "cordis", *, level: Level = Level.DEBUG) -> None:
        self._root = root
        self.level = level

    def export(self, record: Record, /) -> None:
        target = stdlib.getLogger(f"{self._root}.{record.name}")
        target.log(
            int(record.level),
            record.message,
            *record.args,
            extra={
                FIBER_FIELD: record.fiber,
                SEQ_FIELD: record.seq,
                **dict(record.extra),
            },
        )

    def __repr__(self) -> str:
        return f"<StdlibExporter root={self._root!r} level={self.level.name}>"
