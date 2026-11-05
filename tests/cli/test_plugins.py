"""`jot plugin` and `jot job` with the packaged rules plugin."""

from __future__ import annotations

import json

import pytest
from tests.cli.test_cli import data, jot

from jot.app.session import Session
from jot.config import JotHome
from jot.plugins.context import PluginContext
from jot.plugins.host import PluginCatalog, PluginHost

RULE = json.dumps(
    {
        "rules": [
            {
                "name": "money",
                "when": {"text": "invoice"},
                "then": {"add_labels": ["finance"]},
            }
        ]
    }
)


@pytest.mark.usefixtures("home")
class TestPluginCommands:
    """Administer plugins and run them from the CLI."""

    def test_rules_end_to_end(self) -> None:
        """Enable, configure, run once, and see the rule applied."""
        assert "rules" in jot("plugin", "ls")
        assert "disabled" in jot("plugin", "ls")
        enabled = data("plugin", "enable", "rules", "--actor", "me")
        assert enabled["enabled"] is True  # ty: ignore[not-subscriptable]
        settings = data("plugin", "config", "rules", "--set", RULE)
        assert settings["rules"][0]["name"] == "money"  # ty: ignore[not-subscriptable]
        jot("add", "Pay the invoice")
        assert "delivered" in jot("plugin", "run", "--once")
        assert data("show", "1")["jot"]["labels"] == ["finance"]  # ty: ignore[not-subscriptable]
        assert "enabled" in jot("plugin", "ls")
        shown = jot("plugin", "show", "rules")
        assert '"money"' in shown
        assert jot("plugin", "failures", "rules") == "No failures\n"
        assert data("plugin", "failures", "rules") == []
        assert '"money"' in jot("plugin", "config", "rules")
        assert "disabled" in jot("plugin", "disable", "rules")
        events = [e["kind"] for e in data("events")]  # ty: ignore[not-iterable]
        assert {"plugin.enabled", "plugin.configured", "plugin.disabled"} <= set(events)

    def test_config_from_stdin(self) -> None:
        """Settings can be piped in."""
        out = jot("plugin", "config", "rules", "--file", "-", stdin=RULE)
        assert '"money"' in out

    def test_errors(self) -> None:
        """Bad input and unknown ids map to exit codes."""
        jot("plugin", "config", "rules", "--set", "[1]", code=1)
        jot("plugin", "config", "rules", "--set", "{", code=1)
        jot("plugin", "config", "rules", "--set", '{"rules": 5}', code=1)
        jot("plugin", "enable", "nope", code=2)
        jot("plugin", "failures", "nope", code=2)
        jot("job", "cancel", "99", code=2)

    def test_jobs(self, home: JotHome) -> None:
        """Jobs list, cancel, and retry."""
        assert jot("job", "ls") == "No jobs\n"
        with Session.open(home) as session:
            host = PluginHost(session, PluginCatalog.discover())
            PluginContext(
                session, host.store, "rules", host.settings("rules"), clock=host.clock
            ).enqueue("noop", jot_id=None)
        assert "rules:noop" in jot("job", "ls", "--status", "queued")
        assert "cancelled" in jot("job", "cancel", "1")
        assert "queued" in jot("job", "retry", "1")
        jot("job", "retry", "1", code=3)
        listed = data("job", "ls", "--plugin", "rules")
        assert listed[0]["status"] == "queued"  # ty: ignore[not-subscriptable]

    def test_broken_plugin_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Broken plugins show their error in the listing."""
        from tests.plugins.conftest import Broken  # noqa: PLC0415

        monkeypatch.setattr(
            PluginCatalog, "discover", classmethod(lambda cls: cls.of(Broken))
        )
        assert "broken" in jot("plugin", "ls")
        assert jot("plugin", "enable", "broken", code=1) == ""

    def test_run_handles_last_error(self, home: JotHome) -> None:
        """A plugin's last error shows in the listing."""
        with Session.open(home) as session:
            host = PluginHost(session, PluginCatalog.discover())
            row = host.store.row("rules").model_copy(
                update={"enabled": True, "last_error": "event 1: boom"}
            )
            with session.db.write():
                host.store.save(row)
        assert "last error: event 1: boom" in jot("plugin", "ls")

    def test_run_interrupted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ctrl-C stops a foreground worker quietly."""

        def interrupt(*_args: object, **_kwargs: object) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr("jot.plugins.host.Worker.run", interrupt)
        assert jot("plugin", "run") == ""
