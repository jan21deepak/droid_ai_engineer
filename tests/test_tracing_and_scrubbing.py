"""Tests for request tracing and log scrubbing."""

import logging

from fastapi.testclient import TestClient

from app.app import app
from app.logging_conf import ScrubbingFilter, scrub, scrub_ctx
from app.tracing import RequestIDMiddleware, current_request_id

app.add_middleware(RequestIDMiddleware)


class TestRequestTracing:
    def test_response_includes_generated_request_id(self):
        with TestClient(app) as client:
            resp = client.get("/health")
        assert resp.headers["x-request-id"]
        assert len(resp.headers["x-request-id"]) == 32

    def test_incoming_request_id_is_echoed(self):
        with TestClient(app) as client:
            resp = client.get("/health", headers={"X-Request-ID": "trace-me-123"})
        assert resp.headers["x-request-id"] == "trace-me-123"

    def test_contextvar_outside_request_is_placeholder(self):
        assert current_request_id() == "-"


class TestScrubbing:
    def test_masks_github_tokens_in_free_text(self):
        line = "failed request with token ghp_0123456789abcdefghijklmnopqrstuv"
        assert "ghp_0123456789" not in scrub(line)
        assert "[REDACTED]" in scrub(line)

    def test_masks_fine_grained_and_sk_keys(self):
        assert "[REDACTED]" in scrub("using github_pat_ABCDEFGHIJKLMNOP_1234567890")
        assert "[REDACTED]" in scrub("key sk-abcdefghijklmnopqrstuvwx")

    def test_masks_sensitive_key_value_pairs(self):
        line = "config: GITHUB_TOKEN=supersecret123 retrying"
        scrubbed = scrub(line)
        assert "supersecret123" not in scrubbed
        assert "GITHUB_TOKEN=" in scrubbed

    def test_leaves_normal_text_alone(self):
        assert scrub("droid.turn_finished duration_seconds=42.0") == "droid.turn_finished duration_seconds=42.0"

    def test_scrub_ctx_masks_key_named_fields(self):
        cleaned = scrub_ctx({"github_token": "ghp_secretvalue123456", "task_id": 3})
        assert cleaned["github_token"] == "[REDACTED]"
        assert cleaned["task_id"] == 3

    def test_filter_scrubs_record_context(self):
        record = logging.LogRecord("test.scrub", logging.INFO, __file__, 1, "msg", None, None)
        record.ctx = {"api_key": "supersecret"}
        ScrubbingFilter().filter(record)
        assert record.ctx["api_key"] == "[REDACTED]"
