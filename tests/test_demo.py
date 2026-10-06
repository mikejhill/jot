"""Demo workspace seeding and the local demo server (no browser needed)."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from jot.config import JotHome
from jot.demo import DemoServer, DemoWorkspace


class TestDemo:
    """The seeded workspace powers browser tests and screenshots."""

    def test_seed_and_serve(self, tmp_path: Path) -> None:
        """Seeding makes tasks in many statuses plus runs and logs; all are served."""
        home = JotHome(tmp_path / "demo")
        DemoWorkspace(home).seed()
        assert (home.path / "runs" / "1.jsonl").is_file()
        with DemoServer(home) as server:
            with urllib.request.urlopen(f"{server.url}/api/tasks") as response:  # noqa: S310 - local test server
                tasks = json.load(response)
            with urllib.request.urlopen(f"{server.url}/api/runs/1/log") as response:  # noqa: S310 - local test server
                log = json.load(response)
        statuses = {task["status"] for task in tasks}
        assert {"inbox", "ready", "awaiting_approval", "review", "done"} <= statuses
        assert {line["kind"] for line in log} >= {"thinking", "tool", "usage", "text"}
