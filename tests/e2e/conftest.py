"""A live server on a free port and a Chromium page that fails on console errors."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn
from playwright.sync_api import Browser, ConsoleMessage, Page, sync_playwright

from jot.config import JotHome
from jot.plugins.host import PluginCatalog, Worker
from jot.server.app import ApiApplication

# Statuses some tests provoke on purpose (conflicts, invalid input).
EXPECTED = ("409", "422")


class LiveServer:
    """Run uvicorn in a background thread for one test."""

    def __init__(self, home: JotHome) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = int(probe.getsockname()[1])
        catalog = PluginCatalog.discover()
        config = uvicorn.Config(
            ApiApplication(home, catalog).build(), port=self.port, log_level="warning"
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.worker = Worker(home, catalog, interval=0.1)

    @property
    def url(self) -> str:
        """Return the base URL."""
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> LiveServer:
        """Start and wait until the server accepts connections."""
        self.thread.start()
        self.worker.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline:  # pragma: no cover - startup failure
                raise RuntimeError("server did not start")
            time.sleep(0.02)
        return self

    def __exit__(self, *_exc: object) -> None:
        """Stop the server thread."""
        self.worker.stop()
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture
def live(home: JotHome) -> Iterator[LiveServer]:
    """A running server on an isolated data home."""
    with LiveServer(home) as server:
        yield server


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    """One headless Chromium for the session."""
    with sync_playwright() as playwright:
        chromium = playwright.chromium.launch()
        yield chromium
        chromium.close()


@pytest.fixture
def page(browser: Browser, live: LiveServer) -> Iterator[Page]:
    """A fresh page on the live server that must log no console errors."""
    context = browser.new_context(base_url=live.url)
    tab = context.new_page()
    errors: list[str] = []

    def record(message: ConsoleMessage) -> None:
        expected = any(f"status of {code}" in message.text for code in EXPECTED)
        if message.type == "error" and not expected:
            errors.append(message.text)

    tab.on("console", record)
    tab.on("pageerror", lambda error: errors.append(str(error)))
    yield tab
    context.close()
    assert errors == []
