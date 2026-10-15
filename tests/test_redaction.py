"""Known credentials are scrubbed before persistence, logging and API display.

The sentinels are deliberately *not* token-shaped, so only the known-value
layer (configured settings and sensitive environment names) can catch them.
"""

import asyncio
import io
import logging
import time
from urllib.parse import quote

import httpx
import pytest
from sqlalchemy import text

from app import droid_client
from app.config import get_settings
from app.database import db_session
from app.droid_client import DroidRunError, _git
from app.logging_conf import ScrubbingFilter, StructuredFormatter, log_event
from app.models import Repository, ReviewTask, Task, TaskStatus
from app.redaction import MASK, git_basic_auth_value, redact, redact_text, safe_error

GITHUB_SENTINEL = "plain-github-sentinel-Zx81"
OPERATOR_SENTINEL = "plain-operator-sentinel-0123456789-abcdefghij"
WEBHOOK_SENTINEL = "plain-webhook-sentinel-Qq42"
SENTINELS = (GITHUB_SENTINEL, OPERATOR_SENTINEL, WEBHOOK_SENTINEL)


@pytest.fixture
def secrets(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", GITHUB_SENTINEL)
    monkeypatch.setenv("FORGE_OPERATOR_TOKEN", OPERATOR_SENTINEL)
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SENTINEL)
    get_settings.cache_clear()


def contains_secret(value: str) -> bool:
    encodings = [git_basic_auth_value(s) for s in SENTINELS]
    return any(s in value for s in (*SENTINELS, *encodings))


class TestRedact:
    def test_known_values_and_their_encodings_are_masked(self, secrets):
        message = (
            f"push failed for https://github.com/o/r.git token {GITHUB_SENTINEL}; "
            f"header basic {git_basic_auth_value(GITHUB_SENTINEL)}; quoted {quote(OPERATOR_SENTINEL, safe='')}"
        )
        cleaned = redact_text(message)
        assert not contains_secret(cleaned)
        assert MASK in cleaned
        assert "push failed for https://github.com/o/r.git" in cleaned

    def test_url_userinfo_is_masked_even_for_unknown_values(self):
        cleaned = redact_text("fatal: unable to access 'https://x-access-token:unknown-value@github.com/o/r.git/'")
        assert "unknown-value" not in cleaned
        assert f"https://{MASK}@github.com/o/r.git" in cleaned

    def test_nested_error_payloads_are_scrubbed_recursively(self, secrets):
        payload = {
            "detail": "GitHub request failed",
            "errors": [
                {"message": f"Bad credentials {GITHUB_SENTINEL}", "context": ("webhook", WEBHOOK_SENTINEL)},
                {"nested": {"deeper": [f"operator={OPERATOR_SENTINEL}"]}},
            ],
            "authorization": "anything",
            "input_tokens": 1234,
            "count": 3,
        }
        cleaned = redact(payload)
        assert not contains_secret(repr(cleaned))
        assert cleaned["authorization"] == MASK
        assert cleaned["input_tokens"] == 1234
        assert cleaned["count"] == 3
        assert cleaned["detail"] == "GitHub request failed"

    def test_exceptions_become_redacted_text(self, secrets):
        exc = RuntimeError(f"clone failed: {GITHUB_SENTINEL}")
        assert not contains_secret(redact(exc))
        assert not contains_secret(safe_error(exc))

    def test_safe_error_is_bounded(self):
        assert len(safe_error("x" * 10_000)) <= 2000

    def test_key_value_redaction_preserves_non_secret_names(self):
        assert redact_text("GITHUB_TOKEN=abcdefgh1234") == f"GITHUB_TOKEN={MASK}"
        assert redact_text('my_api_key: "abcdefgh1234"') == f'my_api_key: "{MASK}"'
        assert redact_text("token_value=abcdefgh1234") == f"token_value={MASK}"
        assert redact_text("a=token=abcdefgh1234") == f"a=token={MASK}"
        assert redact_text("tokens=1234") == "tokens=1234"
        assert redact_text("x.token_a.b=12345678") == "x.token_a.b=12345678"

    def test_key_value_redaction_handles_adversarial_inputs_quickly(self):
        for adversarial in ("token_" + "-_" * 20_000 + "=x", "a=" * 20_000):
            started = time.perf_counter()
            redact_text(adversarial)
            assert time.perf_counter() - started < 1


class TestPersistenceAndDisplay:
    def test_task_and_review_errors_are_scrubbed_before_storage(self, secrets):
        with db_session() as session:
            task = Task(
                repository="o/r",
                issue_number=1,
                status=TaskStatus.FAILED,
                error=f"git push failed: remote said {GITHUB_SENTINEL}",
                summary=f"Done. I used {OPERATOR_SENTINEL}",
            )
            review = ReviewTask(
                repository="o/r",
                pr_number=2,
                status=TaskStatus.FAILED,
                error=f"review post failed {{'message': '{WEBHOOK_SENTINEL}'}}",
            )
            session.add_all([task, review])
            session.flush()
            task_id, review_id = task.id, review.id

        with db_session() as session:
            raw_task = session.execute(text("SELECT error, summary FROM tasks WHERE id = :i"), {"i": task_id}).one()
            raw_review = session.execute(text("SELECT error FROM review_tasks WHERE id = :i"), {"i": review_id}).one()
        assert not contains_secret(" ".join(str(v) for v in (*raw_task, *raw_review)))
        assert "git push failed" in raw_task[0]

    def test_rows_stored_before_redaction_are_scrubbed_on_display(self, client, secrets):
        with db_session() as session:
            session.add(Task(repository="o/r", issue_number=1, status=TaskStatus.FAILED))
            session.add(ReviewTask(repository="o/r", pr_number=2, status=TaskStatus.FAILED))
        with db_session() as session:
            # Simulate legacy rows written by an older version, bypassing ORM validators.
            session.execute(text("UPDATE tasks SET error = :e, summary = :e"), {"e": f"legacy {GITHUB_SENTINEL}"})
            session.execute(text("UPDATE review_tasks SET error = :e, summary = :e"), {"e": f"old {WEBHOOK_SENTINEL}"})

        body = client.get("/api/tasks").text
        assert not contains_secret(body)
        assert "legacy" in body

    def test_route_error_detail_from_github_failure_is_scrubbed(self, client, monkeypatch, secrets):
        monkeypatch.setenv("FORGE_ALLOWED_REPOSITORIES", "o/r")
        get_settings.cache_clear()

        async def failing_send(self, request, *args, **kwargs):
            raise httpx.ConnectError(f"proxy rejected Authorization {GITHUB_SENTINEL}", request=request)

        monkeypatch.setattr(httpx.AsyncClient, "send", failing_send)
        with db_session() as session:
            repo = Repository(full_name="o/r", url="https://github.com/o/r")
            session.add(repo)
            session.flush()
            repo_id = repo.id

        resp = client.post(f"/api/repositories/{repo_id}/sync-issues")
        assert resp.status_code == 502
        assert not contains_secret(resp.text)
        assert "failed to list issues" in resp.json()["detail"]

    @pytest.mark.parametrize(
        "rejection",
        [
            {"json": {"message": f"Validation failed for {GITHUB_SENTINEL}"}},
            {"json": {"message": {"field": f"token {WEBHOOK_SENTINEL}", GITHUB_SENTINEL: [OPERATOR_SENTINEL]}}},
            {"json": [f"unexpected array {GITHUB_SENTINEL}"]},
            {"content": f"<html>proxy echoed Authorization {GITHUB_SENTINEL}</html>".encode()},
            {"content": b""},
        ],
        ids=["json-text", "json-structured", "json-non-object", "non-json", "empty"],
    )
    def test_issue_creation_rejection_and_partial_result_are_scrubbed(self, client, monkeypatch, secrets, rejection):
        monkeypatch.setenv("FORGE_ALLOWED_REPOSITORIES", "o/r")
        monkeypatch.setenv("FORGE_ALLOW_GITHUB_WRITES", "true")
        get_settings.cache_clear()
        with db_session() as session:
            repo = Repository(full_name="o/r", url="https://github.com/o/r")
            session.add(repo)
            session.flush()
            repo_id = repo.id

        async def fake_get_repository(self, full_name):
            return {"full_name": full_name, "parent": {"full_name": "up/r"}}

        async def fake_list_open_issues(self, full_name, per_page=50, max_pages=5):
            return [
                {"number": n, "title": f"Parent {n} {OPERATOR_SENTINEL}", "body": "b", "html_url": f"https://x/{n}"}
                for n in (1, 2)
            ]

        async def fake_list_issues(self, full_name, state="open", per_page=50, max_pages=5):
            return []

        created: list[str] = []

        async def fake_create_issue(self, repository, title, body="", labels=None):
            if created:
                request = httpx.Request("POST", "https://api.github.com/repos/o/r/issues")
                response = httpx.Response(422, request=request, **rejection)
                raise httpx.HTTPStatusError("rejected", request=request, response=response)
            created.append(title)
            return {"number": 9, "title": title, "html_url": f"https://github.com/o/r/issues/9?{GITHUB_SENTINEL}"}

        monkeypatch.setattr("app.github.GitHubClient.get_repository", fake_get_repository)
        monkeypatch.setattr("app.github.GitHubClient.list_open_issues", fake_list_open_issues)
        monkeypatch.setattr("app.github.GitHubClient.list_issues", fake_list_issues)
        monkeypatch.setattr("app.github.GitHubClient.create_issue", fake_create_issue)

        resp = client.post(f"/api/repositories/{repo_id}/issues")

        assert resp.status_code == 422
        assert not contains_secret(resp.text)
        body = resp.json()
        assert isinstance(body["detail"], str) and body["detail"]
        assert len(body["created"]) == 1
        assert body["created"][0]["number"] == 9


class TestLogs:
    def test_structured_context_and_exceptions_are_scrubbed(self, secrets):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(StructuredFormatter())
        handler.addFilter(ScrubbingFilter())
        logger = logging.getLogger("test.redaction")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            log_event(
                logger,
                logging.WARNING,
                "github.call_failed",
                error=f"401 for {GITHUB_SENTINEL}",
                response={"errors": [{"message": WEBHOOK_SENTINEL}]},
            )
            try:
                raise RuntimeError(f"operator {OPERATOR_SENTINEL}")
            except RuntimeError:
                logger.exception("boom %s", GITHUB_SENTINEL)
        finally:
            logger.removeHandler(handler)
        output = stream.getvalue()
        assert "github.call_failed" in output
        assert not contains_secret(output)


class TestGitErrors:
    @pytest.mark.asyncio
    async def test_git_failure_output_is_scrubbed(self, monkeypatch, secrets):
        class FailingProcess:
            returncode = 128

            async def communicate(self):
                stderr = f"fatal: Authentication failed for 'https://x-access-token:{GITHUB_SENTINEL}@github.com/o/r'"
                return b"", stderr.encode()

        async def fake_exec(*args, **kwargs):
            return FailingProcess()

        monkeypatch.setattr(droid_client.asyncio, "create_subprocess_exec", fake_exec)
        with pytest.raises(DroidRunError) as info:
            await _git(["fetch", "origin"])
        assert not contains_secret(str(info.value))
        assert "exit 128" in str(info.value)

    @pytest.mark.asyncio
    async def test_git_timeout_message_is_scrubbed(self, monkeypatch, secrets):
        class HangingProcess:
            returncode = None

            async def communicate(self):
                await asyncio.sleep(10)
                return b"", b""

            def kill(self):
                pass

        async def fake_exec(*args, **kwargs):
            return HangingProcess()

        monkeypatch.setattr(droid_client.asyncio, "create_subprocess_exec", fake_exec)
        with pytest.raises(DroidRunError) as info:
            await _git(["ls-remote", GITHUB_SENTINEL], timeout=0.01)
        assert not contains_secret(str(info.value))
