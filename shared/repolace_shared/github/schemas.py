from datetime import datetime

from pydantic import BaseModel


class InstallationAccount(BaseModel):
    login: str
    id: int
    type: str


class Installation(BaseModel):
    id: int
    account: InstallationAccount
    suspended_at: str | None = None
    #: What this installation actually granted, e.g. {"contents": "write"}.
    #: Declared permissions on the App and granted permissions on an
    #: installation are different things: changing the former does not update
    #: the latter until the account owner accepts the request.
    permissions: dict[str, str] = {}


class RepoOwner(BaseModel):
    login: str


class Repository(BaseModel):
    id: int
    name: str
    full_name: str
    owner: RepoOwner
    default_branch: str
    private: bool


class IssueLabel(BaseModel):
    name: str


class IssueUser(BaseModel):
    login: str


class Branch(BaseModel):
    name: str


class Issue(BaseModel):
    number: int
    title: str
    html_url: str
    state: str
    #: Untrusted: anyone who can open an issue wrote it. GitHub sends null for
    #: an empty body, which is why this is optional and not `str = ""`.
    body: str | None = None
    pull_request: dict | None = None
    #: Untrusted like the body: label names and logins are chosen by whoever
    #: filed or triaged the issue. Display data only.
    labels: list[IssueLabel] = []
    user: IssueUser | None = None
    created_at: datetime | None = None

    @property
    def label_names(self) -> list[str]:
        return [label.name for label in self.labels]

    @property
    def is_pull_request(self) -> bool:
        return self.pull_request is not None


class PullRequest(BaseModel):
    number: int
    html_url: str
    state: str
