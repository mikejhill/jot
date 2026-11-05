"""Import boundaries between layers."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "jot"
ALLOWED = {
    "core": {"jot.core", "jot.exceptions"},
    "db": {"jot.core", "jot.db", "jot.exceptions"},
    "app": {"jot.core", "jot.db", "jot.app", "jot.config", "jot.exceptions"},
    "server": {"jot.core", "jot.app", "jot.config", "jot.exceptions", "jot.server"},
}
LLM_MODULES = ("anthropic", "claude_agent_sdk", "openai")


def imports(path: Path) -> set[str]:
    """Return the modules a file imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


@pytest.mark.parametrize("layer", sorted(ALLOWED))
def test_layer_imports(layer: str) -> None:
    """Each layer imports only from itself and the layers below it."""
    for path in (SRC / layer).glob("*.py"):
        for module in imports(path):
            if module.startswith("jot"):
                assert any(
                    module == allowed or module.startswith(f"{allowed}.")
                    for allowed in ALLOWED[layer]
                ), f"{path.name} imports {module}"


def test_no_llm_code_in_core() -> None:
    """The core package contains no model SDK imports."""
    for path in SRC.rglob("*.py"):
        assert not any(m.startswith(LLM_MODULES) for m in imports(path)), path.name
