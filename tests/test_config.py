"""Data home initialization and profile configuration."""

from __future__ import annotations

import pytest

from jot.config import DEFAULT_CONFIG, JotHome
from jot.exceptions import ConfigurationError, InvalidInputError

BUG = """
default_profile = "bug"
[profiles.bug]
initial = "new"
statuses = [
  { id = "new", category = "open" },
  { id = "fixed", category = "done" },
]
transitions = [["new", "fixed"]]
plugins = ["triage"]
[profiles.bug.fields.severity]
type = "enum"
options = ["low", "high"]
"""


class TestHome:
    """Initialization writes defaults once and loads settings."""

    def test_defaults(self, home: JotHome) -> None:
        """A new home gets the commented default config."""
        config = home.initialize()
        assert home.config_file.read_text(encoding="utf-8") == DEFAULT_CONFIG
        assert config.registry().get().id == "note"
        assert home.database.name == "jot.db"

    def test_keeps_edits(self, home: JotHome) -> None:
        """Existing settings are never overwritten."""
        home.path.mkdir(parents=True, exist_ok=True)
        home.config_file.write_text(BUG, encoding="utf-8")
        registry = home.initialize().registry()
        bug = registry.get()
        assert bug.id == "bug"
        assert bug.label == "Bug"
        assert bug.transitions == (("new", "fixed"),)
        assert bug.plugins == ("triage",)
        assert home.config_file.read_text(encoding="utf-8") == BUG

    @pytest.mark.parametrize("text", ["not = [toml", "unknown_key = 1"])
    def test_invalid_file(self, home: JotHome, text: str) -> None:
        """Broken or unknown settings fail clearly."""
        home.path.mkdir(parents=True, exist_ok=True)
        home.config_file.write_text(text, encoding="utf-8")
        with pytest.raises(ConfigurationError):
            home.initialize()

    def test_invalid_profile(self, home: JotHome) -> None:
        """An inconsistent profile is a configuration error."""
        home.path.mkdir(parents=True, exist_ok=True)
        home.config_file.write_text(
            '[profiles.x]\ninitial = "nope"\n'
            'statuses = [{id = "a", category = "open"}]',
            encoding="utf-8",
        )
        with pytest.raises(ConfigurationError, match="Profile 'x'"):
            home.initialize().registry()

    def test_unknown_default(self, home: JotHome) -> None:
        """The default profile must exist."""
        home.path.mkdir(parents=True, exist_ok=True)
        home.config_file.write_text('default_profile = "epic"', encoding="utf-8")
        with pytest.raises(InvalidInputError):
            home.initialize().registry()

    def test_resolve_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without JOT_HOME the home is ~/.jot."""
        monkeypatch.delenv("JOT_HOME", raising=False)
        assert JotHome.resolve().path.name == ".jot"
