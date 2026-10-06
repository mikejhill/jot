"""CLI server launch and optional browser opening after startup."""

from __future__ import annotations

import asyncio
import webbrowser

import uvicorn

from jot.config import JotHome
from jot.server.app import create_app


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
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(home),
                host=bind_host,
                port=bind_port,
            )
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
