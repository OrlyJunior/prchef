from pathlib import Path
from typing import Any

import pytest

from prchef.clients.command_client import CommandResult
from prchef.clients.exceptions import CommandExecutionError, CommandTimeoutError
from prchef.config.project_config import ValidationSettings
from prchef.models import ValidationCheck
from prchef.services.validation_service import ValidationService


class RecordingCommandClient:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def run(
        self, command: list[str], *, cwd: Path, timeout_seconds: int
    ) -> CommandResult:
        del cwd, timeout_seconds
        self.commands.append(command)
        return CommandResult(command, 0, "", "", 0.0)


class ScriptedCommandClient:
    """Returns a scripted result, or raises a scripted error, for each command in order."""

    def __init__(self, outcomes: list[CommandResult | Exception]) -> None:
        self._outcomes = outcomes
        self.commands: list[list[str]] = []

    def run(
        self, command: list[str], *, cwd: Path, timeout_seconds: int
    ) -> CommandResult:
        del cwd, timeout_seconds
        self.commands.append(command)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_explicit_source_mypy_command_prevents_full_repository_auto_detection(
    tmp_path: Path,
) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\n[tool.mypy]\npackages = [\"prchef\"]\n", encoding="utf-8"
    )
    commands = RecordingCommandClient()
    settings = ValidationSettings(
        auto_detect=True,
        commands=[
            ["python", "-m", "pytest"],
            ["python", "-m", "ruff", "check", "."],
            ["python", "-m", "mypy", "src/prchef"],
        ],
    )

    result = ValidationService(commands).validate(tmp_path, settings, require_tests=True)

    assert result.success
    assert commands.commands == [
        ["python", "-m", "pytest"],
        ["python", "-m", "ruff", "check", "."],
        ["python", "-m", "mypy", "src/prchef"],
    ]


def test_timeout_becomes_a_failed_check_and_later_commands_still_run(tmp_path: Path) -> None:
    commands = ScriptedCommandClient(
        [
            CommandTimeoutError(
                "Command timed out after 60s: make check",
                timeout_seconds=60,
                stdout="partial stdout",
                stderr="partial stderr",
                duration_seconds=60.5,
            ),
            CommandResult(["make", "lint"], 0, "lint ok", "", 0.2),
        ]
    )
    settings = ValidationSettings(
        auto_detect=False,
        commands=[["make", "check"], ["make", "lint"]],
        timeout_seconds=60,
    )
    payloads: list[dict[str, Any]] = []

    result = ValidationService(commands).validate(
        tmp_path, settings, require_tests=False, on_check_complete=payloads.append
    )

    assert not result.success
    assert result.error_message == "One or more validation commands failed"
    assert commands.commands == [["make", "check"], ["make", "lint"]]
    timed_out, passed = result.checks
    assert timed_out == ValidationCheck(
        name="make check",
        command=["make", "check"],
        success=False,
        exit_code=None,
        stdout="partial stdout",
        stderr="partial stderr",
        duration_seconds=60.5,
        timed_out=True,
        timeout_seconds=60,
    )
    assert passed.success
    assert not passed.timed_out
    assert passed.timeout_seconds is None
    assert payloads[0] == {
        "command": "make check",
        "argv": ["make", "check"],
        "working_directory": str(tmp_path),
        "exit_code": None,
        "elapsed_time_seconds": 60.5,
        "stdout": "partial stdout",
        "stderr": "partial stderr",
        "result": "FAIL",
        "timed_out": True,
        "timeout_seconds": 60,
    }
    assert payloads[1] == {
        "command": "make lint",
        "argv": ["make", "lint"],
        "working_directory": str(tmp_path),
        "exit_code": 0,
        "elapsed_time_seconds": 0.2,
        "stdout": "lint ok",
        "stderr": "",
        "result": "PASS",
    }


def test_completed_failure_is_unchanged_by_timeout_support(tmp_path: Path) -> None:
    commands = ScriptedCommandClient([CommandResult(["make", "check"], 2, "out", "err", 0.3)])
    settings = ValidationSettings(auto_detect=False, commands=[["make", "check"]])

    result = ValidationService(commands).validate(tmp_path, settings, require_tests=False)

    assert not result.success
    assert result.checks == [
        ValidationCheck(
            name="make check",
            command=["make", "check"],
            success=False,
            exit_code=2,
            stdout="out",
            stderr="err",
            duration_seconds=0.3,
        )
    ]


def test_non_timeout_command_errors_still_propagate(tmp_path: Path) -> None:
    commands = ScriptedCommandClient([CommandExecutionError("Executable not found: make")])
    settings = ValidationSettings(auto_detect=False, commands=[["make", "check"]])

    with pytest.raises(CommandExecutionError, match="Executable not found: make"):
        ValidationService(commands).validate(tmp_path, settings, require_tests=False)
