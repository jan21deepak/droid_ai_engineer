"""Restricted reviewer policy: tool exposure, exact review context and structured verdicts."""

import asyncio
import json
from pathlib import Path
from typing import ClassVar

import pytest

from app import droid_client
from app.droid_client import DroidClient
from app.review_policy import (
    REVIEW_POLICY_RESTRICTED,
    ReviewContext,
    ReviewContextIncompleteError,
    ReviewVerdict,
    assess_diff,
    build_review_prompt,
    effective_verdict,
    parse_review_output,
    review_session_config,
)

BASE = "a" * 40
HEAD = "b" * 40
DIFF = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"


def context(**overrides) -> ReviewContext:
    values = {
        "workspace": Path("/tmp/review-ws"),
        "base_ref": "main",
        "base_sha": BASE,
        "head_sha": HEAD,
        "diff": DIFF,
        "incomplete_reason": None,
    }
    values.update(overrides)
    return ReviewContext(**values)


def structured(verdict="APPROVE", findings=None, line=True) -> str:
    payload = {"verdict": verdict, "summary": "Looks right.", "findings": findings or []}
    tail = f"\nVERDICT: {verdict}" if line else ""
    return f"Review notes.\n\n```json\n{json.dumps(payload)}\n```{tail}"


class TestSessionConfig:
    def test_review_config_is_exactly_the_verified_restriction(self):
        config = review_session_config()
        assert sorted(config.restrict_tools) == ["Glob", "Grep", "Read"]
        assert config.disable_builtin_skills is True
        assert config.auto_reject_permission_requests is True
        # No extra enabled/disabled/additional arrays, MCP servers or autonomy override.
        assert config.enabled_tools is None
        assert config.disabled_tools is None
        assert config.additional_tools is None
        assert tuple(config.mcp_servers) == ()
        assert config.autonomy is None


class RecordingSession:
    """Session stand-in recording how review sessions are opened."""

    created: ClassVar[list["RecordingSession"]] = []
    resumed: ClassVar[list[str]] = []

    def __init__(self, *, cwd=None, model=None, config=None, runtime=None, api_key=None):
        self.cwd, self.config, self.id = cwd, config, f"rev-{len(RecordingSession.created) + 1}"
        self.prompts: list[str] = []
        RecordingSession.created.append(self)

    @classmethod
    def resume(cls, session_id, **kwargs):
        cls.resumed.append(session_id)
        raise AssertionError("review sessions must never be resumed")

    async def open(self):
        return None

    async def close(self):
        return None

    def stream(self, prompt, **kwargs):
        self.prompts.append(prompt)
        raise RuntimeError("no real stream in unit tests")


@pytest.fixture
def review_sessions(monkeypatch):
    RecordingSession.created = []
    RecordingSession.resumed = []

    async def fake_runtime(*, cwd, api_key):
        return droid_client.Runtime()

    monkeypatch.setattr(droid_client, "open_agent_runtime", fake_runtime)
    monkeypatch.setattr(droid_client, "Session", RecordingSession)
    return RecordingSession


class TestReviewSessions:
    @pytest.mark.asyncio
    async def test_fresh_review_uses_restricted_config_in_clean_context(self, review_sessions):
        client = DroidClient(api_key="fk_test")
        result = await client.start_review_run(
            context=context(),
            repository_url="https://github.com/o/r",
            review_id=1,
            pr_url="https://github.com/o/r/pull/3",
        )
        for _ in range(5):
            await asyncio.sleep(0)
        assert result["status"] == "running"
        [session] = review_sessions.created
        assert session.config == review_session_config()
        assert session.cwd == "/tmp/review-ws"
        assert review_sessions.resumed == []
        prompt = session.prompts[0]
        assert BASE in prompt and HEAD in prompt and "+new" in prompt
        assert "Read, Grep and Glob" in prompt
        lowered = prompt.lower()
        assert "push" not in lowered
        assert "merge" not in lowered

    @pytest.mark.asyncio
    async def test_session_id_is_handed_over_before_the_turn_starts(self, review_sessions):
        seen: list[tuple[str, int]] = []

        def on_session_opened(session_id):
            seen.append((session_id, len(review_sessions.created[0].prompts)))

        client = DroidClient(api_key="fk_test")
        result = await client.start_review_run(
            context=context(),
            repository_url="https://github.com/o/r",
            review_id=1,
            pr_url="",
            on_session_opened=on_session_opened,
        )
        for _ in range(5):
            await asyncio.sleep(0)
        # Called with the session's id while no prompt had been streamed yet.
        assert seen == [(result["session_id"], 0)]

    @pytest.mark.asyncio
    async def test_failing_session_hook_never_starts_the_turn(self, review_sessions):
        def on_session_opened(session_id):
            raise RuntimeError("row could not be updated")

        client = DroidClient(api_key="fk_test")
        with pytest.raises(RuntimeError):
            await client.start_review_run(
                context=context(),
                repository_url="https://github.com/o/r",
                review_id=1,
                pr_url="",
                on_session_opened=on_session_opened,
            )
        for _ in range(5):
            await asyncio.sleep(0)
        assert review_sessions.created[0].prompts == []
        assert client.active_sessions == set()

    @pytest.mark.asyncio
    async def test_incomplete_context_never_opens_a_session(self, review_sessions):
        client = DroidClient(api_key="fk_test")
        with pytest.raises(ReviewContextIncompleteError):
            await client.start_review_run(
                context=context(incomplete_reason="diff is too large"),
                repository_url="https://github.com/o/r",
                review_id=1,
                pr_url="",
            )
        assert review_sessions.created == []

    def test_prompt_refuses_incomplete_context(self):
        with pytest.raises(ReviewContextIncompleteError):
            build_review_prompt(context(incomplete_reason="x"), repository_url="u", pr_url="p")


class TestDiffAssessment:
    @pytest.mark.parametrize(
        ("base", "head", "diff", "fragment"),
        [
            (None, HEAD, DIFF, "base commit"),
            (BASE, None, DIFF, "head commit"),
            (BASE, BASE, DIFF, "identical"),
            (BASE, HEAD, None, "could not be computed"),
            (BASE, HEAD, "   \n", "empty"),
        ],
    )
    def test_missing_pieces_are_incomplete(self, base, head, diff, fragment):
        assert fragment in assess_diff(base, head, diff)

    def test_oversized_diff_is_incomplete(self, monkeypatch):
        monkeypatch.setattr("app.review_policy.REVIEW_DIFF_MAX_CHARS", 10)
        assert "review limit" in assess_diff(BASE, HEAD, DIFF)

    def test_complete_diff(self):
        assert assess_diff(BASE, HEAD, DIFF) is None


class TestStructuredVerdict:
    def test_approve_with_findings(self):
        parsed = parse_review_output(
            structured(findings=[{"severity": "nit", "file": "x.py", "line": 1, "title": "Rename", "detail": "d"}])
        )
        assert parsed.verdict == ReviewVerdict.APPROVE
        assert parsed.findings == [{"severity": "nit", "title": "Rename", "file": "x.py", "line": 1, "detail": "d"}]

    def test_request_changes(self):
        parsed = parse_review_output(structured("REQUEST_CHANGES", [{"severity": "blocker", "title": "Bug"}]))
        assert parsed.verdict == ReviewVerdict.REQUEST_CHANGES

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "Looks fine.\nVERDICT: APPROVE",  # bare line, no structured block
            "```json\n{not json}\n```\nVERDICT: APPROVE",
            structured("LGTM"),
            structured("APPROVE").replace("VERDICT: APPROVE", "VERDICT: REQUEST_CHANGES"),
            structured("APPROVE", [{"severity": "blocker", "title": "Data loss"}]),
        ],
        ids=["empty", "unstructured", "bad_json", "unknown_verdict", "conflict", "approve_with_blocker"],
    )
    def test_malformed_or_inconsistent_output_is_inconclusive(self, text):
        assert parse_review_output(text).verdict == ReviewVerdict.INCONCLUSIVE

    def test_findings_are_bounded_and_redacted(self):
        secret = "ghp_" + "Z" * 36
        findings = [
            {"severity": "weird", "title": f"t{i}", "detail": f"token {secret} " + "d" * 5000} for i in range(80)
        ]
        parsed = parse_review_output(structured("REQUEST_CHANGES", findings))
        assert len(parsed.findings) == 50
        assert parsed.findings[0]["severity"] == "minor"
        assert len(parsed.findings[0]["detail"]) <= 2000
        assert secret not in json.dumps(parsed.findings)

    @pytest.mark.parametrize(
        ("policy", "complete", "sha"),
        [
            (None, True, HEAD),
            (REVIEW_POLICY_RESTRICTED, None, HEAD),
            (REVIEW_POLICY_RESTRICTED, False, HEAD),
            (REVIEW_POLICY_RESTRICTED, True, None),
        ],
        ids=["legacy_unrestricted", "unknown_diff", "incomplete_diff", "unknown_sha"],
    )
    def test_approval_requires_restricted_complete_sha_bound_review(self, policy, complete, sha):
        parsed = parse_review_output(structured())
        result = effective_verdict(parsed, review_policy=policy, diff_complete=complete, reviewed_sha=sha)
        assert result.verdict == ReviewVerdict.INCONCLUSIVE

    def test_restricted_complete_review_keeps_verdict(self):
        parsed = parse_review_output(structured())
        result = effective_verdict(
            parsed, review_policy=REVIEW_POLICY_RESTRICTED, diff_complete=True, reviewed_sha=HEAD
        )
        assert result.verdict == ReviewVerdict.APPROVE
