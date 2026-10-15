"""Commit-bound readiness, operator approval and the delivery HTTP API.

Git work runs against labeled local fixtures (``tests/delivery_fixtures.py``);
no request reaches GitHub or the Droid SDK.
"""

import json

import pytest

from app import publishing, remote_ci
from app.check_config import DEFAULT_CHECK_TIMEOUT_SECONDS, configured_checks
from app.config import get_settings
from app.database import db_session
from app.delivery import DeliveryRefusedError, approve_candidate, task_delivery
from app.delivery_models import CheckStatus, DeliveryApproval
from app.droid_client import task_workspace_dir
from app.models import TaskStatus
from app.review_policy import ReviewVerdict
from tests.conftest import OPERATOR_TOKEN, PROTECTED_BASE_URL
from tests.delivery_fixtures import (
    BRANCH,
    REPO,
    FakeDeliveryGitHub,
    add_candidate_review,
    add_candidate_task,
    add_check_run,
    configure,
    recandidate,
)

BEARER = {"Authorization": f"Bearer {OPERATOR_TOKEN}", "Origin": PROTECTED_BASE_URL}


@pytest.fixture
def remote(monkeypatch, tmp_path):
    monkeypatch.setattr(publishing, "PR_HEAD_RECHECK_DELAYS", (0.0,))
    return configure(monkeypatch, tmp_path)


def codes(blockers):
    return [b["code"] for b in blockers]


class TestCheckConfiguration:
    def settings(self, monkeypatch, value):
        monkeypatch.setenv("FORGE_ACCEPTANCE_CHECKS", value)
        get_settings.cache_clear()
        return configured_checks()

    def test_valid_list(self, monkeypatch):
        config = self.settings(
            monkeypatch,
            json.dumps(
                [{"name": "unit", "argv": ["pytest", "-q"]}, {"name": "lint", "argv": ["ruff"], "timeout_seconds": 30}]
            ),
        )
        assert config.error is None
        assert config.names == ["unit", "lint"]
        assert [c.timeout_seconds for c in config.checks] == [DEFAULT_CHECK_TIMEOUT_SECONDS, 30.0]
        assert config.to_dict()["checks"][0]["command"] == "pytest -q"

    def test_unset_is_not_configured_rather_than_passing(self, monkeypatch):
        config = self.settings(monkeypatch, "")
        assert (config.checks, config.error) == ((), None)

    @pytest.mark.parametrize(
        "value",
        [
            "not json",
            "[]",
            '{"name": "x"}',
            '[{"name": "x"}]',
            '[{"name": "x", "argv": []}]',
            '[{"name": "x", "argv": "pytest -q"}]',
            '[{"name": "", "argv": ["a"]}]',
            '[{"name": "x", "argv": ["a"]}, {"name": "x", "argv": ["b"]}]',
            '[{"name": "x", "argv": ["a"], "timeout_seconds": 0}]',
            '[{"name": "x", "argv": ["a"], "timeout_seconds": 99999}]',
            '[{"name": "x", "argv": ["a"], "timeout_seconds": true}]',
            json.dumps([{"name": f"c{i}", "argv": ["a"]} for i in range(21)]),
        ],
    )
    def test_invalid_configuration_is_an_error(self, monkeypatch, value):
        config = self.settings(monkeypatch, value)
        assert config.checks == () and config.error


class TestReadiness:
    def test_agent_completion_alone_is_not_ready(self, remote):
        task_id, sha, _ws = add_candidate_task(remote)

        state = task_delivery(task_id)

        assert state["candidate"]["sha"] == sha
        assert state["checks"]["status"] == "missing" and state["checks"]["executed_by"] == "app"
        assert state["checks"]["configuration"]["checks"][0]["name"] == "unit"
        assert state["review"]["status"] == "missing"
        assert "runs no commands" in state["review"]["tools"]
        assert state["readiness"]["state"] == "not_ready"
        assert codes(state["readiness"]["approval_blockers"]) == ["checks_missing", "review_missing"]
        assert state["approval"]["status"] == "not_approved"
        assert state["destination"] == {
            "repository": REPO,
            "branch": BRANCH,
            "pull_request": "draft",
            "base_branch": "repository default branch, read from GitHub at publication",
            "allowed": True,
            "reason": None,
            "force_push": False,
            "merge": "never",
        }
        assert state["remote_ci"]["state"] == "not_published"
        assert state["remote_ci"]["prerequisite_for_publication"] is False

    def test_passing_checks_and_approving_review_mean_ready_for_approval_only(self, remote):
        task_id, sha, _ws = add_candidate_task(remote)
        run_id = add_check_run(task_id, sha)
        review_id = add_candidate_review(task_id, sha, remote.base_sha)

        state = task_delivery(task_id)

        assert state["checks"]["latest_run"]["evaluated_sha"] == sha
        assert state["checks"]["latest_run"]["id"] == run_id
        assert state["review"]["latest"]["id"] == review_id
        assert state["review"]["latest"]["commit_sha"] == sha
        assert state["review"]["latest"]["ai_verdict"] == ReviewVerdict.APPROVE
        assert state["review"]["latest"]["github_outcome"]["formal_github_review"] is False
        readiness = state["readiness"]
        assert (readiness["state"], readiness["ready_for_approval"], readiness["approval_blockers"]) == (
            "ready_for_approval",
            True,
            [],
        )
        assert codes(readiness["publish_blockers"]) == ["approval_missing"]
        assert state["approval"]["current"] is None and state["publication"] is None
        assert state["remote_ci"]["state"] == "not_published"

    @pytest.mark.parametrize(
        ("setup", "expected"),
        [
            (lambda t, s, b: add_check_run(t, s, CheckStatus.FAILED), "checks_failed"),
            (lambda t, s, b: add_check_run(t, s, CheckStatus.TIMED_OUT), "checks_incomplete"),
            (lambda t, s, b: add_check_run(t, s, CheckStatus.RUNNING), "checks_running"),
            (lambda t, s, b: add_check_run(t, s, evaluated="d" * 40), "checks_incomplete"),
            (lambda t, s, b: add_check_run(t, "d" * 40), "checks_missing"),
        ],
        ids=["failed", "timed_out", "running", "evaluated_other_sha", "other_candidate"],
    )
    def test_check_evidence_must_pass_on_this_exact_sha(self, remote, setup, expected):
        task_id, sha, _ws = add_candidate_task(remote)
        add_candidate_review(task_id, sha, remote.base_sha)
        setup(task_id, sha, remote.base_sha)

        assert codes(task_delivery(task_id)["readiness"]["approval_blockers"]) == [expected]

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"verdict": ReviewVerdict.REQUEST_CHANGES}, "review_changes_requested"),
            ({"verdict": ReviewVerdict.INCONCLUSIVE}, "review_inconclusive"),
            ({"diff_complete": False}, "review_incomplete"),
            ({"status": TaskStatus.FAILED}, "review_incomplete"),
            ({"status": TaskStatus.RUNNING}, "review_pending"),
        ],
        ids=["changes_requested", "inconclusive", "partial_diff", "failed", "pending"],
    )
    def test_review_must_approve_this_exact_sha(self, remote, kwargs, expected):
        task_id, sha, _ws = add_candidate_task(remote)
        add_check_run(task_id, sha)
        add_candidate_review(task_id, sha, remote.base_sha, **kwargs)

        assert codes(task_delivery(task_id)["readiness"]["approval_blockers"]) == [expected]

    def test_review_against_another_base_does_not_count(self, remote):
        task_id, sha, _ws = add_candidate_task(remote)
        add_check_run(task_id, sha)
        add_candidate_review(task_id, sha, "9" * 40)

        assert codes(task_delivery(task_id)["readiness"]["approval_blockers"]) == ["review_incomplete"]

    def test_added_check_since_last_run_needs_a_rerun(self, remote, monkeypatch):
        task_id, sha, _ws = add_candidate_task(remote)
        add_check_run(task_id, sha)
        add_candidate_review(task_id, sha, remote.base_sha)
        monkeypatch.setenv(
            "FORGE_ACCEPTANCE_CHECKS",
            json.dumps([{"name": "unit", "argv": ["true"]}, {"name": "lint", "argv": ["true"]}]),
        )
        get_settings.cache_clear()

        assert codes(task_delivery(task_id)["readiness"]["approval_blockers"]) == ["checks_outdated_configuration"]

    @pytest.mark.parametrize(
        ("value", "expected"), [("", "checks_not_configured"), ("[", "checks_configuration_invalid")]
    )
    def test_no_or_invalid_checks_block_approval(self, remote, monkeypatch, value, expected):
        task_id, sha, _ws = add_candidate_task(remote)
        add_check_run(task_id, sha)
        add_candidate_review(task_id, sha, remote.base_sha)
        monkeypatch.setenv("FORGE_ACCEPTANCE_CHECKS", value)
        get_settings.cache_clear()

        assert codes(task_delivery(task_id)["readiness"]["approval_blockers"]) == [expected]


class TestApproval:
    @pytest.mark.asyncio
    async def test_records_exact_sha_time_and_evidence_without_merge_permission(self, remote):
        task_id, sha, _ws = add_candidate_task(remote)
        run_id = add_check_run(task_id, sha)
        review_id = add_candidate_review(task_id, sha, remote.base_sha)

        result = await approve_candidate(task_id, sha, approved_via="session")

        approval = result["approval"]
        assert result["already_approved"] is False
        assert (approval["candidate_sha"], approval["check_run_id"], approval["review_id"]) == (sha, run_id, review_id)
        assert approval["approved_at"] and approval["approved_via"] == "session"
        assert (approval["kind"], approval["grants_merge"], approval["applies_to_current_candidate"]) == (
            "delivery_approval",
            False,
            True,
        )
        state = task_delivery(task_id)
        assert state["readiness"]["state"] == "approved"
        assert state["readiness"]["ready_to_publish"] is True
        assert "no merge permission" in state["approval"]["meaning"]

        again = await approve_candidate(task_id, sha, approved_via="session")
        assert again["already_approved"] is True and again["approval"]["id"] == approval["id"]

    @pytest.mark.asyncio
    async def test_refused_without_passing_evidence(self, remote):
        task_id, sha, _ws = add_candidate_task(remote)
        add_check_run(task_id, sha, CheckStatus.FAILED)

        with pytest.raises(DeliveryRefusedError) as info:
            await approve_candidate(task_id, sha, approved_via="session")

        assert (info.value.status_code, info.value.detail) == (409, "not_ready_for_approval")
        assert codes(info.value.extra["blockers"]) == ["checks_failed", "review_missing"]
        with db_session() as session:
            assert session.query(DeliveryApproval).count() == 0

    @pytest.mark.asyncio
    async def test_follow_up_candidate_makes_approval_and_evidence_historical(self, remote):
        task_id, sha_a, ws = add_candidate_task(remote)
        add_check_run(task_id, sha_a)
        add_candidate_review(task_id, sha_a, remote.base_sha)
        await approve_candidate(task_id, sha_a, approved_via="session")

        sha_b = recandidate(task_id, ws)

        state = task_delivery(task_id)
        assert state["candidate"]["sha"] == sha_b
        assert state["approval"]["current"] is None
        [old] = state["approval"]["history"]
        assert (old["candidate_sha"], old["applies_to_current_candidate"]) == (sha_a, False)
        assert state["checks"]["status"] == "missing" and state["review"]["status"] == "missing"
        assert [(r["candidate_sha"], r["current"]) for r in state["checks"]["history"]] == [(sha_a, False)]
        with pytest.raises(DeliveryRefusedError) as stale:
            await approve_candidate(task_id, sha_a, approved_via="session")
        assert (stale.value.detail, stale.value.extra["current_sha"]) == ("stale_candidate", sha_b)
        with pytest.raises(DeliveryRefusedError) as unready:
            await approve_candidate(task_id, sha_b, approved_via="session")
        assert codes(unready.value.extra["blockers"]) == ["checks_missing", "review_missing"]

    @pytest.mark.asyncio
    async def test_rerecording_the_same_sha_needs_a_fresh_approval(self, remote):
        from app.candidates import WorkspaceState, record_candidate

        task_id, sha, _ws = add_candidate_task(remote)
        add_check_run(task_id, sha)
        add_candidate_review(task_id, sha, remote.base_sha)
        await approve_candidate(task_id, sha, approved_via="session")

        record_candidate(task_id, WorkspaceState(head_sha=sha, branch=BRANCH, dirty=False))

        state = task_delivery(task_id)
        assert state["approval"]["current"] is None
        assert state["readiness"]["state"] == "ready_for_approval"

    @pytest.mark.asyncio
    async def test_dirty_workspace_is_rejected_at_approval(self, remote):
        task_id, sha, ws = add_candidate_task(remote)
        add_check_run(task_id, sha)
        add_candidate_review(task_id, sha, remote.base_sha)
        (ws / "README.md").write_text("edited after the candidate\n")

        with pytest.raises(DeliveryRefusedError) as info:
            await approve_candidate(task_id, sha, approved_via="session")

        assert (info.value.status_code, info.value.detail) == (409, "dirty_workspace")
        with db_session() as session:
            assert session.query(DeliveryApproval).count() == 0

    @pytest.mark.asyncio
    async def test_dirty_candidate_status_is_never_approvable(self, remote):
        from app.candidates import WorkspaceState, record_candidate

        task_id, sha, _ws = add_candidate_task(remote)
        record_candidate(task_id, WorkspaceState(head_sha=sha, branch=BRANCH, dirty=True))

        with pytest.raises(DeliveryRefusedError) as info:
            await approve_candidate(task_id, sha, approved_via="session")
        assert info.value.detail == "candidate_not_ready"


@pytest.fixture
def api(remote, protected_client, monkeypatch):
    github = FakeDeliveryGitHub(remote)
    monkeypatch.setattr(publishing, "GitHubClient", lambda: github)
    monkeypatch.setattr(remote_ci, "GitHubClient", lambda: github)
    protected_client.headers.update(BEARER)
    protected_client.fake_github = github
    return protected_client


class TestDeliveryApi:
    def test_routes_require_operator_access(self, remote, protected_client):
        task_id, sha, _ws = add_candidate_task(remote)
        assert protected_client.get(f"/api/tasks/{task_id}/delivery").status_code == 401
        response = protected_client.post(f"/api/tasks/{task_id}/approve", json={"candidate_sha": sha})
        assert response.status_code in (401, 403)
        with db_session() as session:
            assert session.query(DeliveryApproval).count() == 0

    def test_detail_and_task_detail_expose_delivery(self, api, remote):
        task_id, sha, _ws = add_candidate_task(remote)

        detail = api.get(f"/api/tasks/{task_id}/delivery")
        task = api.get(f"/api/tasks/{task_id}")

        assert detail.status_code == 200
        assert detail.json()["readiness"]["state"] == "not_ready"
        assert task.json()["delivery"]["candidate"]["sha"] == sha
        assert api.get("/api/tasks/999/delivery").status_code == 404

    def test_sha_must_be_a_full_commit_id(self, api, remote):
        task_id, sha, _ws = add_candidate_task(remote)
        assert api.post(f"/api/tasks/{task_id}/approve", json={"candidate_sha": sha[:12]}).status_code == 422
        assert api.post(f"/api/tasks/{task_id}/publish", json={}).status_code == 422

    def test_unapproved_publish_is_refused_with_blockers_and_no_github_call(self, api, remote):
        task_id, sha, _ws = add_candidate_task(remote)

        response = api.post(f"/api/tasks/{task_id}/publish", json={"candidate_sha": sha})

        assert response.status_code == 409
        assert response.json()["detail"] == "not_ready_to_publish"
        assert {"checks_missing", "review_missing", "approval_missing"} == set(codes(response.json()["blockers"]))
        assert api.fake_github.calls == []
        assert remote.head(BRANCH) == remote.base_sha

    def test_ci_refresh_before_publication_is_not_published(self, api, remote):
        task_id, _sha, _ws = add_candidate_task(remote)
        response = api.post(f"/api/tasks/{task_id}/ci/refresh")
        assert (response.status_code, response.json()["detail"]) == (409, "not_published")

    def test_full_explicit_journey_over_http(self, api, remote):
        task_id, sha, _ws = add_candidate_task(remote)
        add_check_run(task_id, sha)
        add_candidate_review(task_id, sha, remote.base_sha)

        approved = api.post(f"/api/tasks/{task_id}/approve", json={"candidate_sha": sha})
        assert approved.status_code == 200
        assert approved.json()["approval"]["approved_via"] == "bearer"
        # Approval alone publishes nothing.
        assert api.fake_github.calls == [] and remote.head(BRANCH) == remote.base_sha

        published = api.post(f"/api/tasks/{task_id}/publish", json={"candidate_sha": sha})
        assert published.status_code == 200, published.text
        assert published.json()["publication"]["pull_request"]["draft"] is True
        assert remote.head(BRANCH) == sha

        retry = api.post(f"/api/tasks/{task_id}/publish", json={"candidate_sha": sha})
        assert retry.status_code == 200 and retry.json()["already_published"] is True
        assert len(api.fake_github.prs) == 1

        api.fake_github.check_runs = [{"name": "ci", "status": "in_progress", "conclusion": None}]
        ci = api.post(f"/api/tasks/{task_id}/ci/refresh")
        assert ci.status_code == 200
        assert ci.json()["remote_ci"]["state"] == "pending"
        state = api.get(f"/api/tasks/{task_id}/delivery").json()
        assert (state["readiness"]["state"], state["remote_ci"]["state"]) == ("published", "pending")
        assert state["remote_ci"]["observed_sha"] == sha and state["remote_ci"]["observed_at"]

    def test_partial_publication_is_reported_as_502_with_known_facts(self, api, remote):
        from tests.delivery_fixtures import http_error

        task_id, sha, _ws = add_candidate_task(remote)
        add_check_run(task_id, sha)
        add_candidate_review(task_id, sha, remote.base_sha)
        api.post(f"/api/tasks/{task_id}/approve", json={"candidate_sha": sha})
        api.fake_github.create_error = http_error(500)

        response = api.post(f"/api/tasks/{task_id}/publish", json={"candidate_sha": sha})

        body = response.json()
        assert response.status_code == 502
        assert (body["published"], body["publication"]["status"], body["publication"]["pushed"]) == (
            False,
            "pr_failed",
            True,
        )
        assert api.get(f"/api/tasks/{task_id}/delivery").json()["readiness"]["state"] == "publication_incomplete"

    def test_checks_refused_without_configuration(self, api, remote, monkeypatch):
        task_id, _sha, _ws = add_candidate_task(remote)
        monkeypatch.setenv("FORGE_ACCEPTANCE_CHECKS", "")
        get_settings.cache_clear()
        response = api.post(f"/api/tasks/{task_id}/checks")
        assert (response.status_code, response.json()["detail"]) == (409, "acceptance_checks_not_configured")

    def test_candidate_review_refused_without_droid(self, api, remote):
        task_id, _sha, _ws = add_candidate_task(remote)
        response = api.post(f"/api/tasks/{task_id}/candidate-review")
        assert (response.status_code, response.json()["detail"]) == (409, "droid_not_configured")


class TestMissionBranchTarget:
    def test_allowed_starting_branch_keeps_the_agent_on_it(self, monkeypatch):
        from app.policy import configured_publication_branch
        from app.prompts import build_fix_prompt

        monkeypatch.setenv("FORGE_ALLOWED_BRANCHES", BRANCH)
        get_settings.cache_clear()
        branch = configured_publication_branch(BRANCH)

        prompt = build_fix_prompt("https://github.com/x/y", 1, "t", "b", BRANCH, work_branch=branch)

        assert branch == BRANCH
        assert f"commit directly on the checked-out branch `{BRANCH}`" in prompt
        assert "Create a new git branch" not in prompt
        assert f"BRANCH: {BRANCH}" in prompt

    def test_other_starting_refs_still_get_a_new_branch(self, monkeypatch):
        from app.policy import configured_publication_branch
        from app.prompts import build_fix_prompt

        monkeypatch.setenv("FORGE_ALLOWED_BRANCHES", BRANCH)
        get_settings.cache_clear()

        assert configured_publication_branch("main") is None
        prompt = build_fix_prompt("https://github.com/x/y", 1, "t", "b", "main", work_branch=None)
        assert "Create a new git branch" in prompt


def test_workspace_fixture_is_isolated(remote):
    task_id, _sha, ws = add_candidate_task(remote)
    assert ws == task_workspace_dir(REPO, task_id)
    assert "test-workspace" not in str(ws) and "data/" not in str(ws)
