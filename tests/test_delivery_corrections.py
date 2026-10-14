"""Regressions for SHA-bound evidence, publication coordination and partial-effect accuracy.

Pushes go to the labeled local bare remote and PR calls to ``FakeDeliveryGitHub``
(``tests/delivery_fixtures.py``). Races are made deterministic by pausing an
awaited lookup on an ``asyncio.Event``. Nothing reaches GitHub or the Droid SDK.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app import acceptance_checks, database, publishing, task_reservations, worker
from app.acceptance_checks import start_check_run
from app.attempts import mark_attempt_running, start_attempt
from app.candidate_review import start_candidate_review
from app.candidates import WorkspaceState
from app.config import get_settings
from app.database import db_session
from app.delivery import DeliveryRefusedError, approve_candidate, task_delivery
from app.delivery_models import CheckRun, DeliveryApproval, Publication
from app.droid_client import task_workspace_dir
from app.models import Task, TaskStatus
from app.publishing import publish_candidate
from app.recovery import ContinuationRefusedError, continue_task
from app.remote_ci import latest_pushed_publication_id, observe_publication_ci
from app.run_models import PARENT_TASK, AttemptKind
from app.task_reservations import Purpose
from tests.conftest import OPERATOR_TOKEN, PROTECTED_BASE_URL
from tests.delivery_fixtures import (
    BRANCH,
    REPO,
    FakeDeliveryGitHub,
    LocalRemote,
    add_candidate_review,
    add_candidate_task,
    add_check_run,
    commit,
    configure,
    git,
    http_error,
    recandidate,
)

BEARER = {"Authorization": f"Bearer {OPERATOR_TOKEN}", "Origin": PROTECTED_BASE_URL}


@pytest.fixture
def remote(monkeypatch, tmp_path):
    monkeypatch.setattr(publishing, "PR_HEAD_RECHECK_DELAYS", (0.0, 0.0))
    return configure(monkeypatch, tmp_path)


@pytest.fixture
def github(remote):
    return FakeDeliveryGitHub(remote)


def set_checks(monkeypatch, checks: list[dict[str, Any]] | str) -> None:
    monkeypatch.setenv("FORGE_ACCEPTANCE_CHECKS", checks if isinstance(checks, str) else json.dumps(checks))
    get_settings.cache_clear()


def codes(blockers):
    return [b["code"] for b in blockers]


def ready_candidate(remote: LocalRemote) -> tuple[int, str]:
    task_id, sha, _ws = add_candidate_task(remote)
    add_check_run(task_id, sha)
    add_candidate_review(task_id, sha, remote.base_sha)
    return task_id, sha


async def approved_candidate(remote: LocalRemote) -> tuple[int, str]:
    task_id, sha = ready_candidate(remote)
    await approve_candidate(task_id, sha, approved_via="bearer")
    return task_id, sha


def workspace(task_id: int):
    return task_workspace_dir(REPO, task_id)


def publication_row(task_id: int) -> dict[str, Any]:
    with db_session() as session:
        [pub] = session.query(Publication).filter(Publication.task_id == task_id).all()
        return pub.to_dict()


class Pause:
    """Holds one awaited call open until the test releases it."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def hold(self) -> None:
        self.reached.set()
        await self.release.wait()


def pause_first_remote_read(monkeypatch) -> Pause:
    pause = Pause()
    original = publishing.remote_branch_head
    calls = []

    async def remote_head(repository, branch):
        calls.append(branch)
        if len(calls) == 1:
            await pause.hold()
        return await original(repository, branch)

    monkeypatch.setattr(publishing, "remote_branch_head", remote_head)
    return pause


def pause_base_lookup(github: FakeDeliveryGitHub) -> Pause:
    pause = Pause()
    original = github.get_repository

    async def get_repository(full_name):
        await pause.hold()
        return await original(full_name)

    github.get_repository = get_repository
    return pause


# ---- checks bound to their effective specification ---------------------------------


class TestCheckSpecificationBinding:
    def test_same_name_argv_change_needs_a_rerun(self, remote, monkeypatch):
        task_id, _sha = ready_candidate(remote)
        assert task_delivery(task_id)["checks"]["status"] == "passed"

        set_checks(monkeypatch, [{"name": "unit", "argv": ["python3", "-c", "print(2)"]}])

        state = task_delivery(task_id)
        assert state["checks"]["status"] == "outdated_configuration"
        assert codes(state["readiness"]["approval_blockers"]) == ["checks_outdated_configuration"]

    @pytest.mark.parametrize(("timeout", "status"), [(60, "outdated_configuration"), (900, "passed")])
    def test_effective_timeout_is_part_of_the_evidence(self, remote, monkeypatch, timeout, status):
        task_id, _sha = ready_candidate(remote)

        # 900 is the default the run was bound to, so stating it explicitly changes nothing.
        set_checks(monkeypatch, [{"name": "unit", "argv": ["python3", "-c", "print(1)"], "timeout_seconds": timeout}])

        assert task_delivery(task_id)["checks"]["status"] == status

    def test_changes_hidden_by_redacted_display_still_invalidate(self, remote, monkeypatch):
        set_checks(monkeypatch, [{"name": "unit", "argv": ["python3", "-c", "print(1)", "api_key=aaaaaaaaaaaa1"]}])
        task_id, _sha = ready_candidate(remote)
        before = task_delivery(task_id)["checks"]["configuration"]["checks"][0]["command"]
        assert task_delivery(task_id)["checks"]["status"] == "passed"

        set_checks(monkeypatch, [{"name": "unit", "argv": ["python3", "-c", "print(1)", "api_key=bbbbbbbbbbbb2"]}])

        state = task_delivery(task_id)
        assert state["checks"]["configuration"]["checks"][0]["command"] == before
        assert "aaaaaaaaaaaa1" not in before and "[REDACTED]" in before
        assert state["checks"]["status"] == "outdated_configuration"

    def test_legacy_names_only_evidence_requires_a_rerun(self, remote):
        task_id, sha, _ws = add_candidate_task(remote)
        add_check_run(task_id, sha, legacy=True)
        add_candidate_review(task_id, sha, remote.base_sha)

        state = task_delivery(task_id)

        assert state["checks"]["status"] == "outdated_configuration"
        assert state["checks"]["latest_run"]["configuration_binding"] == "names_only"
        assert state["checks"]["latest_run"]["configured_checks"] == ["unit"]

    @pytest.mark.asyncio
    async def test_change_after_approval_blocks_approval_and_publication(self, remote, github, monkeypatch):
        task_id, sha = await approved_candidate(remote)

        set_checks(monkeypatch, [{"name": "unit", "argv": ["python3", "-c", "print(3)"]}])

        with pytest.raises(DeliveryRefusedError) as approval:
            await approve_candidate(task_id, sha, approved_via="bearer")
        assert codes(approval.value.extra["blockers"]) == ["checks_outdated_configuration"]
        with pytest.raises(DeliveryRefusedError) as publication:
            await publish_candidate(task_id, sha, github=github)
        assert publication.value.detail == "not_ready_to_publish"
        assert "checks_outdated_configuration" in codes(publication.value.extra["blockers"])
        assert github.calls == [] and remote.head(BRANCH) == remote.base_sha

    @pytest.mark.asyncio
    async def test_real_run_is_bound_across_restart_without_storing_raw_argv(self, remote, monkeypatch):
        sentinel = "--token=fakesentinel0123456789"
        spec = {"name": "unit", "argv": ["python3", "-c", "print(1)", sentinel], "timeout_seconds": 120}
        set_checks(monkeypatch, [spec])
        task_id, _sha, _ws = add_candidate_task(remote)

        started = await start_check_run(task_id)
        await acceptance_checks._running[task_id]

        with db_session() as session:
            stored = session.get(CheckRun, started["check_run_id"]).configuration
        assert "fakesentinel" not in stored
        [entry] = json.loads(stored)
        assert (entry["name"], entry["timeout_seconds"], len(entry["spec_sha256"])) == ("unit", 120.0, 64)
        assert task_delivery(task_id)["checks"]["status"] == "passed"

        # Restart: a fresh engine on the same database, configuration read anew.
        database.reset_for_tests()
        set_checks(monkeypatch, [{**spec, "argv": ["python3", "-c", "print(1)", sentinel, "-v"]}])
        assert task_delivery(task_id)["checks"]["status"] == "outdated_configuration"

        database.reset_for_tests()
        set_checks(monkeypatch, [spec])
        assert task_delivery(task_id)["checks"]["status"] == "passed"


# ---- publication coordinated with every other task mutation ------------------------


class TestPublicationReservation:
    @pytest.mark.asyncio
    async def test_admission_and_readiness_changes_wait_out_publication(self, remote, github):
        task_id, sha = await approved_candidate(remote)
        pause = pause_base_lookup(github)
        publishing_task = asyncio.create_task(publish_candidate(task_id, sha, github=github))
        await pause.reached.wait()

        for kind in (AttemptKind.FOLLOW_UP, AttemptKind.RECOVERY):
            with pytest.raises(ContinuationRefusedError) as cont:
                await continue_task(task_id, kind, "edit more")
            assert (cont.value.status_code, cont.value.detail) == (409, "publication_in_progress")
        for start in (start_check_run, start_candidate_review):
            with pytest.raises(DeliveryRefusedError) as refused:
                await start(task_id)
            assert (refused.value.status_code, refused.value.detail) == (409, "publication_in_progress")
        with pytest.raises(DeliveryRefusedError) as again:
            await approve_candidate(task_id, sha, approved_via="bearer")
        assert again.value.detail == "publication_in_progress"
        with pytest.raises(DeliveryRefusedError) as second:
            await publish_candidate(task_id, sha, github=github)
        assert second.value.detail == "publication_in_progress"

        pause.release.set()
        result = await publishing_task

        assert result["published"] is True and remote.head(BRANCH) == sha
        assert task_reservations.holders(task_id) == []
        with db_session() as session:
            assert session.query(CheckRun).count() == 1
            assert session.query(DeliveryApproval).count() == 1
        # Released: the follow-up is now refused only for its own reason (no Droid configured).
        with pytest.raises(ContinuationRefusedError) as after:
            await continue_task(task_id, AttemptKind.FOLLOW_UP, "edit more")
        assert after.value.status_code == 503

    @pytest.mark.parametrize("change", ["dirty", "moved_head", "recorded_b", "task_active", "evidence", "policy"])
    @pytest.mark.asyncio
    async def test_push_boundary_revalidates_after_awaited_lookups(self, remote, github, monkeypatch, change):
        task_id, sha = await approved_candidate(remote)
        pause = pause_first_remote_read(monkeypatch)
        publishing_task = asyncio.create_task(publish_candidate(task_id, sha, github=github))
        await pause.reached.wait()

        ws = workspace(task_id)
        if change == "dirty":
            (ws / "fix.txt").write_text("edited while publishing\n")
        elif change == "moved_head":
            commit(ws, "late.txt", "unrecorded\n")
        elif change == "recorded_b":
            recandidate(task_id, ws)
        elif change == "task_active":
            with db_session() as session:
                session.get(Task, task_id).status = TaskStatus.RUNNING
        elif change == "evidence":
            set_checks(monkeypatch, [{"name": "unit", "argv": ["python3", "-c", "print(9)"]}])
        else:
            monkeypatch.setenv("FORGE_ALLOW_GITHUB_WRITES", "false")
            get_settings.cache_clear()
        pause.release.set()

        with pytest.raises(DeliveryRefusedError) as info:
            await publishing_task

        expected = {
            "dirty": "dirty_workspace",
            "moved_head": "stale_candidate",
            "recorded_b": "stale_candidate",
            "task_active": "task_active",
            "evidence": "not_ready_to_publish",
            "policy": "policy_denied",
        }[change]
        assert info.value.detail == expected
        assert remote.head(BRANCH) == remote.base_sha and remote.head("main") == remote.base_sha
        assert github.writes() == []
        pub = publication_row(task_id)
        assert (pub["status"], pub["pushed"], pub["complete"]) == ("refused", None, False)
        assert pub["error"]
        assert task_reservations.holders(task_id) == []

    @pytest.mark.asyncio
    async def test_finalization_in_flight_refuses_publication(self, remote, github):
        task_id, sha = await approved_candidate(remote)
        with task_reservations.claim(task_id, Purpose.FINALIZATION), pytest.raises(DeliveryRefusedError) as info:
            await publish_candidate(task_id, sha, github=github)
        assert (info.value.status_code, info.value.detail) == (409, "task_busy")
        assert info.value.extra["reserved_by"] == ["finalization"]
        assert github.calls == []

    @pytest.mark.asyncio
    async def test_terminal_status_to_candidate_recording_gap_blocks_stale_publication(
        self, remote, github, monkeypatch
    ):
        task_id, sha_a = await approved_candidate(remote)
        with db_session() as session:
            session.get(Task, task_id).droid_session_id = "sess-a"
        attempt_id = start_attempt(PARENT_TASK, task_id, AttemptKind.FOLLOW_UP, parent_status=TaskStatus.RUNNING)
        mark_attempt_running(attempt_id.attempt_id, session_id="sess-a")
        sha_b = commit(workspace(task_id), "follow.txt", "agent edit\n")
        pause = Pause()

        class Droid:
            configured = True

            async def inspect_workspace(self, repository, task_id, reported_branch):
                await pause.hold()
                return WorkspaceState(head_sha=sha_b, branch=BRANCH, dirty=False)

        class Comments:
            async def post_issue_comment(self, repository, issue_number, body):
                return {"id": 1}

        monkeypatch.setattr(worker, "get_droid_client", lambda: Droid())
        monkeypatch.setattr(worker, "GitHubClient", lambda: Comments())
        outcome = {"session_id": "sess-a", "success": True, "subtype": "success", "text": "done"}
        finalizing = asyncio.create_task(worker.finalize_fix_task(task_id, outcome, attempt_id=attempt_id.attempt_id))
        await pause.reached.wait()
        with db_session() as session:
            task = session.get(Task, task_id)
            assert (task.status, task.candidate_sha) == (TaskStatus.COMPLETED, sha_a)

        with pytest.raises(DeliveryRefusedError) as gap:
            await publish_candidate(task_id, sha_a, github=github)
        assert (gap.value.detail, gap.value.extra["reserved_by"]) == ("task_busy", ["finalization"])

        pause.release.set()
        await finalizing
        assert task_reservations.holders(task_id) == []
        with pytest.raises(DeliveryRefusedError) as stale:
            await publish_candidate(task_id, sha_a, github=github)
        assert (stale.value.detail, stale.value.extra["current_sha"]) == ("stale_candidate", sha_b)
        assert github.calls == [] and remote.head(BRANCH) == remote.base_sha


# ---- repeated requests still recheck the workspace --------------------------------


class TestRepeatedRequestsRecheckWorkspace:
    @pytest.mark.asyncio
    async def test_repeat_approval(self, remote):
        task_id, sha = await approved_candidate(remote)
        ws = workspace(task_id)

        (ws / "fix.txt").write_text("dirty\n")
        with pytest.raises(DeliveryRefusedError) as dirty:
            await approve_candidate(task_id, sha, approved_via="bearer")
        assert dirty.value.detail == "dirty_workspace"
        git("checkout", "--", "fix.txt", cwd=ws)

        moved = commit(ws, "late.txt", "unrecorded\n")
        with pytest.raises(DeliveryRefusedError) as stale:
            await approve_candidate(task_id, sha, approved_via="bearer")
        assert (stale.value.detail, stale.value.extra["workspace_sha"]) == ("stale_candidate", moved)
        git("reset", "-q", "--hard", sha, cwd=ws)

        again = await approve_candidate(task_id, sha, approved_via="bearer")
        assert again["already_approved"] is True
        with db_session() as session:
            assert session.query(DeliveryApproval).count() == 1

    @pytest.mark.asyncio
    async def test_repeat_publication(self, remote, github):
        task_id, sha = await approved_candidate(remote)
        await publish_candidate(task_id, sha, github=github)
        calls = list(github.calls)
        ws = workspace(task_id)

        (ws / "fix.txt").write_text("dirty\n")
        with pytest.raises(DeliveryRefusedError) as dirty:
            await publish_candidate(task_id, sha, github=github)
        assert dirty.value.detail == "dirty_workspace"
        git("checkout", "--", "fix.txt", cwd=ws)

        commit(ws, "late.txt", "unrecorded\n")
        with pytest.raises(DeliveryRefusedError) as stale:
            await publish_candidate(task_id, sha, github=github)
        assert stale.value.detail == "stale_candidate"
        git("reset", "-q", "--hard", sha, cwd=ws)

        again = await publish_candidate(task_id, sha, github=github)
        assert again["already_published"] is True
        assert github.calls == calls and len(github.prs) == 1 and remote.head(BRANCH) == sha


# ---- only an open, unmerged draft PR at the approved head completes publication -----


class TestDraftPullRequestCompliance:
    @pytest.mark.asyncio
    async def test_reused_ordinary_pr_is_never_complete(self, remote, github):
        task_id, sha = await approved_candidate(remote)
        await github.create_pull_request(REPO, title="human PR", head=BRANCH, base="main", draft=False)
        github.calls.clear()

        result = await publish_candidate(task_id, sha, github=github)

        pub = result["publication"]
        assert (result["published"], pub["status"], pub["complete"]) == (False, "pr_noncompliant", False)
        assert (pub["pushed"], pub["remote_head_sha"]) == (True, sha)
        pr = pub["pull_request"]
        assert (pr["number"], pr["draft"], pr["state"], pr["head_matches"], pr["action"]) == (
            100,
            False,
            "open",
            True,
            "reused",
        )
        assert "draft" in pub["error"]
        assert github.writes() == [] and github.prs[100]["draft"] is False
        assert task_delivery(task_id)["readiness"]["state"] == "publication_incomplete"

        retry = await publish_candidate(task_id, sha, github=github)
        assert retry["publication"]["status"] == "pr_noncompliant" and len(github.prs) == 1

    @pytest.mark.parametrize("ending", ["closed", "merged"])
    @pytest.mark.asyncio
    async def test_pr_closed_or_merged_during_head_recheck(self, remote, github, ending):
        task_id, sha = await approved_candidate(remote)
        github.stale_head = remote.base_sha
        original = github.get_pull_request

        async def ended(repository, number):
            github.stale_head = None
            github.prs[number]["state"] = "closed"
            github.prs[number]["merged"] = ending == "merged"
            return await original(repository, number)

        github.get_pull_request = ended

        result = await publish_candidate(task_id, sha, github=github)

        pub = result["publication"]
        assert (result["published"], pub["status"], pub["pushed"]) == (False, "pr_noncompliant", True)
        assert pub["pull_request"]["state"] == ending
        assert pub["pull_request"]["merged"] is (ending == "merged")
        assert pub["pull_request"]["number"] == 100

        # A retry re-reads the known PR; it never opens a replacement, reopens or merges.
        retry = await publish_candidate(task_id, sha, github=github)
        assert retry["publication"]["status"] == "pr_noncompliant"
        assert retry["publication"]["pull_request"]["number"] == 100
        assert len(github.prs) == 1 and github.writes() == ["create_pull_request"]

    @pytest.mark.asyncio
    async def test_missing_state_or_draft_facts_do_not_prove_a_draft(self, remote, github):
        task_id, sha = await approved_candidate(remote)
        original = github.create_pull_request

        async def create(repository, **kwargs):
            pr = await original(repository, **kwargs)
            pr.pop("state")
            pr.pop("draft")
            return pr

        github.create_pull_request = create

        result = await publish_candidate(task_id, sha, github=github)

        pub = result["publication"]
        assert (pub["status"], pub["pull_request"]["state"], pub["pull_request"]["draft"]) == (
            "pr_noncompliant",
            None,
            False,
        )


# ---- known outward effects survive later retry errors ------------------------------


class TestKnownFactsSurviveRetryErrors:
    @pytest.mark.asyncio
    async def test_push_then_pr_failure_then_lookup_and_remote_read_errors(self, remote, github, monkeypatch):
        task_id, sha = await approved_candidate(remote)
        github.create_error = http_error(500)
        first = (await publish_candidate(task_id, sha, github=github))["publication"]
        assert (first["status"], first["pushed"], first["push_detail"]) == ("pr_failed", True, "pushed")
        known = {k: first[k] for k in ("id", "pushed_at", "remote_head_sha", "remote_verified_at")}

        github.repository_error = http_error(503, "GET")
        lookup = (await publish_candidate(task_id, sha, github=github))["publication"]
        assert lookup["status"] == "failed" and "base branch" in lookup["error"]
        assert (lookup["pushed"], lookup["push_detail"]) == (True, "pushed")
        assert {k: lookup[k] for k in known} == known
        state = task_delivery(task_id)
        assert state["remote_ci"]["state"] == "not_observed"
        assert latest_pushed_publication_id(task_id) == known["id"]

        github.repository_error = None

        async def unreachable(repository, branch):
            raise RuntimeError("ls-remote failed")

        monkeypatch.setattr(publishing, "remote_branch_head", unreachable)
        read = (await publish_candidate(task_id, sha, github=github))["publication"]
        assert read["status"] == "push_failed" and "remote branch" in read["error"]
        assert (read["pushed"], read["push_detail"]) == (True, "pushed")
        assert {k: read[k] for k in known} == known
        github.check_runs = [{"name": "ci", "status": "completed", "conclusion": "success"}]
        assert (await observe_publication_ci(known["id"], github))["state"] == "passing"
        assert publication_row(task_id)["publish_attempts"] == 3

    @pytest.mark.asyncio
    async def test_rejected_retry_push_keeps_the_earlier_push(self, remote, github, tmp_path):
        task_id, sha = await approved_candidate(remote)
        github.create_error = http_error(500)
        first = (await publish_candidate(task_id, sha, github=github))["publication"]
        other = tmp_path / "other"
        git("clone", "-q", "--branch", BRANCH, str(remote.bare), str(other), cwd=tmp_path)
        moved = commit(other, "elsewhere.txt", "someone else\n")
        git("push", "-q", "origin", BRANCH, cwd=other)

        retry = (await publish_candidate(task_id, sha, github=github))["publication"]

        assert retry["status"] == "push_failed" and "never forced" in retry["error"]
        assert (retry["pushed"], retry["push_detail"], retry["pushed_at"]) == (True, "pushed", first["pushed_at"])
        assert (retry["remote_head_sha"], retry["remote_head_matches"]) == (moved, False)
        assert remote.head(BRANCH) == moved and remote.head("main") == remote.base_sha

    @pytest.mark.asyncio
    async def test_retry_after_errors_completes_with_one_identity(self, remote, github):
        task_id, sha = await approved_candidate(remote)
        github.create_error = http_error(500)
        await publish_candidate(task_id, sha, github=github)
        github.create_error = None
        github.repository_error = http_error(503, "GET")
        await publish_candidate(task_id, sha, github=github)
        github.repository_error = None

        result = await publish_candidate(task_id, sha, github=github)

        pub = result["publication"]
        assert (result["published"], pub["status"], pub["publish_attempts"]) == (True, "published", 3)
        assert pub["error"] is None and pub["pull_request"]["draft"] is True
        assert len(github.prs) == 1
        with db_session() as session:
            assert session.query(Publication).count() == 1


# ---- HTTP surface ---------------------------------------------------------------


def test_http_refusals_while_publication_is_reserved(remote, protected_client):
    task_id, sha = ready_candidate(remote)
    protected_client.headers.update(BEARER)

    with task_reservations.claim(task_id, Purpose.PUBLICATION):
        responses = {
            "follow_up": protected_client.post(f"/api/tasks/{task_id}/follow-up", json={"instruction": "x"}),
            "recover": protected_client.post(f"/api/tasks/{task_id}/recover"),
            "checks": protected_client.post(f"/api/tasks/{task_id}/checks"),
            "review": protected_client.post(f"/api/tasks/{task_id}/candidate-review"),
            "approve": protected_client.post(f"/api/tasks/{task_id}/approve", json={"candidate_sha": sha}),
            "publish": protected_client.post(f"/api/tasks/{task_id}/publish", json={"candidate_sha": sha}),
        }

    for name, response in responses.items():
        assert (name, response.status_code, response.json()["detail"]) == (name, 409, "publication_in_progress")
    assert task_reservations.holders(task_id) == []
    with db_session() as session:
        assert session.query(DeliveryApproval).count() == 0
        assert session.query(CheckRun).count() == 1
