import httpx
import structlog

from repolace_shared.github.auth import (
    DEFAULT_MIN_TTL_SECONDS,
    GITHUB_API_BASE,
    InstallationTokenCache,
    build_app_jwt,
)
from repolace_shared.github.schemas import Installation, Issue, PullRequest, Repository

log = structlog.get_logger()

_PAGE_SIZE = 100
_MAX_PAGES = 50


class GithubClient:
    def __init__(self, app_id: str, private_key: str) -> None:
        self._app_id = app_id
        self._private_key = private_key
        self._token_cache = InstallationTokenCache(app_id, private_key)
        self._http = httpx.AsyncClient(base_url=GITHUB_API_BASE, headers={"Accept": "application/vnd.github+json"})

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_installation_token(
        self, installation_id: int, min_ttl_seconds: float = DEFAULT_MIN_TTL_SECONDS
    ) -> str:
        """Mint (or reuse) an installation token.

        Public because `git push` needs one outside of any API call, and needs
        it minted at push time rather than at task start -- see
        ``repolace_shared.git.workspace.installation_token_provider``.
        """
        return await self._token_cache.get_token(installation_id, self._http, min_ttl_seconds=min_ttl_seconds)

    async def get_installation(self, installation_id: int) -> Installation:
        app_jwt = build_app_jwt(self._app_id, self._private_key)
        response = await self._http.get(
            f"/app/installations/{installation_id}",
            headers={"Authorization": f"Bearer {app_jwt}"},
        )
        response.raise_for_status()
        return Installation.model_validate(response.json())

    async def list_installation_repos(self, installation_id: int) -> list[Repository]:
        token = await self._token_cache.get_token(installation_id, self._http)
        repos: list[Repository] = []
        page = 1
        while True:
            response = await self._http.get(
                "/installation/repositories",
                headers={"Authorization": f"token {token}"},
                params={"per_page": _PAGE_SIZE, "page": page},
            )
            response.raise_for_status()
            batch = response.json()["repositories"]
            repos.extend(Repository.model_validate(repo) for repo in batch)
            if len(batch) < _PAGE_SIZE:
                break
            page += 1
            if page > _MAX_PAGES:
                log.warning("github.pagination.max_pages_exceeded", installation_id=installation_id)
                break
        return repos

    async def list_repo_open_issues(self, installation_id: int, owner: str, repo: str) -> list[Issue]:
        token = await self._token_cache.get_token(installation_id, self._http)
        issues: list[Issue] = []
        page = 1
        while True:
            response = await self._http.get(
                f"/repos/{owner}/{repo}/issues",
                headers={"Authorization": f"token {token}"},
                params={"state": "open", "per_page": _PAGE_SIZE, "page": page},
            )
            response.raise_for_status()
            batch = response.json()
            issues.extend(Issue.model_validate(issue) for issue in batch)
            if len(batch) < _PAGE_SIZE:
                break
            page += 1
            if page > _MAX_PAGES:
                log.warning("github.pagination.max_pages_exceeded", owner=owner, repo=repo)
                break
        return [issue for issue in issues if not issue.is_pull_request]

    async def create_pull_request(
        self,
        installation_id: int,
        owner: str,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> PullRequest:
        """Open a PR from the agent branch against the task's target branch.

        ``head`` must already exist on the remote: GitHub resolves it server
        side, so the branch has to be pushed before this is called.
        """
        token = await self._token_cache.get_token(installation_id, self._http)
        response = await self._http.post(
            f"/repos/{owner}/{repo}/pulls",
            headers={"Authorization": f"token {token}"},
            json={"title": title, "body": body, "head": head, "base": base},
        )
        response.raise_for_status()
        pull_request = PullRequest.model_validate(response.json())
        log.info(
            "github.pull_request.created",
            owner=owner,
            repo=repo,
            number=pull_request.number,
            head=head,
            base=base,
        )
        return pull_request
