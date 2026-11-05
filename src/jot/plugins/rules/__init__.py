"""Deterministic rules: when a jot matches conditions, change it.

Rules are evaluated against the jot's current state after each event of the
kinds they listen to. Actions only write what differs, so re-running a rule is
harmless. A rule's own writes never re-trigger it.
"""

from __future__ import annotations

import re
from importlib.resources import files
from typing import TYPE_CHECKING, override

from pydantic import Field, JsonValue, field_validator

from jot.app.commands import JotPatch
from jot.core.models import Origin, Record, StatusCategory
from jot.plugins.api import Plugin

if TYPE_CHECKING:
    from jot.core.models import Event, Jot
    from jot.plugins.context import PluginContext


class Condition(Record):
    """Every given criterion must hold (empty lists match anything)."""

    kinds: list[str] = Field(default_factory=lambda: ["jot.created"], min_length=1)
    profiles: list[str] = Field(default_factory=list)
    categories: list[StatusCategory] = Field(default_factory=list)
    statuses: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    without_labels: list[str] = Field(default_factory=list)
    project: str | None = None
    text: str | None = None
    pattern: str | None = None

    @field_validator("pattern")
    @classmethod
    def compiles(cls, value: str | None) -> str | None:
        """Reject invalid regular expressions up front."""
        if value is not None:
            try:
                re.compile(value)
            except re.error as err:
                raise ValueError(f"invalid pattern: {err}") from err
        return value

    def matches(self, jot: Jot) -> bool:
        """Return whether a jot meets every criterion."""
        haystack = f"{jot.title}\n{jot.body}"
        checks = (
            not self.profiles or jot.profile in self.profiles,
            not self.categories or jot.category in self.categories,
            not self.statuses or jot.status in self.statuses,
            all(label in jot.labels for label in self.labels),
            not any(label in jot.labels for label in self.without_labels),
            self.project is None or jot.project == self.project,
            self.text is None or self.text.casefold() in haystack.casefold(),
            self.pattern is None or re.search(self.pattern, haystack) is not None,
        )
        return all(checks)


class Action(Record):
    """Changes to apply; only differences are written."""

    add_labels: list[str] = Field(default_factory=list)
    remove_labels: list[str] = Field(default_factory=list)
    status: str | None = None
    project: str | None = None
    fields: dict[str, JsonValue] = Field(default_factory=dict)
    comment: str | None = None


class Rule(Record):
    """A named condition and action."""

    name: str = Field(min_length=1)
    enabled: bool = True
    when: Condition = Field(default_factory=Condition)
    then: Action = Field(default_factory=Action)


class RulesSettings(Record):
    """The rule list, evaluated in order."""

    rules: list[Rule] = Field(default_factory=list)
    ignore_plugins: bool = True


class RulesPlugin(Plugin):
    """Apply user-defined rules to jots as they change."""

    id = "rules"
    name = "Rules"
    version = "1"
    description = "Label, move, file, or comment on jots that match conditions."
    settings_model = RulesSettings

    @override
    def handle(self, event: Event, context: PluginContext) -> None:
        """Run every enabled rule that listens to this event kind."""
        settings = context.settings
        if not isinstance(settings, RulesSettings) or event.jot_id is None:
            return
        if settings.ignore_plugins and event.origin == Origin.PLUGIN:
            return
        for rule in settings.rules:
            if not rule.enabled or event.kind not in rule.when.kinds:
                continue
            jot = context.queries.get(event.jot_id)
            if rule.when.matches(jot):
                self.apply(rule, jot, context)

    @staticmethod
    def apply(rule: Rule, jot: Jot, context: PluginContext) -> None:
        """Write the rule's action, skipping parts that are already true."""
        commands = context.commands(reason=f"rule: {rule.name}")
        action = rule.then
        add = [label for label in action.add_labels if label not in jot.labels]
        remove = [label for label in action.remove_labels if label in jot.labels]
        if add or remove:
            commands.label(jot.id, add=add, remove=remove)
        patch: dict[str, JsonValue] = {}
        if action.project is not None and action.project != (jot.project or ""):
            patch["project"] = action.project or None
        current = jot.metadata.get("fields")
        current = current if isinstance(current, dict) else {}
        fields = {k: v for k, v in action.fields.items() if current.get(k) != v}
        if fields:
            patch["fields"] = fields
        if patch:
            commands.update(jot.id, JotPatch.model_validate(patch))
        profile = context.session.profiles.get(jot.profile)
        known = {status.id for status in profile.statuses}
        if action.status and action.status != jot.status and action.status in known:
            commands.set_status(jot.id, action.status)
        if action.comment:
            commands.comment(jot.id, action.comment, kind="rules.note")

    @override
    def ui_module(self) -> str | None:
        """Return the settings page module."""
        return files(__package__ or __name__).joinpath("ui.js").read_text("utf-8")
