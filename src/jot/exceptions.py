"""Expected application failures."""

from __future__ import annotations


class AppError(Exception):
    """Base error for user-facing failures."""

    exit_code = 1


class ConfigurationError(AppError):
    """Invalid configuration."""


class RepositoryError(AppError):
    """Invalid data or database operation."""


class NotFoundError(RepositoryError):
    """Requested record does not exist."""

    exit_code = 2


class InvalidInputError(AppError):
    """A command's input breaks a core or profile rule."""


class ConflictError(AppError):
    """A write was based on a stale version of the record."""

    exit_code = 3


class PluginError(AppError):
    """A plugin is missing, broken, misconfigured, or misused."""
