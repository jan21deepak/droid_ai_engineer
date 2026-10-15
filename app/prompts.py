"""Agent prompts for implementation, follow-up and recovery turns.

Agent prompts describe a local candidate only. Publishing is an app-owned step
that requires checks, a restricted review and operator approval of the exact
commit, so no prompt grants or implies publication authority.
"""

from __future__ import annotations

import re

FIX_PROMPT_TEMPLATE = """You are working in a local clone of the repository: {repository_url}
(checkout of ref "{starting_ref}"). Your result is a local candidate commit; you have no
credentials to publish it, and the application decides separately whether it is published.

Resolve the following GitHub issue (#{issue_number}):

Title: {issue_title}

Description:
{issue_body}

Requirements:
- Make only the requested changes described in the issue.
{branch_instruction}
- Run the project's test suite (or the closest relevant subset if the full suite is impractical) and fix any failures introduced by your changes.
- Commit all of your work on that branch and leave the working tree clean.
- Do NOT push, do NOT open a pull request, and do NOT change git remotes or credentials.
- End your final response with a line of the exact form `BRANCH: {branch_line}` followed by a short summary of the completed work.
"""

NEW_BRANCH_INSTRUCTION = "- Create a new git branch for your work."
# Used when the checked-out ref is itself an approved publication branch, so the
# candidate stays on it and publication needs no additional branch.
CURRENT_BRANCH_INSTRUCTION = (
    "- Work and commit directly on the checked-out branch `{branch}`; do not create or switch to another branch."
)

FOLLOW_UP_PROMPT_TEMPLATE = """Continue work on this agent session.

Follow-up instruction:
{instruction}

Requirements:
- Keep working on the existing local branch.
- Run relevant tests after changes.
- Commit all of your changes on the branch and leave the working tree clean. Do NOT push or open a pull request.
- End your final response with a line of the exact form `BRANCH: <your-branch-name>` and summarize what you changed.
"""

RECOVERY_PROMPT_TEMPLATE = """Your previous turn in this session was interrupted, and the operator asked
for it to be resumed. Continue the task from where you left off.

- If the work is already complete, restate your final summary.
- Make sure all of your work is committed on your local branch. Do NOT push or open a pull request.
- End your final response with a line of the exact form `BRANCH: <your-branch-name>` and a short summary.
"""

_BRANCH_RE = re.compile(r"^\s*BRANCH:\s*([A-Za-z0-9._/-]+)\s*$", re.MULTILINE)


def build_fix_prompt(
    repository_url: str,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    starting_ref: str,
    *,
    work_branch: str | None = None,
) -> str:
    """``work_branch`` keeps the agent on that checked-out branch instead of creating a new one."""
    return FIX_PROMPT_TEMPLATE.format(
        repository_url=repository_url,
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body or "(no description provided)",
        starting_ref=starting_ref or "the repository default branch",
        branch_instruction=CURRENT_BRANCH_INSTRUCTION.format(branch=work_branch)
        if work_branch
        else NEW_BRANCH_INSTRUCTION,
        branch_line=work_branch or "<your-branch-name>",
    )


def build_follow_up_prompt(instruction: str) -> str:
    return FOLLOW_UP_PROMPT_TEMPLATE.format(instruction=instruction.strip() or "Continue.")


def extract_branch_name(text: str | None) -> str | None:
    """Pull the agent-reported branch name from a turn result."""
    if not text:
        return None
    match = _BRANCH_RE.search(text)
    return match.group(1) if match else None


__all__ = [
    "FIX_PROMPT_TEMPLATE",
    "FOLLOW_UP_PROMPT_TEMPLATE",
    "RECOVERY_PROMPT_TEMPLATE",
    "build_fix_prompt",
    "build_follow_up_prompt",
    "extract_branch_name",
]
