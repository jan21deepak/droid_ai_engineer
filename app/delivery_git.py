"""App-owned Git for delivery: clean candidate checkouts, workspace rechecks, push and remote head.

The agent's task workspace is never where checks or reviews run: each gets a
fresh clone of the workspace checked out (detached) at the exact candidate
SHA, so uncommitted or later edits in the workspace cannot leak into the
evidence. Pushes send that exact SHA (never ``HEAD``, never forced) to the
canonical remote with the app's transient credential, after the caller has
checked outward-action policy.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from app import droid_client
from app.droid_client import DroidRunError, _git, _git_or_none, get_droid_client, task_workspace_dir, workspace_root
from app.models import Task
from app.policy import OutwardAction, require_outward
from app.repos import parse_repository_ref
from app.review_policy import ReviewContext, assess_diff


def delivery_dir(repository: str, name: str) -> Path:
    full_name = parse_repository_ref(repository) or repository
    owner, _, repo = full_name.partition("/")
    return workspace_root() / owner / repo / name


async def checkout_candidate(source: Path, sha: str, dest: Path) -> str:
    """Clone ``source`` into ``dest`` and check out ``sha`` detached and clean; returns the resolved HEAD."""
    if not (source / ".git").exists():
        raise DroidRunError("task workspace is missing")
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    await _git(["clone", "-q", "--no-checkout", "--no-hardlinks", str(source), str(dest)])
    if await _git_or_none(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=dest) is None:
        raise DroidRunError(f"candidate commit {sha[:12]} is not in the task workspace")
    await _git(["checkout", "-q", "--force", "--detach", sha], cwd=dest)
    await _git(["clean", "-q", "-ffdx"], cwd=dest)
    await _git(["remote", "remove", "origin"], cwd=dest)
    return await _git(["rev-parse", "--verify", "HEAD^{commit}"], cwd=dest)


def remove_checkout(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


async def prepare_candidate_review_context(
    *, repository: str, task_id: int, review_id: int, base_sha: str, candidate_sha: str, base_label: str
) -> ReviewContext:
    """A clean checkout of the candidate plus the complete base..candidate diff for a restricted reviewer."""
    ws = droid_client.review_workspace_dir(repository, review_id)
    head = await checkout_candidate(task_workspace_dir(repository, task_id), candidate_sha, ws)
    diff = None
    if base_sha and head and base_sha != head:
        diff = await _git_or_none(
            ["diff", "--no-color", "--no-ext-diff", "--no-textconv", "--find-renames", base_sha, head], cwd=ws
        )
    return ReviewContext(
        workspace=ws,
        base_ref=base_label,
        base_sha=base_sha,
        head_sha=head or None,
        diff=diff or "",
        incomplete_reason=assess_diff(base_sha, head, diff),
    )


@dataclass(frozen=True)
class WorkspaceRecheck:
    head_sha: str | None
    dirty: bool
    # None when the workspace still holds exactly the clean candidate.
    code: str | None
    problem: str | None

    @property
    def ok(self) -> bool:
        return self.code is None


async def recheck_candidate_workspace(task: Task) -> WorkspaceRecheck:
    """Re-inspect the task workspace now, rather than trusting what was recorded earlier.

    This reads the actual HEAD, never the candidate branch tip: a workspace moved
    or detached elsewhere is stale even when the branch still points at the
    candidate. A clean HEAD detached exactly at the candidate is fine.
    """
    state = await get_droid_client().inspect_workspace(task.repository, task.id, None)
    code = problem = None
    if not state.head_sha:
        code, problem = "workspace_unavailable", "the task workspace could not be inspected"
    elif state.head_sha != task.candidate_sha:
        code = "stale_candidate"
        problem = (
            f"the workspace now holds {state.head_sha[:12]}, not candidate {(task.candidate_sha or '')[:12]}; "
            "a new candidate needs its own checks, review and approval"
        )
    elif state.dirty:
        code, problem = "dirty_workspace", "the task workspace has uncommitted changes"
    return WorkspaceRecheck(head_sha=state.head_sha, dirty=state.dirty, code=code, problem=problem)


async def remote_branch_head(repository: str, branch: str) -> str | None:
    """The commit ``branch`` points at on the canonical remote, or ``None`` if the branch does not exist."""
    remote = droid_client._remote_url(repository)
    ref = f"refs/heads/{branch}"
    out = await _git(["ls-remote", "--heads", remote, ref], credential_url=remote)
    for line in out.splitlines():
        sha, _, name = line.partition("\t")
        if name.strip() == ref:
            return sha.strip()
    return None


async def push_candidate(repository: str, task_id: int, sha: str, branch: str) -> None:
    """Fast-forward ``branch`` on the canonical remote to exactly ``sha``. Raises on rejection."""
    require_outward(OutwardAction.BRANCH_PUSH, repository, ref=branch)
    remote = droid_client._remote_url(repository)
    await _git(
        ["push", "--porcelain", remote, f"{sha}:refs/heads/{branch}"],
        cwd=task_workspace_dir(repository, task_id),
        credential_url=remote,
    )


__all__ = [
    "WorkspaceRecheck",
    "checkout_candidate",
    "delivery_dir",
    "prepare_candidate_review_context",
    "push_candidate",
    "recheck_candidate_workspace",
    "remote_branch_head",
    "remove_checkout",
]
