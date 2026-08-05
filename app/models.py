from datetime import datetime
from typing import Literal

from pydantic import BaseModel

type UpdateStatus = Literal["updated", "unchanged"]


class GitHubUser(BaseModel):
    login: str
    html_url: str


class Label(BaseModel):
    name: str


class PullRequestBase(BaseModel):
    ref: str


class PullRequest(BaseModel):
    number: int
    title: str
    html_url: str
    merged: bool
    user: GitHubUser
    labels: list[Label]
    base: PullRequestBase


class Repository(BaseModel):
    id: int
    full_name: str
    default_branch: str


class PullRequestWebhook(BaseModel):
    action: str
    pull_request: PullRequest
    repository: Repository


class Installation(BaseModel):
    id: int


class TokenRepository(BaseModel):
    id: int


class InstallationToken(BaseModel):
    token: str
    expires_at: datetime
    permissions: dict[str, str]
    repository_selection: str
    repositories: list[TokenRepository]


class RepositoryFile(BaseModel):
    type: Literal["file"]
    path: str
    sha: str
    size: int
    encoding: str
    content: str


class WebhookResponse(BaseModel):
    status: UpdateStatus | Literal["skipped"]
    repository: str | None = None
    path: str | None = None
