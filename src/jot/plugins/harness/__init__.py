"""Harness plugins: one router service over coding-agent runtimes.

``harness`` provides the ``harness@1`` service: it routes each request to an
enabled backend by action, applies the model policy, and caps permissions.
Backends (``harness-claude``, ``harness-codex``, ``harness-copilot``,
``harness-fake``) each provide ``harness.backend@1`` and need the router.
Other plugins call ``context.service("harness@1")`` and never see a vendor.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Annotated, ClassVar, override

import typer
from pydantic import BaseModel, Field

from jot.core.models import Record
from jot.plugins.api import Plugin
from jot.plugins.harness.api import (
    BACKEND,
    SERVICE,
    Harness,
    HarnessError,
    HarnessEvent,
    HarnessInfo,
    Permission,
    RunRequest,
    StructuredRequest,
    StructuredResult,
    Usage,
)
from jot.plugins.harness.backends import (
    ClaudeHarness,
    CliHarness,
    CliSettings,
    CodexHarness,
    CopilotHarness,
    FakeHarness,
    FakeSettings,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from jot.plugins.api import PluginCli
    from jot.plugins.context import PluginContext
    from jot.plugins.host import PluginHost

LEVELS = list(Permission)
CHECK_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
}


class Route(Record):
    """Which backend and model serve one action (e.g. ``triage``)."""

    harness: str | None = None
    model: str | None = None


class RouterSettings(Record):
    """Routing and policy for every harness request."""

    default: str | None = Field(
        default=None, description="Backend id; empty means the first available"
    )
    routes: dict[str, Route] = Field(default_factory=dict)
    allowed_models: list[str] = Field(
        default_factory=list, description="Explicit models must be listed (empty: any)"
    )
    blocked_models: list[str] = Field(default_factory=list)
    max_permission: Permission = Field(
        default=Permission.WORKSPACE_WRITE,
        description="The most access any run may get; full must be opted into",
    )


class Selection(Record):
    """The backend and model chosen for one request."""

    harness: str
    model: str | None = None


class CheckResult(Record):
    """Outcome of ``jot harness check``."""

    harness: str
    ok: bool
    seconds: float
    model: str | None = None
    usage: Usage | None = None
    error: str | None = None


class HarnessService:
    """The ``harness@1`` service: route, enforce policy, delegate."""

    def __init__(self, backends: list[Harness], settings: RouterSettings) -> None:
        self.backends = {backend.id: backend for backend in backends}
        self.settings = settings

    def infos(self) -> list[HarnessInfo]:
        """Describe every enabled backend, sorted by id."""
        return [self.backends[key].info() for key in sorted(self.backends)]

    def get(self, harness_id: str) -> Harness:
        """Return an enabled backend that can run here."""
        backend = self.backends.get(harness_id)
        if backend is None:
            enabled = ", ".join(sorted(self.backends)) or "none"
            raise HarnessError(
                f"Harness {harness_id!r} is not enabled (enabled: {enabled}); "
                f"run `jot plugin enable harness-{harness_id}`"
            )
        if problem := backend.problem():
            raise HarnessError(f"Harness {harness_id} cannot run: {problem}")
        return backend

    def select(
        self,
        action: str | None = None,
        harness: str | None = None,
        model: str | None = None,
    ) -> Selection:
        """Pick a backend and model: explicit, then route, then default."""
        route = self.settings.routes.get(action or "", Route())
        chosen = harness or route.harness or self.settings.default
        if chosen is None:
            chosen = next(
                (
                    key
                    for key in sorted(self.backends)
                    if not self.backends[key].problem()
                ),
                None,
            )
            if chosen is None:
                raise HarnessError(
                    "No harness is available; enable one, e.g. "
                    "`jot plugin enable harness-claude`"
                )
        self.get(chosen)
        selected = model or route.model
        self.check_model(selected)
        return Selection(harness=chosen, model=selected)

    def resumable(self, action: str | None = None) -> bool:
        """Whether the backend chosen for ``action`` can resume a session."""
        selection = self.select(action)
        return self.backends[selection.harness].capabilities().resume

    def check_model(self, model: str | None) -> None:
        """Refuse blocked models, and unlisted ones when an allow-list is set."""
        if model is None:
            return
        if model in self.settings.blocked_models:
            raise HarnessError(f"Model {model} is blocked by the harness policy")
        allowed = self.settings.allowed_models
        if allowed and model not in allowed:
            raise HarnessError(
                f"Model {model} is not in the allowed models: {', '.join(allowed)}"
            )

    def check_permission(self, permission: Permission) -> None:
        """Refuse runs above the configured ceiling."""
        ceiling = self.settings.max_permission
        if LEVELS.index(permission) > LEVELS.index(ceiling):
            raise HarnessError(
                f"Permission {permission.value} exceeds the harness policy "
                f"maximum {ceiling.value}"
            )

    def structured(
        self,
        request: StructuredRequest,
        *,
        action: str | None = None,
        harness: str | None = None,
    ) -> StructuredResult:
        """Answer with one JSON object from the selected backend."""
        selection = self.select(action, harness, request.model)
        backend = self.backends[selection.harness]
        if not backend.capabilities().structured:
            raise HarnessError(f"Harness {backend.id} has no structured mode")
        return backend.structured(request.model_copy(update={"model": selection.model}))

    def run(
        self,
        request: RunRequest,
        *,
        action: str | None = None,
        harness: str | None = None,
    ) -> Iterator[HarnessEvent]:
        """Stream an agentic run from the selected backend."""
        self.check_permission(request.permission)
        selection = self.select(action, harness, request.model)
        backend = self.backends[selection.harness]
        capabilities = backend.capabilities()
        if not capabilities.run:
            raise HarnessError(f"Harness {backend.id} has no run mode")
        if request.session_id and not capabilities.resume:
            raise HarnessError(f"Harness {backend.id} cannot resume sessions")
        backend.check_permission(request.permission)
        return backend.run(request.model_copy(update={"model": selection.model}))

    def check(self, harness_id: str) -> CheckResult:
        """Make one tiny structured call to prove a backend works end to end.

        Success means the round trip worked and the answer matched the schema.
        """
        start = time.perf_counter()
        try:
            result = self.structured(
                StructuredRequest(
                    prompt='Reply with {"ok": true}.', output_schema=CHECK_SCHEMA
                ),
                harness=harness_id,
            )
        except HarnessError as err:
            return CheckResult(
                harness=harness_id,
                ok=False,
                seconds=round(time.perf_counter() - start, 2),
                error=str(err),
            )
        return CheckResult(
            harness=harness_id,
            ok=True,
            seconds=round(time.perf_counter() - start, 2),
            model=result.model,
            usage=result.usage,
        )


class HarnessPlugin(Plugin):
    """Router for the ``harness@1`` service."""

    id = "harness"
    name = "Harness"
    version = "1"
    description = "Routes AI requests to an enabled agent runtime."
    listens = False
    provides = frozenset({SERVICE})
    settings_model = RouterSettings

    @override
    def service(self, name: str, context: PluginContext) -> HarnessService:
        """Build the router over the currently enabled backends."""
        settings = context.settings
        if not isinstance(settings, RouterSettings):  # pragma: no cover - host bug
            raise TypeError("harness settings have the wrong type")
        backends = [b for b in context.services(BACKEND) if isinstance(b, Harness)]
        return HarnessService(backends, settings)

    @classmethod
    @override
    def commands(cls, cli: PluginCli) -> typer.Typer:
        """``jot harness ls`` and ``jot harness check``."""
        return HarnessCommands(cli).app()


class BackendPlugin(Plugin):
    """A plugin that contributes one harness backend."""

    version = "1"
    listens = False
    provides = frozenset({BACKEND})
    requires = frozenset({SERVICE})

    @override
    def service(self, name: str, context: PluginContext) -> Harness:
        """Build the backend from this plugin's settings."""
        return self.build(context.settings)

    def build(self, settings: BaseModel) -> Harness:
        """Return the harness for these settings."""
        raise NotImplementedError


class CliBackendPlugin(BackendPlugin):
    """A backend that drives a signed-in agent CLI."""

    settings_model = CliSettings
    harness_class: ClassVar[type[CliHarness]] = CliHarness

    @override
    def build(self, settings: BaseModel) -> Harness:
        """Wrap the configured CLI."""
        if not isinstance(settings, CliSettings):  # pragma: no cover - host bug
            raise TypeError("backend settings have the wrong type")
        return self.harness_class(settings)


class ClaudePlugin(CliBackendPlugin):
    """Claude Code backend."""

    id = "harness-claude"
    name = "Claude Code harness"
    description = "Runs the signed-in Claude Code CLI."
    harness_class = ClaudeHarness


class CodexPlugin(CliBackendPlugin):
    """Codex backend."""

    id = "harness-codex"
    name = "Codex harness"
    description = "Runs the signed-in Codex CLI."
    harness_class = CodexHarness


class CopilotPlugin(CliBackendPlugin):
    """GitHub Copilot backend."""

    id = "harness-copilot"
    name = "Copilot harness"
    description = "Runs the signed-in GitHub Copilot CLI."
    harness_class = CopilotHarness


class FakePlugin(BackendPlugin):
    """Offline backend for tests and demos."""

    id = "harness-fake"
    name = "Fake harness"
    description = "Deterministic offline answers, for tests and demos."
    settings_model = FakeSettings

    @override
    def build(self, settings: BaseModel) -> Harness:
        """Return the scripted fake."""
        if not isinstance(settings, FakeSettings):  # pragma: no cover - host bug
            raise TypeError("backend settings have the wrong type")
        return FakeHarness(settings)


Json = Annotated[bool, typer.Option("--json", help="Emit JSON")]


class HarnessCommands:
    """``jot harness``: list backends and check that they work."""

    def __init__(self, cli: PluginCli) -> None:
        self.cli = cli

    def app(self) -> typer.Typer:
        """Build the command group."""
        app = typer.Typer(no_args_is_help=True, help="Inspect AI harnesses")
        app.command("ls")(self.ls)
        app.command("check")(self.check)
        return app

    @staticmethod
    def router(host: PluginHost) -> HarnessService:
        """Return the enabled router or explain how to enable it."""
        for service in host.services(SERVICE):
            if isinstance(service, HarnessService):
                return service
        raise HarnessError("The harness plugin is off; run `jot plugin enable harness`")

    @staticmethod
    def line(info: HarnessInfo) -> str:
        """One backend with its permission mapping."""
        state = "ready" if info.available else f"unavailable: {info.problem}"
        rows = [
            f"{info.id:<10} {info.label}  ({state}, usage {info.capabilities.usage})"
        ]
        rows += [
            f"  {permission.value:<16} {' '.join(args) or '(none)'}"
            for permission, args in info.permissions.items()
        ]
        return "\n".join(rows) + "\n"

    def ls(self, *, json: Json = False) -> None:
        """List enabled backends and how each maps the permission levels."""
        with self.cli.host(json=json) as host:
            infos = self.router(host).infos()
        self.cli.emit(infos, "".join(map(self.line, infos)) or "No harness enabled\n")

    def check(
        self,
        harness_id: Annotated[str, typer.Argument(help="Backend id, e.g. claude")],
        *,
        json: Json = False,
    ) -> None:
        """Make one tiny structured call and report whether it worked."""
        with self.cli.host(json=json) as host:
            result = self.router(host).check(harness_id)
        text = (
            f"{harness_id}: ok in {result.seconds}s"
            f" ({result.model or 'default model'})\n"
            if result.ok
            else f"{harness_id}: FAILED in {result.seconds}s: {result.error}\n"
        )
        self.cli.emit(result, text)
        if not result.ok:
            raise typer.Exit(1)
