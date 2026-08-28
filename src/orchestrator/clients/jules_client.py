from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from orchestrator.clients.exceptions import (
    JulesClientError,
    JulesSessionCancelledError,
    JulesSessionFailedError,
    JulesSessionTimeoutError,
    JulesTransientPollError,
)
from orchestrator.config.project_config import JulesIntegrationSettings
from orchestrator.models.jules_session import (
    JulesOperationResult,
    JulesSession,
    JulesSessionState,
)


class JulesClient:
    """Official Jules REST session lifecycle, using only documented endpoints."""

    _TERMINAL_STATES = {
        JulesSessionState.COMPLETED,
        JulesSessionState.FAILED,
        JulesSessionState.CANCELLED,
    }

    def __init__(
        self,
        api_key: str | None,
        settings: JulesIntegrationSettings,
        *,
        http_client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise JulesClientError(
                "Jules API key is required for the documented asynchronous session API; "
                "set JULES_API_KEY or ORCHESTRATOR_JULES_API_KEY"
            )
        self._settings = settings
        self._http = http_client or httpx.Client(
            base_url="https://jules.googleapis.com/v1alpha",
            headers={"x-goog-api-key": api_key},
            timeout=30.0,
        )
        self._clock = clock
        self._sleep = sleep

    def generate_spec(
        self,
        prompt: str,
        repository_slug: str,
        base_branch: str,
        on_session_created: Callable[[JulesSession], None],
    ) -> JulesOperationResult:
        return self._run_operation(
            prompt,
            "Orchestrator technical specification",
            repository_slug,
            base_branch,
            on_session_created,
        )

    def review(
        self,
        prompt: str,
        repository_slug: str,
        base_branch: str,
        on_session_created: Callable[[JulesSession], None],
    ) -> JulesOperationResult:
        return self._run_operation(
            prompt,
            "Orchestrator code review",
            repository_slug,
            base_branch,
            on_session_created,
        )

    def _run_operation(
        self,
        prompt: str,
        title: str,
        repository_slug: str,
        base_branch: str,
        on_session_created: Callable[[JulesSession], None],
    ) -> JulesOperationResult:
        deadline = self._clock() + self._settings.timeout_seconds
        source = self._settings.source or self._resolve_source(repository_slug)
        session = self._create_session(prompt, title, source, base_branch)
        on_session_created(session)
        completed = self._wait_for_session(session, deadline)
        return JulesOperationResult(completed, self._get_session_result(completed, deadline))

    def _resolve_source(self, repository_slug: str) -> str:
        page_token: str | None = None
        while True:
            params: dict[str, str | int] = {"pageSize": 100}
            if page_token:
                params["pageToken"] = page_token
            payload = self._request("GET", "/sources", params=params)
            sources = payload.get("sources", [])
            if not isinstance(sources, list):
                raise JulesClientError("Jules sources response is invalid")
            for source in sources:
                if not isinstance(source, dict):
                    continue
                github = source.get("githubRepo", {})
                if not isinstance(github, dict):
                    continue
                slug = f"{github.get('owner', '')}/{github.get('repo', '')}".casefold()
                source_name = source.get("name")
                if slug == repository_slug.casefold() and isinstance(source_name, str):
                    return source_name
            page_token = payload.get("nextPageToken")
            if not isinstance(page_token, str) or not page_token:
                break
        raise JulesClientError(
            f"No Jules source is connected for GitHub repository {repository_slug}"
        )

    def _create_session(
        self, prompt: str, title: str, source: str, base_branch: str
    ) -> JulesSession:
        payload = self._request(
            "POST",
            "/sessions",
            json_body={
                "prompt": prompt,
                "title": title,
                "sourceContext": {
                    "source": source,
                    "githubRepoContext": {"startingBranch": base_branch},
                },
                # The documented unspecified/default mode creates no PR or other
                # automation. The prompts also instruct Jules not to publish.
                "automationMode": "AUTOMATION_MODE_UNSPECIFIED",
                "requirePlanApproval": False,
            },
        )
        return self._session_from_payload(payload)

    def _wait_for_session(self, created: JulesSession, deadline: float) -> JulesSession:
        session = created
        retry_count = 0
        while True:
            self._ensure_before_deadline(deadline, session.id)
            try:
                session = self._get_session_status(session.id)
                retry_count = 0
            except JulesTransientPollError as exc:
                if retry_count >= self._settings.poll_retry_attempts:
                    raise JulesTransientPollError(str(exc), session.id) from exc
                self._sleep_until_deadline(
                    self._settings.poll_retry_backoff_seconds * (2**retry_count),
                    deadline,
                    session.id,
                )
                retry_count += 1
                continue

            if session.state is JulesSessionState.COMPLETED:
                return session
            if session.state is JulesSessionState.FAILED:
                raise JulesSessionFailedError("Jules session failed", session.id)
            if session.state is JulesSessionState.CANCELLED:
                raise JulesSessionCancelledError("Jules session was cancelled", session.id)
            self._sleep_until_deadline(
                self._settings.poll_interval_seconds, deadline, session.id
            )

    def _get_session_status(self, session_id: str) -> JulesSession:
        return self._session_from_payload(
            self._request("GET", f"/sessions/{session_id}", transient=True)
        )

    def _get_session_result(self, session: JulesSession, deadline: float) -> str:
        def fetch() -> list[dict[str, Any]]:
            activities: list[dict[str, Any]] = []
            page_token: str | None = None
            while True:
                self._ensure_before_deadline(deadline, session.id)
                params: dict[str, str | int] = {"pageSize": 100}
                if page_token:
                    params["pageToken"] = page_token
                payload = self._request(
                    "GET", f"/sessions/{session.id}/activities", params=params, transient=True
                )
                values = payload.get("activities", [])
                if not isinstance(values, list):
                    raise JulesClientError("Jules activities response is invalid", session.id)
                activities.extend(value for value in values if isinstance(value, dict))
                page_token = payload.get("nextPageToken")
                if not isinstance(page_token, str) or not page_token:
                    return activities

        activities = self._with_poll_retries(fetch, session.id, deadline)
        messages = [
            activity.get("agentMessaged", {}).get("agentMessage")
            for activity in activities
            if isinstance(activity.get("agentMessaged"), dict)
        ]
        text_messages = [
            message for message in messages if isinstance(message, str) and message.strip()
        ]
        if not text_messages:
            raise JulesClientError("Jules completed without an agent message result", session.id)
        return text_messages[-1]

    def _with_poll_retries(
        self, operation: Callable[[], Any], session_id: str, deadline: float
    ) -> Any:
        for attempt in range(self._settings.poll_retry_attempts + 1):
            self._ensure_before_deadline(deadline, session_id)
            try:
                return operation()
            except JulesTransientPollError as exc:
                if attempt >= self._settings.poll_retry_attempts:
                    raise JulesTransientPollError(str(exc), session_id) from exc
                self._sleep_until_deadline(
                    self._settings.poll_retry_backoff_seconds * (2**attempt),
                    deadline,
                    session_id,
                )
        raise AssertionError("unreachable")

    def _ensure_before_deadline(self, deadline: float, session_id: str) -> None:
        if self._clock() >= deadline:
            raise JulesSessionTimeoutError(
                f"Jules session timed out after {self._settings.timeout_seconds}s", session_id
            )

    def _sleep_until_deadline(self, seconds: float, deadline: float, session_id: str) -> None:
        self._ensure_before_deadline(deadline, session_id)
        self._sleep(min(seconds, deadline - self._clock()))

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str | int] | None = None,
        transient: bool = False,
    ) -> dict[str, Any]:
        try:
            response = self._http.request(method, path, json=json_body, params=params)
        except httpx.RequestError as exc:
            if transient:
                raise JulesTransientPollError(f"Transient Jules request failure: {exc}") from exc
            raise JulesClientError(f"Jules request failed: {exc}") from exc
        if response.status_code in {408, 429} or response.status_code >= 500:
            message = f"Jules returned HTTP {response.status_code}: {response.text}"
            if transient:
                raise JulesTransientPollError(message)
            raise JulesClientError(message)
        if response.is_error:
            raise JulesClientError(f"Jules returned HTTP {response.status_code}: {response.text}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise JulesClientError("Jules returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise JulesClientError("Jules response must be a JSON object")
        return payload

    @staticmethod
    def _session_from_payload(payload: dict[str, Any]) -> JulesSession:
        session_id = payload.get("id")
        name = payload.get("name")
        if not isinstance(session_id, str) or not session_id:
            if isinstance(name, str) and name.startswith("sessions/"):
                session_id = name.removeprefix("sessions/")
            else:
                raise JulesClientError("Jules session response has no id")
        raw_state = payload.get("state")
        try:
            state = JulesSessionState(str(raw_state))
        except ValueError as exc:
            raise JulesClientError(
                f"Unsupported Jules session state: {raw_state}", session_id
            ) from exc
        url = payload.get("url")
        return JulesSession(
            id=session_id,
            url=url if isinstance(url, str) else None,
            state=state,
            resource_name=name if isinstance(name, str) else None,
        )
