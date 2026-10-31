from __future__ import annotations

import json
from pathlib import Path

import pytest

from prchef.clients.codex_client import CodexClient
from prchef.clients.exceptions import (
    CommandInterruptedError,
    CommandProcessTreeUnconfirmedError,
    CommandTimeoutError,
)
from prchef.models.agent import AgentRole
from prchef.models.execution_diagnostic import DiagnosticCategory
from prchef.providers.agent_provider import AgentProviderError
from prchef.providers.codex_provider import CodexAgentProvider
from prchef.providers.diagnostic_provider import DiagnosticAgentProvider
from tests.codex_usage_fixtures import CAPTURE, USAGE, Commands, request


def wrapped(commands: Commands, events: list, **kwargs):
    client = CodexClient(commands, ["codex", "exec"], 10)  # type: ignore[arg-type]
    return DiagnosticAgentProvider(
        CodexAgentProvider(client),
        role=AgentRole.IMPLEMENTER,
        provider_config={},
        record=lambda *event: events.append(event),
        **kwargs,
    )



def test_captured_stream_through_diagnostics(tmp_path: Path) -> None:
    events: list = []
    result = wrapped(Commands(CAPTURE), events).execute(request(tmp_path))
    assert result.output == "ok"
    assert result.metadata == USAGE
    assert events[-1][2]["usage"] == USAGE
    assert events[-1][2]["provider_metadata"] == USAGE


def test_multiple_turns_and_invalid_fields() -> None:
    turn = CAPTURE.splitlines()[-1]
    partial = json.dumps(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 3,
                "output_tokens": True,
                "cached_input_tokens": -1,
                "cache_write_input_tokens": 1.5,
                "reasoning_output_tokens": "4",
                "extra": 42,
            },
        }
    )
    stream = CAPTURE + turn + "\n" + partial + '\n{"type":"turn.completed","usage":null}\n{broken'
    assert CodexClient._structured_execution_signals(stream).usage == {
        "input_tokens": 34747,
        "cached_input_tokens": 26624,
        "cache_write_input_tokens": 0,
        "output_tokens": 10,
        "reasoning_output_tokens": 0,
    }
    no_usage = CAPTURE.replace(turn, '{"type":"turn.completed"}')
    assert CodexClient._structured_execution_signals(no_usage).usage == {}


@pytest.mark.parametrize(
    "exit_code,tail", [(1, ""), (0, '\n{"type":"turn.failed","error":{"message":"failed"}}')]
)
def test_failed_response_keeps_usage(tmp_path: Path, exit_code: int, tail: str) -> None:
    events: list = []
    result = wrapped(Commands(CAPTURE + tail, exit_code), events).execute(request(tmp_path))
    assert not result.success
    assert events[-1][2]["usage"] == USAGE


def test_retryable_failure_keeps_usage(tmp_path: Path) -> None:
    events: list = []
    stream = CAPTURE + '\n{"type":"error","message":"quota exhausted"}'
    with pytest.raises(AgentProviderError) as caught:
        wrapped(Commands(stream), events).execute(request(tmp_path))
    assert caught.value.category == "quota"
    assert events[-1][2]["provider_metadata"] == USAGE


@pytest.mark.parametrize(
    "error",
    [
        CommandTimeoutError("timeout", timeout_seconds=1, stdout=CAPTURE),
        CommandInterruptedError("interrupted"),
        CommandProcessTreeUnconfirmedError("unconfirmed"),
    ],
)
def test_partial_output_error_keeps_usage(tmp_path: Path, error: Exception) -> None:
    error.stdout = CAPTURE.encode()  # type: ignore[attr-defined]
    events: list = []
    with pytest.raises(type(error)) as caught:
        wrapped(Commands("", error=error), events).execute(request(tmp_path))
    assert caught.value is error
    assert events[-1][2]["provider_metadata"] == USAGE


def test_cancelled_result_records_error_only_when_enabled(tmp_path: Path) -> None:
    def cancel() -> None:
        raise RuntimeError("cancelled")

    for enabled in (False, True):
        events: list = []
        with pytest.raises(RuntimeError, match="cancelled"):
            wrapped(
                Commands(CAPTURE), events, cancellation_check=cancel, retain_cancelled_usage=enabled
            ).execute(request(tmp_path))
        assert [event[0] for event in events] == (
            [DiagnosticCategory.REQUEST, DiagnosticCategory.ERROR]
            if enabled
            else [DiagnosticCategory.REQUEST]
        )
        if enabled:
            assert events[-1][2]["provider_metadata"] == USAGE

@pytest.mark.parametrize("invalid", [None, "1", 1.5, True, -1, {"nested": 1}, [1]])
def test_invalid_usage_values_keep_valid_siblings(invalid: object) -> None:
    event = json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": invalid, "output_tokens": 2, "future_tokens": 99,
    }})
    stream = CAPTURE + event + '\n{"type":"turn.completed","usage":'
    assert CodexClient._structured_execution_signals(stream).usage == {
        **USAGE, "output_tokens": 7,
    }


@pytest.mark.parametrize("tail", [
    '{"type":"turn.failed","error":{"message":"failed"}}',
    '{"type":"error","message":"failed"}',
])
def test_client_nonzero_exit_after_structured_failure_keeps_usage(
    tmp_path: Path, tail: str
) -> None:
    client = CodexClient(Commands(CAPTURE + tail, exit_code=1), ["codex", "exec"], 10)  # type: ignore[arg-type]
    result = client.implement("test", tmp_path)
    assert not result.success
    assert result.metadata == USAGE
