import hashlib
import hmac
import json

from app.github import IssueEvent, parse_issue_event, should_trigger, verify_signature

SECRET = "test-secret"


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def issue_payload(action="opened", labels=("Cursor-complete",), label_added=None, number=42):
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

    def test_no_secret_configured_accepts(self):
        assert verify_signature("", b'{"a": 1}', None)


class TestParsing:
    def test_parse_issue_event(self):
        event = parse_issue_event(issue_payload())
        assert event.repository == "jan21deepak/superset"
        assert event.repository_url == "https://github.com/jan21deepak/superset"
        assert event.issue_number == 42
        assert event.issue_title == "Fix the chart legend"
        assert event.issue_body == "The legend overlaps the chart."
        assert event.labels == ["Cursor-complete"]

    def test_parse_handles_null_body(self):
        payload = issue_payload()
        payload["issue"]["body"] = None
        assert parse_issue_event(payload).issue_body == ""


class TestTriggerRules:
    def test_opened_with_label_triggers(self):
        assert should_trigger(parse_issue_event(issue_payload("opened")), "Cursor-complete")

    def test_opened_without_label_does_not_trigger(self):
        assert not should_trigger(parse_issue_event(issue_payload("opened", labels=())), "Cursor-complete")

    def test_labeled_with_trigger_label(self):
        event = parse_issue_event(
            issue_payload("labeled", labels=("Cursor-complete",), label_added="Cursor-complete")
        )
        assert should_trigger(event, "Cursor-complete")

    def test_labeled_with_other_label(self):
        event = parse_issue_event(issue_payload("labeled", labels=("bug",), label_added="bug"))
        assert not should_trigger(event, "Cursor-complete")

    def test_closed_does_not_trigger(self):
        assert not should_trigger(parse_issue_event(issue_payload("closed")), "Cursor-complete")


class TestWebhookEndpoint:
    def post(self, client, payload, event="issues", secret=SECRET, sig=True):
        body = json.dumps(payload).encode()
        headers = {"X-GitHub-Event": event, "Content-Type": "application/json"}
        if sig:
            headers["X-Hub-Signature-256"] = sign(body, secret)
        return client.post("/webhook", content=body, headers=headers)

    def test_invalid_signature_401(self, client):
        resp = self.post(client, issue_payload(), secret="wrong")
        assert resp.status_code == 401

    def test_ping_ok(self, client):
        resp = self.post(client, {"zen": "Keep it simple."}, event="ping")
        assert resp.status_code == 200
        assert resp.json()["detail"] == "pong"

    def test_non_issue_event_ignored(self, client):
        resp = self.post(client, {"action": "opened"}, event="pull_request")
        assert resp.status_code == 200
        assert "ignored" in resp.json()["detail"]

    def test_issue_without_trigger_label_ignored(self, client):
        resp = self.post(client, issue_payload(labels=()))
        assert resp.status_code == 200
        assert "ignored" in resp.json()["detail"]

    def test_issue_with_label_creates_task(self, client):
        resp = self.post(client, issue_payload())
        assert resp.status_code == 202
        assert "task_id" in resp.json()

    def test_duplicate_issue_not_recreated(self, client):
        first = self.post(client, issue_payload())
        assert first.status_code == 202
        second = self.post(client, issue_payload())
        assert second.status_code == 200
        assert "duplicate" in second.json()["detail"]

    def test_malformed_payload_422(self, client):
        resp = self.post(client, {"action": "opened", "issue": {}, "repository": {}})
        assert resp.status_code == 422
