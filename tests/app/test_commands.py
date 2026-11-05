"""Write commands: validation, versions, history, and context."""

from __future__ import annotations

import pytest

from jot.app.commands import Commands, JotPatch, Placement
from jot.app.session import Session
from jot.core.models import Origin, StatusCategory, WriteContext
from jot.exceptions import ConflictError, InvalidInputError, NotFoundError


class TestCreate:
    """Capture commits immediately with profile defaults."""

    def test_defaults(self, commands: Commands, session: Session) -> None:
        """A bare jot uses the default profile, its initial status, rank, v1."""
        jot = commands.create(title="Optimize sales model")
        assert (jot.profile, jot.status, jot.category) == (
            "note",
            "open",
            StatusCategory.OPEN,
        )
        assert jot.version == 1
        assert jot.rank
        [event] = session.queries.history(jot.id)
        assert event.kind == "jot.created"
        assert (event.actor, event.origin) == ("user", Origin.CLI)

    def test_everything(self, commands: Commands) -> None:
        """Profile, status, project, labels, fields, and metadata are stored."""
        commands.create_project("jot", "Jot")
        jot = commands.create(
            title="Ship",
            body="Details",
            profile="task",
            status="doing",
            project="jot",
            labels=["b", "a"],
            fields={"estimate": "2"},
            metadata={"user": {"source": "phone"}},
        )
        assert jot.status == "doing"
        assert jot.category == StatusCategory.ACTIVE
        assert jot.project == "jot"
        assert jot.labels == ["a", "b"]
        assert jot.metadata == {
            "fields": {"estimate": "2"},
            "user": {"source": "phone"},
        }

    def test_display_title(self, commands: Commands) -> None:
        """Without a title the first body line is shown."""
        jot = commands.create(body="first line\nsecond")
        assert jot.display_title == "first line"

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"title": " "}, "title or a body"),
            ({"title": "x", "profile": "epic"}, "Unknown profile"),
            ({"title": "x", "status": "doing"}, "no status"),
            ({"title": "x", "project": "nope"}, "not found"),
            ({"title": "x", "labels": ["Bad Label"]}, "Invalid label"),
            ({"title": "x", "metadata": {"fields": {}}}, "profile fields"),
            ({"title": "x", "metadata": {"Bad": 1}}, "Invalid metadata"),
        ],
    )
    def test_rejects(
        self, commands: Commands, kwargs: dict[str, object], message: str
    ) -> None:
        """Invalid input is rejected and nothing is written."""
        with pytest.raises((InvalidInputError, NotFoundError), match=message):
            commands.create(**kwargs)  # ty: ignore[invalid-argument-type]

    def test_new_jots_go_to_the_bottom(self, commands: Commands) -> None:
        """Rank keys increase with capture order."""
        ranks = [commands.create(title=str(i)).rank for i in range(5)]
        assert ranks == sorted(ranks)


class TestUpdate:
    """Patches, version checks, and field-level merges."""

    def test_patch(self, commands: Commands, session: Session) -> None:
        """Changed fields bump the version once and are recorded."""
        commands.create_project("p", "P")
        jot = commands.create(title="a")
        updated = commands.update(
            jot.id,
            JotPatch(title="b", body="c", project="p", metadata={"user": {"k": 1}}),
        )
        assert (updated.title, updated.body, updated.project) == ("b", "c", "p")
        assert updated.version == 2
        changes = session.queries.history(jot.id)[-1].data["changes"]
        assert isinstance(changes, dict)
        assert set(changes) == {"title", "body", "project", "metadata.user"}

    def test_no_change_keeps_version(self, commands: Commands) -> None:
        """An empty or identical patch is a no-op."""
        jot = commands.create(title="a")
        assert commands.update(jot.id, JotPatch(title="a")).version == 1

    def test_clear_project_and_metadata(self, commands: Commands) -> None:
        """None clears the project and removes a namespace."""
        commands.create_project("p", "P")
        jot = commands.create(title="a", project="p", metadata={"user": {"k": 1}})
        updated = commands.update(
            jot.id, JotPatch(project=None, metadata={"user": None})
        )
        assert updated.project is None
        assert updated.metadata == {}

    def test_profile_change_maps_status(self, commands: Commands) -> None:
        """Switching profile maps the status by category."""
        jot = commands.create(title="a", profile="task", status="doing")
        moved = commands.update(jot.id, JotPatch(profile="note"))
        assert (moved.profile, moved.status) == ("note", "open")
        same = commands.update(moved.id, JotPatch(profile="task"))
        assert same.status == "todo"

    def test_fields_merge_and_remove(self, commands: Commands) -> None:
        """Fields merge key by key; None removes one."""
        jot = commands.create(title="a", fields={"x": "1"})
        jot = commands.update(jot.id, JotPatch(fields={"y": "2"}))
        assert jot.metadata == {"fields": {"x": "1", "y": "2"}}
        jot = commands.update(jot.id, JotPatch(fields={"x": None, "y": None}))
        assert jot.metadata == {}

    def test_expected_version(self, commands: Commands) -> None:
        """A stale expected version is a conflict."""
        jot = commands.create(title="a")
        commands.update(jot.id, JotPatch(title="b"), expected_version=1)
        with pytest.raises(ConflictError, match="version 2, not 1"):
            commands.update(jot.id, JotPatch(title="c"), expected_version=1)

    def test_base_version_merges_disjoint_fields(self, commands: Commands) -> None:
        """A late writer succeeds when the user changed other fields only."""
        jot = commands.create(title="a")
        commands.update(jot.id, JotPatch(body="user edit"))
        merged = commands.update(
            jot.id, JotPatch(metadata={"triage": {"done": True}}), base_version=1
        )
        assert merged.body == "user edit"
        assert merged.metadata == {"triage": {"done": True}}

    def test_base_version_conflicts_on_same_field(self, commands: Commands) -> None:
        """A late writer cannot overwrite a field the user changed."""
        jot = commands.create(title="a")
        commands.update(jot.id, JotPatch(title="user title"))
        with pytest.raises(ConflictError, match="changed title"):
            commands.update(jot.id, JotPatch(title="triage title"), base_version=1)

    def test_missing_and_deleted(self, commands: Commands) -> None:
        """Unknown and deleted jots are not found."""
        with pytest.raises(NotFoundError):
            commands.update(99, JotPatch(title="x"))
        jot = commands.create(title="a")
        commands.delete(jot.id)
        with pytest.raises(NotFoundError):
            commands.update(jot.id, JotPatch(title="x"))


class TestStatusAndLabels:
    """Status moves and label changes."""

    def test_status(self, commands: Commands, session: Session) -> None:
        """Status and category change together and are recorded."""
        jot = commands.create(title="a", profile="task")
        moved = commands.set_status(jot.id, "done", expected_version=1)
        assert (moved.status, moved.category) == ("done", StatusCategory.DONE)
        assert session.queries.history(jot.id)[-1].kind == "jot.status_changed"
        assert commands.set_status(jot.id, "done").version == moved.version

    def test_transition_rules(self, session: Session) -> None:
        """Profiles with a transition list enforce it."""
        from jot.core.profiles import Profile, StatusSpec  # noqa: PLC0415

        session.profiles.register(
            Profile(
                id="gated",
                statuses=(
                    StatusSpec(id="a", category=StatusCategory.OPEN),
                    StatusSpec(id="b", category=StatusCategory.DONE),
                ),
                transitions=(("a", "b"),),
            )
        )
        commands = session.commands(WriteContext())
        jot = commands.create(title="x", profile="gated")
        commands.set_status(jot.id, "b")
        with pytest.raises(InvalidInputError):
            commands.set_status(jot.id, "a")

    def test_labels(self, commands: Commands) -> None:
        """Labels are added, removed, and versioned; no-ops are free."""
        jot = commands.create(title="a", labels=["triaging"])
        jot = commands.label(jot.id, add=["triaged"], remove=["triaging"])
        assert (jot.labels, jot.version) == (["triaged"], 2)
        assert commands.label(jot.id, add=["triaged"]).version == 2
        with pytest.raises(ConflictError):
            commands.label(jot.id, add=["x"], expected_version=1)


class TestRank:
    """Global ordering."""

    def ids(self, session: Session) -> list[int]:
        """Return live jot ids in rank order."""
        from jot.app.queries import JotFilter  # noqa: PLC0415

        return [j.id for j in session.queries.jots(JotFilter())]

    def test_moves(self, commands: Commands, session: Session) -> None:
        """Top, bottom, before, and after reorder jots."""
        a, b, c = (commands.create(title=t).id for t in "abc")
        commands.move_rank(c, Placement.TOP)
        assert self.ids(session) == [c, a, b]
        commands.move_rank(c, Placement.BOTTOM)
        assert self.ids(session) == [a, b, c]
        commands.move_rank(c, Placement.BEFORE, a)
        assert self.ids(session) == [c, a, b]
        commands.move_rank(c, Placement.AFTER, a)
        assert self.ids(session) == [a, c, b]
        commands.move_rank(a, Placement.AFTER, b)
        assert self.ids(session) == [c, b, a]
        commands.move_rank(c, Placement.BEFORE, b)
        assert self.ids(session) == [c, b, a]

    def test_single_jot(self, commands: Commands) -> None:
        """Moving the only jot is a no-op."""
        jot = commands.create(title="a")
        assert commands.move_rank(jot.id, Placement.TOP).version == 1

    @pytest.mark.parametrize(
        ("placement", "anchor"),
        [(Placement.BEFORE, None), (Placement.TOP, 1), (Placement.AFTER, 1)],
    )
    def test_invalid(
        self, commands: Commands, placement: Placement, anchor: int | None
    ) -> None:
        """Anchors are required exactly for before/after, and not self."""
        commands.create(title="a")
        with pytest.raises(InvalidInputError):
            commands.move_rank(1, placement, anchor)


class TestLifecycle:
    """Archive, delete, and restore."""

    def test_archive(self, commands: Commands, session: Session) -> None:
        """Archiving is reversible and idempotent."""
        jot = commands.create(title="a")
        archived = commands.archive(jot.id)
        assert archived.archived_at is not None
        assert commands.archive(jot.id).version == archived.version
        restored = commands.archive(jot.id, archived=False)
        assert restored.archived_at is None
        kinds = [e.kind for e in session.queries.history(jot.id)]
        assert kinds == ["jot.created", "jot.archived", "jot.unarchived"]

    def test_delete_restore(self, commands: Commands) -> None:
        """Deleting is soft; restoring brings the jot back."""
        jot = commands.create(title="a")
        assert commands.delete(jot.id).deleted_at is not None
        with pytest.raises(NotFoundError):
            commands.delete(jot.id)
        assert commands.delete(jot.id, deleted=False).deleted_at is None
        assert commands.delete(jot.id, deleted=False).deleted_at is None


class TestComments:
    """Comments are a collection with history."""

    def test_lifecycle(self, commands: Commands, session: Session) -> None:
        """Add, reply, edit, and delete keep every text in history."""
        jot = commands.create(title="a")
        first = commands.comment(jot.id, "hello")
        reply = commands.comment(
            jot.id, "answer", kind="execution.question", reply_to=first.id
        )
        assert reply.reply_to == first.id
        edited = commands.edit_comment(first.id, "hello!")
        assert edited.edited_at is not None
        assert commands.edit_comment(first.id, "hello!") == edited
        commands.delete_comment(reply.id)
        assert [c.body for c in session.queries.comments(jot.id)] == ["hello!"]
        kinds = [e.kind for e in session.queries.history(jot.id)]
        assert kinds[1:] == [
            "comment.added",
            "comment.added",
            "comment.edited",
            "comment.deleted",
        ]

    @pytest.mark.parametrize("body", ["", "  "])
    def test_empty(self, commands: Commands, body: str) -> None:
        """Comments need text."""
        jot = commands.create(title="a")
        with pytest.raises(InvalidInputError):
            commands.comment(jot.id, body)
        first = commands.comment(jot.id, "x")
        with pytest.raises(InvalidInputError):
            commands.edit_comment(first.id, body)

    def test_reply_elsewhere(self, commands: Commands) -> None:
        """A reply must be on the same jot; kinds must be valid names."""
        a, b = commands.create(title="a"), commands.create(title="b")
        first = commands.comment(a.id, "x")
        with pytest.raises(InvalidInputError, match="same jot"):
            commands.comment(b.id, "y", reply_to=first.id)
        with pytest.raises(InvalidInputError, match="comment kind"):
            commands.comment(a.id, "y", kind="Bad Kind")
        with pytest.raises(NotFoundError):
            commands.delete_comment(999)


class TestLinks:
    """Typed relations between jots."""

    def test_link_unlink(self, commands: Commands, session: Session) -> None:
        """Links are idempotent and visible from both ends."""
        a, b = commands.create(title="a"), commands.create(title="b")
        commands.link(a.id, b.id, "parent")
        commands.link(a.id, b.id, "parent")
        commands.link(b.id, a.id, "execution.blocks")
        assert len(session.queries.links(a.id)) == 2
        commands.unlink(a.id, b.id, "parent")
        with pytest.raises(NotFoundError):
            commands.unlink(a.id, b.id, "parent")

    @pytest.mark.parametrize("rel", ["nonsense", "Bad.Rel"])
    def test_bad_relation(self, commands: Commands, rel: str) -> None:
        """Unknown core relations and malformed names are rejected."""
        a, b = commands.create(title="a"), commands.create(title="b")
        with pytest.raises(InvalidInputError):
            commands.link(a.id, b.id, rel)

    def test_self_and_missing(self, commands: Commands) -> None:
        """No self links; both ends must exist."""
        a = commands.create(title="a")
        with pytest.raises(InvalidInputError):
            commands.link(a.id, a.id)
        with pytest.raises(NotFoundError):
            commands.link(a.id, 99)


class TestProjects:
    """Projects are a core attribute."""

    def test_create_update(self, commands: Commands, session: Session) -> None:
        """Projects are created, edited, archived, and listed."""
        commands.create_project("jot", "Jot", "Notes core")
        with pytest.raises(InvalidInputError, match="already exists"):
            commands.create_project("jot", "Again")
        project = commands.update_project("jot", name="Jot core", archived=True)
        assert (project.name, project.archived) == ("Jot core", True)
        assert commands.update_project("jot", name="Jot core") == project
        assert session.queries.projects() == []
        assert [p.slug for p in session.queries.projects(include_archived=True)] == [
            "jot"
        ]
        with pytest.raises(NotFoundError):
            commands.update_project("nope", name="x")


class TestContext:
    """Every event carries the writer's context."""

    def test_plugin_context(self, session: Session) -> None:
        """Actor, origin, correlation, and reason are recorded."""
        context = WriteContext(
            actor="plugin:triage",
            origin=Origin.PLUGIN,
            correlation_id="abc",
            reason="enrich",
        )
        jot = session.commands(context).create(title="a")
        [event] = session.queries.history(jot.id)
        assert (event.actor, event.origin, event.correlation_id, event.reason) == (
            "plugin:triage",
            Origin.PLUGIN,
            "abc",
            "enrich",
        )
        assert jot.created_by == "plugin:triage"
