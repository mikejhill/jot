"""Browser tests for the web UI shell."""

from __future__ import annotations

import json
import urllib.request

import pytest
from playwright.sync_api import Page, expect
from tests.e2e.conftest import LiveServer

pytestmark = pytest.mark.e2e

SLOT_SCRIPT = """() => {
  const {html, register} = window.jot;
  register('jot.badges', ({jot}) => html`<b class="ext-badge">B${jot.id}</b>`);
  register('jot.actions', () => html`<button>Ext action</button>`);
  register('jot.panels', ({detail}) =>
    html`<p class="ext-panel">P${detail.jot.version}</p>`);
  register('views', {
    id: 'ext', label: 'Extension', render: () => html`<p class="ext-view">hi</p>`,
  });
  register('settings.pages', {
    id: 's', label: 'Ext settings',
    render: () => html`<p class="ext-settings">s</p>`,
  });
}"""


def api(live: LiveServer, method: str, path: str, body: object = None) -> object:
    """Call the API directly, as another client would."""
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(  # noqa: S310 - local test server
        f"{live.url}/api{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request) as response:  # noqa: S310 - local test server
        text = response.read()
    return json.loads(text) if text else None


def capture(page: Page, text: str) -> None:
    """Type into the capture bar and press Enter."""
    page.get_by_label("Capture a jot").fill(text)
    page.get_by_label("Capture a jot").press("Enter")


class TestCaptureAndList:
    """Capture lands in the list; filters narrow it."""

    def test_capture(self, page: Page, live: LiveServer) -> None:
        """Enter saves a jot, shows a toast, and lists it."""
        page.goto("/")
        page.keyboard.press("Control+k")
        expect(page.get_by_label("Capture a jot")).to_be_focused()
        capture(page, "Optimize sales model")
        expect(page.get_by_role("status")).to_contain_text("Captured #1")
        expect(page.get_by_role("list", name="Jots")).to_contain_text(
            "Optimize sales model"
        )
        jots = api(live, "GET", "/jots")
        assert isinstance(jots, list)
        assert jots[0]["title"] == "Optimize sales model"

    def test_multiline_and_profile(self, page: Page, live: LiveServer) -> None:
        """The first line is the title, the rest the body; profile is honored."""
        page.goto("/")
        page.get_by_label("Profile for new jot").select_option("task")
        page.get_by_label("Capture a jot").fill("Ship it\nwith details")
        page.get_by_role("button", name="Add", exact=True).click()
        expect(page.get_by_role("list", name="Jots")).to_contain_text("todo")
        jot = api(live, "GET", "/jots/1")
        assert isinstance(jot, dict)
        assert (jot["jot"]["title"], jot["jot"]["body"], jot["jot"]["profile"]) == (
            "Ship it",
            "with details",
            "task",
        )

    def test_filters(self, page: Page, live: LiveServer) -> None:
        """Category chips, search, and labels filter the list."""
        api(live, "POST", "/jots", {"title": "alpha", "labels": ["x"]})
        api(
            live,
            "POST",
            "/jots",
            {"title": "beta", "profile": "task", "status": "done"},
        )
        page.goto("/")
        jots = page.get_by_role("list", name="Jots")
        expect(jots).to_contain_text("alpha")
        expect(jots).not_to_contain_text("beta")
        page.get_by_role("button", name="done").click()
        expect(jots).to_contain_text("beta")
        page.get_by_label("Search").fill("bet")
        expect(jots).not_to_contain_text("alpha")
        page.get_by_label("Search").fill("")
        page.get_by_label("Filter by label").select_option("x")
        expect(jots).not_to_contain_text("beta")

    def test_other_clients_appear(self, page: Page, live: LiveServer) -> None:
        """Changes from the CLI or API show up without a reload."""
        page.goto("/")
        expect(page.get_by_text("No jots match")).to_be_visible()
        api(live, "POST", "/jots", {"title": "from the cli"})
        expect(page.get_by_role("list", name="Jots")).to_contain_text(
            "from the cli", timeout=10_000
        )


class TestDrawer:
    """The jot drawer edits every core attribute."""

    def test_edit_everything(self, page: Page, live: LiveServer) -> None:
        """Title, body, status, labels, comments, links, rank, archive."""
        api(live, "POST", "/projects", {"slug": "p", "name": "Proj"})
        api(live, "POST", "/jots", {"title": "first", "profile": "task"})
        api(live, "POST", "/jots", {"title": "second"})
        page.goto("/#/jot/1/list")
        drawer = page.get_by_role("dialog", name="Jot 1")
        drawer.get_by_label("Title").fill("First, renamed")
        drawer.get_by_label("Body").fill("**bold** detail")
        drawer.get_by_role("button", name="Save").click()
        expect(drawer.locator(".preview strong")).to_have_text("bold")
        expect(drawer).to_contain_text("v2")
        drawer.get_by_label("Status").select_option("doing")
        expect(drawer.locator(".drawer-header")).to_contain_text("v3")
        drawer.get_by_label("Project").select_option("p")
        expect(drawer).to_contain_text("v4")
        drawer.get_by_label("New label").fill("urgent")
        drawer.get_by_role("button", name="Add", exact=True).click()
        expect(drawer).to_contain_text("#urgent")
        drawer.get_by_label("New comment").fill("looks good")
        drawer.get_by_role("button", name="Comment").click()
        expect(drawer.locator(".comment")).to_contain_text("looks good")
        drawer.get_by_label("Link to jot id").fill("2")
        drawer.get_by_role("button", name="Link").click()
        expect(drawer.locator(".links")).to_contain_text("#1 relates #2")
        drawer.get_by_role("button", name="Move to bottom").click()
        expect(
            page.get_by_role("list", name="Jots").locator("li").last
        ).to_contain_text("First, renamed")
        drawer.get_by_role("button", name="Archive").click()
        expect(drawer.get_by_role("button", name="Unarchive")).to_be_visible()
        expect(drawer.locator(".history")).to_contain_text("jot.archived")
        detail = api(live, "GET", "/jots/1")
        assert isinstance(detail, dict)
        jot = detail["jot"]
        assert (jot["status"], jot["project"], jot["labels"]) == (
            "doing",
            "p",
            ["urgent"],
        )
        assert detail["history"][1]["origin"] == "ui"

    def test_remove_label_comment_link(self, page: Page, live: LiveServer) -> None:
        """Labels, comments, and links can be removed."""
        api(live, "POST", "/jots", {"title": "a", "labels": ["gone"]})
        api(live, "POST", "/jots", {"title": "b"})
        api(live, "POST", "/jots/1/comments", {"body": "bye"})
        api(live, "POST", "/links", {"src": 1, "dst": 2})
        page.goto("/#/jot/1/list")
        drawer = page.get_by_role("dialog", name="Jot 1")
        drawer.get_by_label("Remove label gone").click()
        expect(drawer).not_to_contain_text("#gone")
        drawer.get_by_label("Delete comment 1").click()
        expect(drawer.locator(".comment")).to_have_count(0)
        drawer.get_by_label("Remove link").click()
        expect(drawer.locator(".links li")).to_have_count(0)

    def test_conflict(self, page: Page, live: LiveServer) -> None:
        """A save over a newer version is refused and the drawer reloads."""
        api(live, "POST", "/jots", {"title": "base"})
        page.goto("/#/jot/1/list")
        drawer = page.get_by_role("dialog", name="Jot 1")
        expect(drawer).to_contain_text("v1")
        api(live, "PATCH", "/jots/1", {"title": "changed elsewhere"})
        drawer.get_by_label("Title").fill("mine")
        drawer.get_by_role("button", name="Save").click()
        expect(page.get_by_role("status")).to_contain_text("changed elsewhere")
        expect(drawer.get_by_label("Title")).to_have_value("changed elsewhere")

    def test_fields_and_delete(
        self, page: Page, live: LiveServer, home: object
    ) -> None:
        """Profile fields render typed inputs; delete and restore work."""
        from jot.config import JotHome  # noqa: PLC0415

        assert isinstance(home, JotHome)
        home.config_file.write_text(
            '[profiles.bug]\nstatuses = [{id = "new", category = "open"}]\n'
            '[profiles.bug.fields.severity]\ntype = "enum"\noptions = ["low", "high"]\n'
            '[profiles.bug.fields.points]\ntype = "number"\n',
            encoding="utf-8",
        )
        api(live, "POST", "/jots", {"title": "crash", "profile": "bug"})
        page.goto("/#/jot/1/list")
        drawer = page.get_by_role("dialog", name="Jot 1")
        drawer.get_by_label("severity").select_option("high")
        drawer.get_by_label("points").fill("3")
        drawer.get_by_role("button", name="Save").click()
        expect(drawer).to_contain_text("v2")
        detail = api(live, "GET", "/jots/1")
        assert isinstance(detail, dict)
        assert detail["jot"]["metadata"]["fields"] == {"severity": "high", "points": 3}
        drawer.get_by_role("button", name="Delete").click()
        expect(drawer.get_by_role("button", name="Restore")).to_be_visible()
        drawer.get_by_role("button", name="Restore").click()
        page.keyboard.press("Escape")
        expect(drawer).to_have_count(0)


class TestViews:
    """Board, settings, routing, layout, and extension slots."""

    def test_board_and_settings(self, page: Page, live: LiveServer) -> None:
        """The board groups by category; settings lists profiles."""
        api(live, "POST", "/jots", {"title": "open one"})
        api(
            live,
            "POST",
            "/jots",
            {"title": "busy", "profile": "task", "status": "doing"},
        )
        page.goto("/#/board")
        expect(page.get_by_role("region", name="open")).to_contain_text("open one")
        expect(page.get_by_role("region", name="active")).to_contain_text("busy")
        page.get_by_role("region", name="active").get_by_text("busy").click()
        expect(page.get_by_role("dialog", name="Jot 2")).to_be_visible()
        assert page.url.endswith("#/jot/2/board")
        page.get_by_role("link", name="Settings").click()
        expect(page.locator(".settings")).to_contain_text("wont do")

    def test_phone_layout(self, page: Page, live: LiveServer) -> None:
        """No horizontal page scroll at phone width."""
        api(live, "POST", "/jots", {"title": "x" * 200, "labels": ["a", "b"]})
        page.set_viewport_size({"width": 375, "height": 800})
        page.goto("/")
        expect(page.get_by_role("list", name="Jots")).to_be_visible()
        width = page.evaluate("document.documentElement.scrollWidth")
        assert width <= 375
        page.goto("/#/jot/1/list")
        expect(page.get_by_role("dialog", name="Jot 1")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth") <= 375

    def test_extension_slots(self, page: Page, live: LiveServer) -> None:
        """Registered views, badges, actions, and panels render."""
        api(live, "POST", "/jots", {"title": "slotted"})
        page.goto("/")
        expect(page.get_by_role("list", name="Jots")).to_contain_text("slotted")
        page.evaluate(SLOT_SCRIPT)
        expect(page.locator(".ext-badge")).to_have_text("B1")
        page.locator(".row").first.click()
        expect(page.get_by_role("button", name="Ext action")).to_be_visible()
        expect(page.locator(".ext-panel")).to_have_text("P1")
        page.get_by_role("link", name="Extension").click()
        expect(page.locator(".ext-view")).to_have_text("hi")
        page.get_by_role("link", name="Settings").click()
        expect(page.locator(".ext-settings")).to_be_visible()
        with pytest.raises(Exception, match="Unknown slot"):
            page.evaluate("() => window.jot.register('nope', () => null)")


class TestPlugins:
    """Plugins are administered and extend the UI from the settings page."""

    def test_rules_plugin(self, page: Page, live: LiveServer) -> None:
        """Enable rules, configure one, and watch it label a new jot."""
        page.goto("/#/settings")
        panel = page.get_by_role("article", name="Plugin rules")
        expect(panel).to_contain_text("Rules")
        panel.get_by_label("Enabled").check()
        expect(panel).to_contain_text("Behind by")
        expect(page.get_by_role("heading", name="Rules", exact=True)).to_be_visible()
        expect(page.locator(".settings")).to_contain_text("No rules yet")
        panel.get_by_text("Settings (JSON)").click()
        rules = {
            "rules": [
                {
                    "name": "money",
                    "when": {"text": "invoice"},
                    "then": {"add_labels": ["finance"]},
                }
            ],
            "ignore_plugins": True,
        }
        editor = panel.get_by_label("Settings for rules")
        editor.fill("{ not json")
        panel.get_by_role("button", name="Save settings").click()
        expect(page.get_by_role("status")).to_contain_text("not valid JSON")
        editor.fill(json.dumps({"rules": [{"name": "x", "when": {"pattern": "("}}]}))
        panel.get_by_role("button", name="Save settings").click()
        expect(page.get_by_role("status").last).to_contain_text("Invalid settings")
        editor.fill(json.dumps(rules))
        panel.get_by_role("button", name="Save settings").click()
        expect(page.get_by_role("status").last).to_contain_text("Saved Rules settings")
        page.reload()
        expect(page.locator(".rules-list")).to_contain_text("money")
        page.get_by_role("link", name="List").click()
        page.get_by_label("Capture a jot").fill("Pay the invoice")
        page.get_by_label("Capture a jot").press("Enter")
        expect(page.get_by_role("list", name="Jots")).to_contain_text("#finance")
        page.get_by_role("link", name="Settings").click()
        expect(panel).to_contain_text("Behind by 0 events")
        panel.get_by_label("Enabled").uncheck()
        expect(panel).not_to_contain_text("Behind by")
        assert api(live, "GET", "/plugins/rules")["enabled"] is False  # ty: ignore[not-subscriptable]
