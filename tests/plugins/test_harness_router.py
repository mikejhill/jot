"""The harness router, service dependencies, and ``jot harness`` commands."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, override

import pytest
from tests.cli.test_cli import data, jot
from tests.plugins.conftest import USER
from tests.plugins.test_harness import Limited

from jot.exceptions import PluginError
from jot.plugins.api import Plugin
from jot.plugins.harness import (
    SERVICE,
    ClaudePlugin,
    CodexPlugin,
    CopilotPlugin,
    FakePlugin,
    HarnessPlugin,
    HarnessService,
    RouterSettings,
)
from jot.plugins.harness.api import (
    BACKEND,
    EventKind,
    HarnessError,
    Permission,
    RunRequest,
    StructuredRequest,
)
from jot.plugins.harness.backends import FakeHarness, FakeSettings
from jot.plugins.host import PluginCatalog, PluginHost

if TYPE_CHECKING:
    from pathlib import Path

    from jot.app.commands import Commands
    from jot.app.session import Session
    from jot.core.models import Metadata
    from jot.plugins.context import PluginContext

PLUGINS = (HarnessPlugin, ClaudePlugin, CodexPlugin, CopilotPlugin, FakePlugin)


class Consumer(Plugin):
    """A plugin that needs the harness and asks it on every created jot."""

    id = "consumer"
    events = frozenset({"jot.created"})
    requires = frozenset({SERVICE})

    @override
    def handle(self, event: object, context: PluginContext) -> None:
        """Ask the routed harness and store its answer."""
        service = context.service(SERVICE)
        assert isinstance(service, HarnessService)
        result = service.structured(StructuredRequest(prompt="x"), action="triage")
        context.state.set("answer", result.value)


@pytest.fixture
def host(session: Session) -> PluginHost:
    """A host over the harness plugins and a consumer."""
    return PluginHost(session, PluginCatalog.of(*PLUGINS, Consumer))


def router(host: PluginHost) -> HarnessService:
    """The enabled router."""
    service = host.services(SERVICE)[0]
    assert isinstance(service, HarnessService)
    return service


def configure(host: PluginHost, plugin_id: str, settings: Metadata) -> None:
    """Save settings for a plugin."""
    host.configure(plugin_id, settings, USER)


class TestDependencies:
    """Services wire plugins together and guard enable and disable."""

    def test_backend_needs_the_router(self, host: PluginHost) -> None:
        """Enabling a backend first explains what to enable."""
        with pytest.raises(PluginError, match=r"requires harness@1 \(enable harness\)"):
            host.enable("harness-fake", USER)
        assert host.status("harness-fake").missing == [SERVICE]
        host.enable("harness", USER)
        assert host.status("harness-fake").missing == []
        assert host.enable("harness-fake", USER).enabled

    def test_router_cannot_be_disabled_under_dependents(self, host: PluginHost) -> None:
        """Dependents must go first; then the router can be disabled."""
        host.enable("harness", USER)
        host.enable("harness-fake", USER)
        with pytest.raises(PluginError, match="Disable harness-fake first"):
            host.disable("harness", USER)
        host.disable("harness-fake", USER)
        assert not host.disable("harness", USER).enabled

    def test_status_lists_services(self, host: PluginHost) -> None:
        """Status shows what a plugin provides and requires."""
        status = host.status("harness-claude")
        assert status.provides == [BACKEND]
        assert status.requires == [SERVICE]
        assert host.status("harness").provides == [SERVICE]

    def test_service_without_provider(self, host: PluginHost) -> None:
        """Asking for a missing service is a plugin error."""
        context = host.context_for("consumer")
        assert context.services(SERVICE) == []
        with pytest.raises(PluginError, match="No enabled plugin provides harness@1"):
            context.service(SERVICE)

    def test_service_only_plugins_skip_the_feed(
        self, host: PluginHost, commands: Commands
    ) -> None:
        """Router and backends never consume events or show lag."""
        host.enable("harness", USER)
        host.enable("harness-fake", USER)
        before = host.status("harness").cursor
        commands.create(title="one")
        assert host.deliver() == 0
        assert host.status("harness").lag == 0
        assert host.status("harness").cursor == before

    def test_consumer_uses_the_routed_backend(
        self, host: PluginHost, commands: Commands
    ) -> None:
        """A dependent plugin reaches the fake through the router."""
        host.enable("harness", USER)
        host.enable("harness-fake", USER)
        configure(host, "harness-fake", {"canned": {"label": "idea"}})
        host.enable("consumer", USER)
        commands.create(title="one")
        host.deliver()
        context = host.context_for("consumer")
        assert context.state.get("answer") == {"label": "idea"}

    def test_default_service_is_not_implemented(self) -> None:
        """Plugins that provide nothing refuse service lookups."""
        with pytest.raises(NotImplementedError):
            Consumer().service("x@1", None)  # ty: ignore[invalid-argument-type]

    def test_invalid_service_names(self) -> None:
        """Service names need a major version."""

        class Bad(Plugin):
            id = "bad"
            provides = frozenset({"harness"})

        with pytest.raises(ValueError, match="invalid service name"):
            Bad.check()


def service(backends: list[str] | None = None, **settings: object) -> HarnessService:
    """A router over fake backends with the given ids."""
    harnesses = []
    for backend_id in backends or ["fake"]:
        harness = FakeHarness(FakeSettings(canned={"from": backend_id}))
        harness.id = backend_id
        harnesses.append(harness)
    return HarnessService(harnesses, RouterSettings.model_validate(settings))


class TestRouting:
    """Explicit choice, then route, then default, then first available."""

    def test_order_of_precedence(self) -> None:
        """Each layer overrides the next."""
        router = service(
            ["a", "b", "c"],
            default="b",
            routes={"triage": {"harness": "c", "model": "small"}},
        )
        assert router.select().harness == "b"
        assert router.select("triage").model_dump() == {
            "harness": "c",
            "model": "small",
        }
        assert router.select("triage", "a", "big").model_dump() == {
            "harness": "a",
            "model": "big",
        }
        assert router.select("other").harness == "b"
        assert service(["b", "a"]).select().harness == "a"

    def test_first_available_skips_broken(self, tmp_path: Path) -> None:
        """Backends that cannot run are passed over and refused by name."""
        broken = ClaudePlugin().build(
            ClaudePlugin.settings_model.model_validate(
                {"command": [str(tmp_path / "nope")]}
            )
        )
        router = HarnessService([broken, FakeHarness(FakeSettings())], RouterSettings())
        assert router.select().harness == "fake"
        with pytest.raises(HarnessError, match="claude cannot run"):
            router.select(harness="claude")
        with pytest.raises(HarnessError, match="No harness is available"):
            HarnessService([broken], RouterSettings()).select()

    def test_unknown_harness(self) -> None:
        """Naming a disabled backend says how to enable it."""
        with pytest.raises(HarnessError, match="harness-codex"):
            service().select(harness="codex")
        with pytest.raises(HarnessError, match="enabled: none"):
            HarnessService([], RouterSettings(default="x")).select()

    def test_model_policy(self) -> None:
        """Blocked and unlisted models are refused; the default model passes."""
        router = service(allowed_models=["ok-model"], blocked_models=["bad"])
        assert router.select(model="ok-model").model == "ok-model"
        assert router.select().model is None
        with pytest.raises(HarnessError, match="blocked"):
            router.select(model="bad")
        with pytest.raises(HarnessError, match="not in the allowed"):
            router.select(model="other")
        routed = service(routes={"x": {"model": "bad"}}, blocked_models=["bad"])
        with pytest.raises(HarnessError, match="blocked"):
            routed.structured(StructuredRequest(prompt="q"), action="x")

    def test_structured_passes_the_model(self) -> None:
        """The routed model reaches the backend."""
        router = service(routes={"triage": {"model": "small"}})
        result = router.structured(StructuredRequest(prompt="q"), action="triage")
        assert result.model == "small"
        assert result.value == {"from": "fake"}

    def test_permission_ceiling(self, tmp_path: Path) -> None:
        """Full access needs an explicit policy; lower levels pass."""
        request = RunRequest(prompt="go", cwd=str(tmp_path), permission=Permission.FULL)
        with pytest.raises(HarnessError, match="exceeds the harness policy"):
            service().run(request)
        events = list(service(max_permission="full").run(request))
        assert events[-1].kind == EventKind.RESULT
        strict = service(max_permission="read_only")
        with pytest.raises(HarnessError, match="workspace_write exceeds"):
            strict.run(
                request.model_copy(update={"permission": Permission.WORKSPACE_WRITE})
            )

    def test_capability_checks(self, tmp_path: Path) -> None:
        """Modes and resume the backend lacks are refused before running."""
        router = HarnessService([Limited()], RouterSettings())
        with pytest.raises(HarnessError, match="no structured mode"):
            router.structured(StructuredRequest(prompt="q"))
        with pytest.raises(HarnessError, match="cannot resume"):
            router.run(RunRequest(prompt="q", cwd=str(tmp_path), session_id="s"))
        with pytest.raises(HarnessError, match="does not support workspace_write"):
            router.run(
                RunRequest(
                    prompt="q",
                    cwd=str(tmp_path),
                    permission=Permission.WORKSPACE_WRITE,
                )
            )
        no_run = Limited()
        no_run.capabilities = lambda: Limited.capabilities(no_run).model_copy(  # ty: ignore[invalid-assignment]
            update={"run": False}
        )
        with pytest.raises(HarnessError, match="no run mode"):
            HarnessService([no_run], RouterSettings()).run(
                RunRequest(prompt="q", cwd=str(tmp_path))
            )

    def test_check(self) -> None:
        """A check reports success or the error, never raises."""
        assert service().check("fake").ok
        failed = service().check("codex")
        assert not failed.ok
        assert failed.error is not None
        assert "not enabled" in failed.error

    def test_settings_are_validated(self, host: PluginHost) -> None:
        """Bad router settings are refused on save."""
        with pytest.raises(PluginError):
            configure(host, "harness", {"max_permission": "root"})


@pytest.mark.usefixtures("home")
class TestHarnessCommands:
    """``jot harness`` is mounted from the installed plugin."""

    def test_ls_and_check(self) -> None:
        """List backends with their permission mapping and check one."""
        assert "harness" in jot("--help")
        assert "plugin enable harness" in jot("harness", "ls", code=1) + jot(
            "harness", "ls", "--json", code=1
        )
        jot("plugin", "enable", "harness")
        assert jot("harness", "ls") == "No harness enabled\n"
        jot("plugin", "enable", "harness-fake")
        listing = jot("harness", "ls")
        assert "fake       Fake (offline)  (ready, usage measured)" in listing
        assert "--fake-permission=full" in listing
        infos = data("harness", "ls")
        assert isinstance(infos, list)
        assert infos[0]["id"] == "fake"
        assert jot("harness", "check", "fake").startswith("fake: ok in ")
        checked = data("harness", "check", "fake")
        assert checked["ok"] is True  # ty: ignore[not-subscriptable]
        failed = jot("harness", "check", "codex", code=1)
        assert "codex: FAILED" in failed
        jot("plugin", "config", "harness-fake", "--set", json.dumps({"fail": True}))
        assert "configured to fail" in jot("harness", "check", "fake", code=1)

    def test_unavailable_backend_is_listed(self, tmp_path: Path) -> None:
        """A backend whose CLI is missing shows why."""
        jot("plugin", "enable", "harness")
        jot("plugin", "enable", "harness-codex")
        settings = json.dumps({"command": [str(tmp_path / "missing-codex")]})
        jot("plugin", "config", "harness-codex", "--set", settings)
        assert "unavailable:" in jot("harness", "ls")
