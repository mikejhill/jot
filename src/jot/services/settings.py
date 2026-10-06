"""Validated, comment-preserving configuration edits and effective settings."""

from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import asdict
from typing import TYPE_CHECKING

import tomlkit
from pydantic import TypeAdapter, ValidationError

from jot.agents.registry import MODEL_SUGGESTIONS
from jot.config import ACTIONS, Config
from jot.exceptions import ConfigurationError
from jot.harnesses import KINDS

if TYPE_CHECKING:
    from jot.config import JotHome


class SettingsStore:
    """Read effective settings and atomically replace validated TOML edits."""

    def __init__(self, home: JotHome) -> None:
        """Bind edits to the explicitly supplied data home."""
        self.home = home

    @staticmethod
    def effective(config: Config) -> dict[str, object]:
        """Expose configured instances, defaults, policy-filtered suggestions, pins."""
        harnesses = config.effective_harnesses()
        return asdict(config) | {
            "harnesses": {
                key: value.model_dump()
                | {
                    "loadout": {
                        action: value.for_action(action).model_dump()
                        for action in ACTIONS
                    }
                }
                for key, value in harnesses.items()
            },
            "actions": {action: config.harness_for(action) for action in ACTIONS},
            "pins": [pin.model_dump(exclude_none=True) for pin in config.pins],
            "model_suggestions": {
                key: [
                    model
                    for model in (
                        h.models.allowed or MODEL_SUGGESTIONS.get(KINDS[h.kind], [])
                    )
                    if h.models.permits(model) and model != "auto"
                ]
                for key, h in harnesses.items()
            },
        }

    def save(self, changes: dict[str, object]) -> Config:
        """Validate the complete merged document before an atomic replacement."""
        path = self.home.path / "config.toml"
        document = tomlkit.parse(path.read_text(encoding="utf-8"))
        unknown = set(changes) - {"harnesses", "actions", "pins"}
        if unknown:
            raise ConfigurationError(
                f"Unknown settings sections: {', '.join(sorted(unknown))}"
            )
        merged = dict(document.unwrap()) | changes
        try:
            config = TypeAdapter(Config).validate_python(merged)
            config.validate_settings()
        except ValidationError as err:
            raise ConfigurationError(f"Invalid settings: {err}") from err
        for key, value in changes.items():
            normalized = (
                [pin.model_dump(exclude_none=True) for pin in config.pins]
                if key == "pins"
                else value
            )
            self._merge(document, key, normalized)
        temporary = path.with_suffix(".toml.tmp")
        try:
            temporary.write_text(tomlkit.dumps(document), encoding="utf-8")
            temporary.replace(path)
        except OSError as err:
            raise ConfigurationError(f"Cannot save settings: {err}") from err
        return config

    @classmethod
    def _merge(
        cls, target: MutableMapping[str, object], key: str, value: object
    ) -> None:
        """Update tables recursively so comments on unchanged keys survive."""
        previous = target.get(key)
        if isinstance(value, dict) and isinstance(previous, MutableMapping):
            for removed in set(previous) - set(value):
                del previous[removed]
            for child, item in value.items():
                cls._merge(previous, child, item)
        else:
            target[key] = value
