"""Read the user's editable markdown instructions from the data home."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jot.config import JotHome


class InstructionStore:
    """Load triage/drawdown/cleanup guidance plus per-project overrides."""

    def __init__(self, home: JotHome) -> None:
        self.root = home.path / "instructions"

    def get(self, name: str) -> str:
        """Return ``instructions/<name>.md`` or an empty string when missing."""
        path = self.root / f"{name}.md"
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def for_project(self, slug: str | None) -> str:
        """Return ``instructions/projects/<slug>.md`` or an empty string."""
        if not slug:
            return ""
        path = self.root / "projects" / f"{slug}.md"
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def compose(self, name: str, slug: str | None) -> str:
        """Return general guidance followed by any project-specific guidance."""
        parts = [self.get(name)]
        project = self.for_project(slug)
        if project:
            parts.append(f"## Project-specific guidance ({slug})\n\n{project}")
        return "\n\n".join(part for part in parts if part)
