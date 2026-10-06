"""Task REST routes delegating lifecycle changes to the domain workflow."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from jot.core.models import EventKind, Status, Task, TaskEvent
from jot.core.prioritize import Prioritizer
from jot.core.questions import Questions
from jot.db.query import TaskQuery
from jot.exceptions import RepositoryError, WorkflowError
from jot.server.runtime import RuntimeAccess
from jot.server.schemas import Capture, Comment, Move, TaskFields, TaskFilters


class TaskRoutes:
    """Capture, search, edit, and audit tasks with live invalidation."""

    def __init__(self, access: RuntimeAccess) -> None:
        self._access = access
        self.router = APIRouter(prefix="/api/tasks")
        self.router.add_api_route("", self.list_tasks, methods=["GET"])
        self.router.add_api_route("", self.create, methods=["POST"], status_code=201)
        self.router.add_api_route("/{task_id}", self.get, methods=["GET"])
        self.router.add_api_route("/{task_id}", self.patch, methods=["PATCH"])
        self.router.add_api_route("/{task_id}", self.delete, methods=["DELETE"])
        self.router.add_api_route("/{task_id}/move", self.move, methods=["POST"])
        self.router.add_api_route("/{task_id}/comment", self.comment, methods=["POST"])

    async def list_tasks(self, filters: Annotated[TaskFilters, Query()]) -> list[Task]:
        """Filter before ranking and limiting so results remain globally ordered."""
        runtime = self._access.runtime
        values = filters.model_dump(exclude={"sort", "type", "status", "limit"})
        values["statuses"] = tuple(filters.statuses) + (
            (filters.status,) if filters.status else ()
        )
        values["labels"] = tuple(filters.labels)
        if filters.sort in {"updated", "created"}:
            values["order"] = f"-{filters.sort}_at"
        tasks = runtime.tasks.query(TaskQuery(**values))
        if filters.type is not None:
            tasks = [task for task in tasks if task.type == filters.type]
        if filters.sort == "priority":
            priorities = {p.id: p.priority for p in runtime.projects.list()}
            tasks = Prioritizer(priorities).rank(tasks)
        return tasks if filters.limit is None else tasks[: filters.limit]

    def _content(self, body: TaskFields) -> dict[str, object]:
        """Resolve project slugs and reject conflicting project identifiers."""
        values: dict[str, object] = {
            name: getattr(body, name)
            for name in body.model_fields_set - {"labels", "text"}
        }
        if "project" in values:
            if "project_id" in values:
                raise RepositoryError("Use project or project_id, not both")
            slug = values.pop("project")
            values["project_id"] = (
                self._access.runtime.projects.by_slug(str(slug)).id if slug else None
            )
        return values

    async def create(self, body: Capture) -> Task:
        """Persist quick capture immediately, leaving enrichment to the worker."""
        if not body.text.strip():
            raise RepositoryError("Capture text cannot be blank")
        values = self._content(body)
        task = Task.model_validate(
            {"title": body.text.strip().splitlines()[0][:200]}
            | values
            | {"raw_input": body.text, "source": "ui", "labels": body.labels or []}
        )
        prefilled = [
            name
            for name, value in (
                ("criticality", body.criticality),
                ("type", body.type),
                ("project", body.project),
                ("description", body.description),
            )
            if value
        ]
        result = self._access.runtime.tasks.create(task, prefilled=prefilled)
        self._access.runtime.changed(result.id)
        return result

    async def get(self, task_id: int) -> dict[str, object]:
        """Return content, project, history, questions, runs, and valid moves."""
        runtime = self._access.runtime
        task = runtime.tasks.get(task_id)
        events = runtime.tasks.events.for_task(task_id)
        return {
            "task": task,
            "events": events,
            "questions": Questions.view(events),
            "resume_phase": self._resume_phase(task, events),
            "runs": [run for run in runtime.runs.list() if run.task_id == task_id],
            "project": runtime.projects.get(task.project_id)
            if task.project_id
            else None,
            "transitions": self._transitions(task),
        }

    @staticmethod
    def _resume_phase(task: Task, events: list[TaskEvent]) -> str | None:
        """Name the run phase that answering a needs_input task resumes."""
        if task.status is not Status.NEEDS_INPUT:
            return None
        resumed = Questions.resume_status(events)
        return "execute" if resumed is Status.EXECUTING else "plan"

    def _transitions(self, task: Task) -> list[str]:
        """Expose only domain-approved moves for unclaimed, live tasks."""
        if task.claimed_by or task.deleted_at:
            return []
        workflow = self._access.runtime.workflow
        result: list[str] = []
        for target in type(task.status):
            if target in {Status.PLANNING, Status.EXECUTING}:
                continue  # entered only by starting a run
            try:
                workflow.validate(
                    task.status,
                    target,
                    workflow.flow_for(task),
                    resume=task.blocked_prior_status,
                )
            except WorkflowError:
                continue
            result.append(target.value)
        return result

    async def patch(self, task_id: int, body: TaskFields) -> Task:
        """Atomically edit content and replace labels without changing lifecycle."""
        runtime = self._access.runtime
        with runtime.db.write():
            task = runtime.tasks.update(task_id, self._content(body))
            if body.labels is not None:
                labels = {label.strip().lower() for label in body.labels}
                for label in set(task.labels) - labels:
                    runtime.tasks.label(task_id, label, remove=True)
                for label in labels - set(task.labels):
                    runtime.tasks.label(task_id, label)
        runtime.changed(task_id)
        return runtime.tasks.get(task_id)

    async def move(self, task_id: int, body: Move) -> Task:
        """Move only through a valid domain transition."""
        runtime = self._access.runtime
        task = runtime.workflow.move(task_id, body.status, actor="ui", manual=True)
        runtime.changed(task_id)
        return task

    async def comment(self, task_id: int, body: Comment) -> TaskEvent:
        """Append human feedback to the task timeline."""
        runtime = self._access.runtime
        runtime.tasks.get(task_id)
        event = runtime.tasks.audit(
            task_id, EventKind.COMMENT, {"text": body.comment}, actor="ui"
        )
        runtime.changed(task_id)
        return event

    async def delete(self, task_id: int) -> dict[str, bool]:
        """Soft-delete a task while retaining history."""
        runtime = self._access.runtime
        runtime.tasks.delete(task_id)
        runtime.changed(task_id)
        return {"deleted": True}
