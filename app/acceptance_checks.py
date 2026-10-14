"""App-owned local acceptance checks for a task's exact candidate commit.

Checks run in a fresh clone of the task workspace checked out at the
candidate SHA, never in the agent's workspace, with the clean agent
environment (no GitHub or operator credentials). The SHA the checkout
actually resolved to is recorded as the evaluated SHA; results only count
for that commit. These checks are separate from the read-only reviewer,
which never runs commands, and from GitHub CI, which can only run after
publication.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.agent_env import agent_environment
from app.check_config import CheckSpec, configured_checks
from app.database import db_session
from app.delivery import DeliveryRefusedError, candidate_refusal, delivery_reservation
from app.delivery_git import checkout_candidate, delivery_dir, remove_checkout
from app.delivery_models import CheckResult, CheckRun, CheckStatus, bounded_output
from app.droid_client import task_workspace_dir
from app.logging_conf import log_event
from app.models import Task
from app.redaction import safe_error
from app.task_reservations import Purpose

logger = logging.getLogger("app.checks")

# One in-flight check run per task, kept so shutdown can cancel it.
_running: dict[int, asyncio.Task[None]] = {}

RESTART_INTERRUPTED = "the service restarted while these checks were running; run them again"


def _now() -> datetime:
    return datetime.now(UTC)


async def start_check_run(task_id: int) -> dict[str, Any]:
    """Queue every configured check against the task's current candidate and run them in the background."""
    config = configured_checks()
    if config.error:
        raise DeliveryRefusedError(409, "acceptance_checks_invalid", reason=config.error)
    if not config.checks:
        raise DeliveryRefusedError(
            409, "acceptance_checks_not_configured", reason="FORGE_ACCEPTANCE_CHECKS lists no checks"
        )
    with delivery_reservation(task_id, Purpose.CHECKS), db_session() as session:
        task = session.get(Task, task_id)
        if task is None:
            raise DeliveryRefusedError(404, "task not found")
        refusal = candidate_refusal(task)
        if refusal is not None:
            raise refusal
        active = (
            session.query(CheckRun).filter(CheckRun.task_id == task_id, CheckRun.status.in_(CheckStatus.ACTIVE)).first()
        )
        if active is not None:
            raise DeliveryRefusedError(409, "checks_already_running", check_run_id=active.id)
        run = CheckRun(
            task_id=task_id,
            candidate_sha=task.candidate_sha,
            status=CheckStatus.QUEUED,
            # The specs below are exactly what execute_check_run runs, so results bind to them.
            configuration=json.dumps([spec.identity() for spec in config.checks]),
        )
        session.add(run)
        session.flush()
        for position, spec in enumerate(config.checks):
            session.add(
                CheckResult(
                    run_id=run.id,
                    position=position,
                    name=spec.name,
                    command=spec.display_command,
                    status=CheckStatus.QUEUED,
                )
            )
        run_id, sha = run.id, run.candidate_sha
    background = asyncio.create_task(execute_check_run(run_id, config.checks))
    _running[task_id] = background
    background.add_done_callback(lambda _t: _running.pop(task_id, None) if _running.get(task_id) is _t else None)
    log_event(logger, logging.INFO, "checks.started", task_id=task_id, check_run_id=run_id, sha=sha)
    return {"check_run_id": run_id, "candidate_sha": sha, "started": True, "checks": config.names}


def _update_run(run_id: int, **fields: Any) -> None:
    with db_session() as session:
        run = session.get(CheckRun, run_id)
        if run is not None:
            for key, value in fields.items():
                setattr(run, key, value)


def _update_result(run_id: int, position: int, **fields: Any) -> None:
    with db_session() as session:
        result = (
            session.query(CheckResult).filter(CheckResult.run_id == run_id, CheckResult.position == position).first()
        )
        if result is not None:
            for key, value in fields.items():
                setattr(result, key, value)


def _close_pending_results(run_id: int, status: str) -> None:
    with db_session() as session:
        for result in session.query(CheckResult).filter(
            CheckResult.run_id == run_id, CheckResult.status.in_(CheckStatus.ACTIVE)
        ):
            result.status = status
            result.finished_at = result.finished_at or (_now() if status != CheckStatus.SKIPPED else None)


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


async def run_check(spec: CheckSpec, cwd: str) -> tuple[str, int | None, str]:
    """Run one check; returns ``(status, exit_code, output)``. The whole process group is killed on timeout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *spec.argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            env=agent_environment(),
            start_new_session=True,
        )
    except OSError as exc:
        return CheckStatus.ERROR, None, f"check could not start: {safe_error(exc)}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=spec.timeout_seconds)
    except TimeoutError:
        _kill_group(proc)
        await proc.wait()
        return CheckStatus.TIMED_OUT, None, f"check exceeded its {spec.timeout_seconds:g}s timeout"
    except asyncio.CancelledError:
        _kill_group(proc)
        raise
    text = (out or b"").decode(errors="replace")
    status = CheckStatus.PASSED if proc.returncode == 0 else CheckStatus.FAILED
    return status, proc.returncode, text


async def execute_check_run(run_id: int, specs: tuple[CheckSpec, ...]) -> None:
    with db_session() as session:
        run = session.get(CheckRun, run_id)
        task = session.get(Task, run.task_id) if run else None
        if run is None or task is None or run.status != CheckStatus.QUEUED:
            return
        run.status, run.started_at = CheckStatus.RUNNING, _now()
        repository, task_id, sha = task.repository, task.id, run.candidate_sha
    checkout = delivery_dir(repository, f"checks-{run_id}")
    try:
        await _execute(run_id, specs, repository, task_id, sha, checkout)
    except asyncio.CancelledError:
        _close_pending_results(run_id, CheckStatus.INTERRUPTED)
        _update_run(run_id, status=CheckStatus.INTERRUPTED, finished_at=_now(), error="check run was cancelled")
        raise
    except Exception as exc:
        _close_pending_results(run_id, CheckStatus.SKIPPED)
        _update_run(run_id, status=CheckStatus.ERROR, finished_at=_now(), error=safe_error(exc))
        log_event(logger, logging.WARNING, "checks.error", check_run_id=run_id, error=str(exc))
    finally:
        remove_checkout(checkout)


async def _execute(
    run_id: int, specs: tuple[CheckSpec, ...], repository: str, task_id: int, sha: str, checkout: Path
) -> None:
    evaluated = await checkout_candidate(task_workspace_dir(repository, task_id), sha, checkout)
    _update_run(run_id, evaluated_sha=evaluated)
    if evaluated != sha:
        raise RuntimeError(f"the check checkout resolved to {evaluated[:12]}, not candidate {sha[:12]}")
    statuses: list[str] = []
    for position, spec in enumerate(specs):
        started = time.monotonic()
        _update_result(run_id, position, status=CheckStatus.RUNNING, started_at=_now())
        status, exit_code, output = await run_check(spec, str(checkout))
        statuses.append(status)
        _update_result(
            run_id,
            position,
            status=status,
            exit_code=exit_code,
            output=bounded_output(output),
            finished_at=_now(),
            duration_seconds=round(time.monotonic() - started, 3),
        )
    overall = CheckStatus.PASSED if all(s == CheckStatus.PASSED for s in statuses) else CheckStatus.FAILED
    _update_run(run_id, status=overall, finished_at=_now())
    log_event(logger, logging.INFO, "checks.finished", check_run_id=run_id, sha=sha, status=overall)


def recover_interrupted_check_runs() -> int:
    """At startup, mark runs left in flight by a restart as interrupted; they are never resumed."""
    with db_session() as session:
        runs = session.query(CheckRun).filter(CheckRun.status.in_(CheckStatus.ACTIVE)).all()
        ids = [r.id for r in runs]
        for run in runs:
            run.status, run.error, run.finished_at = CheckStatus.INTERRUPTED, RESTART_INTERRUPTED, _now()
    for run_id in ids:
        _close_pending_results(run_id, CheckStatus.INTERRUPTED)
    if ids:
        log_event(logger, logging.INFO, "checks.recovered", interrupted=len(ids))
    return len(ids)


async def shutdown_check_runs() -> None:
    pending = [t for t in _running.values() if not t.done()]
    for background in pending:
        background.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


__all__ = [
    "execute_check_run",
    "recover_interrupted_check_runs",
    "run_check",
    "shutdown_check_runs",
    "start_check_run",
]
