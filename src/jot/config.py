"""Resolve the data home and load validated settings."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jot.core.profiles import FieldSpec, Profile, ProfileRegistry, StatusSpec
from jot.exceptions import ConfigurationError

DEFAULT_CONFIG = """# Jot settings. See docs/user-guide.md.

# Profile used when `jot add` is not given --profile.
default_profile = "note"

# Built-in profiles: note (open, done) and task (todo, doing, done, wont_do).
# Define more, or override a built-in, under [profiles.<id>]. Example:
#
# [profiles.bug]
# label = "Bug"
# initial = "new"
# statuses = [
#   { id = "new", category = "open" },
#   { id = "fixing", category = "active" },
#   { id = "fixed", category = "done" },
#   { id = "invalid", category = "cancelled" },
# ]
# transitions = []        # empty = any move; else [["new", "fixing"], ...]
# [profiles.bug.fields.severity]
# type = "enum"
# options = ["low", "high"]
"""


class ProfileConfig(BaseModel):
    """A profile as written in config.toml (its id is the table key)."""

    model_config = ConfigDict(extra="forbid")

    label: str = ""
    statuses: list[StatusSpec] = Field(min_length=1)
    initial: str = ""
    transitions: list[tuple[str, str]] = Field(default_factory=list)
    fields: dict[str, FieldSpec] = Field(default_factory=dict)
    plugins: list[str] = Field(default_factory=list)

    def build(self, profile_id: str) -> Profile:
        """Return the validated profile."""
        return Profile(
            id=profile_id,
            label=self.label or profile_id.replace("_", " ").title(),
            statuses=tuple(self.statuses),
            initial=self.initial,
            transitions=tuple(self.transitions),
            fields=self.fields,
            plugins=tuple(self.plugins),
        )


class Config(BaseModel):
    """Validated contents of config.toml."""

    model_config = ConfigDict(extra="forbid")

    default_profile: str = "note"
    profiles: dict[str, ProfileConfig] = Field(default_factory=dict)

    def registry(self) -> ProfileRegistry:
        """Build the profile registry, failing on an unknown default."""
        registry = ProfileRegistry(self.default_profile)
        for profile_id, spec in self.profiles.items():
            try:
                registry.register(spec.build(profile_id))
            except ValidationError as err:
                raise ConfigurationError(f"Profile {profile_id!r}: {err}") from err
        registry.get()
        return registry


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

    @property
    def config_file(self) -> Path:
        """Return the settings path."""
        return self.path / "config.toml"

    def initialize(self) -> Config:
        """Create missing defaults and load configuration without overwriting edits."""
        try:
            self.path.mkdir(parents=True, exist_ok=True)
            self._write_missing(self.config_file, DEFAULT_CONFIG)
            with self.config_file.open("rb") as stream:
                return Config.model_validate(tomllib.load(stream))
        except (OSError, tomllib.TOMLDecodeError, ValidationError) as err:
            raise ConfigurationError(
                f"Cannot load Jot home {self.path}: {err}"
            ) from err

    @staticmethod
    def _write_missing(path: Path, content: str) -> None:
        """Use exclusive creation so concurrent initialization preserves edits."""
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(content)
        except FileExistsError:
            return
