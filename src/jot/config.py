"""Resolve the data home and load validated, typed local settings."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

from pydantic import Field, TypeAdapter, ValidationError

from jot.core.models import Flow
from jot.exceptions import ConfigurationError
from jot.harnesses import Action, HarnessConfig, Pin

DEFAULT_MODEL = "default"
ACTIONS = ("triage", "plan", "execute", "cleanup", "assist", "router")

DEFAULT_CONFIG = """[triage]
backend = "claude"
model = "default"

[drawdown]
default_flow = "planned"
backend = "claude"
concurrency = 1

[execution]
isolation = "worktree"
# Where task worktrees go: <worktree_root>/<repo>/<id>-<slug> on branch jot/<id>-<slug>.
# Empty = $JOT_HOME/worktrees.
worktree_root = ""

[server]
host = "127.0.0.1"
port = 8765

# Default model per backend and action; "default" = the provider's own default.
# Override per run with --model (CLI) or the model picker (UI).
[models.claude]
triage = "default"
plan = "default"
execute = "default"
cleanup = "default"

[models.codex]
triage = "default"
plan = "default"
execute = "default"
cleanup = "default"

[models.copilot]
triage = "default"
plan = "default"
execute = "default"
cleanup = "default"

[cleanup]
inbox_days = 30
ready_days = 60
done_days = 90
project_days = 120
"""

TRIAGE = """# Triage
Preserve the original note. Write a short actionable title and describe the
desired outcome, evidence, constraints, and acceptance criteria. Match projects
by slug, name, or aliases (a leading "orbit api -" style prefix usually names the
project); propose a new project only when the note clearly names one. Reuse
existing labels before inventing new ones and never create synonyms. Use 2-5
lowercase labels covering: the project/system, the topic (e.g. health-checks,
auth), the kind of work (feature, bug, research), and the change type (e.g.
code-change, config-change, docs). Do not guess sensitive facts.

Criticality rubric:
- critical: active outage, exploitable security issue, or imminent data loss.
- high: substantial user impact or a time-sensitive blocker with evidence.
- medium: useful improvement or ordinary defect without urgent impact.
- low: cosmetic polish, speculative ideas, or optional housekeeping.

Use feature, bug, chore, research, or idea based on the intended deliverable.
Keep uncertain captures in inbox. Mark ready only when scope and outcome are
clear enough to plan. Never claim that a guessed repository or deadline is known.
"""

DRAWDOWN = """# Drawdown
Select ready work by criticality multiplied by project priority and age; break
ties by nearest due date, then task id. Claim before working, heartbeat the lease,
and release on cancellation. Do not work a task held by another agent.

Refine the task into a bounded outcome with acceptance checks. Read task history,
project instructions, repository conventions, and existing implementations.
Ask: What user problem is solved? What is in scope? What must remain compatible?
Which evidence supports urgency? What are the acceptance checks and rollback?
Post unresolved questions as events instead of silently inventing requirements.

Planned flow: inspect read-only, post a concrete plan and questions, then stop at
awaiting_approval (or needs_input while questions block the plan). Execute only
after explicit approval. Direct flow: proceed within the authorized scope,
recording assumptions. In either flow, stop at needs_input with structured
questions when the owner's input is required to continue.
During execution use the configured isolation, preserve unrelated work, make
focused changes, run acceptance checks, and post evidence plus remaining risks.
Move to review when results are ready for a human; do not self-approve completion.
"""

CLEANUP = """# Cleanup
Propose review for untouched inbox tasks after inbox_days, ready tasks after
ready_days, and inactive projects after project_days. Propose archiving done
tasks after done_days. Age alone never proves a task is unwanted.
Compare likely duplicates by outcome and project; retain the stronger record
and link context rather than dropping useful notes. Flag missing repository
paths for correction. Exclude active leases and recently updated tasks.
Explain the reason and proposed action for every item. Nothing changes without
item-level approval. Prefer reversible archive or soft deletion; never purge
automatically. Respect deadlines and recurring work before marking anything stale.
"""


@dataclass(frozen=True, slots=True)
class TriageConfig:
    """Enrichment provider settings."""

    backend: str = "claude"
    model: str = "default"


@dataclass(frozen=True, slots=True)
class DrawdownConfig:
    """Default flow, provider, and worker cap."""

    default_flow: Flow = Flow.PLANNED
    backend: str = "claude"
    concurrency: Annotated[int, Field(gt=0)] = 1


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    """Repository isolation policy."""

    isolation: str = "worktree"
    worktree_root: str = ""


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """Local server binding."""

    host: str = "127.0.0.1"
    port: Annotated[int, Field(gt=0, le=65535)] = 8765


@dataclass(frozen=True, slots=True)
class CleanupConfig:
    """Staleness thresholds measured in days."""

    inbox_days: Annotated[int, Field(ge=0)] = 30
    ready_days: Annotated[int, Field(ge=0)] = 60
    done_days: Annotated[int, Field(ge=0)] = 90
    project_days: Annotated[int, Field(ge=0)] = 120


@dataclass(frozen=True, slots=True)
class ActionModels:
    """Default model per action for one backend ("default" = provider default)."""

    triage: str = DEFAULT_MODEL
    plan: str = DEFAULT_MODEL
    execute: str = DEFAULT_MODEL
    cleanup: str = DEFAULT_MODEL
    assist: str = DEFAULT_MODEL
    router: str = DEFAULT_MODEL


@dataclass(frozen=True, slots=True)
class Config:
    """Validated settings for the data home."""

    triage: TriageConfig = field(default_factory=TriageConfig)
    drawdown: DrawdownConfig = field(default_factory=DrawdownConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)
    models: dict[str, ActionModels] = field(default_factory=dict)

    harnesses: dict[str, HarnessConfig] = field(default_factory=dict)
    actions: dict[Action, str] = field(default_factory=dict)
    pins: list[Pin] = field(default_factory=list)

    def effective_harnesses(self) -> dict[str, HarnessConfig]:
        """Overlay named instances onto backward-compatible provider instances."""
        kinds = {
            "claude": "claude-sdk",
            "codex": "codex-cli",
            "copilot": "copilot-cli",
            "fake": "fake",
        }
        names = {
            "claude",
            "codex",
            "copilot",
            self.triage.backend,
            self.drawdown.backend,
        }
        result = {}
        for name in sorted(names):
            if name not in kinds:
                continue
            defaults = {
                action: self._legacy_model(name, action) or DEFAULT_MODEL
                for action in ACTIONS
            }
            result[name] = HarnessConfig.model_validate(
                {
                    "kind": kinds[name],
                    "label": name.title(),
                    "models": {"default": defaults},
                }
            )
        return result | self.harnesses

    def harness_for(self, action: str, override: str | None = None) -> str:
        """Resolve an action's configured harness, including the router fallback."""
        if override:
            return override
        selected = next(
            (value for key, value in self.actions.items() if key == action), None
        )
        if selected:
            return selected
        if action == "router":
            return self.harness_for("triage")
        return (
            self.triage.backend
            if action in {"triage", "cleanup"}
            else self.drawdown.backend
        )

    def resolve(
        self, action: str, harness: str | None = None, model: str | None = None
    ) -> tuple[str, str | None]:
        """Resolve explicit choices before action defaults and enforce policy."""
        name = self.harness_for(action, harness)
        configured = self.effective_harnesses().get(name)
        if configured is None:
            raise ConfigurationError(f"Unknown harness {name!r}")
        chosen = (model or "").strip() or next(
            (
                value
                for key, value in configured.models.default.items()
                if key == action
            ),
            DEFAULT_MODEL,
        )
        try:
            configured.validate_model(chosen)
        except ConfigurationError as err:
            raise ConfigurationError(f"Harness {name!r}: {err}") from err
        return name, None if chosen == DEFAULT_MODEL else chosen

    def validate_settings(self) -> None:
        """Check cross references and defaults before accepting configuration."""
        for action in ACTIONS:
            self.resolve(action)
        for name, harness in self.effective_harnesses().items():
            for model in harness.models.default.values():
                if not harness.models.permits(model):
                    raise ConfigurationError(
                        f"Harness {name!r}: default model {model!r} violates policy"
                    )
        for pin in self.pins:
            self.resolve("plan", pin.harness, pin.model)

    def model_for(
        self, backend: str, action: str, override: str | None = None
    ) -> str | None:
        """Resolve a model for a named harness with policy validation."""
        return self.resolve(action, backend, override)[1]

    def _legacy_model(
        self, backend: str, action: str, override: str | None = None
    ) -> str | None:
        """Resolve a model: explicit override, then config, then provider default.

        Returns None when the provider's own default should be used.
        """
        chosen = (override or "").strip()
        if not chosen:
            models = self.models.get(backend, ActionModels())
            by_action = {
                "triage": models.triage,
                "plan": models.plan,
                "execute": models.execute,
                "cleanup": models.cleanup,
                "assist": models.assist,
                "router": models.router,
            }
            chosen = by_action.get(action, DEFAULT_MODEL)
            if (
                action == "triage"
                and chosen == DEFAULT_MODEL
                and backend == self.triage.backend
            ):
                # Legacy [triage] model applies only to the legacy triage backend.
                chosen = self.triage.model
        return None if chosen in ("", DEFAULT_MODEL) else chosen


@dataclass(frozen=True, slots=True)
class JotHome:
    """Data paths and non-destructive initialization."""

    path: Path

    @classmethod
    def resolve(cls) -> JotHome:
        """Resolve JOT_HOME or the user's .jot directory."""
        override = os.environ.get("JOT_HOME")
        return cls(
            Path(override).expanduser().resolve() if override else Path.home() / ".jot"
        )

    @property
    def database(self) -> Path:
        """Return the SQLite path."""
        return self.path / "jot.db"

    def initialize(self) -> Config:
        """Create missing defaults and load configuration without overwriting edits."""
        try:
            for relative in ("instructions/projects", "backups"):
                (self.path / relative).mkdir(parents=True, exist_ok=True)
            defaults = {
                "config.toml": DEFAULT_CONFIG,
                "instructions/triage.md": TRIAGE,
                "instructions/drawdown.md": DRAWDOWN,
                "instructions/cleanup.md": CLEANUP,
                "instructions/routing.md": (
                    "# Routing\nChoose an enabled harness and permitted model. "
                    "Prefer inexpensive models for routine chores and stronger "
                    "models for critical planning or complex execution. "
                    "Explain the choice briefly.\n"
                ),
            }
            for relative, content in defaults.items():
                self._write_missing(self.path / relative, content)
            with (self.path / "config.toml").open("rb") as stream:
                config = TypeAdapter(Config).validate_python(tomllib.load(stream))
            config.validate_settings()
        except (OSError, tomllib.TOMLDecodeError, ValidationError) as err:
            raise ConfigurationError(
                f"Cannot load Jot home {self.path}: {err}"
            ) from err
        else:
            return config

    @staticmethod
    def _write_missing(path: Path, content: str) -> None:
        """Use exclusive creation so concurrent initialization preserves edits."""
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(content)
        except FileExistsError:
            return
