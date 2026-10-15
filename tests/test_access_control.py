"""Protected-mode operator access: route inventory, sessions, CSRF, Origin and Host defenses."""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import app.auth as auth
from app.database import db_session
from app.models import Repository, Task
from tests.conftest import OPERATOR_TOKEN, PROTECTED_BASE_URL, PROTECTED_ORIGIN

SAME_ORIGIN = {"Origin": PROTECTED_ORIGIN}
HTML = {"Accept": "text/html,application/xhtml+xml"}
PUBLIC_PATHS = {"/health", "/login", "/logout", "/webhook"}
DOC_PATHS = ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"]


def login(client: TestClient, token: str = OPERATOR_TOKEN, headers: dict[str, str] | None = None) -> Any:
    return client.post(
        "/login",
        data={"token": token, "next": "/dashboard"},
        headers=SAME_ORIGIN if headers is None else headers,
        follow_redirects=False,
    )


def csrf_from_dashboard(client: TestClient) -> str:
    page = client.get("/dashboard")
    assert page.status_code == 200
    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text)
    assert match, "dashboard did not expose a CSRF token"
    return match.group(1)


def login_with_csrf(client: TestClient) -> str:
    assert login(client).status_code == 303
    return csrf_from_dashboard(client)


def seed_repository() -> int:
    with db_session() as session:
        repo = Repository(
            full_name="jan21deepak/droid_ai_engineer",
            url="https://github.com/jan21deepak/droid_ai_engineer",
            droid_model="",
            starting_ref="droid_mission",
        )
        session.add(repo)
        session.flush()
        return int(repo.id)


def concrete_path(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


def all_api_routes(routes: list[Any]) -> list[APIRoute]:
    found: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append(route)
        elif hasattr(route, "original_router"):  # FastAPI's lazily included sub-routers (no prefixes used)
            found.extend(all_api_routes(route.original_router.routes))
    return found


def protected_routes() -> list[tuple[str, str]]:
    from app.app import app

    pairs: list[tuple[str, str]] = []
    for route in all_api_routes(app.routes):
        if route.path in PUBLIC_PATHS:
            continue
        for method in sorted(route.methods or ()):
            pairs.append((method, concrete_path(route.path)))
    return pairs


@pytest.fixture
def outbound(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record any GitHub or Droid call the app attempts."""
    calls: list[str] = []

    def recorder(name: str) -> Any:
        async def _record(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            raise AssertionError(f"unexpected outbound call {name}")

        return _record

    for name in (
        "get_repository",
        "list_open_issues",
        "list_issues",
        "list_open_pull_requests",
        "create_issue",
        "ensure_label",
        "add_issue_label",
        "ensure_issues_webhook",
    ):
        monkeypatch.setattr(f"app.github.GitHubClient.{name}", recorder(name))
    monkeypatch.setattr("app.app.start_pr_review", recorder("start_pr_review"))
    return calls


class TestRouteInventory:
    def test_inventory_covers_existing_surfaces(self):
        paths = {path for _, path in protected_routes()}
        for expected in (
            "/",
            "/dashboard",
            "/metrics",
            "/api/health",
            "/api/repositories",
            "/api/repositories/1",
            "/api/issues",
            "/api/pulls",
            "/api/tasks",
            "/api/tasks/1/follow-up",
            "/api/droid/models",
            "/api/session",
        ):
            assert expected in paths

    def test_unauthenticated_api_requests_are_rejected(self, protected_client, outbound):
        for method, path in protected_routes():
            resp = protected_client.request(method, path, headers=SAME_ORIGIN, json={}, follow_redirects=False)
            assert resp.status_code == 401, (method, path, resp.status_code)
            assert resp.json() == {"detail": "authentication required"}
        assert outbound == []

    def test_api_documentation_is_protected(self, protected_client):
        for path in DOC_PATHS:
            resp = protected_client.get(path, follow_redirects=False)
            assert resp.status_code == 401, path
            assert "paths" not in resp.text

    def test_browser_navigation_redirects_to_login(self, protected_client):
        for path in ("/", "/dashboard", "/docs"):
            resp = protected_client.get(path, headers=HTML, follow_redirects=False)
            assert resp.status_code == 303, path
            assert resp.headers["location"].startswith("/login?next=")

    def test_public_surfaces_reveal_no_operator_content(self, protected_client):
        health = protected_client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok", "checks": {"database": "ok"}}
        login_page = protected_client.get("/login")
        assert login_page.status_code == 200
        assert 'name="token"' in login_page.text
        assert OPERATOR_TOKEN not in login_page.text
        assert protected_client.get("/static/dashboard.js").status_code == 200

    def test_detailed_health_requires_auth(self, protected_client):
        assert protected_client.get("/api/health").status_code == 401
        protected_client.headers["Authorization"] = f"Bearer {OPERATOR_TOKEN}"
        detail = protected_client.get("/api/health").json()
        assert set(detail["checks"]) == {"database", "github", "droid"}


class TestLoginLifecycle:
    def test_invalid_login_creates_no_session(self, protected_client):
        resp = login(protected_client, token="wrong-token")
        assert resp.status_code == 401
        assert "set-cookie" not in resp.headers
        assert "Invalid operator token" in resp.text
        assert protected_client.get("/api/repositories").status_code == 401

    def test_valid_login_enables_protected_navigation(self, protected_client):
        seed_repository()
        resp = login(protected_client)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/dashboard"
        page = protected_client.get("/dashboard")
        assert page.status_code == 200
        assert 'id="logout-form"' in page.text
        assert "Local development mode" not in page.text
        assert OPERATOR_TOKEN not in page.text
        repos = protected_client.get("/api/repositories")
        assert repos.status_code == 200
        assert repos.json()["repositories"][0]["full_name"] == "jan21deepak/droid_ai_engineer"
        assert protected_client.get("/metrics").status_code == 200
        assert protected_client.get("/docs").status_code == 200
        assert protected_client.get("/openapi.json").json()["paths"]
        session = protected_client.get("/api/session").json()
        assert session["mode"] == "protected"
        assert session["method"] == "session"
        assert session["expires_at"]

    def test_session_cookie_attributes(self, protected_client):
        resp = login(protected_client)
        cookie = resp.headers["set-cookie"]
        attrs = [part.strip().lower() for part in cookie.split(";")]
        assert attrs[0].startswith(f"{auth.SESSION_COOKIE}=")
        assert "httponly" in attrs
        assert "samesite=strict" in attrs
        assert "path=/" in attrs
        assert f"max-age={8 * 3600}" in attrs
        assert not any(a.startswith("domain=") for a in attrs)
        # Plain-HTTP loopback is the documented demo exception to Secure.
        assert "secure" not in attrs

    def test_cookie_is_secure_off_loopback(self, protected_mode, monkeypatch):
        from app.app import app

        monkeypatch.setenv("FORGE_TRUSTED_HOSTS", "forge.internal")
        from app.config import get_settings

        get_settings.cache_clear()
        with TestClient(app, base_url="http://forge.internal") as c:
            resp = login(c, headers={"Origin": "http://forge.internal"})
        assert resp.status_code == 303
        assert "secure" in [p.strip().lower() for p in resp.headers["set-cookie"].split(";")]

    def test_login_redirect_rejects_offsite_next(self, protected_client):
        resp = protected_client.post(
            "/login",
            data={"token": OPERATOR_TOKEN, "next": "//evil.example/steal"},
            headers=SAME_ORIGIN,
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/dashboard"

    def test_login_requires_same_origin(self, protected_client):
        assert login(protected_client, headers={}).status_code == 403
        cross = login(protected_client, headers={"Origin": "http://evil.example"})
        assert cross.status_code == 403
        assert "set-cookie" not in cross.headers

    def test_logout_invalidates_session(self, protected_client):
        csrf = login_with_csrf(protected_client)
        cookie = protected_client.cookies.get(auth.SESSION_COOKIE)
        resp = protected_client.post("/logout", data={"csrf_token": csrf}, headers=SAME_ORIGIN, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"
        assert protected_client.get("/api/repositories").status_code == 401
        # Replaying the old cookie directly is still rejected.
        protected_client.cookies.set(auth.SESSION_COOKIE, cookie)
        assert protected_client.get("/api/repositories").status_code == 401
        assert protected_client.get("/dashboard", headers=HTML, follow_redirects=False).status_code == 303

    def test_logout_requires_csrf(self, protected_client):
        login_with_csrf(protected_client)
        resp = protected_client.post(
            "/logout", data={"csrf_token": "forged"}, headers=SAME_ORIGIN, follow_redirects=False
        )
        assert resp.status_code == 403
        assert protected_client.get("/api/repositories").status_code == 200

    def test_expired_session_cannot_read_or_mutate(self, protected_client, monkeypatch):
        repo_id = seed_repository()
        csrf = login_with_csrf(protected_client)
        now = auth.SESSIONS.now()
        monkeypatch.setattr(auth.SESSIONS, "now", lambda: now + 8 * 3600 + 1)
        assert protected_client.get("/api/repositories").status_code == 401
        resp = protected_client.patch(
            f"/api/repositories/{repo_id}",
            json={"droid_model": "changed"},
            headers={**SAME_ORIGIN, "X-CSRF-Token": csrf},
        )
        assert resp.status_code == 401
        with db_session() as session:
            assert session.get(Repository, repo_id).droid_model == ""


class TestConfiguration:
    @pytest.mark.parametrize("token", ["", "short-token"])
    def test_missing_or_weak_credential_fails_closed(self, protected_client, monkeypatch, token):
        from app.config import get_settings

        monkeypatch.setenv("FORGE_OPERATOR_TOKEN", token)
        get_settings.cache_clear()
        assert protected_client.get("/api/repositories").status_code == 503
        assert protected_client.get("/dashboard", headers=HTML).status_code == 503
        resp = login(protected_client, token=token)
        assert resp.status_code == 503
        assert "set-cookie" not in resp.headers
        protected_client.headers["Authorization"] = f"Bearer {token}"
        assert protected_client.get("/api/repositories").status_code == 503

    def test_unknown_mode_is_protected(self, protected_client, monkeypatch):
        from app.config import get_settings

        monkeypatch.setenv("FORGE_ACCESS_MODE", "off")
        get_settings.cache_clear()
        assert protected_client.get("/api/repositories").status_code == 401

    def test_unset_mode_defaults_to_protected(self, monkeypatch):
        from app.config import ACCESS_MODE_PROTECTED, Settings, effective_access_mode

        monkeypatch.delenv("FORGE_ACCESS_MODE")
        assert effective_access_mode(Settings(_env_file=None)) == ACCESS_MODE_PROTECTED

    def test_missing_external_keys_do_not_disable_auth(self, protected_client):
        # conftest clears GITHUB_TOKEN/FACTORY_API_KEY; protection still applies.
        assert protected_client.get("/metrics").status_code == 401

    def test_explicit_local_dev_allows_anonymous_operator_access(self, client):
        assert client.get("/api/repositories").status_code == 200
        page = client.get("/dashboard")
        assert page.status_code == 200
        assert "Local development mode: operator sign-in disabled" in page.text
        assert 'id="logout-form"' not in page.text
        session = client.get("/api/session").json()
        assert session == {"mode": "local_dev", "method": "local_dev", "expires_at": None}

    def test_local_dev_still_rejects_cross_origin_mutation(self, client):
        repo_id = seed_repository()
        resp = client.delete(f"/api/repositories/{repo_id}", headers={"Origin": "http://evil.example"})
        assert resp.status_code == 403
        with db_session() as session:
            assert session.get(Repository, repo_id) is not None


class TestBearer:
    def test_bearer_reads_and_mutates_without_cookie(self, protected_client):
        repo_id = seed_repository()
        protected_client.headers["Authorization"] = f"Bearer {OPERATOR_TOKEN}"
        assert protected_client.get("/api/repositories").status_code == 200
        resp = protected_client.patch(f"/api/repositories/{repo_id}", json={"droid_model": "bearer-model"})
        assert resp.status_code == 200
        assert "set-cookie" not in resp.headers

    def test_wrong_bearer_rejected(self, protected_client):
        protected_client.headers["Authorization"] = "Bearer not-the-operator-token"
        assert protected_client.get("/api/repositories").status_code == 401

    def test_credentials_in_url_are_not_accepted(self, protected_client):
        assert protected_client.get(f"/api/repositories?token={OPERATOR_TOKEN}").status_code == 401
        assert protected_client.get(f"/api/repositories?access_token={OPERATOR_TOKEN}").status_code == 401

    def test_comparison_is_constant_time(self, monkeypatch):
        seen: list[tuple[bytes, bytes]] = []
        real = auth.hmac.compare_digest

        def spy(a: bytes, b: bytes) -> bool:
            seen.append((a, b))
            return real(a, b)

        monkeypatch.setattr(auth.hmac, "compare_digest", spy)
        assert auth.tokens_match(OPERATOR_TOKEN, OPERATOR_TOKEN)
        assert not auth.tokens_match("x", OPERATOR_TOKEN)
        assert len(seen) == 2
        assert all(len(a) == len(b) == 32 for a, b in seen)


def mutation_requests(repo_id: int) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        ("POST", "/api/repositories", {"url": "jan21deepak/droid_ai_engineer"}),
        ("PATCH", f"/api/repositories/{repo_id}", {"droid_model": "forged"}),
        ("DELETE", f"/api/repositories/{repo_id}", {}),
        ("POST", f"/api/repositories/{repo_id}/sync-issues", {}),
        ("POST", f"/api/repositories/{repo_id}/issues", {}),
        ("POST", "/api/issues/assign", {"issue_ids": [1]}),
        ("POST", "/api/webhooks/sync", {}),
        ("POST", "/api/pulls/assign", {"pull_ids": [1]}),
        ("POST", "/api/tasks/1/follow-up", {"instruction": "forged"}),
    ]


def state_snapshot() -> tuple[Any, ...]:
    with db_session() as session:
        repos = [(r.id, r.full_name, r.droid_model) for r in session.query(Repository).all()]
        tasks = session.query(Task).count()
    return (repos, tasks)


class TestRequestForgery:
    @pytest.mark.parametrize(
        "headers",
        [
            pytest.param(SAME_ORIGIN, id="missing-csrf"),
            pytest.param({**SAME_ORIGIN, "X-CSRF-Token": "forged"}, id="invalid-csrf"),
            pytest.param({"Origin": "http://evil.example", "X-CSRF-Token": None}, id="cross-origin"),
            pytest.param({"Origin": "null", "X-CSRF-Token": None}, id="null-origin"),
            pytest.param({"Referer": "http://evil.example/x", "X-CSRF-Token": None}, id="cross-referer"),
            pytest.param({"X-CSRF-Token": None}, id="no-origin-or-referer"),
            pytest.param(
                {
                    "Origin": "http://evil.example",
                    "X-Forwarded-Host": "127.0.0.1:8010",
                    "X-Forwarded-Proto": "http",
                    "Forwarded": "host=127.0.0.1:8010;proto=http",
                    "X-CSRF-Token": None,
                },
                id="spoofed-forwarded",
            ),
        ],
    )
    def test_forged_cookie_mutations_change_nothing(self, protected_client, outbound, headers):
        repo_id = seed_repository()
        csrf = login_with_csrf(protected_client)
        resolved = {k: (csrf if v is None else v) for k, v in headers.items()}
        before = state_snapshot()
        for method, path, body in mutation_requests(repo_id):
            resp = protected_client.request(method, path, json=body, headers=resolved)
            assert resp.status_code == 403, (method, path, resp.status_code)
        assert state_snapshot() == before
        assert outbound == []

    def test_every_mutation_route_requires_csrf(self, protected_client, outbound):
        login_with_csrf(protected_client)
        for method, path in protected_routes():
            if method in {"GET", "HEAD", "OPTIONS"}:
                continue
            resp = protected_client.request(method, path, json={}, headers=SAME_ORIGIN)
            assert resp.status_code == 403, (method, path)
        assert outbound == []

    def test_same_origin_mutation_with_csrf_succeeds(self, protected_client):
        repo_id = seed_repository()
        csrf = login_with_csrf(protected_client)
        headers = {**SAME_ORIGIN, "X-CSRF-Token": csrf}
        resp = protected_client.patch(f"/api/repositories/{repo_id}", json={"droid_model": "operator"}, headers=headers)
        assert resp.status_code == 200
        with db_session() as session:
            assert session.get(Repository, repo_id).droid_model == "operator"
        referer_only = {"Referer": f"{PROTECTED_BASE_URL}/dashboard", "X-CSRF-Token": csrf}
        assert protected_client.delete(f"/api/repositories/{repo_id}", headers=referer_only).status_code == 200


class TestHostDefense:
    def test_untrusted_host_rejected(self, protected_client):
        resp = protected_client.get("/health", headers={"Host": "evil.example"})
        assert resp.status_code == 400

    def test_forwarded_host_does_not_confer_trust(self, protected_client):
        resp = protected_client.get(
            "/health",
            headers={"Host": "evil.example", "X-Forwarded-Host": "127.0.0.1:8010", "Forwarded": "host=127.0.0.1"},
        )
        assert resp.status_code == 400

    def test_rebinding_login_rejected(self, protected_client):
        resp = protected_client.post(
            "/login",
            data={"token": OPERATOR_TOKEN},
            headers={"Host": "evil.example", "Origin": "http://evil.example"},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "set-cookie" not in resp.headers

    def test_public_base_url_host_is_trusted(self, protected_mode, monkeypatch):
        from app.app import app
        from app.config import get_settings

        monkeypatch.setenv("PUBLIC_BASE_URL", "https://hooks.example.com")
        get_settings.cache_clear()
        with TestClient(app, base_url="https://hooks.example.com") as c:
            assert c.get("/health").status_code == 200
