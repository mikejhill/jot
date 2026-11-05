"""The deterministic rules plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError
from tests.plugins.conftest import USER, FakeClock, Recorder

from jot.app.commands import JotPatch
from jot.core.models import Origin, WriteContext
from jot.core.profiles import FieldSpec, FieldType
from jot.exceptions import PluginError
from jot.plugins.host import PluginCatalog, PluginHost
from jot.plugins.rules import Condition, RulesPlugin, RulesSettings

if TYPE_CHECKING:
    from jot.app.commands import Commands
    from jot.app.session import Session
    from jot.core.models import Metadata


@pytest.fixture
def rules(session: Session, clock: FakeClock) -> PluginHost:
    """A host with the rules plugin (plus the recorder) enabled."""
    host = PluginHost(session, PluginCatalog.of(RulesPlugin, Recorder), clock=clock)
    host.enable("rules", USER)
    return host


def configure(host: PluginHost, *rules: Metadata, **extra: object) -> None:
    """Replace the rule list."""
    host.configure("rules", {"rules": list(rules), **extra}, USER)  # ty: ignore[invalid-argument-type]


class TestRules:
    """Conditions and actions."""

    def test_labels_on_create(self, rules: PluginHost, commands: Commands) -> None:
        """A text rule labels matching new jots only."""
        configure(
            rules,
            {
                "name": "money",
                "when": {"text": "INVOICE"},
                "then": {"add_labels": ["finance"]},
            },
        )
        commands.create(title="Pay the invoice")
        commands.create(title="Walk the dog")
        rules.deliver()
        queries = rules.session.queries
        assert queries.get(1).labels == ["finance"]
        assert queries.get(2).labels == []
        event = queries.history(1)[-1]
        assert (event.actor, event.origin, event.reason) == (
            "plugin:rules",
            Origin.PLUGIN,
            "rule: money",
        )

    def test_every_action(
        self, rules: PluginHost, commands: Commands, session: Session
    ) -> None:
        """Labels, project, fields, status, and comment apply in one pass."""
        commands.create_project("work", "Work")
        configure(
            rules,
            {
                "name": "triage",
                "when": {
                    "kinds": ["jot.created"],
                    "profiles": ["task"],
                    "categories": ["open"],
                    "statuses": ["todo"],
                    "labels": ["inbox"],
                    "without_labels": ["keep"],
                    "pattern": r"^Fix\b",
                },
                "then": {
                    "add_labels": ["bug"],
                    "remove_labels": ["inbox"],
                    "project": "work",
                    "status": "doing",
                    "comment": "Triaged by rule",
                },
            },
        )
        commands.create(title="Fix login", profile="task", labels=["inbox"])
        rules.deliver()
        jot = session.queries.get(1)
        assert (jot.labels, jot.project, jot.status) == (["bug"], "work", "doing")
        comment = session.queries.comments(1)[0]
        assert (comment.body, comment.kind) == ("Triaged by rule", "rules.note")

    def test_clearing_project(
        self, rules: PluginHost, commands: Commands, session: Session
    ) -> None:
        """An empty project in the action clears the jot's project."""
        commands.create_project("work", "Work")
        configure(
            rules,
            {"name": "unfile", "when": {"project": "work"}, "then": {"project": ""}},
        )
        commands.create(title="a", project="work")
        rules.deliver()
        assert session.queries.get(1).project is None

    def test_only_differences_are_written(
        self, rules: PluginHost, commands: Commands, session: Session
    ) -> None:
        """A rule whose action already holds writes nothing."""
        configure(
            rules,
            {
                "name": "noop",
                "when": {"kinds": ["jot.created", "jot.updated"]},
                "then": {"add_labels": ["x"], "status": "open", "remove_labels": ["y"]},
            },
        )
        commands.create(title="a", labels=["x"])
        rules.deliver()
        assert [e.kind for e in session.queries.history(1)] == ["jot.created"]

    def test_unknown_status_is_ignored(
        self, rules: PluginHost, commands: Commands, session: Session
    ) -> None:
        """A status the jot's profile lacks is skipped, not an error."""
        configure(rules, {"name": "s", "then": {"status": "doing"}})
        commands.create(title="note")
        rules.deliver()
        assert session.queries.get(1).status == "open"
        assert rules.status("rules").last_error is None

    def test_disabled_rules_and_other_kinds(
        self, rules: PluginHost, commands: Commands, session: Session
    ) -> None:
        """Disabled rules and unlisted kinds do nothing."""
        configure(
            rules,
            {"name": "off", "enabled": False, "then": {"add_labels": ["a"]}},
            {
                "name": "upd",
                "when": {"kinds": ["jot.updated"]},
                "then": {"add_labels": ["b"]},
            },
        )
        commands.create(title="a")
        rules.deliver()
        assert session.queries.get(1).labels == []
        commands.update(1, JotPatch(body="x"))
        rules.deliver()
        assert session.queries.get(1).labels == ["b"]

    def test_ignores_other_plugins(
        self, rules: PluginHost, commands: Commands, session: Session
    ) -> None:
        """Changes made by plugins do not trigger rules unless asked."""
        configure(
            rules,
            {
                "name": "upd",
                "when": {"kinds": ["jot.updated"]},
                "then": {"add_labels": ["b"]},
            },
        )
        commands.create(title="a")
        plugin = session.commands(WriteContext(actor="plugin:x", origin=Origin.PLUGIN))
        plugin.update(1, JotPatch(body="x"))
        rules.deliver()
        assert session.queries.get(1).labels == []
        configure(
            rules,
            {
                "name": "upd",
                "when": {"kinds": ["jot.updated"]},
                "then": {"add_labels": ["b"]},
            },
            ignore_plugins=False,
        )
        plugin.update(1, JotPatch(body="y"))
        rules.deliver()
        assert session.queries.get(1).labels == ["b"]

    def test_field_rule(self, session: Session, clock: FakeClock) -> None:
        """Field actions write only changed values (profile fields validated)."""
        task = session.profiles.get("task")
        area = FieldSpec(type=FieldType.STRING)
        session.profiles.register(task.model_copy(update={"fields": {"area": area}}))
        host = PluginHost(session, PluginCatalog.of(RulesPlugin), clock=clock)
        host.enable("rules", USER)
        configure(host, {"name": "f", "then": {"fields": {"area": "ops"}}})
        commands = session.commands(USER)
        commands.create(title="a", profile="task")
        commands.create(title="b", profile="task", fields={"area": "ops"})
        host.deliver()
        assert session.queries.get(1).metadata["fields"] == {"area": "ops"}
        assert [e.kind for e in session.queries.history(2)] == ["jot.created"]

    def test_invalid_settings(self, rules: PluginHost) -> None:
        """Bad patterns and unknown keys are refused."""
        with pytest.raises(PluginError):
            configure(rules, {"name": "x", "when": {"pattern": "("}})
        with pytest.raises(PluginError):
            configure(rules, {"name": "x", "then": {"explode": True}})
        with pytest.raises(ValidationError):
            Condition(kinds=[])

    def test_ui_module(self) -> None:
        """The plugin ships a settings page module."""
        source = RulesPlugin().ui_module()
        assert source is not None
        assert "export default function setup" in source

    def test_wrong_settings_type_is_ignored(self, rules: PluginHost) -> None:
        """handle() tolerates a context without RulesSettings."""
        context = type("C", (), {"settings": object()})()
        RulesPlugin().handle(rules.session.queries.events(0, 1)[0], context)  # ty: ignore[invalid-argument-type]
        assert RulesSettings().rules == []
