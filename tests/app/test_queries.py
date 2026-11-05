"""Read-side queries."""

from __future__ import annotations

import pytest

from jot.app.commands import Commands, JotPatch
from jot.app.queries import JotFilter, Queries, Sort
from jot.app.session import Session
from jot.core.models import StatusCategory


@pytest.fixture
def seeded(commands: Commands) -> Commands:
    """Three jots across profiles, projects, labels, and states."""
    commands.create_project("work", "Work")
    commands.create(title="Sales model for global channels", labels=["sales"])
    commands.create(title="Ship core", profile="task", status="doing", project="work")
    third = commands.create(title="Old idea", body="archive me", labels=["sales"])
    commands.comment(third.id, "mentions quarterly forecast")
    commands.archive(third.id)
    return commands


class TestList:
    """Filters combine with AND; lists within a filter mean any-of."""

    @pytest.mark.usefixtures("seeded")
    @pytest.mark.parametrize(
        ("criteria", "expected"),
        [
            (JotFilter(), [1, 2]),
            (JotFilter(include_archived=True), [1, 2, 3]),
            (JotFilter(profiles=["task"]), [2]),
            (JotFilter(statuses=["open"]), [1]),
            (JotFilter(categories=[StatusCategory.ACTIVE]), [2]),
            (JotFilter(project="work"), [2]),
            (JotFilter(labels=["sales"], include_archived=True), [1, 3]),
            (JotFilter(text="glob"), [1]),
            (JotFilter(text="forecast", include_archived=True), [3]),
            (JotFilter(text="!!!"), []),
            (JotFilter(sort=Sort.CREATED), [2, 1]),
            (JotFilter(sort=Sort.UPDATED, include_archived=True, limit=1), [3]),
        ],
    )
    def test_filters(
        self, session: Session, criteria: JotFilter, expected: list[int]
    ) -> None:
        """Each filter selects the expected jots in order."""
        assert [j.id for j in session.queries.jots(criteria)] == expected

    def test_deleted(self, seeded: Commands, session: Session) -> None:
        """Deleted jots appear only when asked for."""
        seeded.delete(1)
        assert [j.id for j in session.queries.jots(JotFilter())] == [2]
        found = session.queries.jots(JotFilter(include_deleted=True))
        assert [j.id for j in found] == [1, 2]

    def test_search_follows_edits(self, seeded: Commands, session: Session) -> None:
        """The full-text index tracks title edits and comment deletion."""
        seeded.update(2, JotPatch(title="Ship jots"))
        assert [j.id for j in session.queries.jots(JotFilter(text="jots"))] == [2]
        comment = seeded.comment(2, "zebra")
        seeded.delete_comment(comment.id)
        assert session.queries.jots(JotFilter(text="zebra")) == []


class TestDetail:
    """Jot detail, feeds, and catalogs."""

    def test_detail(self, seeded: Commands, session: Session) -> None:
        """Detail includes comments, links, and history."""
        seeded.link(1, 2, "relates")
        detail = session.queries.detail(1)
        assert detail.jot.id == 1
        assert detail.links[0].dst == 2
        assert detail.history[0].kind == "jot.created"

    @pytest.mark.usefixtures("seeded")
    def test_events_feed(self, session: Session) -> None:
        """Events page forward from a cursor."""
        first = session.queries.events(limit=2)
        rest = session.queries.events(after=first[-1].id)
        assert [e.id for e in first + rest] == sorted(e.id for e in first + rest)
        assert first[0].kind == "project.created"

    @pytest.mark.usefixtures("seeded")
    def test_labels(self, session: Session) -> None:
        """Label counts include archived jots."""
        assert session.queries.labels() == [("sales", 2)]

    def test_match(self) -> None:
        """Free text becomes prefix-matched quoted tokens."""
        assert Queries.match("glob chan") == '"glob"* "chan"*'
        assert Queries.match("--") == '""'
