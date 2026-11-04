from __future__ import annotations

import json
from pathlib import Path

import pytest

from prchef.clients.codex_client import CodexClient
from prchef.models.agent import AgentRole
from prchef.models.execution_diagnostic import DiagnosticCategory
from prchef.providers.agent_provider import AgentProviderError
from prchef.providers.codex_provider import CodexAgentProvider
from prchef.providers.diagnostic_provider import DiagnosticAgentProvider
from tests.codex_usage_fixtures import Commands, request

DISCONNECT = "stream disconnected before completion: Unable to verify model access right now. Please retry."  # noqa: E501
HEADER = '{"type":"thread.started","thread_id":"thread-1"}\n{"type":"turn.started"}\n'
FALLBACK = "Falling back from WebSockets to HTTPS transport. ..."
FINAL = '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"ok"}}\n'
COMPLETED = (
    '{"type":"turn.completed","usage":{"input_tokens":16323,"cached_input_tokens":13568,'
    '"cache_write_input_tokens":0,"output_tokens":5,"reasoning_output_tokens":0}}\n'
)
USAGE = {
    "input_tokens": 16323,
    "cached_input_tokens": 13568,
    "cache_write_input_tokens": 0,
    "output_tokens": 5,
    "reasoning_output_tokens": 0,
}


def reconnect(attempt: int) -> str:
    return f"Reconnecting... {attempt}/5 ({DISCONNECT})"


def error_line(message: str) -> str:
    return json.dumps({"type": "error", "message": message}) + "\n"


def turn_failed(message: str) -> str:
    return json.dumps({"type": "turn.failed", "error": {"message": message}}) + "\n"


SUCCESS_NOTICES = [reconnect(attempt) for attempt in (2, 3, 4)]
# Captured from ``codex exec --json``: notices, then a completed turn with a valid answer.
SUCCESS_AFTER_NOTICES = HEADER + "".join(map(error_line, SUCCESS_NOTICES)) + FINAL + COMPLETED

FAILURE_NOTICES = [reconnect(attempt) for attempt in (2, 3, 4, 5, 1, 2, 3, 4, 5)]
# Captured from ``codex exec --json``: retries are exhausted and the turn fails.
FAILURE_AFTER_RETRIES = (
    HEADER
    + "".join(map(error_line, FAILURE_NOTICES))
    + json.dumps(
        {"type": "item.completed", "item": {"id": "item_0", "type": "error", "message": FALLBACK}}
    )
    + "\n"
    + error_line(DISCONNECT)
    + turn_failed(DISCONNECT)
)


def wrapped(commands: Commands, events: list) -> DiagnosticAgentProvider:
    client = CodexClient(commands, ["codex", "exec"], 10)  # type: ignore[arg-type]
    return DiagnosticAgentProvider(
        CodexAgentProvider(client),
        role=AgentRole.IMPLEMENTER,
        provider_config={},
        record=lambda *event: events.append(event),
    )


def categories(events: list) -> list[DiagnosticCategory]:
    return [event[0] for event in events]


def test_reconnect_notices_before_a_valid_final_message_succeed(tmp_path: Path) -> None:
    events: list = []

    result = wrapped(Commands(SUCCESS_AFTER_NOTICES), events).execute(request(tmp_path))

    assert result.success
    assert result.error_message is None
    assert result.output == "ok"
    assert result.metadata == USAGE
    assert result.warnings == tuple(SUCCESS_NOTICES)
    assert categories(events) == [
        DiagnosticCategory.REQUEST,
        DiagnosticCategory.WARNING,
        DiagnosticCategory.RESPONSE,
    ]
    assert events[1][2]["warnings"] == SUCCESS_NOTICES
    assert events[-1][2]["usage"] == USAGE


@pytest.mark.parametrize("exit_code", [1, 0])
def test_exhausted_retries_fail_with_the_terminal_message(tmp_path: Path, exit_code: int) -> None:
    events: list = []

    result = wrapped(Commands(FAILURE_AFTER_RETRIES, exit_code), events).execute(
        request(tmp_path)
    )

    assert not result.success
    assert result.error_message is not None
    assert "event=turn.failed" in result.error_message
    assert f"message={DISCONNECT}" in result.error_message
    assert "Reconnecting" not in result.error_message
    assert "thread.started" not in result.error_message
    assert result.warnings == (*FAILURE_NOTICES, FALLBACK)
    assert categories(events) == [
        DiagnosticCategory.REQUEST,
        DiagnosticCategory.WARNING,
        DiagnosticCategory.RESPONSE,
    ]
    assert events[-1][2]["error"] == result.error_message


@pytest.mark.parametrize("exit_code", [1, 0])
@pytest.mark.parametrize(
    "tail",
    [
        error_line(DISCONNECT) + turn_failed(DISCONNECT),
        turn_failed(DISCONNECT),
        error_line(DISCONNECT),
    ],
)
def test_terminal_error_without_notices_fails(
    tmp_path: Path, exit_code: int, tail: str
) -> None:
    events: list = []

    result = wrapped(Commands(HEADER + tail, exit_code), events).execute(request(tmp_path))

    assert not result.success
    assert result.error_message is not None
    assert f"message={DISCONNECT}" in result.error_message
    assert "thread.started" not in result.error_message
    assert result.warnings == ()
    assert DiagnosticCategory.WARNING not in categories(events)


@pytest.mark.parametrize(
    "stream",
    [
        # No final message.
        HEADER + error_line(reconnect(2)) + COMPLETED,
        # A final message, but the turn never completed.
        HEADER + error_line(reconnect(2)) + FINAL,
        # An empty final message.
        HEADER + error_line(reconnect(2)) + FINAL.replace('"ok"', '"  "') + COMPLETED,
    ],
)
def test_notices_never_hide_a_missing_final_message(tmp_path: Path, stream: str) -> None:
    result = CodexClient(Commands(stream), ["codex", "exec"], 10).implement(  # type: ignore[arg-type]
        "test", tmp_path
    )

    assert not result.success
    assert result.error_message == (
        "Codex did not complete the turn with a valid final message "
        f"(exit 0; last notice: {reconnect(2)})"
    )
    assert result.workspace_changes_may_exist
    assert result.warnings == [reconnect(2)]


@pytest.mark.parametrize("text,success", [("not json", False), ('{"done": true}', True)])
def test_notices_require_a_parseable_structured_final_message(
    tmp_path: Path, text: str, success: bool
) -> None:
    final = json.dumps(
        {"type": "item.completed", "item": {"type": "agent_message", "text": text}}
    )
    stream = HEADER + error_line(reconnect(2)) + final + "\n" + COMPLETED
    result = CodexClient(Commands(stream), ["codex", "exec"], 10).implement(  # type: ignore[arg-type]
        "test", tmp_path, output_schema={"type": "object"}
    )

    assert result.success is success
    assert result.summary == text


def test_valid_final_message_never_hides_turn_failed(tmp_path: Path) -> None:
    stream = SUCCESS_AFTER_NOTICES + turn_failed(DISCONNECT)

    result = CodexClient(Commands(stream), ["codex", "exec"], 10).implement(  # type: ignore[arg-type]
        "test", tmp_path
    )

    assert not result.success
    assert result.error_message is not None
    assert "event=turn.failed" in result.error_message
    assert result.metadata == USAGE


def test_nonzero_exit_without_terminal_event_reports_last_notice(tmp_path: Path) -> None:
    stream = HEADER + error_line(reconnect(2)) + error_line(reconnect(3))

    result = CodexClient(Commands(stream, exit_code=1), ["codex", "exec"], 10).implement(  # type: ignore[arg-type]
        "test", tmp_path
    )

    assert not result.success
    assert result.error_message == (
        "Codex exited with code 1 without a terminal failure event "
        f"(last notice: {reconnect(3)})"
    )
    assert not result.workspace_changes_may_exist


def test_retryable_classification_follows_the_terminal_message(tmp_path: Path) -> None:
    notice = error_line("Reconnecting... 2/5 (rate limit reached)")
    events: list = []

    result = wrapped(Commands(HEADER + notice + FINAL + COMPLETED), events).execute(
        request(tmp_path)
    )
    assert result.success

    stream = HEADER + notice + turn_failed("Rate limit reached for requests")
    with pytest.raises(AgentProviderError) as caught:
        wrapped(Commands(stream, exit_code=1), []).execute(request(tmp_path))
    assert caught.value.category == "rate_limit"
    assert caught.value.metadata == {}
