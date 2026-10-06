"""Settings round trips and action picks in the seeded offline demo."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


class TestAgentSettings:
    """Browser interactions exercise persistence and capture enrichment."""

    def test_edit_save_reload(self, app: Page) -> None:
        """Save a harness label and read it back after a complete page reload."""
        app.get_by_role("button", name="Settings").click()
        app.get_by_label("demo-fast label", exact=True).fill("Quick offline triage")
        app.get_by_role("button", name="Save settings", exact=True).click()
        expect(app.get_by_role("status")).to_contain_text("Settings saved")
        app.reload()
        expect(app.get_by_label("demo-fast label", exact=True)).to_have_value(
            "Quick offline triage"
        )

    def test_capture_pick(self, app: Page) -> None:
        """A non-default capture choice is sent and used by the background worker."""
        app.locator("button.capture-chip").click()
        app.get_by_label("Triage with backend", exact=True).select_option("demo-fast")
        app.get_by_label("Triage with model", exact=True).fill("gpt-6-luna")
        app.get_by_label("Capture a task", exact=True).fill(
            "Capture using the selected harness"
        )
        with app.expect_response(
            lambda response: (
                response.request.method == "POST"
                and response.url.endswith("/api/tasks")
            )
        ) as captured:
            app.get_by_label("Capture a task", exact=True).press("Enter")
        task_id = captured.value.json()["id"]
        app.wait_for_function(
            "async id => {const d = await (await fetch('/api/tasks/' + id)).json();"
            " return d.events.some(e => e.kind === 'enriched'"
            " && e.body.backend === 'demo-fast' && e.body.model === 'gpt-6-luna');}",
            arg=task_id,
        )

    def test_pins(self, app: Page) -> None:
        """Pins respect action filters and clicking one fills the shared picker."""
        app.get_by_role("button", name="List").click()
        app.get_by_role("button", name="Careful plan", exact=True).click()
        expect(app.get_by_label("Plan with backend", exact=True)).to_have_value(
            "demo-deep"
        )
        expect(app.get_by_label("Plan with model", exact=True)).to_have_value("opus")
        expect(
            app.get_by_role("button", name="Quick capture", exact=True)
        ).to_have_count(0)
