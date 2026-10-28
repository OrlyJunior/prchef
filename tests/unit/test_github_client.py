from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from prchef.clients import github_client
from prchef.clients.command_client import CommandResult
from prchef.clients.exceptions import GitHubClientError
from prchef.clients.github_client import GitHubAppAuthenticator, GitHubClient
from prchef.config.project_config import GitHubAppSettings


@pytest.fixture(autouse=True)
def isolate_bot_user_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github_client, "_BOT_USER_IDS", {})


def make_bot_authenticator(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
    *,
    api_url: str = "https://api.github.com",
    bot_name: str | None = None,
    bot_user_id: int | None = None,
) -> GitHubAppAuthenticator:
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "fake-private-key")
    monkeypatch.setattr(github_client.jwt, "encode", lambda *args, **kwargs: "app-jwt")
    return GitHubAppAuthenticator(
        GitHubAppSettings(
            app_id=123, installation_id=456, private_key_env="GITHUB_APP_PRIVATE_KEY",
            api_url=api_url, bot_name=bot_name, bot_user_id=bot_user_id,
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        now=lambda: datetime(2030, 1, 1, tzinfo=UTC),
    )


def test_bot_identity_resolves_and_caches_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/app"):
            return httpx.Response(200, json={"slug": "implementer", "id": 123})
        if request.method == "POST":
            return httpx.Response(201, json={
                "token": "installation-token", "expires_at": "2030-01-01T01:00:00Z",
            })
        return httpx.Response(200, json={"id": 987})

    auth = make_bot_authenticator(monkeypatch, handler, api_url="https://example.com/api/v3")
    expected = ("implementer[bot]", "987+implementer[bot]@users.noreply.github.com")
    assert auth.bot_identity() == expected
    assert auth.bot_identity() == expected
    assert [r.url.path for r in requests] == [
        "/api/v3/app", "/api/v3/app/installations/456/access_tokens",
        "/api/v3/users/implementer[bot]",
    ]
    assert requests[2].url.raw_path.endswith(b"/users/implementer%5Bbot%5D")
    assert requests[0].headers["authorization"] == "Bearer app-jwt"
    assert requests[2].headers["authorization"] == "Bearer installation-token"
    assert requests[2].headers["x-github-api-version"] == "2022-11-28"

    recreated = make_bot_authenticator(monkeypatch, handler, api_url="https://example.com/api/v3/")
    assert recreated.bot_identity() == expected
    assert len(requests) == 4  # Only App metadata is needed by the new instance.


def test_bot_identity_override_wins_over_cached_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/app"):
            return httpx.Response(200, json={"slug": "implementer", "id": 123})
        if request.method == "POST":
            return httpx.Response(201, json={
                "token": "installation-token", "expires_at": "2030-01-01T01:00:00Z",
            })
        return httpx.Response(200, json={"id": 987})

    assert make_bot_authenticator(monkeypatch, handler).bot_identity() == (
        "implementer[bot]", "987+implementer[bot]@users.noreply.github.com",
    )

    def unexpected_request(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"Override unexpectedly made an HTTP request: {request.url.path}")

    override = make_bot_authenticator(
        monkeypatch, unexpected_request, bot_name="implementer[bot]", bot_user_id=555,
    )
    assert override.bot_identity() == (
        "implementer[bot]", "555+implementer[bot]@users.noreply.github.com",
    )


def test_bot_user_cache_isolated_by_endpoint_and_login(monkeypatch: pytest.MonkeyPatch) -> None:
    slug = "first"
    lookups: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/app"):
            return httpx.Response(200, json={"slug": slug, "id": 123})
        if request.method == "POST":
            return httpx.Response(201, json={
                "token": "token", "expires_at": "2030-01-01T01:00:00Z",
            })
        lookups.append(str(request.url))
        return httpx.Response(200, json={"id": len(lookups)})

    assert make_bot_authenticator(monkeypatch, handler).bot_identity()[1].startswith("1+")
    other = make_bot_authenticator(monkeypatch, handler, api_url="https://other.test/api")
    assert other.bot_identity()[1].startswith("2+")
    other_prefix = make_bot_authenticator(monkeypatch, handler, api_url="https://other.test/api/v3")
    assert other_prefix.bot_identity()[1].startswith("3+")
    slug = "second"
    assert make_bot_authenticator(monkeypatch, handler).bot_identity()[1].startswith("4+")
    assert len(lookups) == 4


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        ("network", "network error"), ("timeout", "network timeout"),
        (403, "HTTP 403"), (404, "HTTP 404"), ("json", "invalid user response"),
        ({}, "invalid user response"), ({"id": None}, "invalid user response"),
        ({"id": True}, "invalid user response"), ({"id": "987"}, "invalid user response"),
        ({"id": 0}, "invalid user response"), ({"id": -1}, "invalid user response"),
        ([], "invalid user response"), ("token", "installation token acquisition failed"),
    ],
)
def test_bot_user_lookup_failure_falls_back_and_recovers(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: Any, reason: str,
) -> None:
    failed = True
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/app"):
            return httpx.Response(200, json={"slug": "implementer", "id": 123})
        if request.method == "POST":
            if failed and failure == "token":
                return httpx.Response(403, text="secret-response-body")
            return httpx.Response(201, json={
                "token": "secret-token", "expires_at": "2030-01-01T01:00:00Z",
            })
        if not failed:
            return httpx.Response(200, json={"id": 987})
        if failure == "network":
            raise httpx.ConnectError("secret exception", request=request)
        if failure == "timeout":
            raise httpx.ReadTimeout("secret exception", request=request)
        if isinstance(failure, int):
            return httpx.Response(failure, text="secret-response-body")
        if failure == "json":
            return httpx.Response(200, text="secret-response-body")
        return httpx.Response(200, json=failure)

    auth = make_bot_authenticator(monkeypatch, handler)
    expected = ("implementer[bot]", "123+implementer[bot]@users.noreply.github.com")
    assert auth.bot_identity() == expected
    count = len(requests)
    assert auth.bot_identity() == expected
    assert len(requests) == count
    assert reason in caplog.text
    assert "App ID" in caplog.text and "attribution and avatar may be incorrect" in caplog.text
    assert len(caplog.records) == 1
    assert "secret" not in caplog.text
    failed = False
    assert make_bot_authenticator(monkeypatch, handler).bot_identity()[1].startswith("987+")


def test_bot_identity_preserves_app_metadata_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    auth = make_bot_authenticator(monkeypatch, lambda request: httpx.Response(403))
    with pytest.raises(GitHubClientError, match="Could not resolve GitHub App bot identity"):
        auth.bot_identity()


class RecordingCommands:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def run(self, command: list[str], **kwargs: Any) -> CommandResult:
        self.calls.append({"command": command, **kwargs})
        return CommandResult(
            command=command,
            exit_code=0,
            stdout="{}",
            stderr="",
            duration_seconds=0.01,
        )


def test_github_app_authenticator_mints_and_caches_installation_token(
    monkeypatch: Any,
) -> None:
    requested: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        return httpx.Response(
            201,
            json={"token": "ghs_installation", "expires_at": "2030-01-01T01:00:00Z"},
        )

    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "fake-private-key")
    captured_jwt_payload: dict[str, object] = {}

    def fake_encode(payload: dict[str, object], key: str, algorithm: str) -> str:
        captured_jwt_payload.update(payload)
        assert key == "fake-private-key"
        assert algorithm == "RS256"
        return "signed-app-jwt"

    monkeypatch.setattr("prchef.clients.github_client.jwt.encode", fake_encode)
    authenticator = GitHubAppAuthenticator(
        GitHubAppSettings(
            app_id=123,
            installation_id=456,
            private_key_env="GITHUB_APP_PRIVATE_KEY",
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        now=lambda: datetime(2030, 1, 1, tzinfo=UTC),
    )

    assert authenticator.token() == "ghs_installation"
    assert authenticator.token() == "ghs_installation"
    assert len(requested) == 1
    assert requested[0].url.path == "/app/installations/456/access_tokens"
    assert requested[0].headers["authorization"] == "Bearer signed-app-jwt"
    assert captured_jwt_payload["iss"] == "123"


def test_github_app_authenticator_signs_jwt_with_installed_crypto(monkeypatch: Any) -> None:
    requested: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        return httpx.Response(
            201,
            json={"token": "ghs_installation", "expires_at": "2030-01-01T01:00:00Z"},
        )

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", pem)
    now = datetime(2030, 1, 1, tzinfo=UTC)
    authenticator = GitHubAppAuthenticator(
        GitHubAppSettings(
            app_id=123,
            installation_id=456,
            private_key_env="GITHUB_APP_PRIVATE_KEY",
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        now=lambda: now,
    )

    assert authenticator.token() == "ghs_installation"
    encoded = requested[0].headers["authorization"].removeprefix("Bearer ")
    payload = jwt.decode(encoded, algorithms=["RS256"], options={"verify_signature": False})
    assert payload == {
        "iat": int((now.timestamp()) - 60),
        "exp": int((now.timestamp()) + 9 * 60),
        "iss": "123",
    }


def test_submit_pull_request_review_uses_app_token_and_api_payload(tmp_path: Path) -> None:
    commands = RecordingCommands()
    client = GitHubClient(commands, tmp_path, ["gh"], 120)

    class Authenticator:
        def token(self) -> str:
            return "ghs_installation"

    client._app_authenticator = Authenticator()  # type: ignore[assignment]
    client.submit_pull_request_review(
        repository="owner/repository",
        pull_request_number=42,
        event="REQUEST_CHANGES",
        body="## Automated review: CHANGES_REQUESTED",
    )

    call = commands.calls[0]
    assert call["command"] == [
        "gh",
        "api",
        "--method",
        "POST",
        "repos/owner/repository/pulls/42/reviews",
        "-f",
        "event=REQUEST_CHANGES",
        "-f",
        "body=## Automated review: CHANGES_REQUESTED",
    ]
    assert call["env"] is not None
    assert call["env"]["GH_TOKEN"] == "ghs_installation"


def test_submit_pull_request_review_supports_approval_event(tmp_path: Path) -> None:
    commands = RecordingCommands()
    client = GitHubClient(commands, tmp_path, ["gh"], 120)

    client.submit_pull_request_review(
        repository="owner/repository",
        pull_request_number=42,
        event="APPROVE",
        body="## Automated review: APPROVED",
    )

    assert "event=APPROVE" in commands.calls[0]["command"]


def test_create_pull_request_uses_implementer_app_token(tmp_path: Path) -> None:
    class PullRequestCommands(RecordingCommands):
        def run(self, command: list[str], **kwargs: Any) -> CommandResult:
            self.calls.append({"command": command, **kwargs})
            stdout = (
                "https://github.example/owner/repository/pull/42\n"
                if command[1:3] == ["pr", "create"]
                else (
                    '{"number": 42, "url": "https://github.example/owner/repository/pull/42", '
                    '"title": "Title", "baseRefName": "main", "headRefName": "feature"}'
                )
            )
            return CommandResult(command, 0, stdout, "", 0.01)

    commands = PullRequestCommands()
    client = GitHubClient(commands, tmp_path, ["gh"], 120)

    class Authenticator:
        def token(self) -> str:
            return "ghs_implementer"

    client._implementer_authenticator = Authenticator()  # type: ignore[assignment]
    pull_request = client.create_pull_request(
        title="Title", body="Body", base_branch="main", head_branch="feature"
    )

    assert pull_request.number == 42
    assert all(call["env"]["GH_TOKEN"] == "ghs_implementer" for call in commands.calls)


def test_is_pull_request_open_uses_persisted_pull_request_number(tmp_path: Path) -> None:
    class OpenPullRequestCommands(RecordingCommands):
        def run(self, command: list[str], **kwargs: Any) -> CommandResult:
            self.calls.append({"command": command, **kwargs})
            return CommandResult(command, 0, "open\n", "", 0.01)

    commands = OpenPullRequestCommands()
    client = GitHubClient(commands, tmp_path, ["gh"], 120)

    assert client.is_pull_request_open("owner/repository", 42) is True
    assert commands.calls[0]["command"] == [
        "gh",
        "api",
        "repos/owner/repository/pulls/42",
        "--jq",
        ".state",
    ]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [('{"body":"Current criteria"}', "Current criteria"), ('{"body":null}', "")],
)
def test_pull_request_description_uses_reviewer_authentication(
    tmp_path: Path, payload: str, expected: str
) -> None:
    class DescriptionCommands(RecordingCommands):
        def run(self, command: list[str], **kwargs: Any) -> CommandResult:
            self.calls.append({"command": command, **kwargs})
            return CommandResult(command, 0, payload, "", 0.01)

    commands = DescriptionCommands()
    client = GitHubClient(commands, tmp_path, ["gh"], 120)

    class Authenticator:
        def token(self) -> str:
            return "ghs_reviewer"

    client._app_authenticator = Authenticator()  # type: ignore[assignment]
    assert client.pull_request_description("owner/repository", 42) == expected
    assert commands.calls[0]["command"] == [
        "gh", "api", "repos/owner/repository/pulls/42"
    ]
    assert commands.calls[0]["env"]["GH_TOKEN"] == "ghs_reviewer"


@pytest.mark.parametrize("payload", ["{}", '{"body":42}', "[]", "invalid"])
def test_pull_request_description_rejects_malformed_response(
    tmp_path: Path, payload: str
) -> None:
    class DescriptionCommands(RecordingCommands):
        def run(self, command: list[str], **kwargs: Any) -> CommandResult:
            return CommandResult(command, 0, payload, "", 0.01)

    client = GitHubClient(DescriptionCommands(), tmp_path, ["gh"], 120)
    with pytest.raises(GitHubClientError, match="Invalid GitHub pull request description"):
        client.pull_request_description("owner/repository", 42)


def test_pull_request_description_reports_command_failure(tmp_path: Path) -> None:
    class FailingCommands(RecordingCommands):
        def run(self, command: list[str], **kwargs: Any) -> CommandResult:
            return CommandResult(command, 1, "", "permission denied", 0.01)

    client = GitHubClient(FailingCommands(), tmp_path, ["gh"], 120)
    with pytest.raises(GitHubClientError, match="permission denied"):
        client.pull_request_description("owner/repository", 42)


def test_comment_pull_request_uses_implementer_app_token(tmp_path: Path) -> None:
    commands = RecordingCommands()
    client = GitHubClient(commands, tmp_path, ["gh"], 120)

    class Authenticator:
        def token(self) -> str:
            return "ghs_implementer"

    client._implementer_authenticator = Authenticator()  # type: ignore[assignment]
    client.comment_pull_request(42, "Suggestions were reviewed and skipped.")

    call = commands.calls[0]
    assert call["command"] == [
        "gh",
        "pr",
        "comment",
        "42",
        "--body",
        "Suggestions were reviewed and skipped.",
    ]
    assert call["env"] is not None
    assert call["env"]["GH_TOKEN"] == "ghs_implementer"


def test_assign_pull_request_uses_implementer_app_token_and_workspace(tmp_path: Path) -> None:
    commands = RecordingCommands()
    client = GitHubClient(commands, tmp_path, ["gh"], 120)

    class Authenticator:
        def token(self) -> str:
            return "ghs_implementer"

    workspace = tmp_path / "worktree"
    client._implementer_authenticator = Authenticator()  # type: ignore[assignment]
    client.assign_pull_request(42, ("implementer[bot]",), cwd=workspace)

    call = commands.calls[0]
    assert call["command"] == [
        "gh",
        "pr",
        "edit",
        "42",
        "--add-assignee",
        "implementer[bot]",
    ]
    assert call["cwd"] == workspace
    assert call["env"] is not None
    assert call["env"]["GH_TOKEN"] == "ghs_implementer"


def test_assign_implementer_resolves_bot_identity_and_assigns(tmp_path: Path) -> None:
    commands = RecordingCommands()
    client = GitHubClient(commands, tmp_path, ["gh"], 120)

    class Authenticator:
        def bot_identity(self) -> tuple[str, str]:
            return ("implementer[bot]", "123+implementer[bot]@users.noreply.github.com")

        def token(self) -> str:
            return "ghs_implementer"

    client._implementer_authenticator = Authenticator()  # type: ignore[assignment]

    assert client.assign_implementer(42) == "implementer[bot]"
    assert commands.calls[0]["command"][-2:] == ["--add-assignee", "implementer[bot]"]


def test_assign_implementer_is_noop_without_implementer_app(tmp_path: Path) -> None:
    commands = RecordingCommands()
    client = GitHubClient(commands, tmp_path, ["gh"], 120)

    assert client.assign_implementer(42) is None
    assert commands.calls == []


@pytest.mark.parametrize(
    ("error_template", "redacted"),
    [
        ("{token}", "[REDACTED]"),
        ("GH_TOKEN={token}", "GH_TOKEN=[REDACTED]"),
        ("Authorization: Bearer {token}", "Authorization=[REDACTED] [REDACTED]"),
    ],
)
def test_assign_pull_request_redacts_token_when_github_cli_fails(
    tmp_path: Path, error_template: str, redacted: str,
) -> None:
    token = "ghs_" + ("FAKE_NOT_A_SECRET.AaZz09-_" * 22)[:516]

    class FailingCommands(RecordingCommands):
        def run(self, command: list[str], **kwargs: Any) -> CommandResult:
            self.calls.append({"command": command, **kwargs})
            return CommandResult(command, 1, "", error_template.format(token=token), 0.01)

    client = GitHubClient(FailingCommands(), tmp_path, ["gh"], 120)

    with pytest.raises(GitHubClientError, match="GitHub CLI failed") as exc_info:
        client.assign_pull_request(42, ("implementer[bot]",))

    assert str(exc_info.value) == f"GitHub CLI failed: {redacted}"
