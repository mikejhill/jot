"""Expected application failures."""

from __future__ import annotations


class AppError(Exception):
    """Base error for user-facing failures."""


class ConfigurationError(AppError):
    """Invalid configuration."""


class RepositoryError(AppError):
    """Invalid data or database operation."""


class NotFoundError(RepositoryError):
    """Requested record does not exist."""


class WorkflowError(AppError):
    """Transition or lease operation is invalid."""


class NotImplementedAppError(AppError):
    """Command is reserved for a later phase."""
