from __future__ import annotations

import logging
import os
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from orchestrator.clients.command_client import CommandClient
from orchestrator.models.implementation_result import ImplementationResult
from orchestrator.runtime_logging import format_duration, summarize_text

logger = logging.getLogger(__name__)


def _is_windows() -> bool:
    return os.name == "nt"


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

    def implement(self, prompt: str, repository_path: Path) -> ImplementationResult:
        with tempfile.TemporaryDirectory(prefix="orchestrator-codex-") as temp_root:
            output_file = Path(temp_root) / "last-message.txt"
            command = self._execution_command(output_file)
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
            summary = (
                output_file.read_text(encoding="utf-8", errors="replace").strip()
                if output_file.exists()
                else result.stdout.strip()
            )
            sandbox_write_error = self._sandbox_write_error(
                repository_path,
                summary,
                result.stdout,
                result.stderr,
            )
            if result.success:
                if sandbox_write_error is None:
                    logger.info("[codex] Completed in %s", format_duration(result.duration_seconds))
                else:
                    logger.error("[codex] %s", sandbox_write_error)
            else:
                detail = summarize_text(result.stderr or result.stdout)
                logger.error(
                    "[codex] Failed (exit %s, %s): %s",
                    result.exit_code,
                    format_duration(result.duration_seconds),
                    detail or "no output",
                )
            return ImplementationResult(
                success=result.success and sandbox_write_error is None,
                summary=summary,
                error_message=None
                if result.success and sandbox_write_error is None
                else (
                    sandbox_write_error
                    or result.stderr.strip()
                    or result.stdout.strip()
                    or "Codex failed"
                ),
            )

    def _sandbox_write_error(
        self,
        repository_path: Path,
        summary: str,
        stdout: str,
        stderr: str,
    ) -> str | None:
        if not self._reported_sandbox_write_denial(summary, stdout, stderr):
            return None

        try:
            repository = repository_path.resolve()
        except OSError:
            repository = repository_path
        sandbox_name = "Codex Windows sandbox" if _is_windows() else "Codex sandbox"
        return (
            f"{sandbox_name} could not modify workspace files "
            f"(repository: {repository}; sandbox: {self._sandbox_mode()}). "
            "The Codex CLI reported a sandbox write denial. Run `codex exec --sandbox "
            "workspace-write -` from the repository and ask it to create a disposable file in "
            "the affected directory."
        )

    @staticmethod
    def _reported_sandbox_write_denial(*messages: str) -> bool:
        text = "\n".join(messages).casefold()
        write_marker = any(
            marker in text
            for marker in (
                "failed to write file",
                "write access",
                "requested file path",
                "access to the path",
                "could not modify",
            )
        )
        sandbox_marker = "sandbox" in text
        denial_marker = "denied" in text or "policy_denied" in text
        return write_marker and sandbox_marker and denial_marker

    def _sandbox_mode(self) -> str:
        if self._approval_policy == "approve-for-me":
            return "workspace-write"
        for index, argument in enumerate(self._command):
            if argument == "--sandbox" and index + 1 < len(self._command):
                return self._command[index + 1]
            if argument.startswith("--sandbox="):
                return argument.partition("=")[2] or "unspecified"
        return "unspecified"

    def _execution_command(self, output_file: Path) -> list[str]:
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
        insert_at = self._runtime_insert_index(command)
        command[insert_at:insert_at] = runtime_arguments
        if self._approval_policy is None:
            return command

        command = self._without_explicit_sandbox(command)
        if "--approve-for-me" not in command:
            command.insert(min(2, len(command)), "--approve-for-me")
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
