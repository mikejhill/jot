"""Console and module application entry points."""

from __future__ import annotations

from collections.abc import Sequence

from jot.cli import CommandLine


class Application:
    """Run the Typer application."""

    @classmethod
    def main(cls, argv: Sequence[str] | None = None) -> None:
        """Invoke the console with optional explicit arguments."""
        CommandLine().app(args=list(argv) if argv is not None else None)


if __name__ == "__main__":
    Application.main()
