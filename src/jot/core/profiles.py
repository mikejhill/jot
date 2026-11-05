"""Profiles: declarative definitions of a jot's statuses and fields.

A profile declares; it never runs code. Plugins supply behavior.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from jot.core.models import NAME, StatusCategory
from jot.exceptions import InvalidInputError


class FieldType(StrEnum):
    """Value types a profile field may declare."""

    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATE = "date"
    ENUM = "enum"


class Spec(BaseModel):
    """Strict, immutable configuration record."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class StatusSpec(Spec):
    """One status and its category."""

    id: str = Field(pattern=NAME.pattern)
    label: str = ""
    category: StatusCategory


class FieldSpec(Spec):
    """A predefined metadata field, stored under ``metadata.fields``."""

    type: FieldType = FieldType.STRING
    label: str = ""
    options: tuple[str, ...] = ()
    required: bool = False

    @model_validator(mode="after")
    def enum_options(self) -> FieldSpec:
        """Require options for enum fields only."""
        if (self.type == FieldType.ENUM) != bool(self.options):
            raise ValueError("enum fields need options; other types take none")
        return self

    def coerce(self, name: str, value: JsonValue) -> JsonValue:
        """Validate one value, converting CLI strings to the declared type."""
        if value is None:
            return None
        converted = self._convert(value)
        if converted is None:
            raise InvalidInputError(f"Field {name!r} expects {self.type.value}")
        if self.type == FieldType.ENUM and converted not in self.options:
            raise InvalidInputError(
                f"Field {name!r} must be one of {', '.join(self.options)}"
            )
        return converted

    def _convert(self, value: JsonValue) -> JsonValue:
        """Return the typed value, or None when it cannot be converted."""
        match self.type:
            case FieldType.STRING | FieldType.ENUM:
                return value if isinstance(value, str) else None
            case FieldType.BOOLEAN:
                if isinstance(value, bool):
                    return value
                lowered = str(value).lower()
                return {"true": True, "false": False}.get(lowered)
            case FieldType.NUMBER:
                return self._number(value)
            case FieldType.DATE:
                try:
                    return date.fromisoformat(str(value)).isoformat()
                except ValueError:
                    return None

    @staticmethod
    def _number(value: JsonValue) -> JsonValue:
        """Convert ints, floats, and numeric strings."""
        if isinstance(value, bool):
            return None
        if isinstance(value, int | float):
            return value
        try:
            number = float(str(value))
        except ValueError:
            return None
        return int(number) if number.is_integer() else number


class Profile(Spec):
    """Statuses, optional transition rules, fields, and applicable plugins."""

    id: str = Field(pattern=NAME.pattern)
    label: str = ""
    statuses: tuple[StatusSpec, ...] = Field(min_length=1)
    initial: str = ""
    transitions: tuple[tuple[str, str], ...] = ()
    fields: dict[str, FieldSpec] = Field(default_factory=dict)
    plugins: tuple[str, ...] = ()

    @model_validator(mode="after")
    def consistent(self) -> Profile:
        """Check that every referenced status exists and ids are unique."""
        ids = [status.id for status in self.statuses]
        if len(set(ids)) != len(ids):
            raise ValueError(f"profile {self.id}: duplicate status ids")
        if self.initial and self.initial not in ids:
            raise ValueError(f"profile {self.id}: unknown initial {self.initial!r}")
        for pair in self.transitions:
            unknown = [status for status in pair if status not in ids]
            if unknown:
                raise ValueError(
                    f"profile {self.id}: transition uses unknown {unknown[0]!r}"
                )
        for name in self.fields:
            if not NAME.match(name):
                raise ValueError(f"profile {self.id}: invalid field name {name!r}")
        return self

    @property
    def initial_status(self) -> str:
        """Return the declared initial status, or the first one."""
        return self.initial or self.statuses[0].id

    def status(self, status_id: str) -> StatusSpec:
        """Return a status definition or fail with the valid choices."""
        for status in self.statuses:
            if status.id == status_id:
                return status
        choices = ", ".join(status.id for status in self.statuses)
        raise InvalidInputError(
            f"Profile {self.id!r} has no status {status_id!r} (choices: {choices})"
        )

    def check_transition(self, current: str, target: str) -> None:
        """Reject a move the profile's optional transition list does not allow."""
        self.status(target)
        if current == target or not self.transitions:
            return
        if (current, target) not in self.transitions:
            raise InvalidInputError(
                f"Profile {self.id!r} does not allow {current} -> {target}"
            )

    def map_status(self, other: Profile, status_id: str) -> str:
        """Pick this profile's status for a jot moving here from ``other``.

        Keeps the same id when it exists, else the first status in the same
        category, else the initial status.
        """
        if any(status.id == status_id for status in self.statuses):
            return status_id
        category = other.status(status_id).category
        for status in self.statuses:
            if status.category == category:
                return status.id
        return self.initial_status

    def validate_fields(self, values: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """Coerce declared fields, keep undeclared ones, and enforce required ones."""
        result = dict(values)
        for name, spec in self.fields.items():
            if name in result:
                result[name] = spec.coerce(name, result[name])
            if spec.required and result.get(name) is None:
                raise InvalidInputError(f"Profile {self.id!r} requires field {name!r}")
        return {name: value for name, value in result.items() if value is not None}


def _statuses(*pairs: tuple[str, StatusCategory]) -> tuple[StatusSpec, ...]:
    """Build status specs from (id, category) pairs."""
    return tuple(StatusSpec(id=sid, category=category) for sid, category in pairs)


BUILTIN_PROFILES: tuple[Profile, ...] = (
    Profile(
        id="note",
        label="Note",
        statuses=_statuses(
            ("open", StatusCategory.OPEN), ("done", StatusCategory.DONE)
        ),
    ),
    Profile(
        id="task",
        label="Task",
        statuses=_statuses(
            ("todo", StatusCategory.OPEN),
            ("doing", StatusCategory.ACTIVE),
            ("done", StatusCategory.DONE),
            ("wont_do", StatusCategory.CANCELLED),
        ),
    ),
)


class ProfileRegistry:
    """Built-in profiles overlaid with configured (and later, plugin) profiles."""

    def __init__(self, default: str = "note") -> None:
        self._profiles: dict[str, Profile] = {p.id: p for p in BUILTIN_PROFILES}
        self.default = default

    def register(self, profile: Profile) -> None:
        """Add or replace a profile definition."""
        self._profiles[profile.id] = profile

    def get(self, profile_id: str | None = None) -> Profile:
        """Return a profile by id, or the default profile."""
        key = profile_id or self.default
        try:
            return self._profiles[key]
        except KeyError:
            choices = ", ".join(sorted(self._profiles))
            raise InvalidInputError(
                f"Unknown profile {key!r} (choices: {choices})"
            ) from None

    def all(self) -> list[Profile]:
        """Return every profile, sorted by id."""
        return [self._profiles[key] for key in sorted(self._profiles)]
