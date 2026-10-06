"""Parameterized task filters with validated ordering."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from jot.core.models import Clock, Criticality, Status
from jot.exceptions import RepositoryError

TASK_SELECT = """SELECT t.*, (SELECT json_group_array(name) FROM
    (SELECT l.name FROM labels l JOIN task_labels tl ON tl.label_id=l.id
     WHERE tl.task_id=t.id ORDER BY l.name)) AS labels FROM tasks t"""


@dataclass(frozen=True, slots=True)
class TaskQuery:
    """Composable filters; multiple labels mean all labels must match."""

    project: str | None = None
    labels: tuple[str, ...] = ()
    statuses: tuple[Status, ...] = ()
    criticality: Criticality | None = None
    older_than: int | None = None
    age_field: str = "updated_at"
    q: str | None = None
    include_deleted: bool = False
    limit: int | None = None
    order: str = "id"


class QueryBuilder:
    """Build safe SQL using bound values and fixed identifier allowlists."""

    @staticmethod
    def build(query: TaskQuery) -> tuple[str, list[str | int]]:
        """Return SELECT and bindings for a validated query."""
        clauses: list[str] = []
        values: list[str | int] = []
        if not query.include_deleted:
            clauses.append("t.deleted_at IS NULL")
        if query.project is not None:
            clauses.append("t.project_id IN (SELECT id FROM projects WHERE slug=?)")
            values.append(query.project)
        for label in query.labels:
            clauses.append(
                "t.id IN (SELECT tl.task_id FROM task_labels tl "
                "JOIN labels l ON l.id=tl.label_id WHERE l.name=?)"
            )
            values.append(label)
        if query.statuses:
            placeholders = ",".join("?" for _ in query.statuses)
            clauses.append(f"t.status IN ({placeholders})")
            values.extend(query.statuses)
        if query.criticality is not None:
            clauses.append("t.criticality=?")
            values.append(query.criticality)
        QueryBuilder._age(query, clauses, values)
        if query.q is not None:
            clauses.append(
                "t.id IN (SELECT rowid FROM tasks_fts WHERE tasks_fts MATCH ?)"
            )
            values.append(query.q)
        ordering = QueryBuilder._ordering(query.order)
        sql = TASK_SELECT + (" WHERE " + " AND ".join(clauses) if clauses else "")
        sql += " ORDER BY " + ordering
        if query.limit is not None:
            if query.limit < 0:
                raise RepositoryError("limit must be non-negative")
            sql += " LIMIT ?"
            values.append(query.limit)
        return sql, values

    @staticmethod
    def _age(query: TaskQuery, clauses: list[str], values: list[str | int]) -> None:
        """Append an age comparison on one of two known timestamp fields."""
        if query.age_field not in {"updated_at", "created_at"}:
            raise RepositoryError("age_field must be updated_at or created_at")
        if query.older_than is None:
            return
        if query.older_than < 0:
            raise RepositoryError("older_than must be non-negative")
        clauses.append("t." + query.age_field + " < ?")
        values.append(Clock.stamp(Clock.now() - timedelta(days=query.older_than)))

    @staticmethod
    def _ordering(order: str) -> str:
        """Resolve a fixed ordering, with null due dates last."""
        orders = {
            "id": "t.id ASC",
            "-id": "t.id DESC",
            "created_at": "t.created_at ASC, t.id ASC",
            "-created_at": "t.created_at DESC, t.id ASC",
            "updated_at": "t.updated_at ASC, t.id ASC",
            "-updated_at": "t.updated_at DESC, t.id ASC",
            "due_at": "t.due_at IS NULL, t.due_at ASC, t.id ASC",
        }
        if order not in orders:
            raise RepositoryError(f"Unsupported order: {order}")
        return orders[order]
