"""`GithubClient.list_branches` pages exactly like `list_repo_open_issues`."""

import httpx
import pytest
from repolace_shared.github.client import GithubClient

pytestmark = pytest.mark.anyio


class _Tokens:
    async def get_token(self, *_args, **_kwargs) -> str:
        return "t"


def client_over(handler) -> GithubClient:
    client = GithubClient("1", "unused-key")
    client._token_cache = _Tokens()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.test")
    return client


async def test_list_branches_follows_pages_until_a_short_one():
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        pages.append(page)
        assert request.url.path == "/repos/acme/sample/branches"
        assert request.url.params["per_page"] == "100"
        names = [f"b{page}-{i}" for i in range(100 if page == 1 else 3)]
        return httpx.Response(200, json=[{"name": n, "commit": {"sha": "x"}} for n in names])

    branches = await client_over(handler).list_branches(1, "acme", "sample")

    assert pages == [1, 2]
    assert len(branches) == 103
    assert branches[0].name == "b1-0"
    assert branches[-1].name == "b2-2"


async def test_list_branches_raises_on_a_github_error():
    client = client_over(lambda request: httpx.Response(404, json={}))

    with pytest.raises(httpx.HTTPStatusError):
        await client.list_branches(1, "acme", "sample")


async def test_a_listing_that_hits_the_page_cap_says_so(monkeypatch):
    monkeypatch.setattr("repolace_shared.github.client._MAX_PAGES", 2)
    client = client_over(lambda r: httpx.Response(200, json=[{"name": f"b{i}"} for i in range(100)]))

    branches = await client.list_branches(1, "acme", "sample")

    assert branches.truncated is True


async def test_a_short_listing_is_not_truncated():
    client = client_over(lambda r: httpx.Response(200, json=[{"name": "main"}]))

    assert (await client.list_branches(1, "acme", "sample")).truncated is False


async def test_branch_exists_quotes_the_name_and_reads_404_as_absent():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path.decode())
        return httpx.Response(200 if "feature/a%20b%3F" in seen[-1] else 404, json={})

    client = client_over(handler)

    assert await client.branch_exists(1, "acme", "sample", "feature/a b?") is True
    assert await client.branch_exists(1, "acme", "sample", "other") is False
    assert seen[0] == "/repos/acme/sample/branches/feature/a%20b%3F"
