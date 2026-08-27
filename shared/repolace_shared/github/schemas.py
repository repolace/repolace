from pydantic import BaseModel


class InstallationAccount(BaseModel):
    login: str
    id: int
    type: str


class Installation(BaseModel):
    id: int
    account: InstallationAccount
    suspended_at: str | None = None


class RepoOwner(BaseModel):
    login: str


class Repository(BaseModel):
    id: int
    name: str
    full_name: str
    owner: RepoOwner
    default_branch: str
    private: bool


class Issue(BaseModel):
    number: int
    title: str
    html_url: str
    state: str
    pull_request: dict | None = None

    @property
    def is_pull_request(self) -> bool:
        return self.pull_request is not None


class PullRequest(BaseModel):
    number: int
    html_url: str
    state: str
