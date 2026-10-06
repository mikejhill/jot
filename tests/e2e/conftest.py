"""Browser test fixtures: a seeded demo workspace served on a free port."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from playwright.sync_api import ConsoleMessage, Page

from jot.config import JotHome
from jot.demo import DemoServer, DemoWorkspace

ROOT = Path(__file__).resolve().parents[2] / ".tmp" / "e2e"


@pytest.fixture(scope="module")
def server() -> Iterator[DemoServer]:
    """Serve a freshly seeded demo workspace for one test module."""
    home = JotHome(ROOT / uuid4().hex)
    DemoWorkspace(home).seed()
    with DemoServer(home) as running:
        yield running
    shutil.rmtree(home.path, ignore_errors=True)


@pytest.fixture
def app(page: Page, server: DemoServer) -> Iterator[Page]:
    """Open the app, failing the test on any console error."""
    errors: list[str] = []

    def record(message: ConsoleMessage) -> None:
        if message.type == "error":
            errors.append(message.text)

    page.on("console", record)
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(server.url)
    page.get_by_text("Add functional health checks").first.wait_for()
    yield page
    assert errors == []
