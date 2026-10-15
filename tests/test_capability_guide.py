"""Capability guide wording, task-page economics/export controls and the overview navigation."""

import re

from app.models import TaskStatus
from tests.test_operator_ui import add_task

ALLOWED_DOC_HOSTS = {"docs.factory.com"}


def _guide(client) -> str:
    text = client.get("/dashboard").text
    start = text.index('id="capability-guide"')
    return text[start : text.index("</section>", text.index('id="cap-future-heading"'))]


class TestCapabilityGuide:
    def test_three_statuses_are_kept_apart(self, client):
        guide = _guide(client)
        for heading in ("Implemented in Forge", "Native Factory capability", "Future extension"):
            assert heading in guide
        assert "not integrated in Forge" in guide
        assert "Not built" in guide

    def test_native_breadth_links_official_documentation(self, client):
        guide = _guide(client)
        for family in (
            "Controls and deployment options",
            "Missions",
            "Skills and plugins",
            "Connectors",
            "QA",
            "AutoWiki and Agent Readiness",
            "Agent Effectiveness",
        ):
            assert family in guide, family
        links = re.findall(r'href="(https://[^"]+)"', guide)
        assert len(links) >= 10
        for link in links:
            assert link.split("/")[2] in ALLOWED_DOC_HOSTS, link
        # Pages that returned 404 during research must not be linked.
        assert "/enterprise/index" not in guide
        assert "/delegations/index" not in guide

    def test_no_unsupported_claims(self, client):
        guide = _guide(client).lower()
        for claim in (
            "cognition",
            "devin",
            "only factory",
            "unlike any",
            "exclusive",
            "tenant verified",
            "configured in your tenant",
        ):
            assert claim not in guide, claim
        assert "has not inspected any organization's factory settings" in guide
        assert "trusted workload, not a sandbox" in guide


class TestTaskPageControls:
    def test_task_page_has_effort_form_and_bounded_export(self, client):
        task_id = add_task(status=TaskStatus.COMPLETED)
        text = client.get(f"/tasks/{task_id}").text
        for marker in (
            'id="effort-form"',
            'for="effort-review"',
            'for="effort-rework"',
            "User-entered",
            'id="export-preview-btn"',
            'aria-controls="export-preview"',
            f'href="/api/tasks/{task_id}/evidence?download=true"',
            "/static/task_economics.js",
        ):
            assert marker in text, marker

    def test_static_scripts_are_served(self, client):
        for name in ("economics.js", "task_economics.js"):
            resp = client.get(f"/static/{name}")
            assert resp.status_code == 200, name
            assert "innerHTML" in resp.text and "escapeHtml" in resp.text


class TestNavigation:
    def test_overview_links_every_operator_surface(self, client):
        text = client.get("/dashboard").text
        assert 'id="overview-tab"' in text
        for target in ("overview", "repos", "issues", "reviews", "guide"):
            assert f'data-bs-target="#{target}"' in text or f'href="#{target}"' in text, target

    def test_task_page_returns_to_overview(self, client):
        task_id = add_task(status=TaskStatus.COMPLETED)
        text = client.get(f"/tasks/{task_id}").text
        assert 'href="/dashboard#overview"' in text
        assert 'href="/dashboard#guide"' in text or "/dashboard#guide" in text
