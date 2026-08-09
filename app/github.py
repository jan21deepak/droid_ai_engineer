"""GitHub webhook verification, payload parsing and REST API client."""

import hashlib
import hmac
import logging
from dataclasses import dataclass, field

import httpx

from app.config import get_settings
from app.logging_conf import log_event

logger = logging.getLogger("app.github")


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Validate the X-Hub-Signature-256 header using HMAC SHA-256."""
    if not secret:
        log_event(logger, logging.WARNING, "webhook.signature_skipped", reason="no secret configured")
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


@dataclass
class IssueEvent:
    action: str
    repository: str
    repository_url: str
    issue_number: int
    issue_title: str
    issue_body: str
    labels: list[str] = field(default_factory=list)
    label_added: str | None = None


def parse_issue_event(payload: dict) -> IssueEvent:
    """Extract the relevant fields from a GitHub `issues` webhook payload."""
    repo = payload.get("repository") or {}
    issue = payload.get("issue") or {}
    return IssueEvent(
        action=payload.get("action", ""),
        repository=repo.get("full_name", ""),
        repository_url=repo.get("html_url", ""),
        issue_number=int(issue.get("number", 0)),
        issue_title=issue.get("title", "") or "",
        issue_body=issue.get("body", "") or "",
        labels=[lbl.get("name", "") for lbl in issue.get("labels", []) or []],
        label_added=(payload.get("label") or {}).get("name"),
    )


def should_trigger(event: IssueEvent, trigger_label: str) -> bool:
    """Trigger on `opened` with the label present, or on the label being added."""
    if not event.issue_number:
        return False
    if event.action == "opened" and trigger_label in event.labels:
        return True
    if event.action == "labeled" and event.label_added == trigger_label:
        return True
    return False


class GitHubClient:
    """Minimal async GitHub REST API client."""

    def __init__(self, token: str | None = None, api_url: str | None = None):
        settings = get_settings()
        self.token = token if token is not None else settings.github_token
        self.api_url = (api_url or settings.github_api_url).rstrip("/")

    def _headers(self) -> dict:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def post_issue_comment(self, repository: str, issue_number: int, body: str) -> bool:
        if not self.token:
            log_event(logger, logging.WARNING, "github.comment_skipped", reason="no token configured",
                      repo=repository, issue=issue_number)
            return False
        url = f"{self.api_url}/repos/{repository}/issues/{issue_number}/comments"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, headers=self._headers(), json={"body": body})
                resp.raise_for_status()
            log_event(logger, logging.INFO, "github.comment_posted", repo=repository, issue=issue_number)
            return True
        except httpx.HTTPError as exc:
            log_event(logger, logging.ERROR, "github.comment_failed", repo=repository,
                      issue=issue_number, error=str(exc))
            return False

    async def get_repository(self, full_name: str) -> dict:
        """Fetch repository metadata. Raises httpx.HTTPStatusError on failure."""
        url = f"{self.api_url}/repos/{full_name}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    async def list_issues(
        self,
        full_name: str,
        state: str = "open",
        per_page: int = 50,
        max_pages: int = 5,
    ) -> list[dict]:
        """List issues (excluding PRs) for a repository."""
        issues: list[dict] = []
        async with httpx.AsyncClient(timeout=30) as client:
            for page in range(1, max_pages + 1):
                resp = await client.get(
                    f"{self.api_url}/repos/{full_name}/issues",
                    headers=self._headers(),
                    params={"state": state, "per_page": per_page, "page": page},
                )
                resp.raise_for_status()
                batch = resp.json()
                if not batch:
                    break
                for item in batch:
                    if "pull_request" in item:
                        continue
                    issues.append(item)
                if len(batch) < per_page:
                    break
        return issues

    async def list_open_issues(
        self, full_name: str, per_page: int = 50, max_pages: int = 5
    ) -> list[dict]:
        return await self.list_issues(full_name, "open", per_page, max_pages)

    async def create_issue(
        self,
        repository: str,
        title: str,
        body: str = "",
        labels: list[str] | None = None,
    ) -> dict:
        """Create a GitHub issue and return its API response."""
        if not self.token:
            raise RuntimeError("GitHub token is not configured")
        payload: dict = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{self.api_url}/repos/{repository}/issues",
                headers=self._headers(),
                json=payload,
            )
            resp.raise_for_status()
            issue = resp.json()
        log_event(
            logger,
            logging.INFO,
            "github.issue_created",
            repo=repository,
            issue=issue.get("number"),
        )
        return issue

    async def add_issue_label(self, repository: str, issue_number: int, label: str) -> bool:
        if not self.token:
            return False
        url = f"{self.api_url}/repos/{repository}/issues/{issue_number}/labels"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, headers=self._headers(), json={"labels": [label]})
                resp.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            log_event(logger, logging.WARNING, "github.label_failed", repo=repository,
                      issue=issue_number, label=label, error=str(exc))
            return False

    async def list_open_pull_requests(
        self, full_name: str, per_page: int = 50, max_pages: int = 5
    ) -> list[dict]:
        """List open, non-draft pull requests for a repository."""
        pulls: list[dict] = []
        async with httpx.AsyncClient(timeout=30) as client:
            for page in range(1, max_pages + 1):
                resp = await client.get(
                    f"{self.api_url}/repos/{full_name}/pulls",
                    headers=self._headers(),
                    params={"state": "open", "per_page": per_page, "page": page},
                )
                resp.raise_for_status()
                batch = resp.json()
                if not batch:
                    break
                for item in batch:
                    if item.get("draft"):
                        continue
                    pulls.append(item)
                if len(batch) < per_page:
                    break
        return pulls

    async def get_pull_request(self, repository: str, pr_number: int) -> dict:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.api_url}/repos/{repository}/pulls/{pr_number}",
                headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json()

    async def list_pull_request_reviews(self, repository: str, pr_number: int) -> list[dict]:
        """Return submitted reviews on a pull request (paginated)."""
        reviews: list[dict] = []
        page = 1
        per_page = 100
        async with httpx.AsyncClient(timeout=20) as client:
            while True:
                resp = await client.get(
                    f"{self.api_url}/repos/{repository}/pulls/{pr_number}/reviews",
                    headers=self._headers(),
                    params={"per_page": per_page, "page": page},
                )
                resp.raise_for_status()
                batch = resp.json() or []
                reviews.extend(batch)
                if len(batch) < per_page:
                    break
                page += 1
        return reviews

    async def merge_pull_request(
        self,
        repository: str,
        pr_number: int,
        merge_method: str = "squash",
        commit_title: str | None = None,
    ) -> dict:
        """Merge a pull request via the GitHub REST API."""
        if not self.token:
            raise RuntimeError("GitHub token is not configured")
        payload: dict = {"merge_method": merge_method}
        if commit_title:
            payload["commit_title"] = commit_title
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.put(
                f"{self.api_url}/repos/{repository}/pulls/{pr_number}/merge",
                headers=self._headers(),
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        log_event(
            logger,
            logging.INFO,
            "github.pr_merged",
            repo=repository,
            pr=pr_number,
            sha=data.get("sha"),
        )
        return data

    async def enable_auto_merge(
        self, repository: str, pr_number: int, merge_method: str = "SQUASH"
    ) -> bool:
        """Enable GitHub auto-merge via GraphQL. Returns True on success."""
        if not self.token:
            return False
        try:
            pr = await self.get_pull_request(repository, pr_number)
        except httpx.HTTPError as exc:
            log_event(
                logger,
                logging.WARNING,
                "github.auto_merge_failed",
                repo=repository,
                pr=pr_number,
                error=str(exc),
            )
            return False

        node_id = pr.get("node_id")
        if not node_id:
            return False

        query = """
        mutation($pullRequestId: ID!, $mergeMethod: PullRequestMergeMethod!) {
          enablePullRequestAutoMerge(input: {
            pullRequestId: $pullRequestId,
            mergeMethod: $mergeMethod
          }) {
            pullRequest { number autoMergeRequest { enabledAt } }
          }
        }
        """
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    "https://api.github.com/graphql",
                    headers=self._headers(),
                    json={
                        "query": query,
                        "variables": {
                            "pullRequestId": node_id,
                            "mergeMethod": merge_method,
                        },
                    },
                )
                resp.raise_for_status()
                payload = resp.json()
            if payload.get("errors"):
                log_event(
                    logger,
                    logging.WARNING,
                    "github.auto_merge_failed",
                    repo=repository,
                    pr=pr_number,
                    error=str(payload["errors"]),
                )
                return False
            log_event(
                logger,
                logging.INFO,
                "github.auto_merge_enabled",
                repo=repository,
                pr=pr_number,
            )
            return True
        except httpx.HTTPError as exc:
            log_event(
                logger,
                logging.WARNING,
                "github.auto_merge_failed",
                repo=repository,
                pr=pr_number,
                error=str(exc),
            )
            return False

    async def check_connectivity(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self.api_url}/rate_limit", headers=self._headers())
                return resp.status_code == 200
        except httpx.HTTPError:
            return False
