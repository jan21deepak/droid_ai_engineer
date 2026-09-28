def test_root(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers.get("location") == "/dashboard"


def test_dashboard_html(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    assert b"Droid Forge" in resp.content


def test_health_reports_components(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["application"] == "ok"
    assert data["checks"]["database"] == "ok"
    assert data["status"] in ("healthy", "degraded")
    assert set(data["checks"]) == {"database", "github", "droid"}
