"""Every documented example is runnable and prints its deterministic transcript."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

ROOT: Final = Path(__file__).resolve().parents[1]

EXAMPLES: Final[tuple[tuple[str, str], ...]] = (
    (
        "notes",
        "writer wrote hello to MemoryStore\n"
        "main read 1 from MemoryStore\n"
        "sandbox read 0 from MemoryStore\n"
        "--- swapping the store ---\n"
        "main released MemoryStore\n"
        "writer wrote hello to FileStore\n"
        "main read 1 from FileStore\n",
    ),
    (
        "order_events",
        "quote: 110\n"
        "Ada authorized: True\n"
        "blocked authorized: False\n"
        "weather tool: weather-v1\n"
        "calendar tool: calendar-v1\n"
        "unknown tool: None\n"
        "audit: accepted:Ada:110\n"
        "listeners after close: 0\n",
    ),
    (
        "dynamic_service_restart",
        "initial: PENDING (model-endpoint)\n"
        "primary endpoint: ACTIVE\n"
        "after removal: PENDING (model-endpoint)\n"
        "secondary endpoint: ACTIVE\n"
        "lifecycle: connect:primary | disconnect:primary | connect:secondary | "
        "disconnect:secondary\n",
    ),
    (
        "tenant_scopes",
        "north tools: billing, north-export\n"
        "south tools: billing\n"
        "north alerts: audit:north:invoice-ready | north:invoice-ready\n"
        "south alerts: audit:south:invoice-ready | south:invoice-ready\n"
        "south after north closes: billing\n",
    ),
    (
        "service_isolation",
        "tenant worker: ACTIVE using shared-logger\n"
        "billing adapter: PENDING\n"
        "hidden dependency: billing-token\n"
        "root token remains: root-only-token\n",
    ),
    (
        "service_interception",
        "bulk-export: retries=2 timeout_ms=250\n"
        "payment-webhook: retries=0 timeout_ms=100\n"
        "shared client: yes\n",
    ),
    (
        "configuration_validation",
        "accepted: eu-west retries=2\n"
        "rejected fields: region, retries\n"
        "plugin body runs: 1\n",
    ),
    (
        "runtime_diagnostics",
        "pending dependency: database\n"
        "worker after database: ACTIVE\n"
        "tree: reporting_worker, Database\n",
    ),
    (
        "runtime_observability",
        "health: reporting_worker=ACTIVE trace_exporter=FAILED\n"
        "failure: collector unreachable\n"
        "transition: reporting_worker:PENDING->LOADING\n"
        "transition: reporting_worker:LOADING->ACTIVE\n"
        "transition: trace_exporter:PENDING->LOADING\n"
        "transition: trace_exporter:LOADING->FAILED\n"
        "after shutdown: 0 fibers\n",
    ),
    (
        "scheduled_worker",
        "coalesced batch: latest\nafter shutdown: latest\n",
    ),
)


@pytest.mark.parametrize(("example", "expected"), EXAMPLES)
def test_example_cli_transcript(example: str, expected: str, tmp_path: Path) -> None:
    """Exercise the public `python -m` command from a clean working directory."""
    environment = dict(os.environ)
    paths = [str(ROOT), str(ROOT / "src")]
    if inherited := environment.get("PYTHONPATH"):
        paths.append(inherited)
    environment["PYTHONPATH"] = os.pathsep.join(paths)

    result = subprocess.run(
        [sys.executable, "-m", f"examples.{example}.app"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == expected
