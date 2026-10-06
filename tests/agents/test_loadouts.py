"""Per-kind capability isolation and provider discovery through fake boundaries."""

from __future__ import annotations

import asyncio
import json
import tomllib
from collections.abc import AsyncIterator
from contextlib import nullcontext
from pathlib import Path

import pytest
from claude_agent_sdk import ClaudeAgentOptions, SystemMessage
from tests.agents.conftest import ProcessScript

from jot.agents import claude as claude_module
from jot.agents import copilot as copilot_module
from jot.agents.base import AgentError, AgentMode, RunRequest
from jot.agents.claude import ClaudeBackend
from jot.agents.codex import CodexBackend
from jot.agents.copilot import CopilotBackend
from jot.harnesses import HarnessConfig, HarnessOptions, Loadout


class TestLoadouts:
    """Translate lean defaults and selected capabilities to actual options."""

    def test_claude(self, tmp_path: Path) -> None:
        """Claude is strict by default; explicit MCP subsets need definitions."""
        agent = ClaudeBackend()
        request = RunRequest("go", tmp_path, AgentMode.PLAN, system="base")
        options = agent._run_options(request)
        assert options.skills == []
        assert options.setting_sources == ["project"]
        assert "strict-mcp-config" in options.extra_args
        assert options.plugins == []
        harness = HarnessConfig(
            kind="claude-sdk",
            instructions={"plan": "Extra"},
            loadout={
                "plan": Loadout(
                    skills=["jot"],
                    mcp=["docs"],
                    plugins=["/plugin"],
                    project_instructions=False,
                )
            },
            config=HarnessOptions(mcp_servers={"docs": {"command": "docs-cli"}}),
        )
        agent.configure(harness, "plan")
        options = agent._run_options(request)
        assert options.skills == ["jot"]
        assert options.setting_sources == []
        assert options.plugins == [{"type": "local", "path": "/plugin"}]
        assert "Extra" in str(options.system_prompt)
        assert json.loads(options.extra_args["mcp-config"] or "{}")["mcpServers"] == {
            "docs": {"command": "docs-cli"}
        }
        agent.loadout = Loadout(skills="all", mcp="all")
        assert "strict-mcp-config" not in agent._loadout_args()
        agent.loadout = Loadout(mcp=["unknown"])
        with pytest.raises(AgentError, match="definitions required"):
            agent._loadout_args()

    def test_codex(self) -> None:
        """Codex preserves repo docs, disables account features, and emits TOML."""
        agent = CodexBackend()
        args = agent._loadout_args()
        assert "--ignore-user-config" in args
        assert "project_doc_max_bytes=0" not in args
        assert "skip_host_skill_discovery" in args
        agent.configure(
            HarnessConfig(
                kind="codex-cli",
                loadout={
                    "execute": Loadout(
                        skills=["/skills/jot"], mcp=["docs"], project_instructions=False
                    )
                },
                config=HarnessOptions(
                    disable_features=["browser_use"],
                    mcp_servers={"docs": {"command": "docs-cli"}},
                ),
            ),
            "execute",
        )
        args = agent._loadout_args()
        assert "browser_use" in args
        assert "project_doc_max_bytes=0" in args
        for index, flag in enumerate(args):
            if flag == "-c":
                assert tomllib.loads(args[index + 1])
        agent.loadout = Loadout(mcp=["unknown"])
        with pytest.raises(AgentError, match="definition required"):
            agent._loadout_args()
        agent.loadout = Loadout(mcp="all", skills="all")
        assert "--ignore-user-config" not in agent._loadout_args()
        assert asyncio.run(agent.discover()) == {"skills": [], "mcp": [], "plugins": []}

    def test_copilot(self) -> None:
        """Copilot maps MCP exclusions, plugins, and instruction toggles."""
        agent = CopilotBackend()
        assert "--disable-builtin-mcps" in agent._loadout_args()
        assert "--no-custom-instructions" not in agent._loadout_args()
        agent.configure(
            HarnessConfig(
                kind="copilot-cli",
                loadout={
                    "plan": Loadout(
                        mcp=["docs"], plugins=["/plugin"], project_instructions=False
                    )
                },
                config=HarnessOptions(mcp_servers={"docs": {}, "other": {}}),
            ),
            "plan",
        )
        args = agent._loadout_args()
        assert args == [
            "--disable-builtin-mcps",
            "--disable-mcp-server",
            "other",
            "--no-custom-instructions",
            "--plugin-dir",
            "/plugin",
        ]
        agent.loadout = Loadout(mcp="all")
        assert agent._loadout_args() == []

    def test_account_mcp_exclusions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lean disables every named account connector, not a wildcard server."""
        monkeypatch.setenv("COPILOT_HOME", str(tmp_path))
        (tmp_path / "mcp-config.json").write_text(
            '{"mcpServers":{"account-docs":{"command":"docs"}}}', encoding="utf-8"
        )
        assert CopilotBackend()._loadout_args() == [
            "--disable-builtin-mcps",
            "--disable-mcp-server",
            "account-docs",
        ]
        agent = CopilotBackend()
        agent.configure(
            HarnessConfig(
                kind="copilot-cli",
                loadout={"plan": Loadout(mcp=["selected"])},
                config=HarnessOptions(
                    mcp_servers={"selected": {"command": "selected-cli"}}
                ),
            ),
            "plan",
        )
        assert "--additional-mcp-config" in agent._loadout_args()


class DiscoveryScript:
    """An SDK init message without launching a provider or spending tokens."""

    @staticmethod
    async def query(
        *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[SystemMessage]:
        """Validate discovery is cheap and return loaded capability names."""
        assert prompt == "Reply OK."
        assert options.max_turns == 1
        yield SystemMessage(
            subtype="init",
            data={
                "skills": ["jot"],
                "mcp_servers": [{"name": "docs"}],
                "plugins": [{"name": "local-plugin"}],
            },
        )


class TestDiscovery:
    """Provider init events become uniform capability inventories."""

    def test_claude(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Claude discovery parses all three init fields."""
        monkeypatch.setattr(claude_module, "query", DiscoveryScript.query)
        assert asyncio.run(ClaudeBackend().discover()) == {
            "skills": ["jot"],
            "mcp": ["docs"],
            "plugins": ["local-plugin"],
        }

    def test_copilot(
        self,
        process_script: ProcessScript,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Copilot discovery consumes loaded skills and MCP events."""
        monkeypatch.setattr(
            copilot_module.tempfile,
            "TemporaryDirectory",
            lambda **_: nullcontext(str(tmp_path)),
        )
        process_script.events = [
            {"type": "session.skills_loaded", "data": {"skills": ["jot"]}},
            {
                "type": "session.mcp_servers_loaded",
                "data": {"mcpServers": [{"name": "docs"}]},
            },
        ]
        assert asyncio.run(CopilotBackend().discover()) == {
            "skills": ["jot"],
            "mcp": ["docs"],
            "plugins": [],
        }
