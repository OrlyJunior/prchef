from __future__ import annotations

import json
from pathlib import Path

import pytest

from prchef.clients.codex_client import CodexClient
from prchef.clients.command_client import CommandResult
from prchef.clients.exceptions import (
    CommandInterruptedError,
    CommandProcessTreeUnconfirmedError,
    CommandTimeoutError,
)
from prchef.models.agent import AgentRequest, AgentRole
from prchef.models.execution_diagnostic import DiagnosticCategory
from prchef.providers.agent_provider import AgentProviderError
from prchef.providers.codex_provider import CodexAgentProvider
from prchef.providers.diagnostic_provider import DiagnosticAgentProvider

CAPTURE = """{"type":"thread.started","thread_id":"01a18f2a-bb54-75d3-b556-84be340cb06a"}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"ok"}}
{"type":"turn.completed","usage":{"input_tokens":17372,"cached_input_tokens":13312,"cache_write_input_tokens":0,"output_tokens":5,"reasoning_output_tokens":0}}
"""
USAGE = {
    "input_tokens": 17372,
    "cached_input_tokens": 13312,
    "cache_write_input_tokens": 0,
    "output_tokens": 5,
    "reasoning_output_tokens": 0,
}


class Commands:
    def __init__(self, output: str, exit_code: int = 0, error: Exception | None = None):
        self.output, self.exit_code, self.error = output, exit_code, error

    def run(self, *args, **kwargs):
        if self.error:
            raise self.error
        return CommandResult(
            command=[], exit_code=self.exit_code, stdout=self.output, stderr="", duration_seconds=0
        )


def wrapped(commands: Commands, events: list, **kwargs):
    client = CodexClient(commands, ["codex", "exec"], 10)  # type: ignore[arg-type]
    return DiagnosticAgentProvider(
        CodexAgentProvider(client),
        role=AgentRole.IMPLEMENTER,
        provider_config={},
        record=lambda *event: events.append(event),
        **kwargs,
    )


def request(path: Path) -> AgentRequest:
    return AgentRequest(title="test", instructions="test", workspace_path=path)


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
