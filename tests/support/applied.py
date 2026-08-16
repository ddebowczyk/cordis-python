"""A module that is a plugin: mounting looks for `apply` structurally.

Kept as a real module rather than a fixture because the thing under test is
exactly whether a bare importable module can be named as a target.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cordis.context import Context


def apply(ctx: Context) -> None:
    """The whole plugin. It has nothing to do; being mountable is the point."""
