from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from threading import Lock
from typing import Final, Protocol, cast
from urllib.parse import quote

import httpx

from prchef.clients.command_client import CommandClient, CommandResult
from prchef.clients.exceptions import GitHubClientError
from prchef.config.project_config import GitHubAppSettings
from prchef.models.pull_request import PullRequest
from prchef.runtime_logging import redact_text

logger = logging.getLogger(__name__)

_GITHUB_API_VERSION: Final = "2022-11-28"
_INSTALLATION_TOKEN_REFRESH_BUFFER: Final = timedelta(seconds=60)
_BOT_USER_IDS: dict[tuple[str, str], int] = {}
_BOT_USER_IDS_LOCK = Lock()


class _JwtEncoder(Protocol):
    """Minimal PyJWT interface, kept optional until App auth is used."""

    def encode(
        self,
        payload: dict[str, int | str],
        key: str,
        algorithm: str,
    ) -> str: ...


class _MissingJwtEncoder:
    def encode(
        self,
        payload: dict[str, int | str],
        key: str,
        algorithm: str,
    ) -> str:
        raise RuntimeError("PyJWT is required for GitHub App authentication")


try:
    jwt: _JwtEncoder = cast(_JwtEncoder, import_module("jwt"))
except ModuleNotFoundError:
    # App credentials are optional. Avoid making every GitHub CLI interaction
    # unavailable in environments that have not installed this optional path yet.
    jwt = _MissingJwtEncoder()


class GitHubAppAuthenticator:
    """Mints and caches a GitHub App installation token and bot metadata."""

    def __init__(
        self,
        settings: GitHubAppSettings,
        *,
        http_client: httpx.Client | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._http = http_client or httpx.Client(timeout=30.0)
        self._now = now or (lambda: datetime.now(UTC))
        self._token: str | None = None
        self._token_expires_at: datetime | None = None
        self._bot_identity: tuple[str, str] | None = None

    def token(self) -> str:
        now = self._now()
        if (
            self._token is not None
            and self._token_expires_at is not None
            and now + _INSTALLATION_TOKEN_REFRESH_BUFFER < self._token_expires_at
        ):
            return self._token

        app_jwt = self._app_jwt(now)
        assert self._settings.installation_id is not None
        try:
            response = self._http.post(
                f"{self._settings.api_url}/app/installations/"
                f"{self._settings.installation_id}/access_tokens",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {app_jwt}",
                    "X-GitHub-Api-Version": _GITHUB_API_VERSION,
                },
            )
            response.raise_for_status()
            payload = response.json()
            token = payload["token"]
            expires_at = payload["expires_at"]
            if not isinstance(token, str) or not token:
                raise ValueError("response token is missing")
            if not isinstance(expires_at, str):
                raise ValueError("response expires_at is missing")
            parsed_expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise GitHubClientError("Could not mint a GitHub App installation token") from exc

        self._token = token
        self._token_expires_at = parsed_expiry.astimezone(UTC)
        return token

    def bot_identity(self) -> tuple[str, str]:
        """Return the App bot name and noreply address for Git commit metadata."""
        if self._bot_identity is not None:
            return self._bot_identity
        if self._settings.bot_name is not None and self._settings.bot_user_id is not None:
            self._bot_identity = (
                self._settings.bot_name,
                f"{self._settings.bot_user_id}+{self._settings.bot_name}@users.noreply.github.com",
            )
            return self._bot_identity

        now = self._now()
        try:
            response = self._http.get(
                f"{self._settings.api_url}/app",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self._app_jwt(now)}",
                    "X-GitHub-Api-Version": _GITHUB_API_VERSION,
                },
            )
            response.raise_for_status()
            payload = response.json()
            slug = payload["slug"]
            app_id = payload["id"]
            if not isinstance(slug, str) or not slug or not isinstance(app_id, int):
                raise ValueError("response App identity is missing")
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise GitHubClientError("Could not resolve GitHub App bot identity") from exc
        bot_name = f"{slug}[bot]"
        api_url = httpx.URL(self._settings.api_url).copy_with(userinfo=b"")
        cache_key = (str(api_url).rstrip("/"), bot_name)
        with _BOT_USER_IDS_LOCK:
            user_id = _BOT_USER_IDS.get(cache_key)
        if user_id is None:
            try:
                response = self._http.get(
                    f"{self._settings.api_url}/users/{quote(bot_name, safe='')}",
                    headers={
                        "Accept": "application/vnd.github+json",
                        "Authorization": f"Bearer {self.token()}",
                        "X-GitHub-Api-Version": _GITHUB_API_VERSION,
                    },
                )
                response.raise_for_status()
                user_payload = response.json()
                if not isinstance(user_payload, dict):
                    raise ValueError("invalid user response")
                user_id = user_payload.get("id")
                if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
                    raise ValueError("invalid user ID")
            except (GitHubClientError, httpx.HTTPError, ValueError) as exc:
                if isinstance(exc, httpx.HTTPStatusError):
                    reason = f"HTTP {exc.response.status_code}"
                elif isinstance(exc, httpx.TimeoutException):
                    reason = "network timeout"
                elif isinstance(exc, httpx.HTTPError):
                    reason = "network error"
                elif isinstance(exc, GitHubClientError):
                    reason = "installation token acquisition failed"
                else:
                    reason = "invalid user response"
                logger.warning(
                    "[github] Could not resolve bot user ID (%s); using the App ID. "
                    "Commit attribution and avatar may be incorrect.",
                    redact_text(reason),
                )
                user_id = app_id
            else:
                with _BOT_USER_IDS_LOCK:
                    _BOT_USER_IDS[cache_key] = user_id
        self._bot_identity = (bot_name, f"{user_id}+{bot_name}@users.noreply.github.com")
        return self._bot_identity

    def _app_jwt(self, now: datetime) -> str:
        assert self._settings.app_id is not None
        try:
            encoded = jwt.encode(
                {
                    "iat": int((now - timedelta(seconds=60)).timestamp()),
                    "exp": int((now + timedelta(minutes=9)).timestamp()),
                    "iss": str(self._settings.app_id),
                },
                self._private_key(),
                algorithm="RS256",
            )
            return encoded
        except Exception as exc:
            raise GitHubClientError("Could not sign a GitHub App authentication JWT") from exc

    def _private_key(self) -> str:
        if self._settings.private_key_path is not None:
            try:
                return self._settings.private_key_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise GitHubClientError("Could not read GitHub App private key file") from exc
        assert self._settings.private_key_env is not None
        private_key = os.environ.get(self._settings.private_key_env)
        if not private_key:
            raise GitHubClientError(
                f"GitHub App private key environment variable is not set: "
                f"{self._settings.private_key_env}"
            )
        return private_key.replace("\\n", "\n")


class GitHubClient:
    def __init__(
        self,
        command_client: CommandClient,
        repository_path: Path,
        command: Sequence[str],
        timeout_seconds: int,
        *,
        app: GitHubAppSettings | None = None,
        implementer_app: GitHubAppSettings | None = None,
        review_authenticator: GitHubAppAuthenticator | None = None,
        implementer_authenticator: GitHubAppAuthenticator | None = None,
    ) -> None:
        self._commands = command_client
        self._path = repository_path
        self._command = list(command)
        self._timeout_seconds = timeout_seconds
        self._app = app or GitHubAppSettings()
        self._implementer_app = implementer_app or GitHubAppSettings()
        self._app_authenticator = review_authenticator or (
            GitHubAppAuthenticator(self._app) if self._app.is_configured else None
        )
        self._implementer_authenticator = implementer_authenticator or (
            GitHubAppAuthenticator(self._implementer_app)
            if self._implementer_app.is_configured
            else None
        )

    def at_path(self, repository_path: Path) -> GitHubClient:
        return GitHubClient(
            self._commands,
            repository_path,
            self._command,
            self._timeout_seconds,
            app=self._app,
            implementer_app=self._implementer_app,
            review_authenticator=self._app_authenticator,
            implementer_authenticator=self._implementer_authenticator,
        )

    def create_pull_request(
        self,
        *,
        title: str,
        body: str,
        base_branch: str,
        head_branch: str,
    ) -> PullRequest:
        logger.info("[github] Creating pull request from branch: %s", head_branch)
        created = self._run(
            "pr",
            "create",
            "--title",
            title,
            "--body",
            body,
            "--base",
            base_branch,
            "--head",
            head_branch,
            env=self._implementer_auth_environment(),
        )
        url = next(
            (
                line.strip()
                for line in reversed(created.stdout.splitlines())
                if line.startswith("http")
            ),
            "",
        )
        if not url:
            raise GitHubClientError("GitHub CLI did not return the pull request URL")
        viewed = self._run(
            "pr",
            "view",
            url,
            "--json",
            "number,url,title,baseRefName,headRefName",
            env=self._implementer_auth_environment(),
        )
        try:
            data = json.loads(viewed.stdout)
            pull_request = PullRequest(
                number=int(data["number"]),
                url=str(data["url"]),
                title=str(data["title"]),
                base_branch=str(data["baseRefName"]),
                head_branch=str(data["headRefName"]),
            )
            logger.info(
                "[github] Pull request created: #%s %s",
                pull_request.number,
                pull_request.url,
            )
            return pull_request
        except (ValueError, KeyError, TypeError) as exc:
            raise GitHubClientError(f"Invalid GitHub CLI response: {viewed.stdout}") from exc

    def find_pull_request(self, head_branch: str, base_branch: str) -> PullRequest | None:
        """Find the unique PR for a workflow branch before creating another one."""
        listed = self._run(
            "pr", "list", "--head", head_branch, "--state", "all", "--limit", "1000",
            "--json", "number,url,title,baseRefName,headRefName,state",
            env=self._implementer_auth_environment(),
        )
        try:
            rows = json.loads(listed.stdout)
            matches = [row for row in rows if row["headRefName"] == head_branch
                       and row["baseRefName"] == base_branch]
            if len(matches) > 1:
                raise GitHubClientError(
                    "Several pull requests use the retained branch; inspect them before retrying"
                )
            if not matches:
                return None
            row = matches[0]
            if row["state"] != "OPEN":
                raise GitHubClientError(
                    "Retained pull request is closed; inspect it before retrying"
                )
            return PullRequest(
                number=int(row["number"]), url=str(row["url"]),
                title=str(row["title"]), base_branch=base_branch, head_branch=head_branch,
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise GitHubClientError("Cannot verify retained pull request") from exc

    def review_already_published(
        self, repository: str, pull_request_number: int, event: str, body: str
    ) -> bool:
        viewed = self._run(
            "api", f"repos/{repository}/pulls/{pull_request_number}/reviews",
            "--paginate", "--slurp",
            env=self._review_auth_environment(),
        )
        try:
            pages = json.loads(viewed.stdout)
            reviews = [review for page in pages for review in page]
            state = {
                "APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED",
                "COMMENT": "COMMENTED",
            }[event]
            normalized = body.replace("\r\n", "\n").strip()
            return any(review.get("state") == state and
                       str(review.get("body", "")).replace("\r\n", "\n").strip() == normalized
                       for review in reviews)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise GitHubClientError("Cannot verify retained review publication") from exc

    def is_pull_request_open(self, repository: str, pull_request_number: int) -> bool:
        """Return whether a persisted pull request can still receive branch updates."""
        if pull_request_number <= 0:
            raise ValueError("Pull request number must be positive")
        if not repository.strip():
            raise ValueError("GitHub repository must be non-empty")
        viewed = self._run(
            "api",
            f"repos/{repository}/pulls/{pull_request_number}",
            "--jq",
            ".state",
            env=self._implementer_auth_environment(),
        )
        return viewed.stdout.strip().casefold() == "open"

    def pull_request_description(self, repository: str, pull_request_number: int) -> str:
        """Read the current PR body with the reviewer identity."""
        if pull_request_number <= 0:
            raise ValueError("Pull request number must be positive")
        if not repository.strip():
            raise ValueError("GitHub repository must be non-empty")
        viewed = self._run(
            "api",
            f"repos/{repository}/pulls/{pull_request_number}",
            env=self._review_auth_environment(),
        )
        try:
            data = json.loads(viewed.stdout)
            if not isinstance(data, dict) or "body" not in data:
                raise ValueError("response body is missing")
            body = data["body"]
            if body is None:
                return ""
            if not isinstance(body, str):
                raise ValueError("response body is not a string or null")
            return body
        except (ValueError, TypeError) as exc:
            raise GitHubClientError(
                f"Invalid GitHub pull request description response: {exc}"
            ) from exc

    def submit_pull_request_review(
        self,
        *,
        repository: str,
        pull_request_number: int,
        event: str,
        body: str,
    ) -> None:
        """Publish a formal review using the configured GitHub identity."""
        if event not in {"APPROVE", "REQUEST_CHANGES", "COMMENT"}:
            raise ValueError(f"Unsupported GitHub pull request review event: {event}")
        if pull_request_number <= 0:
            raise ValueError("Pull request number must be positive")
        if not repository.strip():
            raise ValueError("GitHub repository must be non-empty")

        logger.info(
            "[github] Publishing %s review to %s#%s",
            event,
            repository,
            pull_request_number,
        )
        self._run(
            "api",
            "--method",
            "POST",
            f"repos/{repository}/pulls/{pull_request_number}/reviews",
            "-f",
            f"event={event}",
            "-f",
            f"body={body}",
            env=self._review_auth_environment(),
        )

    def comment_pull_request(self, pr_number: int, body: str) -> None:
        """Publish a general pull request comment as the implementer identity."""
        if pr_number <= 0:
            raise ValueError("Pull request number must be positive")
        if not body.strip():
            raise ValueError("Pull request comment body must be non-empty")
        logger.info("[github] Publishing pull request comment to #%s", pr_number)
        self._run(
            "pr",
            "comment",
            str(pr_number),
            "--body",
            body,
            env=self._implementer_auth_environment(),
        )

    def assign_pull_request(
        self,
        pr_number: int,
        assignees: Sequence[str],
        cwd: Path | None = None,
    ) -> None:
        """Assign one or more GitHub users to a pull request as the implementer."""
        if pr_number <= 0:
            raise ValueError("Pull request number must be positive")
        if not assignees or any(not assignee.strip() for assignee in assignees):
            raise ValueError("At least one non-empty pull request assignee is required")

        logger.info("[github] Assigning pull request #%s", pr_number)
        arguments = ["pr", "edit", str(pr_number)]
        for assignee in assignees:
            arguments.extend(("--add-assignee", assignee))
        self._run(
            *arguments,
            env=self._implementer_auth_environment(),
            cwd=cwd,
        )

    def assign_implementer(self, pr_number: int, cwd: Path | None = None) -> str | None:
        """Assign the configured implementer App bot to a pull request, if present."""
        if self._implementer_authenticator is None:
            return None
        bot_name, _ = self._implementer_authenticator.bot_identity()
        self.assign_pull_request(pr_number, (bot_name,), cwd=cwd)
        return bot_name

    def _review_auth_environment(self) -> dict[str, str] | None:
        return self._authentication_environment(self._app_authenticator)

    def _implementer_auth_environment(self) -> dict[str, str] | None:
        return self._authentication_environment(self._implementer_authenticator)

    @staticmethod
    def _authentication_environment(
        authenticator: GitHubAppAuthenticator | None,
    ) -> dict[str, str] | None:
        if authenticator is None:
            return None
        environment = dict(os.environ)
        environment["GH_TOKEN"] = authenticator.token()
        return environment

    def _run(
        self,
        *arguments: str,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> CommandResult:
        result = self._commands.run(
            [*self._command, *arguments],
            cwd=cwd or self._path,
            timeout_seconds=self._timeout_seconds,
            env=env,
        )
        if not result.success:
            detail = result.stderr.strip() or result.stdout.strip()
            raise GitHubClientError(f"GitHub CLI failed: {redact_text(detail)}")
        return result
