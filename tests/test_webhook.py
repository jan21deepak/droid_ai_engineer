import hashlib
import hmac
import json

import pytest
from sqlalchemy.exc import IntegrityError

import app.database as database
from app import tasks, worker
from app.config import get_settings
from app.database import db_session
from app.github import parse_issue_event, should_trigger, verify_signature
from app.models import Repository, Task, TaskStatus, WebhookDelivery
from tests.conftest import OPERATOR_TOKEN, PROTECTED_ORIGIN

SECRET = "test-secret"
TRIGGER_LABEL = "Droid-complete"


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def issue_payload(action="opened", labels=(TRIGGER_LABEL,), label_added=None, number=42):
    payload = {
        "action": action,
        "repository": {
            "full_name": "jan21deepak/superset",
            "html_url": "https://github.com/jan21deepak/superset",
        },
        "issue": {
            "number": number,
            "title": "Fix the chart legend",
            "body": "The legend overlaps the chart.",
            "labels": [{"name": name} for name in labels],
        },
    }
    if label_added:
        payload["label"] = {"name": label_added}
    return payload


class TestSignature:
    def test_valid_signature(self):
        body = b'{"a": 1}'
        assert verify_signature(SECRET, body, sign(body))

    def test_invalid_signature(self):
        assert not verify_signature(SECRET, b'{"a": 1}', "sha256=deadbeef")

    def test_missing_signature(self):
        assert not verify_signature(SECRET, b'{"a": 1}', None)

    def test_no_secret_configured_rejects(self):
        body = b'{"a": 1}'
        assert not verify_signature("", body, None)
        assert not verify_signature("", body, sign(body, ""))


class TestParsing:
    def test_parse_issue_event(self):
        event = parse_issue_event(issue_payload())
        assert event.repository == "jan21deepak/superset"
        assert event.repository_url == "https://github.com/jan21deepak/superset"
        assert event.issue_number == 42
        assert event.issue_title == "Fix the chart legend"
        assert event.issue_body == "The legend overlaps the chart."
        assert event.labels == [TRIGGER_LABEL]

    def test_parse_handles_null_body(self):
        payload = issue_payload()
        payload["issue"]["body"] = None
        assert parse_issue_event(payload).issue_body == ""

    @pytest.mark.parametrize("number", [-1, 0, True, False, 2.5, 2.0, "42", None, 2**40])
    def test_parse_refuses_non_positive_integer_numbers(self, number):
        with pytest.raises(ValueError):
            parse_issue_event(issue_payload(number=number))


class TestTriggerRules:
    def test_opened_with_label_triggers(self):
        assert should_trigger(parse_issue_event(issue_payload("opened")), TRIGGER_LABEL)

    def test_opened_without_label_does_not_trigger(self):
        assert not should_trigger(parse_issue_event(issue_payload("opened", labels=())), TRIGGER_LABEL)

    def test_labeled_with_trigger_label(self):
        event = parse_issue_event(issue_payload("labeled", labels=(TRIGGER_LABEL,), label_added=TRIGGER_LABEL))
        assert should_trigger(event, TRIGGER_LABEL)

    def test_labeled_with_other_label(self):
        assert not should_trigger(
            parse_issue_event(issue_payload("labeled", labels=("bug",), label_added="bug")), TRIGGER_LABEL
        )

    def test_closed_does_not_trigger(self):
        assert not should_trigger(parse_issue_event(issue_payload("closed")), TRIGGER_LABEL)


APPROVED_REPO = "jan21deepak/droid_ai_engineer"


def approved_payload(**kwargs):
    payload = issue_payload(**kwargs)
    payload["repository"] = {"full_name": APPROVED_REPO, "html_url": f"https://github.com/{APPROVED_REPO}"}
    return payload


def register(full_name: str = APPROVED_REPO) -> None:
    with db_session() as session:
        session.add(Repository(full_name=full_name, url=f"https://github.com/{full_name}"))


def task_count() -> int:
    with db_session() as session:
        return session.query(Task).count()


class StubDroid:
    configured = True


@pytest.fixture
def intake(protected_mode, monkeypatch):
    """Protected app with one registered, allowlisted repository and recorded side effects."""
    monkeypatch.setenv("FORGE_ALLOWED_REPOSITORIES", APPROVED_REPO)
    get_settings.cache_clear()
    register()
    spawned: list[int] = []
    github_calls: list[str] = []

    async def record_github(*args, **kwargs):
        github_calls.append("github")
        raise AssertionError("webhook intake must not call GitHub")

    monkeypatch.setattr(tasks, "get_droid_client", lambda: StubDroid())
    monkeypatch.setattr(worker, "spawn_fix_launch", lambda task_id, **kw: spawned.append(task_id))
    for name in ("post_issue_comment", "add_issue_label", "ensure_label", "create_issue"):
        monkeypatch.setattr(f"app.github.GitHubClient.{name}", record_github)
    yield {"spawned": spawned, "github": github_calls}


def simulate_restart() -> None:
    """Drop every in-process cache and reopen the same SQLite database."""
    database.reset_for_tests()
    get_settings.cache_clear()
    database.init_db()


def fail_all_tasks() -> None:
    with db_session() as session:
        session.query(Task).update({Task.status: TaskStatus.FAILED})


def receipts() -> dict[str, int | None]:
    with db_session() as session:
        return {row.delivery_id: row.task_id for row in session.query(WebhookDelivery).all()}


class TestWebhookEndpoint:
    def post(self, client, payload, event="issues", secret=SECRET, sig=True, delivery=None, extra=None, raw=None):
        body = raw if raw is not None else json.dumps(payload).encode()
        headers = {"X-GitHub-Event": event, "Content-Type": "application/json"}
        if sig:
            headers["X-Hub-Signature-256"] = sign(body, secret)
        if delivery:
            headers["X-GitHub-Delivery"] = delivery
        headers.update(extra or {})
        return client.post("/webhook", content=body, headers=headers)

    def assert_nothing_ran(self, intake):
        assert task_count() == 0
        assert intake["spawned"] == []
        assert intake["github"] == []

    def test_accepted_event_schedules_once(self, protected_client, intake):
        resp = self.post(protected_client, approved_payload(), delivery="d-1")
        assert resp.status_code == 202
        assert task_count() == 1
        assert intake["spawned"] == [resp.json()["task_id"]]
        with db_session() as session:
            task = session.query(Task).one()
            assert task.repository == APPROVED_REPO
            assert task.repository_url == f"https://github.com/{APPROVED_REPO}"

    def test_redelivery_does_not_schedule_again(self, protected_client, intake):
        first = self.post(protected_client, approved_payload(), delivery="d-1")
        assert first.status_code == 202
        with db_session() as session:
            session.query(Task).update({Task.status: TaskStatus.FAILED})
        again = self.post(protected_client, approved_payload(), delivery="d-1")
        assert again.status_code == 200
        assert "duplicate" in again.json()["detail"]
        assert task_count() == 1
        assert len(intake["spawned"]) == 1

    def test_duplicate_issue_not_recreated(self, protected_client, intake):
        first = self.post(protected_client, approved_payload(), delivery="d-1")
        second = self.post(protected_client, approved_payload(), delivery="d-2")
        assert first.status_code == 202
        assert second.status_code == 200
        assert "duplicate" in second.json()["detail"]
        assert task_count() == 1
        assert len(intake["spawned"]) == 1

    def test_redelivery_after_failure_and_restart_does_not_relaunch(self, protected_client, intake):
        first = self.post(protected_client, approved_payload(), delivery="d-restart")
        assert first.status_code == 202
        task_id = first.json()["task_id"]
        fail_all_tasks()
        simulate_restart()

        again = self.post(protected_client, approved_payload(), delivery="d-restart")
        assert again.status_code == 200
        assert again.json() == {"detail": "duplicate: delivery already processed", "task_id": task_id}
        assert task_count() == 1
        assert intake["spawned"] == [task_id]

    def test_duplicate_alias_delivery_is_remembered_across_restart(self, protected_client, intake):
        first = self.post(protected_client, approved_payload(), delivery="d-original")
        task_id = first.json()["task_id"]
        alias = self.post(protected_client, approved_payload(), delivery="d-alias")
        assert alias.status_code == 200
        assert receipts() == {"d-original": task_id, "d-alias": task_id}
        fail_all_tasks()
        simulate_restart()

        again = self.post(protected_client, approved_payload(), delivery="d-alias")
        assert again.status_code == 200
        assert again.json()["task_id"] == task_id
        assert task_count() == 1
        assert intake["spawned"] == [task_id]

    def test_receipt_is_committed_before_launch(self, protected_client, intake, monkeypatch):
        seen: list[dict[str, int | None]] = []
        monkeypatch.setattr(worker, "spawn_fix_launch", lambda task_id, **kw: seen.append(receipts()))
        resp = self.post(protected_client, approved_payload(), delivery="d-ordered")
        assert resp.status_code == 202
        assert seen == [{"d-ordered": resp.json()["task_id"]}]

    def test_receipt_unique_constraint_rejects_second_row(self):
        with db_session() as session:
            session.add(WebhookDelivery(delivery_id="d-unique", task_id=None))
        with pytest.raises(IntegrityError), db_session() as session:
            session.add(WebhookDelivery(delivery_id="d-unique", task_id=None))

    def test_concurrent_identical_delivery_loses_race_without_launch(self, protected_client, intake, monkeypatch):
        """Another process commits the same delivery between our lookup and insert."""
        with db_session() as session:
            session.add(WebhookDelivery(delivery_id="d-race", task_id=None))
        monkeypatch.setattr(tasks, "_delivery_receipt", lambda session, delivery_id: None)

        resp = self.post(protected_client, approved_payload(), delivery="d-race")
        assert resp.status_code == 200
        assert "duplicate" in resp.json()["detail"]
        self.assert_nothing_ran(intake)

    def test_explicit_retry_without_that_delivery_key_is_allowed(self, protected_client, intake):
        first = self.post(protected_client, approved_payload(), delivery="d-first")
        fail_all_tasks()
        simulate_restart()

        retry = self.post(protected_client, approved_payload(), delivery="d-new-label-event")
        assert retry.status_code == 202
        assert retry.json()["task_id"] != first.json()["task_id"]
        fail_all_tasks()
        unkeyed = self.post(protected_client, approved_payload())
        assert unkeyed.status_code == 202
        assert task_count() == 3
        assert len(intake["spawned"]) == 3

    @pytest.mark.parametrize(
        "number",
        [-1, 0, True, False, 2.5, 2.0, "42", None, 2**40, "missing"],
        ids=["negative", "zero", "true", "false", "fraction", "float-integral", "string", "null", "huge", "missing"],
    )
    def test_non_positive_integer_issue_number_rejected(self, protected_client, intake, number):
        payload = approved_payload()
        if number == "missing":
            del payload["issue"]["number"]
        else:
            payload["issue"]["number"] = number
        resp = self.post(protected_client, payload, delivery=f"d-bad-{number!r}")
        assert resp.status_code == 422
        self.assert_nothing_ran(intake)
        assert receipts() == {}

    def test_positive_integer_issue_number_accepted(self, protected_client, intake):
        resp = self.post(protected_client, approved_payload(number=7), delivery="d-seven")
        assert resp.status_code == 202
        with db_session() as session:
            assert session.query(Task).one().issue_number == 7

    def test_registered_repo_match_is_case_insensitive(self, protected_client, intake):
        payload = approved_payload()
        payload["repository"]["full_name"] = APPROVED_REPO.upper()
        assert self.post(protected_client, payload).status_code == 202

    def test_unconfigured_secret_rejected(self, protected_client, intake, monkeypatch):
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "")
        get_settings.cache_clear()
        resp = self.post(protected_client, approved_payload(), secret="")
        assert resp.status_code == 503
        self.assert_nothing_ran(intake)

    def test_missing_signature_rejected(self, protected_client, intake):
        assert self.post(protected_client, approved_payload(), sig=False).status_code == 401
        self.assert_nothing_ran(intake)

    def test_invalid_signature_rejected(self, protected_client, intake):
        assert self.post(protected_client, approved_payload(), secret="wrong").status_code == 401
        self.assert_nothing_ran(intake)

    def test_operator_bearer_cannot_replace_hmac(self, protected_client, intake):
        resp = self.post(
            protected_client,
            approved_payload(),
            sig=False,
            extra={"Authorization": f"Bearer {OPERATOR_TOKEN}"},
        )
        assert resp.status_code == 401
        self.assert_nothing_ran(intake)

    def test_operator_session_cannot_replace_hmac(self, protected_client, intake):
        login = protected_client.post(
            "/login",
            data={"token": OPERATOR_TOKEN},
            headers={"Origin": PROTECTED_ORIGIN},
            follow_redirects=False,
        )
        assert login.status_code == 303
        resp = self.post(protected_client, approved_payload(), sig=False, extra={"Origin": PROTECTED_ORIGIN})
        assert resp.status_code == 401
        self.assert_nothing_ran(intake)

    @pytest.mark.parametrize(
        "raw",
        [b"not json", b"[1, 2]", b'"text"', b"\xff\xfe"],
        ids=["not-json", "array", "string", "bad-utf8"],
    )
    def test_malformed_json_rejected(self, protected_client, intake, raw):
        assert self.post(protected_client, None, raw=raw).status_code == 422
        self.assert_nothing_ran(intake)

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda p: p.update(issue={}, repository={}),
            lambda p: p["issue"].update(number="abc"),
            lambda p: p.update(repository="not-an-object"),
            lambda p: p["issue"].update(labels=["not-an-object"]),
        ],
        ids=["empty", "bad-number", "bad-repository", "bad-labels"],
    )
    def test_malformed_issue_payload_rejected(self, protected_client, intake, mutate):
        payload = approved_payload()
        mutate(payload)
        assert self.post(protected_client, payload).status_code == 422
        self.assert_nothing_ran(intake)

    def test_unregistered_allowlisted_repo_rejected(self, protected_client, intake):
        with db_session() as session:
            session.query(Repository).delete()
        resp = self.post(protected_client, approved_payload())
        assert resp.status_code == 403
        self.assert_nothing_ran(intake)

    def test_registered_but_unapproved_repo_rejected(self, protected_client, intake):
        register("jan21deepak/superset")
        resp = self.post(protected_client, issue_payload())
        assert resp.status_code == 403
        self.assert_nothing_ran(intake)

    def test_empty_allowlist_approves_nothing_in_protected_mode(self, protected_client, intake, monkeypatch):
        monkeypatch.setenv("FORGE_ALLOWED_REPOSITORIES", "")
        get_settings.cache_clear()
        assert self.post(protected_client, approved_payload()).status_code == 403
        self.assert_nothing_ran(intake)

    def test_ping_ok(self, protected_client, intake):
        resp = self.post(protected_client, {"zen": "Keep it simple."}, event="ping")
        assert resp.status_code == 200
        assert resp.json()["detail"] == "pong"

    def test_non_issue_event_ignored(self, protected_client, intake):
        resp = self.post(protected_client, {"action": "opened"}, event="pull_request")
        assert resp.status_code == 200
        assert "ignored" in resp.json()["detail"]
        self.assert_nothing_ran(intake)

    def test_issue_without_trigger_label_ignored(self, protected_client, intake):
        resp = self.post(protected_client, approved_payload(labels=()))
        assert resp.status_code == 200
        assert "ignored" in resp.json()["detail"]
        self.assert_nothing_ran(intake)

    def test_local_dev_still_requires_signature_and_registration(self, client, monkeypatch):
        monkeypatch.setattr(tasks, "get_droid_client", lambda: StubDroid())
        monkeypatch.setattr(worker, "spawn_fix_launch", lambda task_id, **kw: None)
        assert self.post(client, approved_payload(), sig=False).status_code == 401
        assert self.post(client, approved_payload()).status_code == 403
        register()
        assert self.post(client, approved_payload()).status_code == 202
