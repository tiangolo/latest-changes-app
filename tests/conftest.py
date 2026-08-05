import hashlib
import hmac
from collections.abc import Callable

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
)

from app.config import Settings
from app.models import (
    GitHubUser,
    Label,
    PullRequest,
    PullRequestBase,
    Repository,
)


@pytest.fixture(scope="session")
def private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        Encoding.PEM,
        PrivateFormat.PKCS8,
        NoEncryption(),
    ).decode()


@pytest.fixture(scope="session")
def public_key(private_key: str) -> str:
    key = load_pem_private_key(private_key.encode(), password=None)
    return (
        key.public_key()
        .public_bytes(
            Encoding.PEM,
            PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )


@pytest.fixture
def settings(private_key: str) -> Settings:
    return Settings(
        github_app_id=123,
        github_app_private_key=private_key,
        github_webhook_secret="webhook-secret",
    )


@pytest.fixture
def repository() -> Repository:
    return Repository(
        id=75369425,
        full_name="fastapi/fastapi",
        default_branch="master",
    )


@pytest.fixture
def pull_request() -> PullRequest:
    return PullRequest(
        number=42,
        title="Add the feature",
        html_url="https://github.com/fastapi/fastapi/pull/42",
        merged=True,
        user=GitHubUser(
            login="contributor",
            html_url="https://github.com/contributor",
        ),
        labels=[Label(name="feature")],
        base=PullRequestBase(ref="master"),
    )


@pytest.fixture
def sign(settings: Settings) -> Callable[[bytes], str]:
    def create_signature(body: bytes) -> str:
        digest = hmac.new(
            settings.github_webhook_secret.get_secret_value().encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
        return f"sha256={digest}"

    return create_signature
