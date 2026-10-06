"""CLI server launch and optional browser opening after startup."""

from __future__ import annotations

import asyncio
import socket
import webbrowser
from types import FrameType
from typing import override

import uvicorn

from jot.config import JotHome
from jot.server.app import Application

# Backstop: after Ctrl+C, wait at most this long for open connections.
GRACEFUL_SHUTDOWN_SECONDS = 3


class JotServer(uvicorn.Server):
    """Uvicorn server that ends live event streams as soon as shutdown begins.

    SSE responses never finish on their own, so uvicorn would otherwise wait on
    them forever ("Waiting for connections to close").
    """

    def __init__(self, config: uvicorn.Config, application: Application) -> None:
        super().__init__(config)
        self._application = application
        self._loop: asyncio.AbstractEventLoop | None = None

    @override
    async def serve(self, sockets: list[socket.socket] | None = None) -> None:
        """Remember the event loop so signal handlers can schedule work on it."""
        self._loop = asyncio.get_running_loop()
        await super().serve(sockets)

    @override
    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        """Begin uvicorn's shutdown and close event streams on the loop thread."""
        super().handle_exit(sig, frame)
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._application.close_streams)


class ServerLauncher:
    """Run uvicorn with data-home defaults and optional command-line overrides."""

    def run(
        self,
        *,
        host: str | None,
        port: int | None,
        open_browser: bool,
    ) -> None:
        """Resolve settings and run one event loop until server shutdown."""
        home = JotHome.resolve()
        config = home.initialize().server
        bind_host, bind_port = host or config.host, port or config.port
        application = Application(home)
        server = JotServer(
            uvicorn.Config(
                application.build(),
                host=bind_host,
                port=bind_port,
                timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
            ),
            application,
        )
        browser_host = "127.0.0.1" if bind_host in {"0.0.0.0", "::"} else bind_host  # noqa: S104 - browser destination normalization
        if ":" in browser_host:
            browser_host = f"[{browser_host}]"
        asyncio.run(
            self._serve(
                server, f"http://{browser_host}:{bind_port}/", open_browser=open_browser
            )
        )

    async def _serve(
        self, server: uvicorn.Server, url: str, *, open_browser: bool
    ) -> None:
        """Open the browser only once uvicorn reports successful startup."""
        serving = asyncio.create_task(server.serve())
        if open_browser:
            while not server.started and not serving.done():
                await asyncio.sleep(0.05)
            if server.started:
                await asyncio.to_thread(webbrowser.open, url)
        await serving
