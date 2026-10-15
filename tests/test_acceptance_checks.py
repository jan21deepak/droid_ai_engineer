"""App-run acceptance checks execute in a clean checkout of the exact candidate SHA."""

import json

import pytest

from app import acceptance_checks
from app.acceptance_checks import recover_interrupted_check_runs, start_check_run
from app.config import get_settings
from app.database import db_session
from app.delivery import DeliveryRefusedError, task_delivery
from app.delivery_git import delivery_dir
from app.delivery_models import CheckResult, CheckRun, CheckStatus
from tests.delivery_fixtures import REPO, add_candidate_task, configure

FAKE_TOKEN = "ghp_" + "Z" * 36


def checks(*specs):
    return json.dumps([{"name": name, "argv": ["python3", "-c", code], **extra} for name, code, extra in specs])


async def run_checks(task_id: int) -> dict:
    started = await start_check_run(task_id)
    await acceptance_checks._running[task_id]
    return started


def latest(task_id: int) -> dict:
    return task_delivery(task_id)["checks"]


class TestExecution:
    @pytest.mark.asyncio
    async def test_runs_in_clean_checkout_of_exact_sha(self, monkeypatch, tmp_path):
        probe = (
            "import os, subprocess, sys;"
            "head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip();"
            "print('HEAD', head);"
            "print('CWD', os.getcwd());"
            "sys.exit(0 if open('fix.txt').read() == 'fix\\n' and not os.path.exists('scratch.txt') else 4)"
        )
        remote = configure(monkeypatch, tmp_path, checks=checks(("probe", probe, {})))
        task_id, sha, ws = add_candidate_task(remote)
        # Uncommitted agent leftovers must not reach the evaluated tree.
        (ws / "scratch.txt").write_text("untracked\n")

        started = await run_checks(task_id)

        assert started["candidate_sha"] == sha and started["checks"] == ["probe"]
        summary = latest(task_id)
        run = summary["latest_run"]
        assert (summary["status"], run["status"], run["evaluated_sha"], run["candidate_sha"]) == (
            "passed",
            "passed",
            sha,
            sha,
        )
        [result] = run["results"]
        assert (result["status"], result["exit_code"]) == ("passed", 0)
        assert f"HEAD {sha}" in result["output_tail"]
        assert str(ws) not in result["output_tail"]
        assert result["started_at"] and result["finished_at"] and result["duration_seconds"] is not None
        assert run["executed_by"] == "app"
        # The temporary checkout is removed afterwards; the agent workspace is untouched.
        assert not delivery_dir(REPO, f"checks-{run['id']}").exists()
        assert (ws / "scratch.txt").exists()

    @pytest.mark.asyncio
    async def test_failing_check_fails_the_run_and_blocks_approval(self, monkeypatch, tmp_path):
        remote = configure(
            monkeypatch,
            tmp_path,
            checks=checks(("ok", "print('fine')", {}), ("bad", "import sys; print('boom'); sys.exit(3)", {})),
        )
        task_id, _sha, _ws = add_candidate_task(remote)

        await run_checks(task_id)

        summary = latest(task_id)
        assert summary["status"] == "failed"
        assert [(r["name"], r["status"], r["exit_code"]) for r in summary["latest_run"]["results"]] == [
            ("ok", "passed", 0),
            ("bad", "failed", 3),
        ]
        assert "boom" in summary["latest_run"]["results"][1]["output_tail"]
        blockers = [b["code"] for b in task_delivery(task_id)["readiness"]["approval_blockers"]]
        assert "checks_failed" in blockers

    @pytest.mark.asyncio
    async def test_timeout_kills_the_check(self, monkeypatch, tmp_path):
        remote = configure(
            monkeypatch, tmp_path, checks=checks(("slow", "import time; time.sleep(30)", {"timeout_seconds": 0.5}))
        )
        task_id, _sha, _ws = add_candidate_task(remote)

        await run_checks(task_id)

        [result] = latest(task_id)["latest_run"]["results"]
        assert (result["status"], result["exit_code"]) == ("timed_out", None)
        assert "timeout" in result["output_tail"]
        assert latest(task_id)["status"] == "failed"

    @pytest.mark.asyncio
    async def test_check_environment_has_no_credentials_and_output_is_redacted(self, monkeypatch, tmp_path):
        code = "import os; print('token-present' if os.environ.get('GITHUB_TOKEN') else 'no-token'); print(os.environ.get('LEAK', ''))"
        remote = configure(monkeypatch, tmp_path, checks=checks(("env", code, {})))
        monkeypatch.setenv("GITHUB_TOKEN", FAKE_TOKEN)
        monkeypatch.setenv("LEAK", FAKE_TOKEN)
        get_settings.cache_clear()
        task_id, _sha, _ws = add_candidate_task(remote)

        await run_checks(task_id)

        [result] = latest(task_id)["latest_run"]["results"]
        assert "no-token" in result["output_tail"]
        with db_session() as session:
            stored = session.query(CheckResult).one().output
        assert FAKE_TOKEN not in stored and FAKE_TOKEN not in json.dumps(task_delivery(task_id))

    @pytest.mark.asyncio
    async def test_missing_command_is_an_error_not_a_pass(self, monkeypatch, tmp_path):
        spec = json.dumps([{"name": "missing", "argv": ["forge-no-such-command-xyz"]}])
        remote = configure(monkeypatch, tmp_path, checks=spec)
        task_id, _sha, _ws = add_candidate_task(remote)

        await run_checks(task_id)

        [result] = latest(task_id)["latest_run"]["results"]
        assert result["status"] == "error"
        assert latest(task_id)["status"] == "failed"


class TestRefusals:
    @pytest.mark.asyncio
    async def test_not_configured(self, monkeypatch, tmp_path):
        remote = configure(monkeypatch, tmp_path, checks="")
        task_id, _sha, _ws = add_candidate_task(remote)
        with pytest.raises(DeliveryRefusedError) as info:
            await start_check_run(task_id)
        assert info.value.detail == "acceptance_checks_not_configured"
        with db_session() as session:
            assert session.query(CheckRun).count() == 0

    @pytest.mark.asyncio
    async def test_invalid_configuration(self, monkeypatch, tmp_path):
        remote = configure(monkeypatch, tmp_path, checks='[{"name": "x"}]')
        task_id, _sha, _ws = add_candidate_task(remote)
        with pytest.raises(DeliveryRefusedError) as info:
            await start_check_run(task_id)
        assert info.value.detail == "acceptance_checks_invalid"

    @pytest.mark.asyncio
    async def test_dirty_candidate_is_not_checked(self, monkeypatch, tmp_path):
        from app.candidates import WorkspaceState, record_candidate

        remote = configure(monkeypatch, tmp_path)
        task_id, sha, _ws = add_candidate_task(remote)
        record_candidate(task_id, WorkspaceState(head_sha=sha, branch="droid_mission", dirty=True))
        with pytest.raises(DeliveryRefusedError) as info:
            await start_check_run(task_id)
        assert info.value.detail == "candidate_not_ready"

    @pytest.mark.asyncio
    async def test_one_run_at_a_time(self, monkeypatch, tmp_path):
        remote = configure(monkeypatch, tmp_path)
        task_id, sha, _ws = add_candidate_task(remote)
        with db_session() as session:
            session.add(CheckRun(task_id=task_id, candidate_sha=sha, status=CheckStatus.RUNNING))
        with pytest.raises(DeliveryRefusedError) as info:
            await start_check_run(task_id)
        assert info.value.detail == "checks_already_running"


def test_restart_marks_in_flight_runs_interrupted(monkeypatch, tmp_path):
    remote = configure(monkeypatch, tmp_path)
    task_id, sha, _ws = add_candidate_task(remote)
    with db_session() as session:
        run = CheckRun(task_id=task_id, candidate_sha=sha, status=CheckStatus.RUNNING)
        session.add(run)
        session.flush()
        session.add(CheckResult(run_id=run.id, position=0, name="unit", status=CheckStatus.RUNNING))

    assert recover_interrupted_check_runs() == 1
    assert recover_interrupted_check_runs() == 0

    summary = latest(task_id)
    assert (summary["status"], summary["latest_run"]["status"]) == ("incomplete", "interrupted")
    assert summary["latest_run"]["results"][0]["status"] == "interrupted"
