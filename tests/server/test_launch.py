"""CLI serve settings, browser timing, and application startup tests."""

from __future__ import annotations

import asyncio
import socket
from typing import ClassVar, override

import pytest
import uvicorn
from typer.testing import CliRunner

from jot.cli import CommandLine
from jot.config import JotHome
from jot.server.app import Application
from jot.server.launch import JotServer, ServerLauncher


class ServerBoundary:
    """Observe uvicorn configuration without opening a listening socket."""

    configs: ClassVar[list[uvicorn.Config]] = []
    urls: ClassVar[list[str]] = []

    @staticmethod
    async def serve(
        server: uvicorn.Server, sockets: list[socket.socket] | None = None
    ) -> None:
        """Record binding and simulate a completed startup."""
        del sockets
        ServerBoundary.configs.append(server.config)
        server.started = True
        await asyncio.sleep(0.06)

    @classmethod
    def open(cls, url: str) -> bool:
        """Record the browser destination without opening a real browser."""
        cls.urls.append(url)
        return True

    @staticmethod
    def fail(
        _self: ServerLauncher, *, host: str | None, port: int | None, open_browser: bool
    ) -> None:
        """Model a recoverable bind failure at the CLI boundary."""
        del host, port, open_browser
        raise OSError("Address already in use")


class TestServerLauncher:
    """Verify configuration defaults and explicit serve overrides."""

    def test_cli_defaults_and_overrides(
        self, home: JotHome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Serve honors home defaults and opens the browser only after startup."""
        monkeypatch.setattr(uvicorn.Server, "serve", ServerBoundary.serve)
        monkeypatch.setattr("jot.server.launch.webbrowser.open", ServerBoundary.open)
        ServerBoundary.configs.clear()
        ServerBoundary.urls.clear()
        config = home.path / "config.toml"
        config.write_text(
            config.read_text(encoding="utf-8").replace("8765", "8888"), encoding="utf-8"
        )
        runner = CliRunner()
        result = runner.invoke(CommandLine().app, ["serve"])
        assert result.exit_code == 0, result.output
        assert ServerBoundary.configs[-1].port == 8888
        assert not ServerBoundary.urls
        result = runner.invoke(
            CommandLine().app,
            ["serve", "--host", "localhost", "--port", "9000", "--open"],
        )
        assert result.exit_code == 0, result.output
        assert ServerBoundary.configs[-1].host == "localhost"
        assert ServerBoundary.configs[-1].port == 9000
        assert ServerBoundary.urls == ["http://localhost:9000/"]
        assert runner.invoke(CommandLine().app, ["serve", "--port", "0"]).exit_code == 2

    def test_cli_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Expected startup failures become the existing CLI error format."""
        monkeypatch.setattr(ServerLauncher, "run", ServerBoundary.fail)
        result = CliRunner().invoke(CommandLine().app, ["--json", "serve"])
        assert result.exit_code == 1
        assert "Address already in use" in result.stdout


class TestGracefulShutdown:
    """Ctrl+C closes live streams instead of waiting on them forever."""

    def test_handle_exit_closes_streams(self, home: JotHome) -> None:
        """handle_exit schedules close_streams on the server loop."""
        application = Application(home)
        closed: list[bool] = []

        class Recorder(Application):
            """Record close_streams calls."""

            @override
            def close_streams(self) -> None:
                """Record the call and close normally."""
                closed.append(True)
                super().close_streams()

        application.__class__ = Recorder
        config = uvicorn.Config(application.build(), timeout_graceful_shutdown=3)
        server = JotServer(config, application)

        async def scenario() -> None:
            server._loop = asyncio.get_running_loop()
            server.handle_exit(2, None)
            await asyncio.sleep(0)

        asyncio.run(scenario())
        assert closed == [True]
        assert server.should_exit
        assert config.timeout_graceful_shutdown == 3
        JotServer(config, application).handle_exit(2, None)  # no loop yet: no-op
