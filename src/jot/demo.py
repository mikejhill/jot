"""Demo workspace, local test server, and documentation screenshot generator.

Used by the browser (end-to-end) tests and ``uv run poe screenshots``. Nothing
here touches a real data home; callers pass a throwaway ``JotHome``.
"""

from __future__ import annotations

import json
import logging
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Self

import uvicorn

from jot.config import JotHome
from jot.core.models import Clock, EventKind, Flow, Project, Run, Status, Task
from jot.core.workflow import Workflow
from jot.db.connection import Database
from jot.db.repository import ProjectRepository, RunRepository, TaskRepository
from jot.server.app import create_app

if TYPE_CHECKING:
    from types import TracebackType

    from playwright.sync_api import Page

logger = logging.getLogger(__name__)

DEMO_CONFIG = """[triage]
backend = "fake"
[drawdown]
backend = "claude"
default_flow = "planned"

[harnesses.demo-fast]
kind = "fake"
label = "Fast triage"
models.allowed = ["gpt-6-luna", "default"]
models.default = { triage = "gpt-6-luna" }

[harnesses.demo-deep]
kind = "fake"
label = "Careful planner"
models.default = { plan = "opus", execute = "sonnet" }
instructions.plan = "Prefer small, reviewable steps with explicit acceptance checks."

[[pins]]
label = "Quick capture"
harness = "demo-fast"
model = "gpt-6-luna"
actions = ["triage"]

[[pins]]
label = "Careful plan"
harness = "demo-deep"
model = "opus"
actions = ["plan"]
"""

PLAN = {
    "summary": (
        "The health endpoint only returns `200 OK` on a TCP ping, so it reports "
        "**healthy** while the database or model service is down."
    ),
    "plan": (
        "1. Add `/health/live` (process up) and `/health/ready` (dependencies).\n"
        "2. Probe **PostgreSQL**, the **model service**, and the **queue** with "
        "2s timeouts.\n"
        "3. Return a JSON body per dependency and `503` when any is down.\n"
        "4. Cover each failure mode with tests; update the load balancer check."
    ),
    "questions": [
        "Should a slow (but up) model service count as *degraded* or *down*?",
        "Which endpoint should the load balancer use: `live` or `ready`?",
    ],
}

RESULT = """## Summary

Added **liveness** and **readiness** endpoints with per-dependency checks.

| Check | Timeout | Failure |
| --- | --- | --- |
| PostgreSQL | 2s | `503` |
| Model service | 2s | `503` |
| Queue | 1s | `503` |

- Tests: `14 passed`
- Branch: `jot/1-functional-health-checks`
"""


@dataclass(frozen=True, slots=True)
class LogLine:
    """One scripted run-log entry."""

    kind: str
    text: str = ""
    model: str | None = None
    usage: dict[str, int | None] | None = None


class DemoWorkspace:
    """Seed a realistic, deterministic workspace into a throwaway data home."""

    def __init__(self, home: JotHome) -> None:
        self.home = home

    def seed(self) -> None:
        """Write config and create projects, tasks, events, runs, and logs."""
        self.home.path.mkdir(parents=True, exist_ok=True)
        (self.home.path / "config.toml").write_text(DEMO_CONFIG, encoding="utf-8")
        self.home.initialize()
        with Database.open(self.home.database) as db:
            self._seed(db)

    def _seed(self, db: Database) -> None:
        """Create the demo records."""
        projects = ProjectRepository(db)
        tasks = TaskRepository(db)
        workflow = Workflow(db)
        orbit = projects.create(
            Project(slug="orbit-api", name="Orbit API", priority=2, aliases=["orbit"])
        )
        docs = projects.create(Project(slug="docs", name="Docs"))

        def task(title: str, **fields: object) -> Task:
            values: dict[str, object] = {"title": title, "raw_input": title}
            values.update(fields)
            created = tasks.create(Task.model_validate(values))
            tasks.update(created.id, {"needs_enrichment": False})
            return created

        health = task(
            "Add functional health checks",
            raw_input="orbit api - health check is simple ping; need to make holistic",
            description=(
                "The health check only pings the service. Probe internal "
                "dependencies so outages are detected.\n\n**Acceptance:** `503` "
                "when PostgreSQL, the model service, or the queue is down."
            ),
            project_id=orbit.id,
            labels=["orbit-api", "health-checks", "feature", "code-change"],
            criticality="high",
            type="feature",
        )
        for target in (Status.READY, Status.PLANNING, Status.AWAITING_APPROVAL):
            workflow.move(health.id, target)
        self._plan_run(db, health.id)

        retry = task(
            "Retry failed exports with backoff",
            project_id=orbit.id,
            labels=["orbit-api", "reliability"],
            criticality="medium",
            type="feature",
            flow=Flow.DIRECT,
        )
        for target in (Status.READY, Status.EXECUTING, Status.REVIEW):
            workflow.move(retry.id, target)
        self._execute_run(db, retry.id)

        task(
            "Document the deploy rollback steps",
            project_id=docs.id,
            labels=["docs"],
            criticality="low",
            type="chore",
        )
        ready = task(
            "Rate-limit the public scoring endpoint",
            project_id=orbit.id,
            labels=["orbit-api", "security"],
            criticality="critical",
            type="feature",
        )
        workflow.move(ready.id, Status.READY)
        done = task(
            "Upgrade the HTTP client library",
            labels=["chore"],
            criticality="low",
            type="chore",
            flow=Flow.DIRECT,
        )
        for target in (Status.READY, Status.EXECUTING, Status.REVIEW, Status.DONE):
            workflow.move(done.id, target)
        task("Try a dark-mode friendly color palette", type="idea", criticality="low")

    def _plan_run(self, db: Database, task_id: int) -> None:
        """Record a finished plan run with structured output and questions."""
        tasks = TaskRepository(db)
        plan_json = json.dumps(PLAN)
        run = self._run(
            db,
            task_id,
            "plan",
            "opus",
            [
                LogLine("thinking", "The ping only proves the port is open."),
                LogLine("tool", "Grep /health"),
                LogLine("tool", "Read src/orbit/health.py"),
                LogLine(
                    "usage",
                    model="claude-opus",
                    usage=self._usage(12, 410, 18000, 2100),
                ),
                LogLine("text", plan_json),
                LogLine(
                    "usage", model="claude-opus", usage=self._usage(8, 620, 20100, 300)
                ),
                LogLine(
                    "usage", "total", "claude-opus", self._usage(20, 1030, 38100, 2400)
                ),
                LogLine("result", plan_json),
            ],
        )
        with db.write():
            tasks.audit(
                task_id,
                EventKind.COMMENT,
                {"text": PLAN["summary"]},
                actor="agent:claude",
            )
            tasks.audit(
                task_id,
                EventKind.PLAN,
                {"text": PLAN["plan"], "run_id": run.id},
                actor="agent:claude",
            )
            for question in PLAN["questions"]:
                tasks.audit(
                    task_id,
                    EventKind.QUESTION,
                    {"text": question},
                    actor="agent:claude",
                )

    def _execute_run(self, db: Database, task_id: int) -> None:
        """Record a finished execute run with a Markdown result."""
        tasks = TaskRepository(db)
        self._run(
            db,
            task_id,
            "execute",
            "sonnet",
            [
                LogLine("tool", "Read src/orbit/export.py"),
                LogLine(
                    "thinking", "Use exponential backoff with jitter; cap at 5 tries."
                ),
                LogLine("tool", "Edit src/orbit/export.py"),
                LogLine("tool", "$ uv run pytest tests/test_export.py"),
                LogLine(
                    "usage",
                    model="claude-sonnet",
                    usage=self._usage(9, 880, 15500, 900),
                ),
                LogLine("text", RESULT),
                LogLine(
                    "usage", "total", "claude-sonnet", self._usage(9, 880, 15500, 900)
                ),
                LogLine("result", RESULT),
            ],
        )
        with db.write():
            tasks.audit(
                task_id, EventKind.RESULT, {"text": RESULT}, actor="agent:claude"
            )

    def _run(
        self, db: Database, task_id: int, phase: str, model: str, lines: list[LogLine]
    ) -> Run:
        """Create a succeeded run row and its JSONL log."""
        runs = RunRepository(db)
        run = runs.create(
            Run(
                task_id=task_id,
                backend="claude",
                model=model,
                phase=phase,
                status="running",
            )
        )
        logs = self.home.path / "runs"
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / f"{run.id}.jsonl").open("w", encoding="utf-8") as stream:
            for line in lines:
                record: dict[str, object] = {
                    "run_id": run.id,
                    "task_id": task_id,
                    "kind": line.kind,
                    "text": line.text,
                    "ts": Clock.stamp(),
                }
                if line.model:
                    record["model"] = line.model
                if line.usage is not None:
                    record["usage"] = line.usage
                stream.write(json.dumps(record) + "\n")
        total = next(
            line.usage for line in lines if line.text == "total" and line.usage
        )
        summary = PLAN["summary"] if phase == "plan" else "Added retries with backoff."
        return runs.update(
            run.id,
            {
                "status": "succeeded",
                "summary": summary,
                "ended_at": Clock.stamp(),
                "input_tokens": total["input"],
                "output_tokens": total["output"],
                "cache_read_tokens": total["cache_read"],
                "cache_write_tokens": total["cache_write"],
            },
        )

    @staticmethod
    def _usage(inp: int, out: int, read: int, write: int) -> dict[str, int | None]:
        """Build a usage record."""
        return {"input": inp, "output": out, "cache_read": read, "cache_write": write}


class DemoServer:
    """Run the Jot web app for a data home on a free local port in a thread."""

    def __init__(self, home: JotHome) -> None:
        self.home = home
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = int(probe.getsockname()[1])
        self.url = f"http://127.0.0.1:{self.port}"
        self._server = uvicorn.Server(
            uvicorn.Config(
                create_app(home),
                host="127.0.0.1",
                port=self.port,
                log_level="warning",
                timeout_graceful_shutdown=1,
            )
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self) -> Self:
        """Start the server and wait until it accepts requests."""
        self._thread.start()
        deadline = time.monotonic() + 20
        while not self._server.started:
            if time.monotonic() > deadline or not self._thread.is_alive():
                raise RuntimeError("demo server failed to start")
            time.sleep(0.05)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stop the server."""
        self._server.should_exit = True
        self._thread.join(timeout=10)


class Screenshots:  # pragma: no cover - needs a browser; run by the CI e2e job
    """Capture documentation screenshots of the demo workspace."""

    WIDTH = 1440
    HEIGHT = 900

    def __init__(self, output: Path, channel: str = "chrome") -> None:
        self.output = output
        self.channel = channel

    def capture(self, home: JotHome) -> list[Path]:
        """Seed ``home``, serve it, and write PNGs to the output folder."""
        # Imported lazily: Playwright is a dev-only dependency.
        from playwright.sync_api import sync_playwright  # noqa: PLC0415

        DemoWorkspace(home).seed()
        self.output.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        with DemoServer(home) as server, sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=self.channel)
            for theme in ("light", "dark"):
                page = browser.new_page(
                    viewport={"width": self.WIDTH, "height": self.HEIGHT},
                    color_scheme=theme,
                    device_scale_factor=1,
                )
                written += self._shots(page, server.url, theme)
                page.close()
            browser.close()
        return written

    def _shots(self, page: Page, url: str, theme: str) -> list[Path]:
        """Capture board, list (expanded), and task drawer for one theme."""
        written: list[Path] = []
        page.goto(f"{url}/#/board")
        page.get_by_text("Add functional health checks").first.wait_for()
        written.append(self._save(page, f"board-{theme}"))
        if theme == "dark":
            return written
        page.get_by_role("button", name="List").click()
        page.get_by_role("button", name="Expand task 1").click()
        page.locator(".expanded").wait_for()
        written.append(self._save(page, "list"))
        page.get_by_role("button", name="Board").click()
        page.get_by_text("Retry failed exports with backoff").first.click()
        page.locator(".run-log").first.wait_for()
        self._scroll_to(page, ".drawer .runs")
        written.append(self._save(page, "task-run-output"))
        page.keyboard.press("Escape")
        page.get_by_text("Add functional health checks").first.click()
        page.locator(".run-log .structured").first.wait_for()
        self._scroll_to(page, ".drawer .runs")
        written.append(self._save(page, "task-plan"))
        page.keyboard.press("Escape")
        page.get_by_role("button", name="Settings").click()
        page.get_by_role("button", name="Save settings").wait_for()
        written.append(self._save(page, "settings"))
        return written

    def _scroll_to(self, page: Page, selector: str) -> None:
        """Scroll an element to the top of its scroll container."""
        page.locator(selector).first.evaluate(
            "node => node.scrollIntoView({block: 'start'})"
        )

    def _save(self, page: Page, name: str) -> Path:
        """Write one screenshot."""
        path = self.output / f"{name}.png"
        page.wait_for_timeout(250)
        page.screenshot(path=str(path))
        logger.info("wrote %s", path)
        return path

    @classmethod
    def main(cls) -> None:
        """CLI: ``python -m jot.demo [output_dir] [--channel chrome]``."""
        args = sys.argv[1:]
        channel = "chrome"
        if "--channel" in args:
            index = args.index("--channel")
            channel = args[index + 1]
            del args[index : index + 2]
        output = Path(args[0]) if args else Path("docs/images")
        logging.basicConfig(level=logging.INFO, stream=sys.stderr)
        home = JotHome(Path.cwd() / ".tmp" / "screenshots-home")
        if home.path.exists():
            for child in sorted(home.path.rglob("*"), reverse=True):
                child.unlink() if child.is_file() else child.rmdir()
        for path in cls(output, channel).capture(home):
            sys.stdout.write(f"{path}\n")


if __name__ == "__main__":  # pragma: no cover
    Screenshots.main()
