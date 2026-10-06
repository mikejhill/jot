"""Typed harness policies, action loadouts, and global shortcuts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from jot.exceptions import ConfigurationError

type Action = Literal["triage", "plan", "execute", "cleanup", "assist", "router"]
type Selection = list[str] | Literal["all"]
KINDS = {
    "claude-sdk": "claude",
    "codex-cli": "codex",
    "copilot-cli": "copilot",
    "fake": "fake",
}


class SettingsRecord(BaseModel):
    """Reject misspelled settings rather than silently ignoring them."""

    model_config = ConfigDict(extra="forbid")


class Loadout(SettingsRecord):
    """Account capabilities explicitly enabled for one action."""

    skills: Selection = Field(default_factory=list)
    mcp: Selection = Field(default_factory=list)
    plugins: list[str] = Field(default_factory=list)
    project_instructions: bool = True


class ModelPolicy(SettingsRecord):
    """Allowed models and defaults; exclusions always take precedence."""

    allowed: list[str] = Field(default_factory=list)
    disallowed: list[str] = Field(default_factory=list)
    default: dict[Action, str] = Field(default_factory=dict)

    def permits(self, model: str) -> bool:
        """Test a concrete model or the explicit provider-default sentinel."""
        return model not in self.disallowed and (
            not self.allowed or model in self.allowed
        )


class HarnessOptions(SettingsRecord):
    """Provider feature switches and explicitly configured MCP definitions."""

    disable_features: list[str] = Field(default_factory=list)
    mcp_servers: dict[str, dict[str, str | list[str]]] = Field(default_factory=dict)


class HarnessConfig(SettingsRecord):
    """One named instance of a supported runtime kind."""

    kind: Literal["claude-sdk", "codex-cli", "copilot-cli", "fake"]
    label: str = ""
    enabled: bool = True
    models: ModelPolicy = Field(default_factory=ModelPolicy)
    instructions: dict[Action, str] = Field(default_factory=dict)
    loadout: dict[Action, Loadout] = Field(default_factory=dict)
    config: HarnessOptions = Field(default_factory=HarnessOptions)

    def for_action(self, action: str) -> Loadout:
        """Force structured phases lean and retain repo instructions otherwise."""
        if action in {"triage", "cleanup", "router"}:
            return Loadout(project_instructions=False)
        return next(
            (value for key, value in self.loadout.items() if key == action), Loadout()
        )

    def validate_model(self, model: str | None) -> None:
        """Reject disabled runtimes and models outside the owner's policy."""
        if not self.enabled:
            raise ConfigurationError("Harness is disabled")
        if not self.models.permits(model or "default"):
            raise ConfigurationError(
                f"Model {model or 'default'!r} is prohibited by harness policy"
            )


class Pin(SettingsRecord):
    """A global shortcut optionally restricted to selected actions."""

    label: str = Field(min_length=1)
    harness: str = Field(min_length=1)
    model: str = Field(min_length=1)
    actions: list[Action] | None = None
