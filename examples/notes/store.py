"""The Definition: what a note store is, and nothing about how one works.

A consumer imports this module and no other. That is the whole discipline of
the capability seam -- the import graph is the swappability guarantee, because
code that never imports a provider cannot depend on one (capability 19).
"""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING

from cordis import Definition

if TYPE_CHECKING:
    from collections.abc import Mapping


class Store(Definition):
    """Somewhere to put a note and get it back, under one registry name."""

    name = "store"

    @abc.abstractmethod
    def put(self, key: str, text: str) -> None:
        """Record ``text`` under ``key``, replacing whatever was there."""

    @abc.abstractmethod
    def all(self) -> Mapping[str, str]:
        """Every note, as a snapshot the caller may keep."""
