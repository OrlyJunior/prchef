from __future__ import annotations

import json
import os
from pathlib import Path

from orchestrator.clients.command_client import CommandClient
from orchestrator.config.project_config import ValidationSettings
from orchestrator.models.validation_result import ValidationCheck, ValidationResult


class ValidationService:
    def __init__(self, command_client: CommandClient) -> None:
        self._commands = command_client

    def validate(
        self,
        repository_path: Path,
        settings: ValidationSettings,
        *,
        require_tests: bool,
    ) -> ValidationResult:
        commands = [list(command) for command in settings.commands]
        if settings.auto_detect:
            for detected in self._detect(repository_path):
                if detected not in commands:
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
            result = self._commands.run(
                command,
                cwd=repository_path,
                timeout_seconds=settings.timeout_seconds,
            )
            checks.append(
                ValidationCheck(
                    name=" ".join(command),
                    command=command,
                    success=result.success,
                    exit_code=result.exit_code,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    duration_seconds=result.duration_seconds,
                )
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
