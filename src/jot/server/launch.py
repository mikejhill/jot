"""Run the HTTP server for the current data home."""

from __future__ import annotations

import asyncio
import webbrowser

import uvicorn

from jot.config import JotHome
from jot.plugins.host import PluginCatalog, Worker
from jot.server.app import ApiApplication

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


class ServerLauncher:
    """Start uvicorn and optionally open the browser once it is listening."""

    def __init__(
        self, home: JotHome | None = None, catalog: PluginCatalog | None = None
    ) -> None:
        self.home = home or JotHome.resolve()
        self.catalog = catalog if catalog is not None else PluginCatalog.discover()

    @staticmethod
    def url(host: str, port: int) -> str:
        """Return the browser URL for a bind address."""
        browser = "127.0.0.1" if host in {"0.0.0.0", "::"} else host  # noqa: S104 - bind-all normalization
        if ":" in browser:
            browser = f"[{browser}]"
        return f"http://{browser}:{port}/"

    def server(self, host: str, port: int) -> uvicorn.Server:
        """Build the uvicorn server (validates the home before binding)."""
        self.home.initialize().registry()
        app = ApiApplication(self.home, self.catalog).build()
        return uvicorn.Server(
            uvicorn.Config(app, host=host, port=port, timeout_graceful_shutdown=3)
        )

    def run(
        self, *, host: str, port: int, open_browser: bool, worker: bool = True
    ) -> None:
        """Serve until interrupted, running the plugin worker alongside if asked."""
        server = self.server(host, port)
        background = Worker(self.home, self.catalog) if worker else None
        if background is not None:
            background.start()
        try:
            asyncio.run(
                self.serve(server, self.url(host, port), open_browser=open_browser)
            )
        finally:
            if background is not None:
                background.stop()

    @staticmethod
    async def serve(server: uvicorn.Server, url: str, *, open_browser: bool) -> None:
        """Run the server, opening the browser after startup if asked."""
        task = asyncio.create_task(server.serve())
        if open_browser:
            while not server.started and not task.done():
                await asyncio.sleep(0.05)
            if server.started:
                await asyncio.to_thread(webbrowser.open, url)
        await task
