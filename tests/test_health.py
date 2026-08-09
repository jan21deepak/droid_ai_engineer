def test_root(client):
    resp = client.get("/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["service"] == "devin-ai-engineer"
    assert "links" in data


def test_health_reports_components(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["application"] == "ok"
    assert data["checks"]["database"] == "ok"
    assert data["status"] in ("healthy", "degraded")
    assert set(data["checks"]) == {"database", "github", "devin"}
