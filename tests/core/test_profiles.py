"""Declarative profiles: statuses, transitions, and fields."""

from __future__ import annotations

import pytest
from pydantic import JsonValue, ValidationError

from jot.core.models import StatusCategory
from jot.core.profiles import (
    FieldSpec,
    FieldType,
    Profile,
    ProfileRegistry,
    StatusSpec,
)
from jot.exceptions import InvalidInputError


def statuses(*pairs: tuple[str, StatusCategory]) -> tuple[StatusSpec, ...]:
    """Build status specs."""
    return tuple(StatusSpec(id=sid, category=cat) for sid, cat in pairs)


BUG = Profile(
    id="bug",
    statuses=statuses(
        ("new", StatusCategory.OPEN),
        ("fixing", StatusCategory.ACTIVE),
        ("fixed", StatusCategory.DONE),
    ),
    initial="new",
    transitions=(("new", "fixing"), ("fixing", "fixed")),
    fields={
        "severity": FieldSpec(type=FieldType.ENUM, options=("low", "high")),
        "points": FieldSpec(type=FieldType.NUMBER),
        "due": FieldSpec(type=FieldType.DATE),
        "regression": FieldSpec(type=FieldType.BOOLEAN),
        "owner": FieldSpec(required=True),
    },
)


class TestFields:
    """Field values are coerced to their declared type."""

    @pytest.mark.parametrize(
        ("name", "raw", "expected"),
        [
            ("severity", "high", "high"),
            ("points", "3", 3),
            ("points", "2.5", 2.5),
            ("points", 4, 4),
            ("due", "2026-11-30", "2026-11-30"),
            ("regression", "true", True),
            ("regression", False, False),
            ("owner", "mike", "mike"),
        ],
    )
    def test_coerce(self, name: str, raw: JsonValue, expected: JsonValue) -> None:
        """Strings from the CLI become typed values."""
        assert BUG.fields[name].coerce(name, raw) == expected

    @pytest.mark.parametrize(
        ("name", "raw"),
        [
            ("severity", "medium"),
            ("severity", 3),
            ("points", "many"),
            ("points", True),
            ("due", "tomorrow"),
            ("regression", "maybe"),
            ("owner", 5),
        ],
    )
    def test_reject(self, name: str, raw: JsonValue) -> None:
        """Values that do not fit the type are rejected."""
        with pytest.raises(InvalidInputError):
            BUG.fields[name].coerce(name, raw)

    def test_none_passes_through(self) -> None:
        """None means unset."""
        assert BUG.fields["points"].coerce("points", None) is None

    def test_validate_fields(self) -> None:
        """Declared fields are coerced; undeclared kept; None dropped."""
        result = BUG.validate_fields({"owner": "a", "points": "2", "extra": "x"})
        assert result == {"owner": "a", "points": 2, "extra": "x"}

    def test_required(self) -> None:
        """A required field must be present."""
        with pytest.raises(InvalidInputError, match="requires field 'owner'"):
            BUG.validate_fields({"points": 1})

    def test_enum_needs_options(self) -> None:
        """Enum fields need options and other types reject them."""
        with pytest.raises(ValidationError):
            FieldSpec(type=FieldType.ENUM)
        with pytest.raises(ValidationError):
            FieldSpec(type=FieldType.STRING, options=("a",))


class TestStatuses:
    """Status lookup, transitions, and mapping across profiles."""

    def test_initial(self) -> None:
        """The declared initial status wins; otherwise the first."""
        assert BUG.initial_status == "new"
        assert ProfileRegistry().get("task").initial_status == "todo"

    def test_unknown_status(self) -> None:
        """Unknown statuses list the choices."""
        with pytest.raises(InvalidInputError, match="choices: new, fixing, fixed"):
            BUG.status("closed")

    def test_transitions(self) -> None:
        """Only listed moves are allowed when a list is declared."""
        BUG.check_transition("new", "fixing")
        BUG.check_transition("new", "new")
        with pytest.raises(InvalidInputError, match="does not allow new -> fixed"):
            BUG.check_transition("new", "fixed")

    def test_any_transition_without_list(self) -> None:
        """No list means any move between known statuses."""
        ProfileRegistry().get("task").check_transition("done", "todo")

    def test_map_status(self) -> None:
        """Same id, then same category, then the initial status."""
        task = ProfileRegistry().get("task")
        note = ProfileRegistry().get("note")
        assert task.map_status(BUG, "fixing") == "doing"
        assert note.map_status(task, "done") == "done"
        assert note.map_status(task, "doing") == "open"
        assert BUG.map_status(task, "wont_do") == "new"

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"initial": "missing"},
            {"transitions": (("new", "gone"),)},
            {"fields": {"Bad Name": FieldSpec()}},
            {
                "statuses": statuses(
                    ("a", StatusCategory.OPEN), ("a", StatusCategory.DONE)
                )
            },
        ],
    )
    def test_invalid_profiles(self, kwargs: dict[str, object]) -> None:
        """Inconsistent definitions are rejected."""
        base: dict[str, object] = {
            "id": "x",
            "statuses": statuses(("new", StatusCategory.OPEN)),
        }
        with pytest.raises(ValidationError):
            Profile.model_validate(base | kwargs)


class TestRegistry:
    """Built-ins plus registered profiles."""

    def test_builtins_and_default(self) -> None:
        """Note is the default; both built-ins exist."""
        registry = ProfileRegistry()
        assert registry.get().id == "note"
        assert [p.id for p in registry.all()] == ["note", "task"]

    def test_register_and_unknown(self) -> None:
        """Registered profiles are found; unknown ones fail with choices."""
        registry = ProfileRegistry(default="bug")
        registry.register(BUG)
        assert registry.get().id == "bug"
        with pytest.raises(InvalidInputError, match="choices: bug, note, task"):
            registry.get("epic")
