from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from orchestrator.models.development_task import DevelopmentTask


class ConfigurationError(RuntimeError):
    pass


class RepositorySettings(BaseModel):
    name: str = ""
    base_branch: str = "develop"
    protected_branches: list[str] = Field(default_factory=lambda: ["main", "master", "develop"])
    branch_prefix: str = "orchestrator"


class TaskSettings(BaseModel):
    provider: Literal["trello", "github"] | None = None


class RolesSettings(BaseModel):
    spec: str = "jules"
    implementation: str = "codex"
    review: str = "jules"


class SafetySettings(BaseModel):
    allow_direct_merge: bool = False
    require_tests: bool = True


class ValidationSettings(BaseModel):
    auto_detect: bool = True
    commands: list[list[str]] = Field(default_factory=list)
    timeout_seconds: int = 900


class CommandIntegrationSettings(BaseModel):
    command: list[str]
    timeout_seconds: int = 1800


class JulesIntegrationSettings(BaseModel):
    source: str | None = None
    poll_interval_seconds: float = Field(default=15.0, gt=0)
    timeout_seconds: int = Field(default=1800, gt=0)
    poll_retry_attempts: int = Field(default=3, ge=0)
    poll_retry_backoff_seconds: float = Field(default=2.0, gt=0)


class IntegrationsSettings(BaseModel):
    jules: JulesIntegrationSettings = Field(default_factory=JulesIntegrationSettings)
    codex: CommandIntegrationSettings = Field(
        default_factory=lambda: CommandIntegrationSettings(
            command=[
                "codex",
                "exec",
                "--sandbox",
                "workspace-write",
                "--output-last-message",
                "{output_file}",
                "-",
            ],
            timeout_seconds=3600,
        )
    )
    github: CommandIntegrationSettings = Field(
        default_factory=lambda: CommandIntegrationSettings(command=["gh"], timeout_seconds=120)
    )


class ProjectConfig(BaseModel):
    path: Path
    task: TaskSettings = Field(default_factory=TaskSettings)
    repository: RepositorySettings = Field(default_factory=RepositorySettings)
    roles: RolesSettings = Field(default_factory=RolesSettings)
    safety: SafetySettings = Field(default_factory=SafetySettings)
    validation: ValidationSettings = Field(default_factory=ValidationSettings)
    integrations: IntegrationsSettings = Field(default_factory=IntegrationsSettings)

    @model_validator(mode="after")
    def enforce_v1_safety(self) -> ProjectConfig:
        if self.safety.allow_direct_merge:
            raise ValueError("allow_direct_merge=true is not supported by V1")
        return self


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _load_yaml(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise ConfigurationError(f"Configuration file not found: {path}")
        return {}
    try:
        content = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"Cannot read configuration {path}: {exc}") from exc
    if not isinstance(content, dict):
        raise ConfigurationError(f"Configuration root must be a mapping: {path}")
    return content


class ProjectConfigLoader:
    def __init__(self, config_file: Path) -> None:
        self._config_file = config_file.resolve()

    def load(self, task: DevelopmentTask, repository_override: str | None = None) -> ProjectConfig:
        global_config = _load_yaml(self._config_file, required=True)
        repository_ref = repository_override or self._repository_ref_from_task(task)
        if not repository_ref:
            raise ConfigurationError(
                "Task has no repository; configure one in the task or use --repository"
            )

        registered = global_config.get("repositories", {})
        if not isinstance(registered, dict):
            raise ConfigurationError("'repositories' must be a mapping")

        entry: dict[str, Any]
        if repository_ref in registered:
            raw_entry = registered[repository_ref]
            if not isinstance(raw_entry, dict):
                raise ConfigurationError(f"Repository entry '{repository_ref}' must be a mapping")
            entry = deepcopy(raw_entry)
            raw_path = entry.pop("path", None)
            if raw_path is None:
                raise ConfigurationError(f"Repository entry '{repository_ref}' has no path")
            repository_path = Path(str(raw_path)).expanduser()
        else:
            repository_path = Path(repository_ref).expanduser()
            entry = {}

        if not repository_path.is_absolute():
            repository_path = (self._config_file.parent / repository_path).resolve()
        else:
            repository_path = repository_path.resolve()
        if not repository_path.is_dir():
            raise ConfigurationError(f"Repository path does not exist: {repository_path}")
        if not (repository_path / ".git").exists():
            raise ConfigurationError(f"Target is not a Git repository: {repository_path}")

        defaults = global_config.get("defaults", {})
        if not isinstance(defaults, dict):
            raise ConfigurationError("'defaults' must be a mapping")
        local = _load_yaml(repository_path / ".orchestrator.yml", required=False)
        merged = _deep_merge(defaults, entry)
        merged = _deep_merge(merged, local)
        merged["path"] = repository_path
        repository_settings = merged.setdefault("repository", {})
        if not isinstance(repository_settings, dict):
            raise ConfigurationError("'repository' must be a mapping")
        repository_settings.setdefault("name", repository_ref)

        try:
            return ProjectConfig.model_validate(merged)
        except ValueError as exc:
            raise ConfigurationError(f"Invalid project configuration: {exc}") from exc

    @staticmethod
    def _repository_ref_from_task(task: DevelopmentTask) -> str | None:
        if task.repository:
            return task.repository
        custom_fields = task.metadata.get("custom_fields", {})
        if isinstance(custom_fields, dict):
            lowered = {str(key).lower(): value for key, value in custom_fields.items()}
            for key in ("repository", "repo"):
                value = lowered.get(key)
                if value is not None and str(value).strip():
                    return str(value).strip()
        direct = task.metadata.get("repository")
        return str(direct).strip() if direct else None

    def default_task_provider(self) -> str | None:
        global_config = _load_yaml(self._config_file, required=True)
        raw_task = global_config.get("task")
        if raw_task is None:
            defaults = global_config.get("defaults", {})
            raw_task = defaults.get("task") if isinstance(defaults, dict) else None
        if raw_task is None:
            return None
        if not isinstance(raw_task, dict):
            raise ConfigurationError("'task' must be a mapping")
        provider = raw_task.get("provider")
        if provider is None:
            return None
        normalized = str(provider).strip().casefold()
        if normalized not in {"trello", "github"}:
            raise ConfigurationError("task.provider must be 'trello' or 'github'")
        return normalized
