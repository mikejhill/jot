"""Data-home defaults, non-destructive initialization, and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from jot.config import JotHome
from jot.core.models import Flow
from jot.exceptions import ConfigurationError


class TestJotHome:
    """Home initialization preserves user instructions and settings."""

    def test_defaults(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default and explicit homes resolve and create useful guidance."""
        monkeypatch.delenv("JOT_HOME", raising=False)
        assert JotHome.resolve().path == Path.home() / ".jot"
        monkeypatch.setenv("JOT_HOME", str(tmp_path))
        home = JotHome.resolve()
        config = home.initialize()
        assert config.drawdown.default_flow == Flow.PLANNED
        assert config.server.port == 8765
        assert (tmp_path / "instructions/projects").is_dir()
        assert (tmp_path / "backups").is_dir()
        for name in ("triage", "drawdown", "cleanup"):
            assert (
                len((tmp_path / f"instructions/{name}.md").read_text(encoding="utf-8"))
                > 400
            )
        custom = tmp_path / "instructions/triage.md"
        custom.write_text("custom", encoding="utf-8")
        home.initialize()
        assert custom.read_text(encoding="utf-8") == "custom"

    @pytest.mark.parametrize(
        "content",
        [
            "[drawdown]\nconcurrency=0",
            '[drawdown]\ndefault_flow="wrong"',
            "[server]\nport=99999",
            "invalid [",
        ],
    )
    def test_invalid_config(self, home: JotHome, content: str) -> None:
        """Invalid TOML or config types produce a ConfigurationError."""
        (home.path / "config.toml").write_text(content, encoding="utf-8")
        with pytest.raises(ConfigurationError, match="Cannot load"):
            home.initialize()
