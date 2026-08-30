from pathlib import Path

from orchestrator.clients.command_client import CommandResult
from orchestrator.config.project_config import ValidationSettings
from orchestrator.services.validation_service import ValidationService


class RecordingCommandClient:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def run(
        self, command: list[str], *, cwd: Path, timeout_seconds: int
    ) -> CommandResult:
        del cwd, timeout_seconds
        self.commands.append(command)
        return CommandResult(command, 0, "", "", 0.0)


def test_explicit_source_mypy_command_prevents_full_repository_auto_detection(
    tmp_path: Path,
) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\n[tool.mypy]\npackages = [\"orchestrator\"]\n", encoding="utf-8"
    )
    commands = RecordingCommandClient()
    settings = ValidationSettings(
        auto_detect=True,
        commands=[
            ["python", "-m", "pytest"],
            ["python", "-m", "ruff", "check", "."],
            ["python", "-m", "mypy", "src/orchestrator"],
        ],
    )

    result = ValidationService(commands).validate(tmp_path, settings, require_tests=True)

    assert result.success
    assert commands.commands == [
        ["python", "-m", "pytest"],
        ["python", "-m", "ruff", "check", "."],
        ["python", "-m", "mypy", "src/orchestrator"],
    ]
