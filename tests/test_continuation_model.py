"""Regressions: a continuation runs on the repository's explicitly selected model.

``Session.resume`` takes no model, so a resumed session keeps the model saved
with it unless ``update_settings(model=...)`` is applied after it opens and
before the turn is sent. The SDK here is a deterministic local fake installed
as ``droid_client.Session`` (the real ``DroidClient`` code paths run against
it); task workspaces are clones of the labeled local bare remote from
``tests/delivery_fixtures.py``. Nothing reaches the Droid SDK or GitHub.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app import droid_client, recovery, task_reservations, worker
from app.config import get_settings
from app.database import db_session
from app.delivery import DeliveryRefusedError, approve_candidate
from app.droid_client import DroidClient
from app.models import Repository, Task, TaskStatus
from app.prompts import RECOVERY_PROMPT_TEMPLATE, build_follow_up_prompt
from app.publishing import publish_candidate
from app.recovery import ContinuationRefusedError, continue_task
from app.run_control import stop_parent
from app.run_models import PARENT_TASK, AttemptKind, AttemptStatus, RunAttempt, UsageCompleteness
from tests.delivery_fixtures import (
    BRANCH,
    REPO,
    FakeDeliveryGitHub,
    add_candidate_review,
    add_candidate_task,
    add_check_run,
    configure,
)

SELECTED = "claude-sonnet-4-6"
GLOBAL_DEFAULT = "global-default-model"
SAVED = "auto"
SESSION_ID = "sess-saved"
INSTRUCTION = "Add a short operator note about supported repository URL inputs."
KINDS = [AttemptKind.FOLLOW_UP, AttemptKind.RECOVERY]


class Pause:
    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def hold(self) -> None:
        self.reached.set()
        await self.release.wait()


class FakeSDK:
    """Controls every resumed fake session; ``calls`` is the ordered SDK traffic of each."""

    def __init__(self) -> None:
        self.sessions: list[ResumedSession] = []
        self.settings_error: BaseException | None = None
        self.settings_pause: Pause | None = None

    def calls(self) -> list[tuple[Any, ...]]:
        return [call for session in self.sessions for call in session.calls]

    def streamed(self) -> list[str]:
        return [call[1] for call in self.calls() if call[0] == "stream"]


class ResumedSession:
    """A fake resumed SDK session with its saved model; it streams one successful, empty turn."""

    sdk: FakeSDK

    def __init__(self, session_id: str) -> None:
        self.id = session_id
        self.settings = SimpleNamespace(model=SAVED)
        self.calls: list[tuple[Any, ...]] = [("resume", session_id)]
        self.result = SimpleNamespace(success=True, subtype="success", text="Done.", duration=None, usage=None)

    @classmethod
    def resume(cls, session_id: str, **kwargs: Any) -> ResumedSession:
        assert "model" not in kwargs, "the installed SDK's Session.resume takes no model"
        session = cls(session_id)
        cls.sdk.sessions.append(session)
        return session

    async def open(self) -> None:
        self.calls.append(("open",))

    async def update_settings(self, *, model: str | None = None) -> None:
        self.calls.append(("update_settings", model))
        if self.sdk.settings_pause is not None:
            await self.sdk.settings_pause.hold()
        if self.sdk.settings_error is not None:
            raise self.sdk.settings_error
        self.settings = SimpleNamespace(model=model)

    async def interrupt(self) -> None:
        self.calls.append(("interrupt",))

    async def close(self) -> None:
        self.calls.append(("close",))

    def stream(self, prompt: str, **kwargs: Any) -> ResumedSession:
        self.calls.append(("stream", prompt))
        return self

    async def __aenter__(self) -> ResumedSession:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def __aiter__(self) -> ResumedSession:
        return self

    async def __anext__(self) -> Any:
        raise StopAsyncIteration


@pytest.fixture
def remote(monkeypatch, tmp_path):
    # A global default model exists; a continuation must still never fall back to it.
    monkeypatch.setenv("DROID_MODEL", GLOBAL_DEFAULT)
    return configure(monkeypatch, tmp_path)


@pytest.fixture
def sdk(monkeypatch):
    controller = FakeSDK()
    monkeypatch.setattr(ResumedSession, "sdk", controller, raising=False)
    monkeypatch.setattr(droid_client, "Session", ResumedSession)

    async def fake_runtime(*, cwd, api_key):
        return droid_client.Runtime()

    monkeypatch.setattr(droid_client, "open_agent_runtime", fake_runtime)
    return controller


@pytest.fixture
def outcomes(monkeypatch):
    """Completion callbacks are recorded instead of finalizing, so each test observes the turn outcome."""
    seen: list[dict[str, Any]] = []

    async def finalize(task_id, outcome, *, attempt_id):
        seen.append({"task_id": task_id, "attempt_id": attempt_id, **outcome})

    monkeypatch.setattr(worker, "finalize_fix_task", finalize)
    return seen


@pytest.fixture
async def client(monkeypatch, remote):
    instance = DroidClient(api_key="fk_test")
    assert instance.model == GLOBAL_DEFAULT
    monkeypatch.setattr(droid_client, "_client", instance)
    yield instance
    await asyncio.wait_for(instance.shutdown(), timeout=5)


def register(model: str | None) -> None:
    """Register the repository with ``model`` as its selected model (``None``: not registered)."""
    if model is None:
        return
    with db_session() as session:
        session.add(Repository(full_name=REPO, url=f"https://github.com/{REPO}", droid_model=model))


def continuable_task(remote, kind: str) -> int:
    task_id, _sha, _ws = add_candidate_task(remote)
    with db_session() as session:
        task = session.get(Task, task_id)
        task.droid_session_id = SESSION_ID
        if kind == AttemptKind.RECOVERY:
            task.status = TaskStatus.INTERRUPTED
            task.error = "interrupted by service restart"
    return task_id


def task_state(task_id: int) -> tuple[str, str | None]:
    with db_session() as session:
        task = session.get(Task, task_id)
        return task.status, task.error


def attempts(task_id: int) -> list[RunAttempt]:
    with db_session() as session:
        rows = session.query(RunAttempt).filter(RunAttempt.parent_id == task_id).order_by(RunAttempt.sequence).all()
        session.expunge_all()
        return rows


def expected_prompt(kind: str) -> str:
    return build_follow_up_prompt(INSTRUCTION) if kind == AttemptKind.FOLLOW_UP else RECOVERY_PROMPT_TEMPLATE


async def bounded(awaitable: Any) -> Any:
    return await asyncio.wait_for(awaitable, timeout=5)


async def settle(client: DroidClient) -> None:
    pending = [task for task in client._tasks.values() if not task.done()]
    if pending:
        await bounded(asyncio.wait(pending))
    await asyncio.sleep(0)


def assert_quiescent(client: DroidClient, task_id: int) -> None:
    assert task_reservations.holders(task_id) == []
    assert client.active_sessions == set() and client._sessions == {}
    with db_session() as session:
        active = (
            session.query(RunAttempt)
            .filter(RunAttempt.parent_id == task_id, RunAttempt.status.in_(AttemptStatus.ACTIVE))
            .count()
        )
    assert active == 0


# ---- the selected model is applied and accounted -------------------------------------


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.asyncio
async def test_selected_model_is_applied_to_the_same_session_before_the_turn(remote, sdk, outcomes, client, kind):
    register(SELECTED)
    task_id = continuable_task(remote, kind)

    started = await continue_task(task_id, kind, INSTRUCTION)
    await settle(client)

    assert started["status"] == AttemptStatus.RUNNING
    assert (started["session_id"], started["requested_model"]) == (SESSION_ID, SELECTED)
    assert sdk.calls() == [
        ("resume", SESSION_ID),
        ("open",),
        ("update_settings", SELECTED),
        ("stream", expected_prompt(kind)),
        ("close",),
    ]
    [attempt] = attempts(task_id)
    assert (attempt.kind, attempt.requested_model, attempt.session_id) == (kind, SELECTED, SESSION_ID)
    [outcome] = outcomes
    assert (outcome["attempt_id"], outcome["session_id"]) == (attempt.id, SESSION_ID)
    assert outcome["reported_model"] == SELECTED


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("selection", [None, "", "   "], ids=["unregistered", "empty", "blank"])
@pytest.mark.asyncio
async def test_without_a_selection_the_saved_session_model_is_kept(remote, sdk, outcomes, client, kind, selection):
    register(selection)
    task_id = continuable_task(remote, kind)

    started = await continue_task(task_id, kind, INSTRUCTION)
    await settle(client)

    assert started["requested_model"] is None
    assert [call[0] for call in sdk.calls()] == ["resume", "open", "stream", "close"]
    assert sdk.streamed() == [expected_prompt(kind)]
    [attempt] = attempts(task_id)
    assert attempt.requested_model is None
    assert get_settings().droid_model == GLOBAL_DEFAULT
    [outcome] = outcomes
    assert outcome["reported_model"] == SAVED


# ---- a settings failure or cancellation sends nothing -------------------------------------


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.asyncio
async def test_settings_failure_sends_nothing_and_restores_the_task(remote, sdk, outcomes, client, kind):
    register(SELECTED)
    task_id = continuable_task(remote, kind)
    before = task_state(task_id)
    sdk.settings_error = RuntimeError("model is not available for this account")

    with pytest.raises(ContinuationRefusedError) as refused:
        await continue_task(task_id, kind, INSTRUCTION)
    await settle(client)

    assert refused.value.status_code == 502
    assert "model is not available" in refused.value.detail
    assert sdk.calls() == [("resume", SESSION_ID), ("open",), ("update_settings", SELECTED), ("close",)]
    assert outcomes == []
    [attempt] = attempts(task_id)
    assert (attempt.status, attempt.outcome_subtype) == (AttemptStatus.FAILED, "start_failed")
    assert attempt.requested_model == SELECTED
    assert attempt.usage_completeness == UsageCompleteness.UNKNOWN and attempt.factory_credits is None
    assert task_state(task_id) == before
    assert_quiescent(client, task_id)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.asyncio
async def test_cancellation_during_settings_cleans_up_before_admission_is_released(
    remote, sdk, outcomes, client, monkeypatch, kind
):
    register(SELECTED)
    task_id = continuable_task(remote, kind)
    before = task_state(task_id)
    sdk.settings_pause = Pause()
    held_at_cleanup: list[list[str]] = []
    original = recovery.fail_attempt_start

    def fail_start(attempt_id: int, error: str) -> bool:
        held_at_cleanup.append(task_reservations.holders(task_id))
        return original(attempt_id, error)

    monkeypatch.setattr(recovery, "fail_attempt_start", fail_start)

    continuing = asyncio.create_task(continue_task(task_id, kind, INSTRUCTION))
    await bounded(sdk.settings_pause.reached.wait())
    continuing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await bounded(continuing)
    await settle(client)

    assert held_at_cleanup == [["admission"]]
    assert sdk.calls() == [("resume", SESSION_ID), ("open",), ("update_settings", SELECTED), ("close",)]
    assert outcomes == []
    [attempt] = attempts(task_id)
    assert (attempt.status, attempt.outcome_subtype) == (AttemptStatus.FAILED, "start_failed")
    assert task_state(task_id) == before
    assert_quiescent(client, task_id)


# ---- a stop during the settings update stays a stop -------------------------------------


@pytest.mark.parametrize("settings", ["applied", "failed"])
@pytest.mark.asyncio
async def test_stop_during_settings_update_never_streams_or_resurrects(remote, sdk, outcomes, client, settings):
    register(SELECTED)
    task_id = continuable_task(remote, AttemptKind.FOLLOW_UP)
    with db_session() as session:
        sha = session.get(Task, task_id).candidate_sha
    add_check_run(task_id, sha)
    add_candidate_review(task_id, sha, remote.base_sha)
    await approve_candidate(task_id, sha, approved_via="bearer")
    github = FakeDeliveryGitHub(remote)
    sdk.settings_pause = Pause()
    if settings == "failed":
        sdk.settings_error = RuntimeError("settings RPC failed")

    continuing = asyncio.create_task(continue_task(task_id, AttemptKind.FOLLOW_UP, INSTRUCTION))
    await bounded(sdk.settings_pause.reached.wait())
    stopped = await stop_parent(PARENT_TASK, task_id)
    assert task_state(task_id)[0] == TaskStatus.STOPPED

    # Startup still holds admission, so nothing can be published under it.
    with pytest.raises(DeliveryRefusedError) as blocked:
        await publish_candidate(task_id, sha, github=github)
    assert (blocked.value.detail, blocked.value.extra["reserved_by"]) == ("task_busy", ["admission"])

    sdk.settings_pause.release.set()
    if settings == "failed":
        with pytest.raises(ContinuationRefusedError):
            await bounded(continuing)
    else:
        assert (await bounded(continuing))["status"] == AttemptStatus.STOPPED
    await settle(client)

    assert stopped["attempt_id"] == attempts(task_id)[0].id
    assert sdk.streamed() == []
    assert ("close",) in sdk.calls()
    assert outcomes == []
    [attempt] = attempts(task_id)
    assert attempt.status == AttemptStatus.STOPPED
    assert task_state(task_id)[0] == TaskStatus.STOPPED
    assert_quiescent(client, task_id)
    assert github.calls == [] and remote.head(BRANCH) == remote.base_sha

    # Once startup has fully wound down, the stopped task no longer blocks delivery.
    assert (await publish_candidate(task_id, sha, github=github))["published"] is True


# ---- existing refusals still come before any SDK startup ----------------------------------


@pytest.mark.parametrize("refusal", ["policy", "busy", "continuation_limit", "spending"])
@pytest.mark.asyncio
async def test_refusals_happen_before_any_sdk_startup(remote, sdk, outcomes, client, monkeypatch, refusal):
    register(SELECTED)
    task_id = continuable_task(remote, AttemptKind.FOLLOW_UP)
    before = task_state(task_id)
    busy: asyncio.Task[None] | None = None
    if refusal == "policy":
        monkeypatch.setenv("FORGE_ALLOWED_REPOSITORIES", "someone/else")
    elif refusal == "continuation_limit":
        monkeypatch.setenv("FORGE_MAX_CONTINUATIONS", "0")
    elif refusal == "spending":
        monkeypatch.setenv("FORGE_BUDGET_POLICY", "unrecognised")
    else:
        busy = asyncio.create_task(asyncio.Event().wait())
        client.track(SESSION_ID, busy)
    get_settings.cache_clear()

    try:
        with pytest.raises(ContinuationRefusedError) as refused:
            await continue_task(task_id, AttemptKind.FOLLOW_UP, INSTRUCTION)
    finally:
        if busy is not None:
            busy.cancel()
            await asyncio.gather(busy, return_exceptions=True)

    assert refused.value.status_code in {403, 409}
    assert sdk.sessions == [] and outcomes == []
    assert all(a.status == AttemptStatus.BLOCKED for a in attempts(task_id))
    assert task_state(task_id) == before
    assert task_reservations.holders(task_id) == []
