"""End-to-end UI/UX checks in a real browser against the demo workspace."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


class TestBoardAndCapture:
    """Main board, capture bar, and theme."""

    def test_board_columns_and_cards(self, app: Page) -> None:
        """The board shows status columns and seeded cards with labels."""
        for column in ("Inbox", "Ready", "Awaiting Approval", "Review"):
            expect(app.locator(".column", has_text=column).first).to_be_visible()
        card = app.locator(".card", has_text="Rate-limit the public scoring endpoint")
        expect(card).to_contain_text("critical")
        expect(app.locator(".card", has_text="dark-mode")).to_contain_text("No project")

    def test_capture_adds_task(self, app: Page) -> None:
        """Ctrl+K focuses capture; Enter creates an inbox card immediately."""
        app.keyboard.press("Control+k")
        expect(app.locator("#capture")).to_be_focused()
        app.keyboard.type("orbit api - add request tracing")
        app.keyboard.press("Enter")
        expect(app.locator(".card", has_text="add request tracing")).to_be_visible()

    def test_theme_toggle(self, app: Page) -> None:
        """The theme button switches to dark mode."""
        app.get_by_role("button", name="Toggle light and dark theme").click()
        theme = app.evaluate("document.documentElement.dataset.theme")
        assert theme in {"dark", "light"}
        app.get_by_role("button", name="Toggle light and dark theme").click()
        assert app.evaluate("document.documentElement.dataset.theme") != theme


class TestTaskDrawer:
    """Run output and timeline rendering in the task drawer."""

    def test_run_output_markdown_usage_and_collapsed_steps(self, app: Page) -> None:
        """Agent Markdown renders; tool/thinking collapse; usage shows per turn."""
        app.locator(".card", has_text="Retry failed exports").click()
        log = app.locator(".drawer .run-log").first
        expect(log.locator("table")).to_be_visible()
        expect(log.locator("h2", has_text="Summary")).to_be_visible()
        activity = log.locator("details.activity").first
        expect(activity).to_contain_text("3 tool calls · 1 thinking")
        expect(activity.locator(".log.tool").first).to_be_hidden()
        activity.locator("summary").click()
        expect(activity.locator(".log.tool").first).to_be_visible()
        expect(log.locator(".usage").first).to_contain_text("claude-sonnet")
        expect(log.locator(".usage.total")).to_contain_text("out 880")
        expect(app.locator(".drawer .run .usage.total").first).to_contain_text("Total")

    def test_structured_plan_and_readable_timeline(self, app: Page) -> None:
        """Plan JSON renders as sections; system events read as sentences."""
        app.locator(".card", has_text="Add functional health checks").click()
        structured = app.locator(".drawer .run-log .structured").first
        expect(structured.locator("h5", has_text="Plan")).to_be_visible()
        expect(structured.locator("ol").last.locator("li")).to_have_count(2)
        expect(structured).not_to_contain_text('"summary"')
        timeline = app.locator(".drawer .timeline")
        expect(timeline.locator(".event-summary", has_text="→").first).to_be_visible()
        expect(timeline.locator("pre")).to_have_count(0)


class TestListView:
    """One-click row actions and inline plan review."""

    def test_row_actions_and_inline_plan(self, app: Page) -> None:
        """Rows offer actions by status; expanding shows the plan and questions."""
        app.get_by_role("button", name="List").click()
        ready = app.locator("tr", has_text="Rate-limit the public scoring endpoint")
        expect(ready.get_by_role("button", name="Plan")).to_be_visible()
        expect(ready.get_by_role("button", name="Run now")).to_be_visible()
        waiting = app.locator("tr", has_text="Add functional health checks")
        expect(waiting.get_by_role("button", name="Approve")).to_be_visible()
        app.get_by_role("button", name="Expand task 1").click()
        expanded = app.locator(".expanded")
        expect(expanded).to_contain_text("Latest plan")
        expect(expanded.locator("strong", has_text="PostgreSQL")).to_be_visible()
        expect(app.locator(".picker select").first).not_to_have_value("")


class TestResponsive:
    """Narrow screens stay usable."""

    def test_no_page_level_horizontal_scroll(self, app: Page) -> None:
        """At phone width the page itself does not scroll sideways."""
        app.set_viewport_size({"width": 390, "height": 844})
        app.reload()
        app.locator("#capture").wait_for()
        overflow = app.evaluate(
            "document.scrollingElement.scrollWidth - window.innerWidth"
        )
        assert overflow <= 1
