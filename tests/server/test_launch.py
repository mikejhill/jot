"""Server launcher."""

from __future__ import annotations

import asyncio

import pytest
import uvicorn

from jot.config import JotHome
from jot.server.launch import ServerLauncher


class FakeServer:
    """Stands in for uvicorn.Server."""

    def __init__(self, *, start: bool) -> None:
        self.started = False
        self.start = start

    async def serve(self) -> None:
        """Pretend to start, then stop."""
        await asyncio.sleep(0.01)
        self.started = self.start


class TestLauncher:
    """URLs, server construction, and browser opening."""

    @pytest.mark.parametrize(
        ("host", "url"),
        [
            ("127.0.0.1", "http://127.0.0.1:8765/"),
            ("0.0.0.0", "http://127.0.0.1:8765/"),  # noqa: S104 - normalization test
            ("::1", "http://[::1]:8765/"),
        ],
    )
    def test_url(self, host: str, url: str) -> None:
        """Bind-all addresses open on loopback; IPv6 is bracketed."""
        assert ServerLauncher.url(host, 8765) == url

    def test_server(self, home: JotHome) -> None:
        """The server is configured for the requested address."""
        server = ServerLauncher(home).server("127.0.0.1", 9999)
        assert isinstance(server, uvicorn.Server)
        assert server.config.port == 9999

    @pytest.mark.parametrize("start", [True, False])
    def test_open_browser(
        self, monkeypatch: pytest.MonkeyPatch, *, start: bool
    ) -> None:
        """The browser opens only after a successful start."""
        opened: list[str] = []
        monkeypatch.setattr("webbrowser.open", opened.append)
        fake = FakeServer(start=start)

        async def serve() -> None:
            task = asyncio.create_task(
                ServerLauncher.serve(fake, "http://x/", open_browser=True)  # ty: ignore[invalid-argument-type]
            )
            await asyncio.sleep(0.02)
            fake.started = start
            await task

        asyncio.run(serve())
        assert opened == (["http://x/"] if start else [])

    def test_run(self, home: JotHome, monkeypatch: pytest.MonkeyPatch) -> None:
        """run() builds the server and serves it without a browser."""
        calls: list[bool] = []

        async def serve(_server: object, _url: str, *, open_browser: bool) -> None:
            calls.append(open_browser)

        monkeypatch.setattr(ServerLauncher, "serve", staticmethod(serve))
        ServerLauncher(home).run(host="127.0.0.1", port=9998, open_browser=False)
        assert calls == [False]
