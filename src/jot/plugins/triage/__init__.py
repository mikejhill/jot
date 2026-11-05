"""Triage: turn quick captures into tidy jots with an AI harness.

On ``jot.created`` the plugin labels the jot ``triaging`` and queues a job.
The job asks the ``harness@1`` service (action ``triage``) for a title, body,
project, labels, and profile fields, then applies only what is safe: fields
the user changed after capture are never overwritten, and existing values are
kept. The jot ends up labelled ``triaged`` (plus ``needs-input`` when the
model was unsure or asked questions). Capture never waits on a model.
"""

from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING, Annotated, override

import typer
from pydantic import Field, JsonValue

from jot.app.commands import JotPatch
from jot.core.models import NAME, SLUG, Record
from jot.exceptions import AppError, InvalidInputError
from jot.plugins.api import Plugin
from jot.plugins.harness import SERVICE, HarnessService
from jot.plugins.harness.api import StructuredRequest, integer, number, obj, text_of

if TYPE_CHECKING:
    from jot.core.models import Event, Jot, Metadata
    from jot.core.profiles import Profile
    from jot.plugins.api import Job, PluginCli
    from jot.plugins.context import PluginContext

TRIAGING = "triaging"
TRIAGED = "triaged"
NEEDS_INPUT = "needs-input"
FAILED = "triage-failed"
RESERVED = frozenset({TRIAGING, TRIAGED, NEEDS_INPUT, FAILED})
TOP_LABELS = 60
TITLE_LIMIT = 200
SYSTEM = (
    "You triage quick notes captured in a personal tracker into tidy, "
    "structured entries. Follow the owner's guidance exactly. Never invent "
    "facts that are not in the note. Return only the JSON object requested."
)
GUIDANCE = (
    "- Title: short and specific; imperative for tasks. Keep the owner's wording "
    "when it is already clear.\n"
    "- Body: Markdown expanding the note only with what it implies: the problem, "
    "the desired outcome, and acceptance criteria for tasks. Empty if the note "
    "says nothing more.\n"
    "- Project: an existing slug when the note clearly belongs to it, else empty.\n"
    "- Labels: reuse existing labels; at most a few.\n"
    "- Confidence: how clear the scope and outcome are (0 to 1).\n"
    "- Questions: only what the owner must answer before acting."
)


def slugify(text: str, pattern: str) -> str | None:
    """Lowercase, dash-join, and validate a label or project slug."""
    cleaned = re.sub(r"[^a-z0-9_.-]+", "-", text.strip().lower()).strip("-.")
    return cleaned if cleaned and re.match(pattern, cleaned) else None


class TriageSettings(Record):
    """How triage behaves."""

    profiles: list[str] = Field(
        default_factory=list, description="Profiles to triage (empty: every profile)"
    )
    guidance: str = Field(default=GUIDANCE, description="Instructions for the model")
    min_confidence: float = Field(default=0.6, ge=0, le=1)
    rewrite_title: bool = True
    create_projects: bool = False
    max_labels: int = Field(default=5, ge=0, le=20)
    max_attempts: int = Field(default=3, ge=1, le=10)


class Proposal(Record):
    """The model's answer, coerced leniently into safe values."""

    title: str = ""
    body: str = ""
    project: str | None = None
    new_project: str = ""
    labels: list[str] = Field(default_factory=list)
    fields: dict[str, JsonValue] = Field(default_factory=dict)
    confidence: float = 0.0
    questions: list[str] = Field(default_factory=list)

    @classmethod
    def parse(cls, value: Metadata, max_labels: int) -> Proposal:
        """Keep well-typed values; drop the rest."""
        labels = value.get("labels")
        questions = value.get("questions")
        names = (
            [slugify(text_of(item), NAME.pattern) for item in (labels or [])]
            if isinstance(labels, list)
            else []
        )
        confidence = number(value.get("confidence"))
        return cls(
            title=" ".join(text_of(value.get("title")).split())[:TITLE_LIMIT],
            body=text_of(value.get("body")).strip(),
            project=slugify(text_of(value.get("project")), SLUG),
            new_project=" ".join(text_of(value.get("new_project")).split()),
            labels=list(dict.fromkeys(n for n in names if n and n not in RESERVED))[
                :max_labels
            ],
            fields=obj(value.get("fields")),
            confidence=min(max(confidence or 0.0, 0.0), 1.0),
            questions=[
                text_of(q).strip()
                for q in (questions if isinstance(questions, list) else [])
                if text_of(q).strip()
            ],
        )


class Triager:
    """Builds the request for one jot and turns the answer into safe writes."""

    def __init__(self, context: PluginContext, settings: TriageSettings) -> None:
        self.context = context
        self.settings = settings

    def profile(self, jot: Jot) -> Profile:
        """The jot's profile."""
        return self.context.session.profiles.get(jot.profile)

    def open_fields(self, jot: Jot) -> Metadata:
        """JSON schemas for profile fields the jot has no value for yet."""
        current = obj(jot.metadata.get("fields"))
        schemas: Metadata = {}
        for name, spec in self.profile(jot).fields.items():
            if current.get(name) is not None:
                continue
            kind = spec.type.value
            schema: Metadata = {"description": spec.label or name}
            if kind == "enum":
                schema["enum"] = list(spec.options)
            elif kind == "date":
                schema |= {"type": "string", "description": f"{name} as YYYY-MM-DD"}
            else:
                schema["type"] = kind
            schemas[name] = schema
        return schemas

    def schema(self, jot: Jot) -> Metadata:
        """The answer's JSON schema."""
        strings: Metadata = {"type": "array", "items": {"type": "string"}}
        properties: Metadata = {
            "title": {"type": "string"},
            "body": {"type": "string", "description": "Markdown"},
            "project": {"type": "string", "description": "Existing slug, or empty"},
            "new_project": {
                "type": "string",
                "description": "Name of a clearly missing project, else empty",
            },
            "labels": strings,
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "questions": strings,
        }
        if fields := self.open_fields(jot):
            properties["fields"] = {"type": "object", "properties": fields}
        return {
            "type": "object",
            "properties": properties,
            "required": [
                "title",
                "body",
                "project",
                "new_project",
                "labels",
                "confidence",
                "questions",
            ],
        }

    def prompt(self, jot: Jot) -> str:
        """Guidance, context, and the note itself."""
        queries = self.context.queries
        projects = [
            f"- {p.slug}: {p.name}" + (f" - {p.description}" if p.description else "")
            for p in queries.projects()
        ]
        counted = sorted(queries.labels(), key=lambda pair: (-pair[1], pair[0]))
        labels = [name for name, _ in counted if name not in RESERVED][:TOP_LABELS]
        kept = [f"profile: {jot.profile}"]
        if jot.project:
            kept.append(f"project: {jot.project}")
        if owned := [label for label in jot.labels if label not in RESERVED]:
            kept.append(f"labels: {', '.join(owned)}")
        sections = [
            f"## Guidance\n\n{self.settings.guidance}",
            "## Existing projects\n\n" + ("\n".join(projects) or "(none)"),
            "## Existing labels (reuse these)\n\n" + (", ".join(labels) or "(none)"),
            f"## Today\n\n{date.today().isoformat()}",  # noqa: DTZ011 - user's day
            "## Already set by the owner (keep)\n\n" + "\n".join(kept),
            f"## Note\n\n{jot.title}" + (f"\n\n{jot.body}" if jot.body else ""),
        ]
        return "\n\n".join(sections)

    def changed_since(self, jot_id: int, version: int) -> set[str]:
        """Fields someone else changed after ``version``."""
        changed: set[str] = set()
        for event in self.context.queries.history(jot_id):
            if (event.version or 0) > version and event.actor != self.context.actor:
                changed |= set(obj(event.data.get("changes")))
        return changed

    def patch(self, jot: Jot, proposal: Proposal, changed: set[str]) -> JotPatch:
        """Only values that are missing, unchanged by the user, and allowed."""
        values: dict[str, object] = {}
        if (
            self.settings.rewrite_title
            and proposal.title
            and proposal.title != jot.title
            and "title" not in changed
        ):
            values["title"] = proposal.title
        if proposal.body and not jot.body and "body" not in changed:
            values["body"] = proposal.body
        if jot.project is None and "project" not in changed:
            project = self.project(proposal)
            if project is not None:
                values["project"] = project
        if "metadata.fields" not in changed:
            allowed = self.open_fields(jot)
            fields = {k: v for k, v in proposal.fields.items() if k in allowed}
            if fields:
                values["fields"] = fields
        record: Metadata = {
            "confidence": proposal.confidence,
            "questions": list(proposal.questions),
        }
        if "title" in values:
            record["original_title"] = jot.title
        values["metadata"] = {"triage": record}
        return JotPatch.model_validate(values)

    def project(self, proposal: Proposal) -> str | None:
        """An existing project, or a new one when allowed."""
        known = {p.slug for p in self.context.queries.projects(include_archived=True)}
        if proposal.project in known:
            return proposal.project
        if not (self.settings.create_projects and proposal.new_project):
            return None
        slug = slugify(proposal.new_project, SLUG)
        if slug is None:
            return None
        if slug not in known:
            self.context.commands("triage: new project").create_project(
                slug, proposal.new_project
            )
        return slug

    def apply(
        self, jot_id: int, proposal: Proposal, base: int, model: str | None
    ) -> Jot:
        """Write the proposal atomically; never overwrite newer user edits."""
        commands = self.context.commands("triage")
        with self.context.session.db.write():
            jot = self.context.queries.get(jot_id)
            patch = self.patch(jot, proposal, self.changed_since(jot_id, base))
            triage = obj(patch.metadata.get("triage")) | {"model": model}
            patch = patch.model_copy(update={"metadata": {"triage": triage}})
            jot = commands.update(jot_id, patch)
            for question in proposal.questions:
                commands.comment(jot_id, question, kind="triage.question")
            unsure = proposal.confidence < self.settings.min_confidence or bool(
                proposal.questions
            )
            return commands.label(
                jot_id,
                add=[*proposal.labels, TRIAGED, *([NEEDS_INPUT] if unsure else [])],
                remove=[TRIAGING, FAILED],
            )

    def give_up(self, jot_id: int, error: str) -> None:
        """Mark a jot whose triage failed for good, and say why."""
        commands = self.context.commands("triage failed")
        with self.context.session.db.write():
            commands.label(jot_id, add=[FAILED], remove=[TRIAGING])
            commands.comment(
                jot_id,
                f"Triage failed: {error[:500]}\n\n"
                f"Retry with `jot triage run {jot_id}`.",
                kind="triage.note",
            )

    def run(self, jot_id: int, base: int) -> Jot:
        """Ask the harness and apply the answer."""
        jot = self.context.queries.get(jot_id)
        service = self.context.service(SERVICE)
        if not isinstance(service, HarnessService):  # pragma: no cover - host bug
            raise TypeError("harness@1 has the wrong type")
        result = service.structured(
            StructuredRequest(
                prompt=self.prompt(jot), output_schema=self.schema(jot), system=SYSTEM
            ),
            action="triage",
        )
        proposal = Proposal.parse(result.value, self.settings.max_labels)
        return self.apply(jot_id, proposal, base, result.model)


class TriagePlugin(Plugin):
    """Queues triage for new jots and runs it through the harness."""

    id = "triage"
    name = "Triage"
    version = "1"
    description = "Tidies new jots with an AI harness: title, body, project, labels."
    events = frozenset({"jot.created"})
    requires = frozenset({SERVICE})
    settings_model = TriageSettings

    @staticmethod
    def options(context: PluginContext) -> TriageSettings:
        """Typed settings."""
        settings = context.settings
        if not isinstance(settings, TriageSettings):  # pragma: no cover - host bug
            raise TypeError("triage settings have the wrong type")
        return settings

    @override
    def handle(self, event: Event, context: PluginContext) -> None:
        """Label a new jot ``triaging`` and queue its job."""
        if event.jot_id is not None:
            self.queue(event.jot_id, context)

    def queue(self, jot_id: int, context: PluginContext) -> Job | None:
        """Mark a jot and queue triage unless its profile is excluded."""
        settings = self.options(context)
        jot = context.queries.get(jot_id)
        if settings.profiles and jot.profile not in settings.profiles:
            return None
        jot = context.commands("triage queued").label(
            jot_id, add=[TRIAGING], remove=[FAILED]
        )
        return context.enqueue(
            "triage",
            {"base_version": jot.version},
            jot_id=jot_id,
            dedupe_key=f"jot-{jot_id}",
            max_attempts=settings.max_attempts,
        )

    @override
    def run(self, job: Job, context: PluginContext) -> JsonValue:
        """Triage one jot; on the last failed attempt, mark it and stop."""
        jot_id = job.jot_id
        if jot_id is None:
            return {"skipped": "no jot"}
        jot = context.queries.get(jot_id, include_deleted=True)
        if jot.deleted_at is not None or TRIAGING not in jot.labels:
            return {"skipped": "no longer waiting for triage"}
        base = integer(job.payload.get("base_version")) or jot.version
        triager = Triager(context, self.options(context))
        try:
            done = triager.run(jot_id, base)
        except AppError as err:
            if job.attempts < job.max_attempts:
                raise
            triager.give_up(jot_id, str(err))
            return {"failed": str(err)}
        return {"version": done.version, "labels": list(done.labels)}

    @classmethod
    @override
    def commands(cls, cli: PluginCli) -> typer.Typer:
        """``jot triage run``."""
        app = typer.Typer(no_args_is_help=True, help="AI triage of jots")

        @app.command("run")
        def run(
            ids: Annotated[list[int], typer.Argument(help="Jot ids")],
            *,
            now: Annotated[
                bool, typer.Option("--now", help="Triage here instead of queueing")
            ] = False,
            json: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
        ) -> None:
            """Queue (or with --now, perform) triage for jots."""
            with cli.host(json=json) as host:
                if not host.status(cls.id).enabled:
                    raise InvalidInputError(
                        "Triage is off; run `jot plugin enable triage`"
                    )
                plugin = host.instance(cls.id)
                assert isinstance(plugin, TriagePlugin)  # noqa: S101 - catalog maps it
                jots: list[Jot] = []
                for jot_id in ids:
                    context = host.context_for(cls.id)
                    job = plugin.queue(jot_id, context)
                    if job is None:
                        raise InvalidInputError(
                            f"Jot {jot_id}'s profile is excluded from triage"
                        )
                    if now:
                        host.cancel(job.id)
                        base = integer(job.payload.get("base_version")) or 0
                        triager = Triager(context, cls.options(context))
                        try:
                            jots.append(triager.run(jot_id, base))
                        except AppError as err:
                            triager.give_up(jot_id, str(err))
                            raise
                    else:
                        jots.append(context.queries.get(jot_id))
            verb = "triaged" if now else "queued"
            cli.emit(jots, "".join(f"{jot.id} {verb}: {jot.title}\n" for jot in jots))

        return app
