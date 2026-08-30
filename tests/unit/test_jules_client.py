from __future__ import annotations

import json
import logging
from collections.abc import Callable

import httpx
import pytest

from orchestrator.clients.exceptions import (
    JulesClientError,
    JulesSessionCancelledError,
    JulesSessionFailedError,
    JulesSessionNeedsHumanError,
    JulesSessionTimeoutError,
    JulesTransientPollError,
)
from orchestrator.clients.jules_client import JulesClient
from orchestrator.config.project_config import JulesIntegrationSettings
from orchestrator.models.jules_session import JulesSession, JulesSessionState


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


def _documented_create_session() -> dict[str, object]:
    """The immediate POST example in Jules' official quickstart."""
    return {
        "name": "sessions/session-1",
        "id": "session-1",
        "title": "Orchestrator technical specification",
        "sourceContext": {
            "source": "sources/github-example-api",
            "githubRepoContext": {"startingBranch": "develop"},
        },
        "prompt": "produce JSON",
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
            assert payload["requirePlanApproval"] is False
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

    created: list[JulesSession] = []
    result = _run(_client(handler, clock), created)

    assert result.session.id == "session-1"
    assert result.session.url == "https://jules.google.com/session/session-1"
    assert result.result_text == '{"safe_to_implement": true}'
    assert len(created) == 1
    assert sum(request.method == "POST" for request in requests) == 1


def test_documented_create_payload_without_state_persists_id_before_get_session() -> None:
    clock = FakeClock()
    requests: list[httpx.Request] = []
    created: list[JulesSession] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            assert request.url.path == "/v1alpha/sessions"
            return httpx.Response(200, json=_documented_create_session())
        if request.url.path == "/v1alpha/sessions/session-1":
            assert len(created) == 1
            return httpx.Response(200, json=_session("COMPLETED"))
        assert request.url.path == "/v1alpha/sessions/session-1/activities"
        return httpx.Response(
            200, json={"activities": [{"agentMessaged": {"agentMessage": "done"}}]}
        )

    result = _run(_client(handler, clock), created)

    assert result.result_text == "done"
    assert len(created) == 1
    session = created[0]
    assert session.id == "session-1"
    assert session.url is None
    assert session.state is JulesSessionState.STATE_UNSPECIFIED
    assert [request.url.path for request in requests] == [
        "/v1alpha/sessions",
        "/v1alpha/sessions/session-1",
        "/v1alpha/sessions/session-1/activities",
    ]


def test_get_session_requires_state_and_preserves_created_session_on_none_state() -> None:
    clock = FakeClock()
    post_count = 0
    created: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if request.method == "POST":
            post_count += 1
            return httpx.Response(200, json=_documented_create_session())
        return httpx.Response(
            200,
            json={
                "id": "session-1",
                "name": "sessions/session-1",
                "url": "https://jules.google.com/session/session-1",
                "state": None,
            },
        )

    with pytest.raises(JulesClientError) as error:
        _run(_client(handler, clock), created)

    assert "Get Session response is missing required field 'state'" in str(error.value)
    assert "response keys: id, name, state, url" in str(error.value)
    assert len(created) == 1
    assert post_count == 1


def test_documented_get_session_payload_is_parsed_as_a_session() -> None:
    session = JulesClient._session_from_payload(
        _session("IN_PROGRESS"), response_name="Get Session", require_state=True
    )

    assert session.id == "session-1"
    assert session.state is JulesSessionState.IN_PROGRESS
    assert session.url == "https://jules.google.com/session/session-1"


def test_get_session_with_activity_shape_reports_available_keys() -> None:
    with pytest.raises(JulesClientError) as error:
        JulesClient._session_from_payload(
            {"activities": []}, response_name="Get Session", require_state=True
        )

    assert "missing required field 'id'" in str(error.value)
    assert "response keys: activities" in str(error.value)


def test_poll_logs_state_transitions_without_repeating_unchanged_state(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock()
    states = iter(["IN_PROGRESS", "IN_PROGRESS", "COMPLETED"])
    target_logger = logging.getLogger("orchestrator.clients.jules_client")
    target_logger.addHandler(caplog.handler)
    previous_propagation = target_logger.propagate
    target_logger.propagate = False
    try:
        with caplog.at_level(logging.INFO, logger=target_logger.name):
            _run(
                _client(
                    lambda request: (
                        httpx.Response(200, json=_session("QUEUED"))
                        if request.method == "POST"
                        else (
                            httpx.Response(
                                200,
                                json={
                                    "activities": [
                                        {"agentMessaged": {"agentMessage": "done"}}
                                    ]
                                },
                            )
                            if request.url.path.endswith("/activities")
                            else httpx.Response(200, json=_session(next(states)))
                        )
                    ),
                    clock,
                ),
                [],
            )
    finally:
        target_logger.removeHandler(caplog.handler)
        target_logger.propagate = previous_propagation

    messages = [record.getMessage() for record in caplog.records]
    assert sum(message == "[jules] State: IN_PROGRESS" for message in messages) == 1
    assert "[jules] State changed: IN_PROGRESS -> COMPLETED" in messages
    assert "[jules] State changed: IN_PROGRESS -> IN_PROGRESS" not in messages


def test_poll_logs_heartbeat_for_a_long_unchanged_state(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock()
    states = iter(["IN_PROGRESS", "IN_PROGRESS", "IN_PROGRESS", "COMPLETED"])
    target_logger = logging.getLogger("orchestrator.clients.jules_client")
    target_logger.addHandler(caplog.handler)
    previous_propagation = target_logger.propagate
    target_logger.propagate = False
    try:
        with caplog.at_level(logging.INFO, logger=target_logger.name):
            _run(
                _client(
                    lambda request: (
                        httpx.Response(200, json=_session("QUEUED"))
                        if request.method == "POST"
                        else (
                            httpx.Response(
                                200,
                                json={
                                    "activities": [
                                        {"agentMessaged": {"agentMessage": "done"}}
                                    ]
                                },
                            )
                            if request.url.path.endswith("/activities")
                            else httpx.Response(200, json=_session(next(states)))
                        )
                    ),
                    clock,
                    poll_interval_seconds=30,
                    timeout_seconds=200,
                ),
                [],
            )
    finally:
        target_logger.removeHandler(caplog.handler)
        target_logger.propagate = previous_propagation

    assert any(
        "[jules] Still IN_PROGRESS after 1m" in record.getMessage()
        for record in caplog.records
    )


def test_poll_logs_no_jules_api_key_from_transient_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock()
    target_logger = logging.getLogger("orchestrator.clients.jules_client")
    target_logger.addHandler(caplog.handler)
    previous_propagation = target_logger.propagate
    target_logger.propagate = False
    try:
        with caplog.at_level(logging.DEBUG, logger=target_logger.name):
            with pytest.raises(JulesTransientPollError):
                _run(
                    _client(
                        lambda request: (
                            httpx.Response(200, json=_session("QUEUED"))
                            if request.method == "POST"
                            else httpx.Response(
                                503, text="JULES_API_KEY=super-secret-value"
                            )
                        ),
                        clock,
                        poll_retry_attempts=1,
                    ),
                    [],
                )
    finally:
        target_logger.removeHandler(caplog.handler)
        target_logger.propagate = previous_propagation

    assert "super-secret-value" not in caplog.text


def test_persists_session_before_the_first_poll() -> None:
    clock = FakeClock()
    persisted_session_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=_session("QUEUED"))
        assert persisted_session_ids == ["session-1"]
        if request.url.path.endswith("/activities"):
            return httpx.Response(
                200, json={"activities": [{"agentMessaged": {"agentMessage": "done"}}]}
            )
        return httpx.Response(200, json=_session("COMPLETED"))

    _client(handler, clock).generate_spec(
        "produce JSON",
        "example/api",
        "develop",
        lambda session: persisted_session_ids.append(session.id),
    )


def test_review_uses_feature_branch_and_never_enables_auto_create_pr() -> None:
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            payload = json.loads(request.content)
            context = payload["sourceContext"]["githubRepoContext"]
            assert context["startingBranch"] == "orchestrator/42"
            assert payload["automationMode"] != "AUTO_CREATE_PR"
            return httpx.Response(200, json=_session("QUEUED"))
        if request.url.path.endswith("/activities"):
            return httpx.Response(
                200, json={"activities": [{"agentMessaged": {"agentMessage": "done"}}]}
            )
        return httpx.Response(200, json=_session("COMPLETED"))

    _client(handler, clock).review(
        "review", "example/api", "orchestrator/42", lambda session: None
    )


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


@pytest.mark.parametrize(
    ("state", "message"),
    [
        ("AWAITING_USER_FEEDBACK", "awaiting user feedback"),
        ("PAUSED", "paused"),
        ("AWAITING_PLAN_APPROVAL", "awaiting plan approval"),
    ],
)
def test_human_intervention_states_stop_polling_immediately(
    state: str, message: str
) -> None:
    clock = FakeClock()
    get_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_count
        if request.method == "POST":
            return httpx.Response(200, json=_session("QUEUED"))
        get_count += 1
        return httpx.Response(200, json=_session(state))

    created: list[object] = []
    with pytest.raises(JulesSessionNeedsHumanError) as error:
        _run(_client(handler, clock), created)

    assert error.value.session_id == "session-1"
    assert message in str(error.value)
    assert len(created) == 1
    assert get_count == 1
    assert clock.value == 0


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
