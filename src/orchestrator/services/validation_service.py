from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orchestrator.clients.command_client import CommandClient
from orchestrator.clients.exceptions import CommandTimeoutError
from orchestrator.config.project_config import ValidationSettings
from orchestrator.models.validation_result import ValidationCheck, ValidationResult
from orchestrator.runtime_logging import format_command, format_duration, summarize_text

logger = logging.getLogger(__name__)


class ValidationService:
    def __init__(self, command_client: CommandClient) -> None:
        self._commands = command_client

    def validate(
        self,
        repository_path: Path,
        settings: ValidationSettings,
        *,
        require_tests: bool,
        on_check_complete: Callable[[dict[str, Any]], None] | None = None,
    ) -> ValidationResult:
        commands = [list(command) for command in settings.commands]
        if settings.auto_detect:
            for detected in self._detect(repository_path):
                if not self._is_redundant_detected_command(detected, commands):
                    commands.append(detected)
        if not commands:
            message = "No validation commands configured or detected"
            return ValidationResult(success=not require_tests, error_message=message)
        if require_tests and not any(self._is_test_command(command) for command in commands):
            return ValidationResult(
                success=False,
                error_message="Tests are required but no test command was configured or detected",
            )

        checks: list[ValidationCheck] = []
        for command in commands:
            command_text = format_command(command)
            logger.info("[validation] Running: %s", command_text)
            try:
                result = self._commands.run(
                    command,
                    cwd=repository_path,
                    timeout_seconds=settings.timeout_seconds,
                )
            except CommandTimeoutError as exc:
                # A timeout is a validation failure like any other: it is recorded as a failed
                # check (with no exit code) so the correction loop can act on it.
                logger.error(
                    "[validation] Timed out: %s (after %ss): %s",
                    command_text,
                    exc.timeout_seconds,
                    summarize_text(exc.stderr or exc.stdout) or "no output",
                )
                check = ValidationCheck(
                    name=" ".join(command),
                    command=command,
                    success=False,
                    exit_code=None,
                    stdout=exc.stdout,
                    stderr=exc.stderr,
                    duration_seconds=exc.duration_seconds,
                    timed_out=True,
                    timeout_seconds=exc.timeout_seconds,
                )
                checks.append(check)
                if on_check_complete is not None:
                    on_check_complete(
                        {
                            "command": command_text,
                            "argv": command,
                            "working_directory": str(repository_path),
                            "exit_code": None,
                            "elapsed_time_seconds": exc.duration_seconds,
                            "stdout": exc.stdout,
                            "stderr": exc.stderr,
                            "result": "FAIL",
                            "timed_out": True,
                            "timeout_seconds": exc.timeout_seconds,
                        }
                    )
                continue
            if result.success:
                logger.info(
                    "[validation] Passed: %s (%s)",
                    command_text,
                    format_duration(result.duration_seconds),
                )
            else:
                output = summarize_text(result.stderr or result.stdout)
                logger.error(
                    "[validation] Failed: %s (exit %s, %s): %s",
                    command_text,
                    result.exit_code,
                    format_duration(result.duration_seconds),
                    output or "no output",
                )
            check = ValidationCheck(
                name=" ".join(command),
                command=command,
                success=result.success,
                exit_code=result.exit_code,
                stdout=result.stdout,
                stderr=result.stderr,
                duration_seconds=result.duration_seconds,
            )
            checks.append(check)
            if on_check_complete is not None:
                on_check_complete(
                    {
                        "command": format_command(command),
                        "argv": command,
                        "working_directory": str(repository_path),
                        "exit_code": result.exit_code,
                        "elapsed_time_seconds": result.duration_seconds,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                        "result": "PASS" if result.success else "FAIL",
                    }
                )
        success = all(check.success for check in checks)
        return ValidationResult(
            success=success,
            checks=checks,
            error_message=None if success else "One or more validation commands failed",
        )

    @staticmethod
    def _is_test_command(command: list[str]) -> bool:
        normalized = " ".join(command).casefold()
        return any(token in normalized for token in ("pytest", " test", "test ", "test:"))

    @staticmethod
    def _is_redundant_detected_command(
        detected: list[str], configured: list[list[str]]
    ) -> bool:
        if detected in configured:
            return True
        return ValidationService._is_mypy_command(detected) and any(
            ValidationService._is_mypy_command(command) for command in configured
        )

    @staticmethod
    def _is_mypy_command(command: list[str]) -> bool:
        if len(command) >= 3 and command[1] == "-m":
            return command[2].casefold() == "mypy"
        return bool(command) and Path(command[0]).stem.casefold() == "mypy"

    def _detect(self, path: Path) -> list[list[str]]:
        commands: list[list[str]] = []
        pyproject = path / "pyproject.toml"
        pyproject_text = pyproject.read_text(encoding="utf-8") if pyproject.exists() else ""
        has_python_tests = (
            (path / "tests").is_dir()
            or (path / "pytest.ini").exists()
            or "pytest" in pyproject_text
        )
        if has_python_tests:
            commands.append(["python", "-m", "pytest"])
        if "[tool.ruff" in pyproject_text:
            commands.append(["python", "-m", "ruff", "check", "."])
        if "[tool.mypy" in pyproject_text or (path / "mypy.ini").exists():
            commands.append(["python", "-m", "mypy", "."])

        package_json = path / "package.json"
        if package_json.exists():
            try:
                scripts = json.loads(package_json.read_text(encoding="utf-8")).get("scripts", {})
            except (OSError, ValueError, AttributeError):
                scripts = {}
            if isinstance(scripts, dict):
                for script in ("test", "lint", "typecheck", "build"):
                    value = scripts.get(script)
                    if value and "no test specified" not in str(value).casefold():
                        commands.append(["npm", "run", script])
        if (path / "pom.xml").exists():
            commands.append(["mvn", "test"])
        gradle = path / ("gradlew.bat" if os.name == "nt" else "gradlew")
        if gradle.exists():
            commands.append([str(gradle), "test"])
        return commands
