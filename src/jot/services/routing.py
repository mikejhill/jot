"""One lean model-selection call with policy validation and audited fallback."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from jot.agents.base import Json, JsonObject, TokenUsage
from jot.agents.registry import MODEL_SUGGESTIONS, BackendRegistry
from jot.core.models import EventBody, EventKind
from jot.db.repository import TaskRepository
from jot.exceptions import ConfigurationError
from jot.harnesses import KINDS

if TYPE_CHECKING:
    from jot.config import Config, JotHome
    from jot.core.models import Task
    from jot.db.connection import Database

logger = logging.getLogger(__name__)
ROUTING_SCHEMA: JsonObject = {
    "type": "object",
    "additionalProperties": False,
    "required": ["harness", "model", "reason"],
    "properties": {key: {"type": "string"} for key in ("harness", "model", "reason")},
}


class Router:
    """Resolve Auto through the facade and retain an explanation on each task."""

    def __init__(self, db: Database, home: JotHome, config: Config) -> None:
        """Bind routing to a settings snapshot and an explicit data home."""
        self.db, self.home, self.config = db, home, config
        self.event: EventBody | None = None

    def candidates(self, action: str) -> list[dict[str, str]]:
        """Enumerate permitted known models, defaults, and action-visible pins."""
        found: list[dict[str, str]] = []
        for name, harness in self.config.effective_harnesses().items():
            if not harness.enabled:
                continue
            models = harness.models.allowed or [
                *MODEL_SUGGESTIONS.get(KINDS[harness.kind], []),
                *harness.models.default.values(),
                "default",
            ]
            found.extend(
                {"harness": name, "model": model}
                for model in dict.fromkeys(models)
                if model != "auto" and harness.models.permits(model)
            )
        for pin in self.config.pins:
            if pin.actions is None or action in pin.actions:
                candidate = {"harness": pin.harness, "model": pin.model}
                if candidate not in found:
                    found.append(candidate)
        return found

    async def resolve(
        self, task: Task, action: str, harness: str | None, model: str | None
    ) -> tuple[str, str | None]:
        """Route Auto; ordinary picks resolve without any additional LLM call."""
        if harness != "auto" and model != "auto":
            return self.config.resolve(action, harness, model)
        chosen, selected = self.config.resolve(action)
        reason = ""
        router_name, router_model = self.config.harness_for("router"), None
        usage = TokenUsage()
        try:
            router_name, router_model = self.config.resolve("router")
            agent = BackendRegistry.configured(
                self.config, "router", router_name, router_model
            )
            guidance = (self.home.path / "instructions/routing.md").read_text(
                encoding="utf-8"
            )
            candidates = self.candidates(action)
            prompt = json.dumps(
                {
                    "action": action,
                    "task": {
                        "title": task.title,
                        "type": task.type,
                        "criticality": task.criticality,
                        "labels": task.labels,
                        "description": task.description[:4000],
                    },
                    "candidates": candidates,
                }
            )
            data = await agent.structured(guidance, prompt, ROUTING_SCHEMA)
            usage = agent.last_usage
            candidate = {
                "harness": Json.text(data.get("harness")),
                "model": Json.text(data.get("model")),
            }
            self._validate(candidate, candidates, Json.text(data.get("reason")))
            chosen, selected = self.config.resolve(
                action, candidate["harness"], candidate["model"]
            )
            reason = Json.text(data.get("reason"))
        except Exception as err:  # noqa: BLE001 - Auto must fall back on any provider failure
            logger.warning("Routing failed; using action default: %s", err)
            reason = f"Fallback to action default: {err}"
        self.event = {
            "action": action,
            "harness": chosen,
            "model": selected or "default",
            "reason": reason,
            "router_harness": router_name,
            "router_model": router_model or "default",
            "input_tokens": usage.input,
            "output_tokens": usage.output,
            "cache_read_tokens": usage.cache_read,
            "cache_write_tokens": usage.cache_write,
            "premium_requests": usage.premium_requests,
        }
        with self.db.write():
            TaskRepository(self.db).audit(
                task.id, EventKind.ROUTING, self.event, actor="router"
            )
        logs = self.home.path / "runs"
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / f"routing-{task.id}.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(self.event) + "\n")
        return chosen, selected

    @staticmethod
    def _validate(
        candidate: dict[str, str], candidates: list[dict[str, str]], reason: str
    ) -> None:
        """Reject invented choices and missing explanations from the model."""
        if candidate not in candidates or not reason.strip():
            raise ConfigurationError(
                "Router returned an invalid candidate or empty reason"
            )
