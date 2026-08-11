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

    async def list_issue_comments(
        self, repository: str, issue_number: int, *, per_page: int = 100
    ) -> list[dict]:
        """List comments on an issue or pull request (same GitHub issues API)."""
        if not self.token:
            return []
        comments: list[dict] = []
        page = 1
        async with httpx.AsyncClient(timeout=20) as client:
            while page <= 5:
                resp = await client.get(
                    f"{self.api_url}/repos/{repository}/issues/{issue_number}/comments",
                    headers=self._headers(),
                    params={"per_page": per_page, "page": page},
                )
                resp.raise_for_status()
                batch = resp.json() or []
                comments.extend(batch)
                if len(batch) < per_page:
                    break
                page += 1
        return comments

    async def request_bugbot_review(self, repository: str, pr_number: int) -> bool:
        """Ask Cursor Bugbot to review a PR by commenting ``bugbot run`` (idempotent)."""
        if not get_settings().bugbot_trigger_on_pr:
            return False
        try:
            existing = await self.list_issue_comments(repository, pr_number)
        except httpx.HTTPError as exc:
            log_event(
                logger,
                logging.WARNING,
                "github.bugbot_trigger_failed",
                repo=repository,
                pr=pr_number,
                error=str(exc),
            )
            return False
        for comment in existing:
            body = (comment.get("body") or "").strip().lower()
            if body == "bugbot run" or body.startswith("bugbot run"):
                log_event(
                    logger,
                    logging.INFO,
                    "github.bugbot_already_requested",
                    repo=repository,
                    pr=pr_number,
                )
                return False
        ok = await self.post_issue_comment(repository, pr_number, "bugbot run")
        if ok:
            log_event(
                logger,
                logging.INFO,
                "github.bugbot_requested",
                repo=repository,
                pr=pr_number,
            )
        return ok

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
            log_event(
                logger,
                logging.INFO,
                "github.label_added",
                repo=repository,
                issue=issue_number,
                label=label,
            )
            return True
        except httpx.HTTPError as exc:
            log_event(logger, logging.WARNING, "github.label_failed", repo=repository,
                      issue=issue_number, label=label, error=str(exc))
            return False

    async def ensure_label(
        self,
        repository: str,
        name: str,
        *,
        color: str = "0E8A16",
        description: str = "Assign to Cursor Forge for autonomous fix",
    ) -> bool:
        """Create the repo label if missing. Returns True if it exists or was created."""
        if not self.token or not name:
            return False
        encoded = name.replace(" ", "%20")
        async with httpx.AsyncClient(timeout=15) as client:
            get = await client.get(
                f"{self.api_url}/repos/{repository}/labels/{encoded}",
                headers=self._headers(),
            )
            if get.status_code == 200:
                return True
            if get.status_code not in (404,):
                log_event(
                    logger,
                    logging.WARNING,
                    "github.label_lookup_failed",
                    repo=repository,
                    label=name,
                    status=get.status_code,
                )
            create = await client.post(
                f"{self.api_url}/repos/{repository}/labels",
                headers=self._headers(),
                json={"name": name, "color": color.lstrip("#"), "description": description[:100]},
            )
            if create.status_code in (201, 422):
                # 422 = already exists (race)
                return True
            log_event(
                logger,
                logging.WARNING,
                "github.label_create_failed",
                repo=repository,
                label=name,
                status=create.status_code,
                error=create.text[:200],
            )
            return False

    async def ensure_issues_webhook(
        self,
        repository: str,
        *,
        webhook_url: str,
        secret: str,
    ) -> dict:
        """Install or update an issues webhook pointing at forge's public URL."""
        if not self.token:
            raise RuntimeError("GitHub token is not configured")
        webhook_url = (webhook_url or "").rstrip("/")
        if not webhook_url.endswith("/webhook"):
            webhook_url = f"{webhook_url}/webhook"
        async with httpx.AsyncClient(timeout=20) as client:
            listed = await client.get(
                f"{self.api_url}/repos/{repository}/hooks",
                headers=self._headers(),
                params={"per_page": 100},
            )
            listed.raise_for_status()
            hooks = listed.json() or []
            existing = None
            fallback = None
            for hook in hooks:
                cfg = hook.get("config") or {}
                url = (cfg.get("url") or "").rstrip("/")
                events = hook.get("events") or []
                if url == webhook_url:
                    existing = hook
                    break
                if fallback is None and url.endswith("/webhook") and "issues" in events:
                    fallback = hook
            if existing is None:
                existing = fallback

            payload = {
                "name": "web",
                "active": True,
                "events": ["issues"],
                "config": {
                    "url": webhook_url,
                    "content_type": "json",
                    "secret": secret or "unset",
                    "insecure_ssl": "0",
                },
            }
            if existing:
                hook_id = existing["id"]
                resp = await client.patch(
                    f"{self.api_url}/repos/{repository}/hooks/{hook_id}",
                    headers=self._headers(),
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                action = "updated"
            else:
                resp = await client.post(
                    f"{self.api_url}/repos/{repository}/hooks",
                    headers=self._headers(),
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                action = "created"
        log_event(
            logger,
            logging.INFO,
            "github.webhook_ensured",
            repo=repository,
            action=action,
            hook_id=data.get("id"),
            url=webhook_url,
        )
        return {"action": action, "hook": data, "url": webhook_url}

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

    async def create_pull_request(
        self,
        repository: str,
        *,
        title: str,
        head: str,
        base: str,
        body: str = "",
        draft: bool = False,
        same_repo: bool = True,
    ) -> dict:
        """Open a pull request. Returns the GitHub PR payload.

        When ``same_repo`` is True (default), ``head`` is normalized to
        ``owner:branch`` so GitHub opens a PR *within* ``repository`` rather
        than defaulting a fork compare against an upstream parent.
        """
        if not self.token:
            raise RuntimeError("GitHub token is not configured")
        owner = repository.split("/", 1)[0]
        head_ref = head.strip()
        if same_repo:
            branch = head_ref.split(":", 1)[-1]
            head_ref = f"{owner}:{branch}"
        payload = {
            "title": title,
            "head": head_ref,
            "base": base,
            "body": body,
            "draft": draft,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.api_url}/repos/{repository}/pulls",
                headers=self._headers(),
                json=payload,
            )
            if resp.status_code == 422:
                # An open PR for this head may already exist — surface it if present.
                detail = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                errors = detail.get("errors") or []
                message = detail.get("message") or "validation failed"
                log_event(
                    logger,
                    logging.WARNING,
                    "github.pr_create_rejected",
                    repo=repository,
                    head=head_ref,
                    base=base,
                    error=message,
                    errors=errors,
                )
            resp.raise_for_status()
            pr = resp.json()

        base_repo = ((pr.get("base") or {}).get("repo") or {}).get("full_name") or ""
        head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name") or ""
        if same_repo and base_repo and base_repo != repository:
            log_event(
                logger,
                logging.ERROR,
                "github.pr_wrong_base_repo",
                repo=repository,
                base_repo=base_repo,
                head_repo=head_repo,
                url=pr.get("html_url"),
            )
            raise RuntimeError(
                f"PR base repo is {base_repo}, expected same-repo PR on {repository}"
            )

        log_event(
            logger,
            logging.INFO,
            "github.pr_created",
            repo=repository,
            pr=pr.get("number"),
            head=head_ref,
            base=base,
            base_repo=base_repo or repository,
            head_repo=head_repo or repository,
            url=pr.get("html_url"),
        )
        return pr

    async def find_open_pull_request_for_head(
        self, repository: str, head: str
    ) -> dict | None:
        """Return an open PR whose head branch matches ``head`` (branch name or owner:branch)."""
        head_branch = head.split(":", 1)[-1]
        owner = repository.split("/", 1)[0]
        candidates = {head_branch, f"{owner}:{head_branch}", head}
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{self.api_url}/repos/{repository}/pulls",
                headers=self._headers(),
                params={"state": "open", "head": f"{owner}:{head_branch}", "per_page": 10},
            )
            if resp.status_code == 200:
                for item in resp.json() or []:
                    item_head = ((item.get("head") or {}).get("ref") or "")
                    item_label = ((item.get("head") or {}).get("label") or "")
                    if item_head in candidates or item_label in candidates:
                        return item
            # Fallback: list open PRs and match by branch name.
            resp = await client.get(
                f"{self.api_url}/repos/{repository}/pulls",
                headers=self._headers(),
                params={"state": "open", "per_page": 50},
            )
            resp.raise_for_status()
            for item in resp.json() or []:
                item_head = ((item.get("head") or {}).get("ref") or "")
                if item_head == head_branch:
                    return item
        return None

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
