from __future__ import annotations

import json
import logging
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from orchestrator.clients.command_client import CommandClient
from orchestrator.models.implementation_result import ImplementationResult
from orchestrator.runtime_logging import format_duration, summarize_text

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _StructuredFailure:
    event_type: str
    code: str | None = None
    message: str | None = None
    operation: str | None = None
    path: str | None = None
    command: str | None = None


@dataclass(frozen=True, slots=True)
class _CodexExecutionSignals:
    file_change_count: int = 0
    failed_command_count: int = 0
    turn_completed: bool = False
    final_message: str | None = None
    terminal_failure: _StructuredFailure | None = None
    sandbox_denial: _StructuredFailure | None = None


class CodexClient:
    def __init__(
        self,
        command_client: CommandClient,
        command: Sequence[str],
        timeout_seconds: int,
        approval_policy: Literal["approve-for-me"] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        provider_name: str = "codex",
    ) -> None:
        self._commands = command_client
        self._command = list(command)
        self._timeout_seconds = timeout_seconds
        self._approval_policy = approval_policy
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._provider_name = provider_name
        command_model = self._command_model(self._command)
        if (
            model is not None
            and self._has_command_model_flag(self._command)
            and command_model is None
        ):
            raise ValueError("Codex command has a model flag without a value")
        if model is not None and command_model is not None and command_model != model:
            raise ValueError(
                "Codex command contains a model flag that conflicts with the configured model"
            )
        command_reasoning_effort = self._command_reasoning_effort(self._command)
        if (
            reasoning_effort is not None
            and self._has_command_reasoning_effort(self._command)
            and command_reasoning_effort is None
        ):
            raise ValueError("Codex command has model_reasoning_effort without a value")
        if (
            reasoning_effort is not None
            and command_reasoning_effort is not None
            and command_reasoning_effort != reasoning_effort
        ):
            raise ValueError(
                "Codex command contains a reasoning effort that conflicts with the configured "
                "reasoning effort"
            )

    def implement(
        self,
        prompt: str,
        repository_path: Path,
        output_schema: Mapping[str, Any] | None = None,
    ) -> ImplementationResult:
        with tempfile.TemporaryDirectory(prefix="orchestrator-codex-") as temp_root:
            output_file = Path(temp_root) / "last-message.txt"
            schema_file: Path | None = None
            if output_schema is not None:
                schema_file = Path(temp_root) / "output-schema.json"
                schema_file.write_text(
                    json.dumps(dict(output_schema), ensure_ascii=False),
                    encoding="utf-8",
                )
            command = self._execution_command(output_file, schema_file)
            logger.info("[codex] Starting implementation")
            logger.debug(
                "[codex] Starting provider=%s model=%s reasoning_effort=%s",
                self._provider_name,
                self._model or "<external-default>",
                self._reasoning_effort or "<external-default>",
            )
            started = time.monotonic()
            try:
                result = self._commands.run(
                    command,
                    cwd=repository_path,
                    input_text=prompt,
                    timeout_seconds=self._timeout_seconds,
                    on_progress=lambda elapsed: logger.info(
                        "[codex] Still running after %s", format_duration(elapsed)
                    ),
                    heartbeat_interval_seconds=60.0,
                )
            except Exception as exc:
                logger.error(
                    "[codex] Failed (%s after %s)",
                    type(exc).__name__,
                    format_duration(time.monotonic() - started),
                )
                raise
            signals = self._structured_execution_signals(result.stdout)
            summary = self._summary(output_file, result.stdout, signals)
            failure = signals.sandbox_denial or (
                signals.terminal_failure if result.success else None
            )
            error_message = (
                self._structured_failure_message(
                    failure,
                    result.exit_code,
                    sandbox_denial=failure is signals.sandbox_denial,
                )
                if failure is not None
                else None
            )
            logger.debug(
                "[codex] Structured events: file_changes=%s failed_commands=%s terminal=%s",
                signals.file_change_count,
                signals.failed_command_count,
                "failed"
                if signals.terminal_failure is not None
                else "completed"
                if signals.turn_completed
                else "unknown",
            )
            if result.success and failure is None:
                logger.info("[codex] Completed in %s", format_duration(result.duration_seconds))
            elif failure is not None:
                logger.error("[codex] %s", error_message)
            else:
                detail = summarize_text(result.stderr or result.stdout)
                logger.error(
                    "[codex] Failed (exit %s, %s): %s",
                    result.exit_code,
                    format_duration(result.duration_seconds),
                    detail or "no output",
                )
            return ImplementationResult(
                success=result.success and failure is None,
                summary=summary,
                error_message=None
                if result.success and failure is None
                else (
                    error_message
                    or summarize_text(result.stderr or result.stdout, limit=2_000)
                    or "Codex failed"
                ),
                workspace_changes_may_exist=failure is not None,
            )

    @staticmethod
    def _structured_execution_signals(stdout: str) -> _CodexExecutionSignals:
        """Read Codex JSONL events without interpreting command output as an error."""
        file_change_count = 0
        failed_command_count = 0
        turn_completed = False
        latest_final_message: str | None = None
        terminal_failure: _StructuredFailure | None = None
        sandbox_denial: _StructuredFailure | None = None

        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue

            event_type = CodexClient._string_field(event, "type")
            if event_type == "turn.completed":
                turn_completed = True
            item = event.get("item")
            item_mapping = item if isinstance(item, dict) else None
            item_type = CodexClient._string_field(item_mapping, "type")
            item_status = CodexClient._string_field(item_mapping, "status")

            if event_type == "item.completed" and item_type == "file_change":
                file_change_count += 1
            final_message = (
                CodexClient._string_field(item_mapping, "text")
                if event_type == "item.completed" and item_type == "agent_message"
                else None
            )
            if final_message is not None:
                latest_final_message = final_message
            if event_type == "item.failed" and item_type == "command_execution":
                failed_command_count += 1

            failure_payload: Mapping[str, object] | None = None
            if event_type in {"error", "turn.failed"}:
                failure_payload = event
            elif event_type == "item.failed" or item_status == "failed":
                failure_payload = item_mapping or event
            if failure_payload is None:
                continue

            failure = CodexClient._structured_failure(event_type or "unknown", failure_payload)
            if CodexClient._is_structured_sandbox_denial(failure):
                sandbox_denial = sandbox_denial or failure
            if event_type in {"error", "turn.failed"}:
                terminal_failure = terminal_failure or failure

        return _CodexExecutionSignals(
            file_change_count=file_change_count,
            failed_command_count=failed_command_count,
            turn_completed=turn_completed,
            final_message=latest_final_message,
            terminal_failure=terminal_failure,
            sandbox_denial=sandbox_denial,
        )

    @staticmethod
    def _summary(
        output_file: Path, stdout: str, signals: _CodexExecutionSignals
    ) -> str:
        if output_file.exists():
            return output_file.read_text(encoding="utf-8", errors="replace").strip()
        if signals.final_message is not None:
            return signals.final_message
        return summarize_text(stdout, limit=2_000)

    @staticmethod
    def _structured_failure(
        event_type: str, payload: Mapping[str, object]
    ) -> _StructuredFailure:
        error = payload.get("error")
        error_mapping = error if isinstance(error, dict) else None
        mappings = CodexClient._failure_mappings(error_mapping, payload)
        code = CodexClient._first_string(mappings, "code", "kind")
        if code is None:
            code = CodexClient._first_string(mappings, "type")
        message = CodexClient._first_string(mappings, "message", "detail", "reason")
        if message is None and isinstance(error, str):
            message = error
        operation = CodexClient._first_string(
            mappings, "operation", "action", "capability"
        )
        path = CodexClient._first_string(
            mappings,
            "path",
            "target_path",
            "file_path",
            "resource_path",
            "target",
        )
        command = CodexClient._first_command(mappings, "command", "cmd", "argv")
        return _StructuredFailure(
            event_type=event_type,
            code=code,
            message=message,
            operation=operation,
            path=path,
            command=command,
        )

    @staticmethod
    def _failure_mappings(
        *roots: Mapping[str, object] | None,
    ) -> tuple[Mapping[str, object], ...]:
        """Collect structured failure fields from version-varying Codex event shapes."""
        mappings: list[Mapping[str, object]] = []
        seen: set[int] = set()

        def visit(value: object) -> None:
            if isinstance(value, Mapping):
                marker = id(value)
                if marker in seen:
                    return
                seen.add(marker)
                mappings.append(value)
                for key in ("error", "details", "metadata", "context", "data"):
                    nested_value = value.get(key)
                    if nested_value is not None:
                        visit(nested_value)
            elif isinstance(value, list):
                for nested_value in value:
                    visit(nested_value)

        for root in roots:
            if root is not None:
                visit(root)
        return tuple(mappings)

    @staticmethod
    def _first_string(
        mappings: Sequence[Mapping[str, object]], *keys: str
    ) -> str | None:
        for mapping in mappings:
            for key in keys:
                value = CodexClient._string_field(mapping, key)
                if value is not None:
                    return value
        return None

    @staticmethod
    def _first_command(
        mappings: Sequence[Mapping[str, object]], *keys: str
    ) -> str | None:
        for mapping in mappings:
            for key in keys:
                value = mapping.get(key)
                if isinstance(value, str):
                    stripped = value.strip()
                    if stripped:
                        return stripped
                elif isinstance(value, list):
                    command_parts: list[str] = []
                    for part in value:
                        if not isinstance(part, str):
                            break
                        if part.strip():
                            command_parts.append(part)
                    else:
                        command = " ".join(command_parts).strip()
                        if command:
                            return command
        return None

    @staticmethod
    def _string_field(mapping: Mapping[str, object] | None, key: str) -> str | None:
        if mapping is None:
            return None
        value = mapping.get(key)
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        return stripped or None

    @staticmethod
    def _is_structured_sandbox_denial(failure: _StructuredFailure) -> bool:
        text = " ".join(
            value for value in (failure.code, failure.message) if value is not None
        ).casefold()
        if "policy_denied" in text:
            return True
        return "sandbox" in text and any(
            marker in text
            for marker in ("denied", "not permitted", "permission denied", "forbidden")
        )

    @staticmethod
    def _structured_failure_message(
        failure: _StructuredFailure, exit_code: int, *, sandbox_denial: bool
    ) -> str:
        details = [f"event={failure.event_type}"]
        if failure.code is not None:
            details.append(f"code={failure.code}")
        if failure.operation is not None:
            details.append(f"operation={summarize_text(failure.operation, limit=160)}")
        if failure.path is not None:
            details.append(f"path={summarize_text(failure.path, limit=240)}")
        if failure.command is not None:
            details.append(f"command={summarize_text(failure.command, limit=320)}")
        if failure.message is not None:
            details.append(f"message={summarize_text(failure.message, limit=320)}")
        if sandbox_denial:
            specific_details = [
                detail
                for detail in details
                if detail.startswith(("operation=", "path=", "command="))
            ]
            category = (
                f"sandbox denial for {', '.join(specific_details)}"
                if specific_details
                else "workspace write denial"
            )
        else:
            category = "execution failure"
        return (
            f"Codex reported a structured {category} "
            f"(exit {exit_code}; {'; '.join(details)})"
        )

    def _execution_command(
        self, output_file: Path, output_schema_file: Path | None = None
    ) -> list[str]:
        command = [part.replace("{output_file}", str(output_file)) for part in self._command]
        runtime_arguments: list[str] = []
        if self._model is not None and self._command_model(command) is None:
            runtime_arguments.extend(("--model", self._model))
        if (
            self._reasoning_effort is not None
            and self._command_reasoning_effort(command) is None
        ):
            runtime_arguments.extend(
                ("--config", f'model_reasoning_effort="{self._reasoning_effort}"')
            )
        if output_schema_file is not None:
            runtime_arguments.extend(("--output-schema", str(output_schema_file)))
        insert_at = self._runtime_insert_index(command)
        command[insert_at:insert_at] = runtime_arguments
        command = self._with_json_events(command)
        if self._approval_policy is None:
            return command

        command = self._without_explicit_sandbox(command)
        if "--approve-for-me" not in command:
            command.insert(min(2, len(command)), "--approve-for-me")
        return command

    @staticmethod
    def _with_json_events(command: list[str]) -> list[str]:
        if "exec" not in command or "--json" in command:
            return command
        command.insert(CodexClient._runtime_insert_index(command), "--json")
        return command

    @staticmethod
    def _command_model(command: Sequence[str]) -> str | None:
        for index, argument in enumerate(command):
            if argument in {"--model", "-m"}:
                return command[index + 1] if index + 1 < len(command) else None
            if argument.startswith("--model="):
                return argument.partition("=")[2] or None
        return None

    @staticmethod
    def _has_command_model_flag(command: Sequence[str]) -> bool:
        return any(
            argument in {"--model", "-m"} or argument.startswith("--model=")
            for argument in command
        )

    @staticmethod
    def _command_reasoning_effort(command: Sequence[str]) -> str | None:
        for index, argument in enumerate(command):
            value: str | None = None
            if argument in {"--config", "-c"} and index + 1 < len(command):
                value = command[index + 1]
            elif argument.startswith("--config="):
                value = argument.partition("=")[2]
            if value is None or "=" not in value:
                continue
            key, configured = value.split("=", maxsplit=1)
            if key.strip() == "model_reasoning_effort":
                normalized = configured.strip().strip("\"'")
                return normalized or None
        return None

    @staticmethod
    def _has_command_reasoning_effort(command: Sequence[str]) -> bool:
        for index, argument in enumerate(command):
            value: str | None = None
            if argument in {"--config", "-c"} and index + 1 < len(command):
                value = command[index + 1]
            elif argument.startswith("--config="):
                value = argument.partition("=")[2]
            if value is not None and "=" in value:
                key, _ = value.split("=", maxsplit=1)
                if key.strip() == "model_reasoning_effort":
                    return True
        return False

    @staticmethod
    def _runtime_insert_index(command: Sequence[str]) -> int:
        try:
            return command.index("exec") + 1
        except ValueError:
            return min(2, len(command))

    @staticmethod
    def _without_explicit_sandbox(command: list[str]) -> list[str]:
        filtered: list[str] = []
        skip_next = False
        for argument in command:
            if skip_next:
                skip_next = False
                continue
            if argument == "--sandbox":
                skip_next = True
                continue
            if argument.startswith("--sandbox="):
                continue
            filtered.append(argument)
        return filtered
