"""HTTP fixtures with isolated homes and deterministic service boundaries."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from jot.config import JotHome
from jot.core.models import Task
from jot.server import create_app
from jot.services.enrich import EnrichService


class QuietEnrichment:
    """Prevent tests from launching real provider processes in the worker."""

    @staticmethod
    async def pending(_self: EnrichService, limit: int = 10) -> list[Task]:
        """Leave captured tasks pending without contacting a backend."""
        del limit
        return []


@pytest.fixture
def client(home: JotHome, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Run real lifespan and HTTP routes against a temporary JOT_HOME."""
    monkeypatch.setattr(EnrichService, "enrich_pending", QuietEnrichment.pending)
    with TestClient(create_app(home)) as result:
        yield result
