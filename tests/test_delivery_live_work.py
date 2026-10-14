"""Regressions: delivery rechecks the actual workspace HEAD and waits out the task's live SDK work.

Pushes go to the labeled local bare remote and PR calls to ``FakeDeliveryGitHub``
(``tests/delivery_fixtures.py``). SDK sessions are deterministic fakes whose
interrupt, stream and close each block on an ``asyncio.Event`` the test
releases, so a stopped turn's wind-down is observed step by step. Nothing
reaches GitHub or the Droid SDK.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app import droid_client, publishing, task_reservations, worker
from app.attempts import mark_attempt_running, start_attempt
from app.database import db_session
from app.delivery import DeliveryRefusedError, approve_candidate, task_delivery
from app.delivery_models import DeliveryApproval, Publication
from app.droid_client import DroidClient, get_droid_client, task_workspace_dir
from app.models import Task, TaskStatus
from app.publishing import publish_candidate
from app.recovery import continue_task
from app.run_control import stop_parent
from app.run_models import PARENT_TASK, AttemptKind, AttemptStatus, RunAttempt
from tests.conftest import OPERATOR_TOKEN, PROTECTED_BASE_URL
from tests.delivery_fixtures import (
    BRANCH,
    REPO,
    FakeDeliveryGitHub,
    LocalRemote,
    add_candidate_review,
    add_candidate_task,
    add_check_run,
    configure,
    git,
)

BEARER = {"Authorization": f"Bearer {OPERATOR_TOKEN}", "Origin": PROTECTED_BASE_URL}


@pytest.fixture
def remote(monkeypatch, tmp_path):
    monkeypatch.setattr(publishing, "PR_HEAD_RECHECK_DELAYS", (0.0, 0.0))
    return configure(monkeypatch, tmp_path)


@pytest.fixture
def github(remote):
    return FakeDeliveryGitHub(remote)


_sessions: list[LiveSession] = []


@pytest.fixture
async def client(monkeypatch):
    """A configured process-wide Droid client whose sessions are test fakes, all released at teardown."""
    instance = DroidClient(api_key="fk_test")
    instance.stop_grace_seconds = 30.0
    monkeypatch.setattr(droid_client, "_client", instance)
    yield instance
    for live in _sessions:
        live.release_all()
    _sessions.clear()
    await asyncio.wait_for(instance.shutdown(), timeout=5)


def ready_candidate(remote: LocalRemote, content: str = "fix\n") -> tuple[int, str]:
    task_id, sha, _ws = add_candidate_task(remote, content=content)
    add_check_run(task_id, sha)
    add_candidate_review(task_id, sha, remote.base_sha)
    return task_id, sha


async def approved_candidate(remote: LocalRemote) -> tuple[int, str]:
    task_id, sha = ready_candidate(remote)
    await approve_candidate(task_id, sha, approved_via="bearer")
    return task_id, sha


def workspace(task_id: int):
    return task_workspace_dir(REPO, task_id)


def set_session(task_id: int, session_id: str) -> None:
    with db_session() as session:
        session.get(Task, task_id).droid_session_id = session_id


def codes(blockers: list[dict[str, Any]]) -> list[str]:
    return [b["code"] for b in blockers]


def publication_count() -> int:
    with db_session() as session:
        return session.query(Publication).count()


async def bounded(awaitable: Any) -> Any:
    return await asyncio.wait_for(awaitable, timeout=5)


class Pause:
    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def hold(self) -> None:
        self.reached.set()
        await self.release.wait()


class LiveSession:
    """A fake SDK session whose interrupt, turn and close each wait for the test."""

    def __init__(self, session_id: str) -> None:
        self.id = session_id
        self.settings = SimpleNamespace(model="auto")
        self.result = SimpleNamespace(success=False, subtype="interrupted", text="", duration=None, usage=None)
        self.interrupting = Pause()
        self.closing = Pause()
        self.streaming = asyncio.Event()
        self.end_turn = asyncio.Event()
        _sessions.append(self)

    def release_all(self) -> None:
        for event in (self.interrupting.release, self.closing.release, self.end_turn):
            event.set()

    async def interrupt(self) -> None:
        await self.interrupting.hold()

    async def close(self) -> None:
        await self.closing.hold()

    def stream(self, prompt: str, **kwargs: Any) -> LiveSession:
        return self

    async def __aenter__(self) -> LiveSession:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def __aiter__(self) -> LiveSession:
        return self

    async def __anext__(self) -> Any:
        self.streaming.set()
        await self.end_turn.wait()
        raise StopAsyncIteration


def fake_open(monkeypatch, session: LiveSession, opening: Pause | None = None) -> None:
    async def open_session(self, *, cwd, resume_id=None, **kwargs):
        if opening is not None:
            await opening.hold()
        return session

    monkeypatch.setattr(DroidClient, "_open_session", open_session)


async def assert_refused(task_id: int, sha: str, github: FakeDeliveryGitHub, remote: LocalRemote, code: str) -> None:
    """Approval and publication are both refused with ``code`` and nothing outward happens."""
    pushes = publication_count()
    with pytest.raises(DeliveryRefusedError) as approval:
        await approve_candidate(task_id, sha, approved_via="bearer")
    assert approval.value.detail == code
    with pytest.raises(DeliveryRefusedError) as publication:
        await publish_candidate(task_id, sha, github=github)
    assert publication.value.detail == code
    assert github.calls == [] and remote.head(BRANCH) == remote.base_sha
    assert publication_count() == pushes


# ---- the workspace's actual HEAD, not the candidate branch tip --------------------


class TestActualWorkspaceHead:
    @pytest.mark.asyncio
    async def test_clean_detached_head_elsewhere_refuses_initial_approval(self, remote):
        task_id, sha = ready_candidate(remote)
        git("checkout", "-q", "--detach", remote.base_sha, cwd=workspace(task_id))

        with pytest.raises(DeliveryRefusedError) as info:
            await approve_candidate(task_id, sha, approved_via="bearer")

        assert (info.value.detail, info.value.extra["workspace_sha"]) == ("stale_candidate", remote.base_sha)
        with db_session() as session:
            assert session.query(DeliveryApproval).count() == 0

    @pytest.mark.asyncio
    async def test_clean_detached_head_elsewhere_refuses_repeat_approval_and_publication(self, remote, github):
        task_id, sha = await approved_candidate(remote)
        ws = workspace(task_id)
        git("checkout", "-q", "--detach", remote.base_sha, cwd=ws)
        assert git("status", "--porcelain", cwd=ws) == ""
        assert git("rev-parse", f"refs/heads/{BRANCH}", cwd=ws) == sha

        await assert_refused(task_id, sha, github, remote, "stale_candidate")
        with db_session() as session:
            assert session.query(DeliveryApproval).count() == 1

    @pytest.mark.asyncio
    async def test_push_boundary_sees_a_detach_during_the_remote_read(self, remote, github, monkeypatch):
        task_id, sha = await approved_candidate(remote)
        pause = Pause()
        original = publishing.remote_branch_head

        async def remote_head(repository, branch):
            if not pause.reached.is_set():
                await pause.hold()
            return await original(repository, branch)

        monkeypatch.setattr(publishing, "remote_branch_head", remote_head)
        publishing_task = asyncio.create_task(publish_candidate(task_id, sha, github=github))
        await bounded(pause.reached.wait())
        git("checkout", "-q", "--detach", remote.base_sha, cwd=workspace(task_id))
        pause.release.set()

        with pytest.raises(DeliveryRefusedError) as info:
            await publishing_task

        assert info.value.detail == "stale_candidate"
        assert remote.head(BRANCH) == remote.base_sha and github.writes() == []
        with db_session() as session:
            [pub] = session.query(Publication).all()
            assert (pub.status, pub.pushed) == ("refused", None)

    @pytest.mark.asyncio
    async def test_detached_head_at_the_candidate_is_usable(self, remote, github):
        task_id, sha = ready_candidate(remote)
        git("checkout", "-q", "--detach", sha, cwd=workspace(task_id))

        approved = await approve_candidate(task_id, sha, approved_via="bearer")
        result = await publish_candidate(task_id, sha, github=github)

        assert approved["already_approved"] is False
        assert result["published"] is True and remote.head(BRANCH) == sha

    @pytest.mark.asyncio
    async def test_candidate_discovery_still_prefers_the_reported_branch(self, remote):
        task_id, sha = ready_candidate(remote)
        git("checkout", "-q", "--detach", remote.base_sha, cwd=workspace(task_id))

        state = await get_droid_client().inspect_workspace(REPO, task_id, BRANCH)

        assert (state.head_sha, state.branch, state.dirty) == (sha, BRANCH, False)


# ---- live SDK work on the task blocks approval and publication ----------------------


class TestStoppedTurnWindDown:
    @pytest.mark.parametrize("ending", ["winds_down", "cancelled"])
    @pytest.mark.asyncio
    async def test_stopped_follow_up_blocks_until_the_turn_has_fully_ended(
        self, remote, github, client, monkeypatch, ending
    ):
        task_id, sha = await approved_candidate(remote)
        set_session(task_id, "sess-live")
        live = LiveSession("sess-live")
        fake_open(monkeypatch, live)

        started = await continue_task(task_id, AttemptKind.FOLLOW_UP, "edit more")
        assert started["status"] == AttemptStatus.RUNNING
        turn = client._tasks["sess-live"]
        await bounded(live.streaming.wait())
        # Admission never spans the SDK turn or its callbacks.
        assert task_reservations.holders(task_id) == []

        if ending == "cancelled":
            client.stop_grace_seconds = 0.05
            live.interrupting.release.set()
        stopping = asyncio.create_task(stop_parent(PARENT_TASK, task_id))
        if ending == "winds_down":
            await bounded(live.interrupting.reached.wait())
            with db_session() as session:
                assert session.get(Task, task_id).status == TaskStatus.STOPPED
            assert not stopping.done()
            await assert_refused(task_id, sha, github, remote, "task_active")
            state = task_delivery(task_id)
            assert "task_active" in codes(state["readiness"]["publish_blockers"])
            live.interrupting.release.set()
        await bounded(stopping)

        # The interrupt was answered but the turn is still streaming (or awaiting its cancellation).
        await assert_refused(task_id, sha, github, remote, "task_active")
        if ending == "winds_down":
            live.end_turn.set()
        await bounded(live.closing.reached.wait())
        await assert_refused(task_id, sha, github, remote, "task_active")

        live.closing.release.set()
        await asyncio.wait({turn}, timeout=5)
        await asyncio.sleep(0)

        result = await publish_candidate(task_id, sha, github=github)
        assert result["published"] is True and remote.head(BRANCH) == sha
        with db_session() as session:
            assert session.get(Task, task_id).candidate_sha == sha

    @pytest.mark.asyncio
    async def test_completion_callback_in_flight_still_blocks(self, remote, github, client, monkeypatch):
        task_id, sha = await approved_candidate(remote)
        set_session(task_id, "sess-cb")
        live = LiveSession("sess-cb")
        live.closing.release.set()
        live.interrupting.release.set()
        fake_open(monkeypatch, live)
        callback = Pause()
        original = worker.finalize_fix_task

        async def finalize(task_id, outcome, *, attempt_id):
            await callback.hold()
            await original(task_id, outcome, attempt_id=attempt_id)

        monkeypatch.setattr(worker, "finalize_fix_task", finalize)
        # Recovery continues work that ended without completing.
        with db_session() as session:
            session.get(Task, task_id).status = TaskStatus.INTERRUPTED
        await continue_task(task_id, AttemptKind.RECOVERY)
        turn = client._tasks["sess-cb"]
        await bounded(live.streaming.wait())
        await stop_parent(PARENT_TASK, task_id)
        live.end_turn.set()
        await bounded(callback.reached.wait())

        await assert_refused(task_id, sha, github, remote, "task_active")

        callback.release.set()
        await asyncio.wait({turn}, timeout=5)
        await asyncio.sleep(0)
        assert (await publish_candidate(task_id, sha, github=github))["published"] is True

    @pytest.mark.asyncio
    async def test_paused_continuation_startup_blocks_before_session_linkage(self, remote, github, client, monkeypatch):
        task_id, sha = await approved_candidate(remote)
        set_session(task_id, "sess-start")
        live = LiveSession("sess-start")
        live.closing.release.set()
        opening = Pause()
        fake_open(monkeypatch, live, opening)

        continuing = asyncio.create_task(continue_task(task_id, AttemptKind.FOLLOW_UP, "edit more"))
        await bounded(opening.reached.wait())
        await stop_parent(PARENT_TASK, task_id)
        with db_session() as session:
            assert session.get(Task, task_id).status == TaskStatus.STOPPED

        with pytest.raises(DeliveryRefusedError) as info:
            await publish_candidate(task_id, sha, github=github)
        assert (info.value.detail, info.value.extra["reserved_by"]) == ("task_busy", ["admission"])
        assert github.calls == [] and remote.head(BRANCH) == remote.base_sha

        opening.release.set()
        started = await bounded(continuing)
        assert started["status"] == AttemptStatus.STOPPED
        assert task_reservations.holders(task_id) == []
        turn = client._tasks.get("sess-start")
        if turn is not None:
            await asyncio.wait({turn}, timeout=5)
            await asyncio.sleep(0)

        assert (await publish_candidate(task_id, sha, github=github))["published"] is True

    @pytest.mark.parametrize("key", ["launch", "attempt_session"])
    @pytest.mark.asyncio
    async def test_pending_launch_or_any_attempt_session_blocks(self, remote, github, client, key):
        task_id, sha = await approved_candidate(remote)
        set_session(task_id, "sess-parent")
        if key == "launch":
            handle = f"launch:task-{task_id}"
        else:
            handle = "sess-attempt"
            start = start_attempt(PARENT_TASK, task_id, AttemptKind.RECOVERY, parent_status=TaskStatus.RUNNING)
            mark_attempt_running(start.attempt_id, session_id=handle)
            with db_session() as session:
                session.get(RunAttempt, start.attempt_id).status = AttemptStatus.STOPPED
                session.get(Task, task_id).status = TaskStatus.STOPPED
        gate = asyncio.Event()
        work = client.track(handle, asyncio.create_task(gate.wait()))

        await assert_refused(task_id, sha, github, remote, "task_active")

        gate.set()
        await work
        await asyncio.sleep(0)
        assert (await publish_candidate(task_id, sha, github=github))["published"] is True

    @pytest.mark.asyncio
    async def test_unrelated_historical_and_timer_only_entries_do_not_block(self, remote, github, client):
        task_id, sha = await approved_candidate(remote)
        set_session(task_id, "sess-own")
        start = start_attempt(PARENT_TASK, task_id, AttemptKind.FOLLOW_UP, parent_status=TaskStatus.RUNNING)
        mark_attempt_running(start.attempt_id, session_id="sess-old")
        with db_session() as session:
            session.get(RunAttempt, start.attempt_id).status = AttemptStatus.STOPPED
            session.get(Task, task_id).status = TaskStatus.STOPPED
        never = asyncio.Event()
        unrelated = [
            client.track("sess-unrelated", asyncio.create_task(never.wait())),
            client.track(f"launch:task-{task_id + 100}", asyncio.create_task(never.wait())),
            client.track("stop:sess-own", asyncio.create_task(never.wait())),
        ]

        async def nothing() -> None:
            return None

        finished = asyncio.create_task(nothing())
        await finished
        # A finished handle whose registry cleanup has not run yet is history, not live work.
        client._tasks["sess-old"] = finished

        try:
            assert "task_active" not in codes(task_delivery(task_id)["readiness"]["publish_blockers"])
            assert (await approve_candidate(task_id, sha, approved_via="bearer"))["already_approved"] is True
            assert (await publish_candidate(task_id, sha, github=github))["published"] is True
        finally:
            never.set()
            await asyncio.gather(*unrelated)


# ---- registry cleanup ------------------------------------------------------------


class TestRegistryCleanup:
    @pytest.mark.asyncio
    async def test_finished_handle_cleanup_keeps_a_newer_handle_under_the_same_key(self):
        client = DroidClient(api_key="fk_test")
        finishing = asyncio.Event()

        async def first() -> None:
            finishing.set()

        old = client.track("sess-reused", asyncio.create_task(first()))
        await finishing.wait()
        # ``old`` has finished, but its registry cleanup callback has not run yet.
        assert old.done() and client._tasks.get("sess-reused") is old
        gate = asyncio.Event()
        new = client.track("sess-reused", asyncio.create_task(gate.wait()))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert client._tasks.get("sess-reused") is new
        assert client.session_busy("sess-reused")
        gate.set()
        await new
        await asyncio.sleep(0)
        assert "sess-reused" not in client._tasks


# ---- HTTP surface ------------------------------------------------------------------


def test_http_detached_head_refusals(remote, protected_client):
    task_id, sha = ready_candidate(remote)
    protected_client.headers.update(BEARER)
    ws = workspace(task_id)
    git("checkout", "-q", "--detach", remote.base_sha, cwd=ws)

    refused = protected_client.post(f"/api/tasks/{task_id}/approve", json={"candidate_sha": sha})
    assert (refused.status_code, refused.json()["detail"]) == (409, "stale_candidate")

    git("checkout", "-q", "--detach", sha, cwd=ws)
    approved = protected_client.post(f"/api/tasks/{task_id}/approve", json={"candidate_sha": sha})
    assert approved.status_code == 200 and approved.json()["already_approved"] is False

    git("checkout", "-q", "--detach", remote.base_sha, cwd=ws)
    repeat = protected_client.post(f"/api/tasks/{task_id}/approve", json={"candidate_sha": sha})
    publish = protected_client.post(f"/api/tasks/{task_id}/publish", json={"candidate_sha": sha})
    assert (repeat.status_code, repeat.json()["detail"]) == (409, "stale_candidate")
    assert (publish.status_code, publish.json()["detail"]) == (409, "stale_candidate")
    assert remote.head(BRANCH) == remote.base_sha
    assert publication_count() == 0


def test_http_live_turn_refusals(remote, protected_client):
    task_id, sha = ready_candidate(remote)
    protected_client.headers.update(BEARER)
    set_session(task_id, "sess-http")
    approved = protected_client.post(f"/api/tasks/{task_id}/approve", json={"candidate_sha": sha})
    assert approved.status_code == 200
    loop = asyncio.new_event_loop()
    try:
        # Stands in for a stopped turn still winding down; only ``done()`` is consulted.
        turn = loop.create_future()
        get_droid_client().track("sess-http", turn)

        delivery = protected_client.get(f"/api/tasks/{task_id}/delivery").json()
        repeat = protected_client.post(f"/api/tasks/{task_id}/approve", json={"candidate_sha": sha})
        publish = protected_client.post(f"/api/tasks/{task_id}/publish", json={"candidate_sha": sha})
        assert "task_active" in codes(delivery["readiness"]["publish_blockers"])
        assert (repeat.status_code, repeat.json()["detail"]) == (409, "task_active")
        assert (publish.status_code, publish.json()["detail"], publish.json()["live_work"]) == (
            409,
            "task_active",
            ["sess-http"],
        )
        assert publication_count() == 0 and remote.head(BRANCH) == remote.base_sha

        turn.set_result(None)
        after = protected_client.get(f"/api/tasks/{task_id}/delivery").json()
        assert "task_active" not in codes(after["readiness"]["publish_blockers"])
        again = protected_client.post(f"/api/tasks/{task_id}/approve", json={"candidate_sha": sha})
        assert again.status_code == 200 and again.json()["already_approved"] is True
    finally:
        loop.close()
