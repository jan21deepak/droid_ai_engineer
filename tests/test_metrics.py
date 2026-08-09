from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.database import db_session
from app.models import Repository, ReviewTask, Task, TaskStatus
from app.repos import parse_repository_ref
from app.timeutil import now_sgt


def add_task(session, status, duration=None, acus_consumed=None, cost_usd=None, pr_url=None, pr_state=None):
    task = Task(
        repository="jan21deepak/superset",
        issue_number=1,
        issue_title="t",
        cursor_agent_id=f"session-{status}-{duration}",
        status=status,
        duration_seconds=duration,
        acus_consumed=acus_consumed,
        cost_usd=cost_usd,
        pull_request_url=pr_url,
        pr_state=pr_state,
        completed_at=datetime.now(timezone.utc) if duration else None,
    )
    session.add(task)


def add_review(session, status, duration=None, merged=False):
    review = ReviewTask(
        repository="jan21deepak/superset",
        pr_number=3,
        pr_title="demo review",
        pr_url="https://github.com/jan21deepak/superset/pull/3",
        status=status,
        duration_seconds=duration,
        merged=merged,
        completed_at=datetime.now(timezone.utc) if duration else None,
    )
    session.add(review)


def test_metrics_empty(client):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["running"] == 0
    assert data["completed"] == 0
    assert data["failed"] == 0
    assert data["success_rate"] == 0
    assert data["average_runtime_seconds"] == 0
    assert data["average_runtime_minutes"] == 0
    assert data["total_runtime_minutes"] == 0
    assert data["fix"]["average_runtime_minutes"] == 0
    assert data["review"]["average_runtime_minutes"] == 0
    assert data["engineering"]["pr_cycle_time_hours"] == 0
    assert data["engineering"]["prs_last_7_days"] == 0
    assert data["tokens_used"] is None
    assert data["cursor_cost_usd"] == 0
    assert data["productivity_gained_usd"] == 0


def test_metrics_with_tasks(client):
    with db_session() as session:
        add_task(session, TaskStatus.RUNNING)
        add_task(session, TaskStatus.QUEUED)
        add_task(session, TaskStatus.COMPLETED, duration=100, cost_usd=4.5)
        add_task(session, TaskStatus.COMPLETED, duration=200, cost_usd=2.25)
        add_task(session, TaskStatus.FAILED)
        add_review(session, TaskStatus.COMPLETED, duration=60, merged=True)
        add_review(session, TaskStatus.RUNNING)

    data = client.get("/metrics").json()
    assert data["total"] == 7
    assert data["running"] == 3
    assert data["completed"] == 3
    assert data["failed"] == 1
    assert data["success_rate"] == 75.0
    assert data["fix"]["average_runtime_seconds"] == 150
    assert data["fix"]["average_runtime_minutes"] == 2.5
    assert data["fix"]["total_runtime_minutes"] == 5.0
    assert data["review"]["average_runtime_minutes"] == 1.0
    assert data["review"]["total_runtime_minutes"] == 1.0
    assert data["review"]["merged"] == 1
    assert data["cursor_cost_usd"] == 6.75
    assert data["tokens_used"] is None
    # (2 fixes × 4h + 1 review × 1h) × (150000/1920) − 6.75
    assert data["productivity_gained_usd"] > 0
    assert "assumptions" in data
    assert data["assumptions"]["junior_hours_per_review"] == 1.0


def test_dashboard_renders(client):
    with db_session() as session:
        add_task(session, TaskStatus.COMPLETED, duration=60)
        add_review(session, TaskStatus.COMPLETED, duration=30)
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "Repositories" in resp.text
    assert "Assign to Cursor" in resp.text
    assert "Review ready PRs" not in resp.text
    assert "Assign to Cursor Review" not in resp.text
    assert "Fix Time Stats" in resp.text
    assert "Review Time Stats" in resp.text
    assert "Engineering KPIs" in resp.text
    assert "Avg PR Cycle Time" in resp.text
    assert "PRs Delivered (7d)" in resp.text
    assert "Merge Rate" in resp.text
    assert "Lead Time to PR" not in resp.text
    assert "Change Failure Rate" in resp.text
    assert "Time-to-Dollar Savings" in resp.text
    assert "Cursor Agent" in resp.text
    assert "Cursor Forge" in resp.text
    assert "The AI Engineer That Delivers" in resp.text
    assert "Autonomous Issue Resolution" not in resp.text
    assert 'data-bs-theme="dark"' in resp.text
    assert "Daily Activity" in resp.text
    assert 'id="daily-chart"' in resp.text
    assert "last 14 days · SGT" in resp.text
    # Status cards, KPIs, ROI figure and the daily chart all carry explanations
    assert resp.text.count('class="metric-info"') == 12
    assert "Cursor Runs Completed" in resp.text
    assert "PRs merged" in resp.text
    assert "Tokens Used" not in resp.text
    assert "Algorithm:" not in resp.text
    assert "Cursor Cost" not in resp.text
    assert "ZZZ-gone" not in resp.text
    assert "Recent Activity" in resp.text
    assert 'id="activity-pager"' in resp.text
    assert 'id="activity-pagination"' in resp.text
    # Static assets carry a cache-busting token so browsers can't run stale JS
    assert "/static/dashboard.js?v=" in resp.text
    assert "/static/dashboard.css?v=" in resp.text


def test_pagination_controls_render_server_side(client):
    """Page links must exist in the HTML itself, not only after JS runs."""
    with db_session() as session:
        for _ in range(12):
            add_task(session, TaskStatus.COMPLETED, duration=60)

    body = client.get("/dashboard").text
    assert 'data-page="2"' in body
    assert body.count('class="page-link"') >= 4


def test_activity_api_paginates_without_trimming_total(client):
    """Every fix + review is reachable across pages; nothing is silently dropped."""
    with db_session() as session:
        for i in range(12):
            add_task(session, TaskStatus.COMPLETED, duration=60 + i)
            add_review(session, TaskStatus.COMPLETED, duration=30 + i, merged=False)

    page1 = client.get("/api/tasks?page=1&page_size=10").json()
    assert page1["total"] == 24
    assert page1["total_pages"] == 3
    assert page1["page"] == 1
    assert page1["page_size"] == 10
    assert len(page1["tasks"]) == 10

    page2 = client.get("/api/tasks?page=2&page_size=10").json()
    assert page2["page"] == 2
    assert len(page2["tasks"]) == 10

    page3 = client.get("/api/tasks?page=3&page_size=10").json()
    assert page3["page"] == 3
    assert len(page3["tasks"]) == 4

    seen = {
        (t["kind"], t.get("issue_number") or t.get("pr_number"), t.get("id"))
        for page in (page1, page2, page3)
        for t in page["tasks"]
    }
    assert len(seen) == 24

    overflow = client.get("/api/tasks?page=99&page_size=10").json()
    assert overflow["page"] == 3
    assert len(overflow["tasks"]) == 4


def test_failed_review_counts_toward_change_failure_rate(client):
    """A failed review must not read as 0% while the Failed card shows it."""
    with db_session() as session:
        add_task(session, TaskStatus.COMPLETED, duration=600)
        add_review(session, TaskStatus.COMPLETED, duration=60)
        add_review(session, TaskStatus.FAILED, duration=30)

    data = client.get("/metrics").json()
    eng = data["engineering"]
    assert data["failed"] == 1
    assert eng["fixes_failed"] == 0
    assert eng["reviews_failed"] == 1
    assert eng["runs_failed"] == 1
    assert eng["runs_finished"] == 3
    assert eng["change_failure_rate_percent"] == 33.33


def test_metrics_include_engineering_kpis(client):
    with db_session() as session:
        add_task(
            session,
            TaskStatus.COMPLETED,
            duration=3600,
            acus_consumed=1.0,
            pr_url="https://github.com/jan21deepak/superset/pull/3",
            pr_state="merged",
        )
        add_task(session, TaskStatus.FAILED, duration=100)
        add_review(session, TaskStatus.COMPLETED, duration=600, merged=True)
    data = client.get("/metrics").json()
    eng = data["engineering"]
    assert eng["merge_rate_percent"] == 100.0
    assert eng["prs_merged"] == 1
    assert eng["reviews_merged"] == 1
    assert eng["reviews_completed"] == 1
    # 1 failed fix out of 3 finished runs (2 fixes + 1 review)
    assert eng["change_failure_rate_percent"] == 33.33
    assert eng["fixes_failed"] == 1
    assert eng["fixes_finished"] == 2
    assert eng["runs_failed"] == 1
    assert eng["runs_finished"] == 3
    assert eng["pr_cycle_time_hours"] == 1.0
    assert eng["pr_cycle_time_sample"] == 1
    assert "lead_time_to_pr_hours" not in eng
    # PR opened just now by the completed fix falls inside the 7-day window
    assert eng["prs_last_7_days"] == 1
    assert eng["prs_total"] == 1
    assert data["productivity_gained_usd"] > 0


def test_prs_outside_seven_day_window_excluded(client):
    old = datetime.now(timezone.utc) - timedelta(days=30)
    with db_session() as session:
        task = Task(
            repository="jan21deepak/superset",
            issue_number=9,
            issue_title="old fix",
            cursor_agent_id="session-old",
            status=TaskStatus.COMPLETED,
            duration_seconds=1200,
            pull_request_url="https://github.com/jan21deepak/superset/pull/9",
            created_at=old,
            completed_at=old,
        )
        session.add(task)
    eng = client.get("/metrics").json()["engineering"]
    assert eng["prs_total"] == 1
    assert eng["prs_last_7_days"] == 0


def test_prs_counted_for_still_running_sessions(client):
    """Cursor often leaves a session running (waiting_for_user) after opening a
    PR. The PR is still delivered work and must be counted."""
    with db_session() as session:
        add_task(
            session,
            TaskStatus.RUNNING,
            pr_url="https://github.com/jan21deepak/superset/pull/10",
        )
        add_task(
            session,
            TaskStatus.COMPLETED,
            duration=600,
            pr_url="https://github.com/jan21deepak/superset/pull/11",
        )
    eng = client.get("/metrics").json()["engineering"]
    assert eng["prs_total"] == 2
    assert eng["prs_last_7_days"] == 2


def test_merge_rate_counts_unreviewed_prs(client):
    """An unmerged PR must drag the merge rate down even when no Cursor Review
    row exists for it — otherwise the rate reads 100% while PRs sit open."""
    with db_session() as session:
        add_task(
            session,
            TaskStatus.COMPLETED,
            duration=600,
            pr_url="https://github.com/jan21deepak/superset/pull/3",
            pr_state="merged",
        )
        add_task(
            session,
            TaskStatus.COMPLETED,
            duration=700,
            pr_url="https://github.com/jan21deepak/omnigent/pull/7",
            pr_state="open",
        )
        # A completed, merged review exists only for the first PR.
        add_review(session, TaskStatus.COMPLETED, duration=60, merged=True)
    eng = client.get("/metrics").json()["engineering"]
    assert eng["prs_total"] == 2
    assert eng["prs_merged"] == 1
    assert eng["prs_open"] == 1
    assert eng["merge_rate_percent"] == 50.0


def test_closed_unmerged_pr_counts_against_merge_rate(client):
    with db_session() as session:
        add_task(
            session,
            TaskStatus.COMPLETED,
            duration=600,
            pr_url="https://github.com/jan21deepak/superset/pull/3",
            pr_state="merged",
        )
        add_task(
            session,
            TaskStatus.COMPLETED,
            duration=700,
            pr_url="https://github.com/jan21deepak/superset/pull/4",
            pr_state="closed",
        )
    eng = client.get("/metrics").json()["engineering"]
    assert eng["prs_closed"] == 1
    assert eng["prs_open"] == 0
    assert eng["merge_rate_percent"] == 50.0


def test_prs_deduplicated_across_tasks(client):
    with db_session() as session:
        add_task(
            session,
            TaskStatus.FAILED,
            duration=100,
            pr_url="https://github.com/jan21deepak/superset/pull/12",
        )
        add_task(
            session,
            TaskStatus.COMPLETED,
            duration=200,
            pr_url="https://github.com/jan21deepak/superset/pull/12",
        )
    eng = client.get("/metrics").json()["engineering"]
    assert eng["prs_total"] == 1


def test_timestamps_are_rendered_in_sgt(client):
    # 2026-08-02 18:30 UTC is 2026-08-03 02:30 SGT — a different calendar day.
    stamp = datetime(2026, 8, 2, 18, 30, tzinfo=timezone.utc)
    with db_session() as session:
        session.add(
            Task(
                repository="jan21deepak/superset",
                issue_number=1,
                issue_title="t",
                cursor_agent_id="sgt-session",
                status=TaskStatus.COMPLETED,
                duration_seconds=600,
                created_at=stamp,
                completed_at=stamp,
            )
        )
    task = client.get("/api/tasks").json()["tasks"][0]
    assert task["completed_at"] == "2026-08-03T02:30:00+08:00"
    assert task["completed_at_display"] == "03 Aug 2026 02:30 SGT"
    assert "SGT" in client.get("/dashboard").text


def test_daily_activity_buckets_by_sgt_day(client):
    stamp = datetime(2026, 8, 2, 18, 30, tzinfo=timezone.utc)
    with db_session() as session:
        session.add(
            Task(
                repository="jan21deepak/superset",
                issue_number=1,
                issue_title="t",
                cursor_agent_id="daily-session",
                status=TaskStatus.COMPLETED,
                duration_seconds=600,
                pull_request_url="https://github.com/jan21deepak/superset/pull/20",
                created_at=stamp,
                completed_at=stamp,
            )
        )
    daily = client.get("/metrics").json()["daily"]
    assert len(daily) == 14
    assert daily[-1]["date"] == now_sgt().date().isoformat()
    by_date = {d["date"]: d for d in daily}
    # Counted on the SGT day (3 Aug), not the UTC day (2 Aug).
    if "2026-08-03" in by_date:
        assert by_date["2026-08-03"]["fixes_completed"] == 1
        assert by_date["2026-08-03"]["prs_opened"] == 1
        assert by_date["2026-08-03"]["runtime_minutes"] == 10.0
        assert by_date.get("2026-08-02", {}).get("fixes_completed", 0) == 0


def test_parse_repository_ref():
    assert parse_repository_ref("https://github.com/jan21deepak/superset") == "jan21deepak/superset"
    assert parse_repository_ref("jan21deepak/superset") == "jan21deepak/superset"
    assert parse_repository_ref("https://github.com/jan21deepak/superset.git") == "jan21deepak/superset"
    assert parse_repository_ref("not a repo") is None


def test_add_repository_endpoint(client, monkeypatch):
    async def fake_get_repository(self, full_name):
        return {
            "full_name": full_name,
            "html_url": f"https://github.com/{full_name}",
            "description": "demo",
        }

    monkeypatch.setattr("app.github.GitHubClient.get_repository", fake_get_repository)
    resp = client.post("/api/repositories", json={"url": "https://github.com/jan21deepak/superset"})
    assert resp.status_code == 201
    assert resp.json()["repository"]["full_name"] == "jan21deepak/superset"

    listed = client.get("/api/repositories").json()
    assert len(listed["repositories"]) == 1


def test_add_issues_imports_five_from_parent_and_syncs(client, monkeypatch):
    with db_session() as session:
        repo = Repository(
            full_name="jan21deepak/superset",
            url="https://github.com/jan21deepak/superset",
        )
        session.add(repo)
        session.flush()
        repo_id = repo.id

    async def fake_get_repository(self, full_name):
        return {
            "full_name": full_name,
            "parent": {"full_name": "apache/superset"},
        }

    created_count = 0

    async def fake_create_issue(self, repository, title, body="", labels=None):
        nonlocal created_count
        created_count += 1
        assert repository == "jan21deepak/superset"
        assert "Source issue: https://github.com/apache/superset/issues/" in body
        return {
            "number": created_count + 10,
            "title": title,
            "body": body,
            "state": "open",
            "labels": [],
            "html_url": f"https://github.com/{repository}/issues/{created_count + 10}",
        }

    async def fake_list_open_issues(self, full_name, per_page=50, max_pages=5):
        if full_name == "apache/superset":
            return [
                {
                    "number": number,
                    "title": f"Parent issue {number}",
                    "body": "Acceptance criteria",
                    "state": "open",
                    "labels": [],
                    "html_url": f"https://github.com/apache/superset/issues/{number}",
                }
                for number in range(101, 107)
            ]
        if created_count:
            return [
                {
                    "number": number + 10,
                    "title": f"Parent issue {number + 100}",
                    "body": (
                        "Acceptance criteria\n\n"
                        f"Source issue: https://github.com/apache/superset/issues/{number + 100}"
                    ),
                    "state": "open",
                    "labels": [],
                    "html_url": f"https://github.com/{full_name}/issues/{number + 10}",
                }
                for number in range(1, created_count + 1)
            ]
        return []

    async def fake_list_issues(
        self, full_name, state="open", per_page=50, max_pages=5
    ):
        return await fake_list_open_issues(self, full_name, per_page, max_pages)

    monkeypatch.setattr("app.github.GitHubClient.get_repository", fake_get_repository)
    monkeypatch.setattr("app.github.GitHubClient.create_issue", fake_create_issue)
    monkeypatch.setattr("app.github.GitHubClient.list_issues", fake_list_issues)
    monkeypatch.setattr("app.github.GitHubClient.list_open_issues", fake_list_open_issues)

    response = client.post(f"/api/repositories/{repo_id}/issues")
    assert response.status_code == 200
    assert response.json()["parent"] == "apache/superset"
    assert len(response.json()["created"]) == 5
    assert response.json()["synced_count"] == 5

    grouped = client.get("/api/issues").json()["repositories"]
    assert len(grouped[0]["issues"]) == 5


def test_list_and_assign_pulls_to_cursor_review(client, monkeypatch):
    with db_session() as session:
        repo = Repository(
            full_name="jan21deepak/superset",
            url="https://github.com/jan21deepak/superset",
        )
        session.add(repo)

    async def fake_list_open_pull_requests(self, full_name, per_page=50, max_pages=5):
        return [
            {
                "number": 3,
                "title": "Fix null metadata",
                "body": "Returns empty columns",
                "html_url": f"https://github.com/{full_name}/pull/3",
                "draft": False,
                "state": "open",
                "user": {"login": "cursor-bot"},
                "head": {"sha": "abc123"},
            }
        ]

    async def fake_create_review_agent(self, **kwargs):
        return {
            "agent_id": "bc-review",
            "run_id": "run-review",
            "url": "https://cursor.com/agents/bc-review",
        }

    monkeypatch.setattr(
        "app.github.GitHubClient.list_open_pull_requests", fake_list_open_pull_requests
    )
    monkeypatch.setattr(
        "app.cursor_client.CursorClient.create_review_agent", fake_create_review_agent
    )
    monkeypatch.setenv("CURSOR_API_KEY", "cursor_test")
    get_settings.cache_clear()

    listed = client.get("/api/pulls").json()
    assert len(listed["repositories"]) == 1
    assert listed["repositories"][0]["pulls"][0]["pr_number"] == 3
    pull_id = listed["repositories"][0]["pulls"][0]["id"]

    assigned = client.post("/api/pulls/assign", json={"pull_ids": [pull_id]})
    assert assigned.status_code == 200
    body = assigned.json()
    assert body["accepted"] == 1
    assert body["results"][0]["ok"] is True

    with db_session() as session:
        reviews = session.query(ReviewTask).all()
        assert len(reviews) == 1
        assert reviews[0].pr_number == 3
        assert reviews[0].status == TaskStatus.RUNNING
