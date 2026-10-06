"""Harness compatibility, policy enforcement, persistence, and routing tests."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from pydantic import TypeAdapter
from typer.testing import CliRunner

from jot.agents.base import AgentMode
from jot.agents.fake import FakeBackend
from jot.agents.registry import BackendRegistry
from jot.cli import CommandLine
from jot.config import ACTIONS, Config, JotHome
from jot.core.models import EventKind, Run, Status, Task
from jot.core.workflow import Workflow
from jot.db.connection import Database
from jot.db.migrations import Migrations
from jot.db.repository import TaskRepository
from jot.exceptions import ConfigurationError
from jot.harnesses import HarnessConfig, Loadout, ModelPolicy, Pin
from jot.services.bus import EventBus
from jot.services.enrich import EnrichService
from jot.services.routing import Router
from jot.services.runner import RunService
from jot.services.settings import SettingsStore
from jot.services.workspace import Workspace


class TestHarnessPolicy:
    """One resolution contract for explicit picks, defaults, and legacy config."""

    def test_legacy_and_multiple(self) -> None:
        """Legacy tables still work alongside several instances of one kind."""
        config = TypeAdapter(Config).validate_python(
            {
                "triage": {"backend": "codex", "model": "old"},
                "drawdown": {"backend": "claude"},
                "models": {"claude": {"plan": "opus"}},
                "harnesses": {
                    "quick": {
                        "kind": "codex-cli",
                        "models": {"default": {"plan": "luna"}},
                    }
                },
                "actions": {"plan": "quick"},
            }
        )
        assert config.resolve("plan") == ("quick", "luna")
        assert config.resolve("plan", "claude") == ("claude", "opus")
        assert config.resolve("plan", "quick", "explicit") == ("quick", "explicit")
        assert config.resolve("triage") == ("codex", "old")
        assert config.harness_for("router") == "codex"
        assert config.resolve("execute") == ("claude", None)
        assert config.model_for("claude", "execute", "default") is None

    def test_policy(self) -> None:
        """Deny wins; unknown and disabled harnesses fail with clear errors."""
        harness = HarnessConfig(
            kind="fake", models=ModelPolicy(allowed=["ok", "no"], disallowed=["no"])
        )
        config = Config(harnesses={"custom": harness})
        assert config.resolve("plan", "custom", "ok") == ("custom", "ok")
        for model in ("no", "other", "default"):
            with pytest.raises(ConfigurationError, match="policy"):
                config.resolve("plan", "custom", model)
        with pytest.raises(ConfigurationError, match="Unknown harness"):
            config.resolve("plan", "missing")
        harness.enabled = False
        with pytest.raises(ConfigurationError, match="disabled"):
            config.resolve("plan", "custom", "ok")

    def test_lean_actions(self) -> None:
        """Structured phases cannot opt out of lean; project docs stay on otherwise."""
        harness = HarnessConfig(
            kind="fake", loadout={"triage": Loadout(skills="all", mcp="all")}
        )
        for action in ("triage", "cleanup", "router"):
            assert harness.for_action(action) == Loadout(project_instructions=False)
        for action in ("plan", "execute", "assist"):
            assert harness.for_action(action) == Loadout()

    def test_invalid_cross_references(self) -> None:
        """Invalid action defaults and pins are rejected before persistence."""
        with pytest.raises(ConfigurationError, match="Unknown harness"):
            Config(
                pins=[Pin(label="bad", harness="absent", model="x")]
            ).validate_settings()
        with pytest.raises(ConfigurationError, match="violates policy"):
            Config(
                harnesses={
                    "extra": HarnessConfig(
                        kind="fake",
                        models=ModelPolicy(disallowed=["x"], default={"assist": "x"}),
                    )
                }
            ).validate_settings()


class TestSettingsStore:
    """Round trips preserve existing comments and reject invalid writes."""

    def test_round_trip(self, home: JotHome) -> None:
        """Editing a nested model retains comments and unrelated settings."""
        path = home.path / "config.toml"
        path.write_text(
            "# owner comment\n[server]\nport=8888 # binding\n"
            '[harnesses.extra]\nkind="fake" # runtime\nlabel="Old" # label\n',
            encoding="utf-8",
        )
        store = SettingsStore(home)
        config = store.save(
            {
                "harnesses": {"extra": {"kind": "fake", "label": "New"}},
                "pins": [
                    {
                        "label": "Fast",
                        "harness": "extra",
                        "model": "m",
                        "actions": ["plan"],
                    }
                ],
            }
        )
        assert config.server.port == 8888
        text = path.read_text(encoding="utf-8")
        for comment in ("# owner comment", "# binding", "# runtime", "# label"):
            assert comment in text
        assert home.initialize().pins[0].model == "m"
        assert SettingsStore.effective(config)["actions"]
        before = path.read_bytes()
        invalid: list[dict[str, object]] = [
            {"harnesses": {"extra": {"kind": "invalid"}}},
            {"actions": {"plan": "unknown"}},
            {"projects": {}},
        ]
        for changes in invalid:
            with pytest.raises(ConfigurationError):
                store.save(changes)
            assert path.read_bytes() == before


class TestRouting:
    """Canned routing results cannot bypass policy and always leave evidence."""

    @pytest.mark.parametrize("valid", [True, False])
    def test_route_and_fallback(
        self,
        home: JotHome,
        db: Database,
        monkeypatch: pytest.MonkeyPatch,
        *,
        valid: bool,
    ) -> None:
        """Valid Auto choices and invalid-choice fallbacks record reasons."""
        config = Config(
            harnesses={
                "test": HarnessConfig(
                    kind="fake", models=ModelPolicy(allowed=["small", "default"])
                )
            },
            actions={"router": "test", "plan": "test"},
            pins=[Pin(label="Small", harness="test", model="small", actions=["plan"])],
        )
        task = TaskRepository(db).create(Task(title="A chore"))
        monkeypatch.setattr(
            FakeBackend,
            "canned",
            {
                "harness": "test",
                "model": "small" if valid else "blocked",
                "reason": "Routine work",
            },
        )
        router = Router(db, home, config)
        result = asyncio.run(router.resolve(task, "plan", "auto", None))
        assert result == ("test", "small" if valid else None)
        events = [
            e
            for e in TaskRepository(db).events.for_task(task.id)
            if e.kind is EventKind.ROUTING
        ]
        assert len(events) == 1
        assert events[0].body["harness"] == "test"
        assert events[0].body["input_tokens"] == 10
        assert (home.path / f"runs/routing-{task.id}.jsonl").is_file()
        assert ("Fallback" in str(events[0].body["reason"])) is not valid
        assert asyncio.run(router.resolve(task, "plan", "test", "small")) == (
            "test",
            "small",
        )

    def test_capture_choice(
        self, home: JotHome, db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Persisted capture overrides survive a later worker enrichment pass."""
        config = Config(harnesses={"custom": HarnessConfig(kind="fake")})
        task = TaskRepository(db).create(
            Task(title="note", raw_input="note"), harness="custom", model="chosen"
        )
        monkeypatch.setattr(FakeBackend, "canned", {"title": "Clean note"})
        asyncio.run(EnrichService(db, home, config).enrich(task.id))
        event = next(
            e
            for e in TaskRepository(db).events.for_task(task.id)
            if e.kind is EventKind.ENRICHED and "backend" in e.body
        )
        assert (event.body["backend"], event.body["model"]) == ("custom", "chosen")


class TestSettingsMigration:
    """Version five retains old runs and allows routing events."""

    def test_upgrade_v4(self, tmp_path: Path) -> None:
        """A genuine v4 schema gains nullable metadata without losing history."""
        connection = sqlite3.connect(tmp_path / "old.db")
        old = (
            Migrations.schema()
            .replace(", harness TEXT, loadout TEXT", "")
            .replace(",'routing'", "")
        )
        connection.executescript(old + "\nPRAGMA user_version=4;")
        connection.executescript("""
            INSERT INTO tasks(
                id,title,type,criticality,status,source,created_at,updated_at)
            VALUES(7,'Old task','chore','low','ready','cli','2026-01-01','2026-01-01');
            INSERT INTO runs(id,task_id,backend,phase,status,started_at)
            VALUES(9,7,'claude','plan','succeeded','2026-01-01');
            INSERT INTO task_events(id,task_id,ts,actor,kind,body)
            VALUES(11,7,'2026-01-01','owner','created','{}');
        """)
        Migrations.apply(connection)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
        assert {"harness", "loadout"} <= columns
        assert (
            "routing"
            in connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='task_events'"
            ).fetchone()[0]
        )
        assert connection.execute("SELECT id,harness,loadout FROM runs").fetchone() == (
            9,
            None,
            None,
        )
        assert connection.execute("SELECT id FROM task_events").fetchone()[0] == 11
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        connection.close()


class TestSettingsCommands:
    """CLI inventory, detail, discovery, and aliases stay offline with FakeBackend."""

    def test_commands(self, home: JotHome) -> None:
        """Commands expose configured harnesses and filtered global pins."""
        SettingsStore(home).save(
            {
                "harnesses": {"test": {"kind": "fake"}},
                "pins": [{"label": "Fast", "harness": "test", "model": "small"}],
            }
        )
        cli = CliRunner()
        app = CommandLine().app
        for args in (
            ["harness", "ls"],
            ["harness", "show", "test"],
            ["harness", "discover", "test"],
            ["pins", "ls"],
        ):
            result = cli.invoke(app, [*args, "--json"])
            assert result.exit_code == 0, result.output
        assert cli.invoke(app, ["harness", "show", "missing"]).exit_code == 1
        assert "--harness" in cli.invoke(app, ["plan", "--help"]).output
        assert (
            BackendRegistry.configured(home.initialize(), "plan", "test").name == "fake"
        )
        assert len(ACTIONS) == 6


class TestConfiguredRuns:
    """Verify model and loadout snapshots without launching a real git process."""

    def test_metadata_and_provider_default(
        self, home: JotHome, db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit provider default wins over the configured action model."""
        config = Config(
            harnesses={
                "test": HarnessConfig(
                    kind="fake", models=ModelPolicy(default={"plan": "configured"})
                )
            },
            actions={"plan": "test", "router": "test"},
        )
        task = TaskRepository(db).create(Task(title="A bounded task"))
        Workflow(db).move(task.id, Status.READY)

        async def workspace(
            _self: RunService, _run: Run, _phase: AgentMode
        ) -> Workspace:
            """Use an already isolated directory, with no subprocess boundary."""
            return Workspace(cwd=home.path)

        monkeypatch.setattr(RunService, "_workspace", workspace)

        async def scenario() -> Run:
            """Execute the complete fake plan lifecycle."""
            service = RunService(db, home, config, EventBus())
            run = await service.start(task.id, model="default")
            return await service.wait(run.id)

        run = asyncio.run(scenario())
        assert run.status == "succeeded"
        assert run.harness == "test"
        assert run.model == "default"
        log = RunService(db, home, config, EventBus()).log(run.id)
        assert (
            next(line for line in log if line["kind"] == "usage")["model"]
            == "fake-model"
        )
        assert run.loadout == Loadout().model_dump_json()
