"""Turn raw captured notes into structured tasks using an agent backend."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jot.agents.base import Json, JsonObject
from jot.agents.registry import BackendRegistry
from jot.core.models import Clock, Criticality, EventKind, Project, Status, TaskType
from jot.core.workflow import Workflow
from jot.db.query import TaskQuery
from jot.db.repository import ProjectRepository, TaskRepository
from jot.exceptions import AppError
from jot.services.bus import Topic
from jot.services.instructions import InstructionStore

if TYPE_CHECKING:
    from jot.config import Config, JotHome
    from jot.core.models import Task
    from jot.db.connection import Database
    from jot.services.bus import EventBus

logger = logging.getLogger(__name__)

ACTOR = "enricher"
READY_CONFIDENCE = 0.6
AUTO_ENRICH_STATUSES = frozenset({Status.INBOX, Status.READY})
TOP_LABELS = 60
TITLE_LIMIT = 80
SLUG_PATTERN = re.compile(r"[^a-z0-9]+")

TRIAGE_SCHEMA: JsonObject = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "title",
        "description",
        "project_slug",
        "new_project_name",
        "labels",
        "criticality",
        "type",
        "confidence",
        "questions",
    ],
    "properties": {
        "title": {"type": "string", "description": "Short imperative title"},
        "description": {
            "type": "string",
            "description": "Markdown: problem, desired outcome, acceptance criteria",
        },
        "project_slug": {
            "type": "string",
            "description": "Existing project slug, or empty string",
        },
        "new_project_name": {
            "type": "string",
            "description": "Clearly named project missing from the list, else empty",
        },
        "labels": {"type": "array", "items": {"type": "string"}},
        "criticality": {"type": "string", "enum": [c.value for c in Criticality]},
        "type": {"type": "string", "enum": [t.value for t in TaskType]},
        "confidence": {
            "type": "number",
            "description": "0-1: how clear scope and outcome are",
        },
        "questions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Open questions for the owner; empty if none",
        },
    },
}

SYSTEM = (
    "You triage quick task notes into structured tasks for a personal task "
    "tracker. Follow the owner's triage guidance exactly. Return only the JSON "
    "object requested."
)


@dataclass(frozen=True, slots=True)
class Triage:
    """Validated triage output."""

    title: str
    description: str
    project_slug: str
    new_project_name: str
    labels: tuple[str, ...]
    criticality: Criticality
    type: TaskType
    confidence: float
    questions: tuple[str, ...]

    @classmethod
    def parse(cls, data: JsonObject, fallback_title: str) -> Triage:
        """Coerce untyped model output into a Triage, defaulting bad fields."""
        labels = data.get("labels")
        questions = data.get("questions")
        confidence = data.get("confidence")
        criticality = Json.text(data.get("criticality"))
        task_type = Json.text(data.get("type"))
        return cls(
            title=(Json.text(data.get("title")).strip() or fallback_title)[:200],
            description=Json.text(data.get("description")).strip(),
            project_slug=Json.text(data.get("project_slug")).strip().lower(),
            new_project_name=Json.text(data.get("new_project_name")).strip(),
            labels=tuple(
                Json.text(label) for label in labels if Json.text(label).strip()
            )
            if isinstance(labels, list)
            else (),
            criticality=Criticality(criticality)
            if criticality in Criticality
            else Criticality.MEDIUM,
            type=TaskType(task_type) if task_type in TaskType else TaskType.IDEA,
            confidence=float(confidence)
            if isinstance(confidence, (int, float))
            else 0.0,
            questions=tuple(Json.text(q) for q in questions if Json.text(q).strip())
            if isinstance(questions, list)
            else (),
        )


class EnrichService:
    """Enrich inbox tasks: title, description, project, labels, criticality, type."""

    def __init__(
        self, db: Database, home: JotHome, config: Config, bus: EventBus | None = None
    ) -> None:
        self.db = db
        self.home = home
        self.config = config
        self.bus = bus
        self.tasks = TaskRepository(db)
        self.projects = ProjectRepository(db)
        self.workflow = Workflow(db, config.drawdown.default_flow)
        self.instructions = InstructionStore(home)

    async def enrich(
        self, task_id: int, backend: str | None = None, model: str | None = None
    ) -> Task:
        """Enrich one task and return it (status inbox->ready when confident).

        Raises:
            AgentError: The backend failed; the task is left unchanged.
        """
        task = self.tasks.get(task_id)
        name = backend or self.config.triage.backend
        resolved = self.config.model_for(name, "triage", model)
        agent = BackendRegistry.create(name, resolved)
        data = await agent.structured(SYSTEM, self._prompt(task), TRIAGE_SCHEMA)
        triage = Triage.parse(data, task.title)
        result = self._apply(task, triage, name, resolved or "default")
        self._publish(task_id)
        return result

    async def enrich_pending(self, limit: int = 10) -> list[Task]:
        """Enrich up to ``limit`` unclaimed inbox/ready tasks still needing it.

        A failed task gets a comment and a heuristic fallback so it is not retried
        forever; ``jot enrich <id>`` retries it explicitly.
        """
        pending = [
            task
            for task in self.tasks.query(TaskQuery(order="id"))
            if task.needs_enrichment
            and task.claimed_by is None
            and task.status in AUTO_ENRICH_STATUSES
        ][:limit]
        enriched: list[Task] = []
        for task in pending:
            try:
                enriched.append(await self.enrich(task.id))
            except AppError as err:
                logger.warning("enrichment failed for task %s: %s", task.id, err)
                enriched.append(self._fallback(task, str(err)))
        return enriched

    def _prompt(self, task: Task) -> str:
        """Build the triage prompt: guidance, projects, label vocabulary, note."""
        projects = [
            f"- {p.slug}: {p.name}"
            + (f" (aliases: {', '.join(p.aliases)})" if p.aliases else "")
            + (f" - {p.description}" if p.description else "")
            for p in self.projects.list()
            if not p.archived
        ]
        labels = self._top_labels()
        return "\n\n".join(
            [
                f"## Triage guidance\n\n{self.instructions.get('triage')}",
                "## Existing projects\n\n" + ("\n".join(projects) or "(none yet)"),
                "## Existing labels (reuse these)\n\n"
                + (", ".join(labels) or "(none)"),
                f"## Today\n\n{Clock.now().date().isoformat()}",
                f"## Note to triage\n\n{task.raw_input or task.title}",
                "## Fields already set by the owner (keep them)\n\n"
                + self._prefilled(task),
            ]
        )

    def _prefilled_fields(self, task_id: int) -> set[str]:
        """Return fields the owner set explicitly when capturing the task."""
        for event in self.tasks.events.for_task(task_id):
            if event.kind is EventKind.CREATED:
                return {
                    f for f in str(event.body.get("prefilled") or "").split(",") if f
                }
        return set()

    def _prefilled(self, task: Task) -> str:
        """Describe fields the owner already supplied at capture time."""
        parts = []
        if task.title != task.raw_input:
            parts.append(f"title: {task.title}")
        if task.labels:
            parts.append(f"labels: {', '.join(task.labels)}")
        if task.project_id is not None:
            parts.append(f"project: {self.projects.get(task.project_id).slug}")
        kept = self._prefilled_fields(task.id)
        if "criticality" in kept:
            parts.append(f"criticality: {task.criticality}")
        if "type" in kept:
            parts.append(f"type: {task.type}")
        return "\n".join(parts) or "(none)"

    def _top_labels(self) -> list[str]:
        """Return the most used label names."""
        rows = self.db.connection.execute(
            "SELECT l.name FROM labels l LEFT JOIN task_labels tl ON tl.label_id=l.id "
            "GROUP BY l.id ORDER BY COUNT(tl.task_id) DESC, l.name LIMIT ?",
            (TOP_LABELS,),
        ).fetchall()
        return [str(row[0]) for row in rows]

    def _apply(self, task: Task, triage: Triage, backend: str, model: str) -> Task:
        """Persist triage output, add labels/questions, and promote when confident."""
        project_id = task.project_id or self._project_id(triage)
        kept = self._prefilled_fields(task.id)
        changes: dict[str, object] = {
            "description": triage.description,
            "criticality": triage.criticality,
            "type": triage.type,
            "project_id": project_id,
            "needs_enrichment": False,
        }
        for field in kept & {"description", "criticality", "type"}:
            del changes[field]  # the owner set it explicitly at capture
        if task.title == task.raw_input:
            changes["title"] = triage.title
        with self.db.write():
            self.tasks.update(task.id, changes)
            for label in triage.labels:
                self.tasks.label(
                    task.id, SLUG_PATTERN.sub("-", label.lower()).strip("-")
                )
            for question in triage.questions:
                self.tasks.audit(
                    task.id, EventKind.QUESTION, {"text": question}, actor=ACTOR
                )
            self.tasks.audit(
                task.id,
                EventKind.ENRICHED,
                {
                    "backend": backend,
                    "model": model,
                    "confidence": triage.confidence,
                },
                actor=ACTOR,
            )
        current = self.tasks.get(task.id)
        if current.status is Status.INBOX and triage.confidence >= READY_CONFIDENCE:
            return self.workflow.move(task.id, Status.READY, actor=ACTOR)
        return current

    def _project_id(self, triage: Triage) -> int | None:
        """Resolve an existing project slug or create a clearly named new one."""
        known = {p.slug: p for p in self.projects.list()}
        if triage.project_slug in known:
            return known[triage.project_slug].id
        if not triage.new_project_name:
            return None
        slug = SLUG_PATTERN.sub("-", triage.new_project_name.lower()).strip("-")
        if not slug:
            return None
        if slug in known:
            return known[slug].id
        return self.projects.create(Project(slug=slug, name=triage.new_project_name)).id

    def _fallback(self, task: Task, error: str) -> Task:
        """Heuristic enrichment: alias project match and a trimmed title."""
        text = (task.raw_input or task.title).lower()
        project_id = task.project_id
        if project_id is None:
            for project in self.projects.list():
                names = [project.slug, project.name.lower(), *project.aliases]
                if any(name.lower() in text for name in names if name):
                    project_id = project.id
                    break
        changes: dict[str, object] = {
            "needs_enrichment": False,
            "project_id": project_id,
        }
        if task.title == task.raw_input and len(task.title) > TITLE_LIMIT:
            changes["title"] = task.title[: TITLE_LIMIT - 1] + "…"
        with self.db.write():
            self.tasks.update(task.id, changes)
            self.tasks.audit(
                task.id,
                EventKind.COMMENT,
                {
                    "text": f"Automatic enrichment failed ({error[:300]}); "
                    "used heuristics. Run `jot enrich` to retry."
                },
                actor=ACTOR,
            )
        self._publish(task.id)
        return self.tasks.get(task.id)

    def _publish(self, task_id: int) -> None:
        """Notify live UIs that a task changed."""
        if self.bus is not None:
            self.bus.publish(Topic.TASK, {"id": task_id})
