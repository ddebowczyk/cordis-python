"""Scaffold smoke test.

Exists so the toolchain gate (ruff, mypy --strict, pytest) is proven working
on an otherwise empty package, before any capability depends on it.
"""

from __future__ import annotations

import cordis


def test_package_imports() -> None:
    assert cordis.__version__


def test_public_surface_is_declared() -> None:
    """Every exported name must be real -- catches a stale __all__ early."""
    for name in cordis.__all__:
        assert hasattr(cordis, name), name
