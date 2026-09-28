"""Tests for the GitHub REST client's pull-request review posting."""

import httpx
import pytest
import respx

from app.github import GitHubClient

API = "https://api.github.com"


@pytest.mark.asyncio
@respx.mock
async def test_create_pull_request_review_posts_body_and_event():
    route = respx.post(f"{API}/repos/o/r/pulls/1/reviews").mock(
        return_value=httpx.Response(200, json={"id": 5, "state": "COMMENTED"})
    )
    client = GitHubClient(token="tok", api_url=API)

    # This exercises the log_event call that previously passed a reserved
    # keyword ("event") and raised TypeError, silently losing success accounting.
    review = await client.create_pull_request_review("o/r", 1, "looks good", event="COMMENT")

    assert review["id"] == 5
    assert route.called
    sent = route.calls.last.request
    assert b'"event":"COMMENT"' in sent.content


@pytest.mark.asyncio
@respx.mock
async def test_create_pull_request_review_raises_on_error():
    respx.post(f"{API}/repos/o/r/pulls/2/reviews").mock(
        return_value=httpx.Response(422, json={"message": "Can not approve your own pull request"})
    )
    client = GitHubClient(token="tok", api_url=API)
    with pytest.raises(httpx.HTTPStatusError):
        await client.create_pull_request_review("o/r", 2, "body", event="APPROVE")
