from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from orchestrator.clients.command_client import CommandResult
from orchestrator.clients.github_client import GitHubAppAuthenticator, GitHubClient
from orchestrator.config.project_config import GitHubAppSettings


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

    monkeypatch.setattr("orchestrator.clients.github_client.jwt.encode", fake_encode)
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
