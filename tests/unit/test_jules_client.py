from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from orchestrator.clients.exceptions import (
    JulesSessionCancelledError,
    JulesSessionFailedError,
    JulesSessionTimeoutError,
    JulesTransientPollError,
)
from orchestrator.clients.jules_client import JulesClient
from orchestrator.config.project_config import JulesIntegrationSettings


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    clock: FakeClock,
    **settings: object,
) -> JulesClient:
    options = {
        "source": "sources/github-example-api",
        "poll_interval_seconds": 1,
        "timeout_seconds": 10,
        "poll_retry_attempts": 2,
        "poll_retry_backoff_seconds": 0.25,
    }
    options.update(settings)
    config = JulesIntegrationSettings.model_validate(options)
    http_client = httpx.Client(
        base_url="https://jules.googleapis.com/v1alpha",
        transport=httpx.MockTransport(handler),
    )
    return JulesClient(
        "test-key", config, http_client=http_client, clock=clock.now, sleep=clock.sleep
    )


def _session(state: str) -> dict[str, str]:
    return {
        "id": "session-1",
        "name": "sessions/session-1",
        "url": "https://jules.google.com/session/session-1",
        "state": state,
    }


def _run(client: JulesClient, created: list[object]):
    return client.generate_spec(
        "produce JSON",
        "example/api",
        "develop",
        created.append,
    )


def test_creates_session_polls_to_completed_and_recovers_final_result() -> None:
    clock = FakeClock()
    states = iter(["IN_PROGRESS", "COMPLETED"])
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            payload = json.loads(request.content)
            assert payload["sourceContext"]["githubRepoContext"] == {
                "startingBranch": "develop"
            }
            assert payload["automationMode"] == "AUTOMATION_MODE_UNSPECIFIED"
            return httpx.Response(200, json=_session("QUEUED"))
        if request.url.path.endswith("/activities"):
            return httpx.Response(
                200,
                json={
                    "activities": [
                        {"agentMessaged": {"agentMessage": '{"safe_to_implement": true}'}}
                    ]
                },
            )
        return httpx.Response(200, json=_session(next(states)))

    created: list[object] = []
    result = _run(_client(handler, clock), created)

    assert result.session.id == "session-1"
    assert result.session.url == "https://jules.google.com/session/session-1"
    assert result.result_text == '{"safe_to_implement": true}'
    assert len(created) == 1
    assert sum(request.method == "POST" for request in requests) == 1


def test_failed_session_preserves_created_session_and_raises() -> None:
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        state = "QUEUED" if request.method == "POST" else "FAILED"
        return httpx.Response(200, json=_session(state))

    created: list[object] = []
    with pytest.raises(JulesSessionFailedError) as error:
        _run(_client(handler, clock), created)

    assert error.value.session_id == "session-1"
    assert len(created) == 1


def test_cancelled_session_preserves_created_session_and_raises() -> None:
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        state = "QUEUED" if request.method == "POST" else "CANCELLED"
        return httpx.Response(200, json=_session(state))

    created: list[object] = []
    with pytest.raises(JulesSessionCancelledError) as error:
        _run(_client(handler, clock), created)

    assert error.value.session_id == "session-1"
    assert len(created) == 1


def test_timeout_does_not_create_a_second_session() -> None:
    clock = FakeClock()
    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if request.method == "POST":
            post_count += 1
            return httpx.Response(200, json=_session("QUEUED"))
        return httpx.Response(200, json=_session("IN_PROGRESS"))

    with pytest.raises(JulesSessionTimeoutError) as error:
        _run(_client(handler, clock, timeout_seconds=2), [])

    assert error.value.session_id == "session-1"
    assert post_count == 1


def test_transient_poll_error_retries_then_succeeds_without_new_session() -> None:
    clock = FakeClock()
    poll_count = 0
    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count, post_count
        if request.method == "POST":
            post_count += 1
            return httpx.Response(200, json=_session("QUEUED"))
        if request.url.path.endswith("/activities"):
            return httpx.Response(
                200, json={"activities": [{"agentMessaged": {"agentMessage": "ok"}}]}
            )
        poll_count += 1
        if poll_count == 1:
            return httpx.Response(503, text="temporary")
        return httpx.Response(200, json=_session("COMPLETED"))

    result = _run(_client(handler, clock), [])

    assert result.result_text == "ok"
    assert poll_count == 2
    assert post_count == 1


def test_exhausted_transient_poll_errors_do_not_create_a_second_session() -> None:
    clock = FakeClock()
    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if request.method == "POST":
            post_count += 1
            return httpx.Response(200, json=_session("QUEUED"))
        return httpx.Response(503, text="temporary")

    with pytest.raises(JulesTransientPollError) as error:
        _run(_client(handler, clock, poll_retry_attempts=1), [])

    assert error.value.session_id == "session-1"
    assert post_count == 1
