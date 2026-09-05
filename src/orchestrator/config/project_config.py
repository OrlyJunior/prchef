from __future__ import annotations

import os
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from orchestrator.models.development_task import DevelopmentTask


class ConfigurationError(RuntimeError):
    pass


GLOBAL_CONFIG_FILENAME = "orchestrator.yml"
_CONFIG_ENVIRONMENT_VARIABLES = (
    "ORCHESTRATOR_CONFIG",
    "ORCHESTRATOR_CONFIG_PATH",
    "ORCHESTRATOR_CONFIG_FILE",
)


@dataclass(frozen=True, slots=True)
class ConfiguredRepository:
    """A repository registered by the global configuration.

    This intentionally contains only discovery information. Resolving the full
    project configuration, including local overrides, remains the responsibility
    of :class:`ProjectConfigLoader.load`.
    """

    key: str
    path: Path


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


class AgentRoleSettings(BaseModel):
    provider: str


class AgentsSettings(BaseModel):
    planner: AgentRoleSettings = Field(
        default_factory=lambda: AgentRoleSettings(provider="jules")
    )
    implementer: AgentRoleSettings = Field(
        default_factory=lambda: AgentRoleSettings(provider="codex")
    )
    reviewer: AgentRoleSettings = Field(
        default_factory=lambda: AgentRoleSettings(provider="jules")
    )


class SafetySettings(BaseModel):
    allow_direct_merge: bool = False
    require_tests: bool = True


class ValidationSettings(BaseModel):
    auto_detect: bool = True
    commands: list[list[str]] = Field(default_factory=list)
    timeout_seconds: int = 900
    max_validation_retries: int = Field(default=2, ge=0)


class ReviewSettings(BaseModel):
    """Controls bounded implementation corrections requested by code review."""

    max_review_retries: int = Field(default=1, ge=0)


class NotificationsSettings(BaseModel):
    """Optional delivery settings for user-visible workflow outcomes."""

    enabled: bool = False
    channel: str = "desktop"

    @field_validator("channel")
    @classmethod
    def validate_channel(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not normalized:
            raise ValueError("Notification channel must be a non-empty string")
        return normalized


class WorkspaceSettings(BaseModel):
    """Location for disposable, per-execution Git worktrees."""

    root: Path | None = None


class CommandIntegrationSettings(BaseModel):
    command: list[str]
    timeout_seconds: int = 1800


class GitHubAppSettings(BaseModel):
    """Credentials used to mint short-lived GitHub App installation tokens."""

    app_id: int | None = Field(default=None, gt=0)
    installation_id: int | None = Field(default=None, gt=0)
    private_key_path: Path | None = None
    private_key_env: str | None = None
    api_url: str = "https://api.github.com"
    bot_name: str | None = None
    bot_user_id: int | None = Field(default=None, gt=0)

    @field_validator("private_key_env")
    @classmethod
    def validate_private_key_env(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("GitHub App private_key_env must be a non-empty string")
        return normalized

    @field_validator("api_url")
    @classmethod
    def validate_api_url(cls, value: str) -> str:
        normalized = value.rstrip("/")
        if not normalized.startswith(("https://", "http://")):
            raise ValueError("GitHub App api_url must be an HTTP(S) URL")
        return normalized

    @field_validator("bot_name")
    @classmethod
    def validate_bot_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("GitHub App bot_name must be a non-empty string")
        return normalized

    @model_validator(mode="after")
    def validate_complete_credentials(self) -> GitHubAppSettings:
        configured = [
            self.app_id is not None,
            self.installation_id is not None,
            self.private_key_path is not None or self.private_key_env is not None,
        ]
        if any(configured) and not all(configured):
            raise ValueError(
                "GitHub App configuration requires app_id, installation_id, and a private key"
            )
        if self.private_key_path is not None and self.private_key_env is not None:
            raise ValueError(
                "GitHub App private key must use either private_key_path or "
                "private_key_env, not both"
            )
        if (self.bot_name is None) != (self.bot_user_id is None):
            raise ValueError(
                "GitHub App bot_name and bot_user_id must be configured together"
            )
        return self

    @property
    def is_configured(self) -> bool:
        return self.app_id is not None


class GitHubIntegrationSettings(CommandIntegrationSettings):
    """GitHub CLI integration identities.

    ``app`` remains the reviewer identity for backwards compatibility. The
    implementer identity is deliberately separate from review credentials.
    """

    command: list[str] = Field(default_factory=lambda: ["gh"])
    timeout_seconds: int = 120
    app: GitHubAppSettings = Field(default_factory=GitHubAppSettings)
    implementer_app: GitHubAppSettings = Field(default_factory=GitHubAppSettings)


class CodexIntegrationSettings(CommandIntegrationSettings):
    """Codex-specific controls layered over the generic command settings."""

    approval_policy: Literal["approve-for-me"] | None = None
    model: str | None = None
    reasoning_effort: str | None = None

    @field_validator("model", "reasoning_effort")
    @classmethod
    def validate_runtime_setting(
        cls, value: str | None, info: ValidationInfo
    ) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"Codex provider {info.field_name} must be a non-empty string")
        return normalized

    @model_validator(mode="after")
    def validate_approval_policy(self) -> CodexIntegrationSettings:
        sandbox_mode: str | None = None
        for index, argument in enumerate(self.command):
            if argument == "--sandbox" and index + 1 < len(self.command):
                sandbox_mode = self.command[index + 1]
                break
            if argument.startswith("--sandbox="):
                sandbox_mode = argument.partition("=")[2]
                break
        if (
            self.approval_policy == "approve-for-me"
            and sandbox_mode is not None
            and sandbox_mode != "workspace-write"
        ):
            raise ValueError(
                "Codex approval_policy='approve-for-me' requires "
                "the workspace-write sandbox"
            )
        command_model = _command_model(self.command)
        if (
            self.model is not None
            and _has_command_model_flag(self.command)
            and command_model is None
        ):
            raise ValueError("Codex command has a model flag without a value")
        if self.model is not None and command_model is not None and command_model != self.model:
            raise ValueError(
                "Codex provider specifies both model "
                f"{self.model!r} and conflicting model flag {command_model!r} in command"
            )
        command_reasoning_effort = _command_reasoning_effort(self.command)
        if (
            self.reasoning_effort is not None
            and _has_command_reasoning_effort(self.command)
            and command_reasoning_effort is None
        ):
            raise ValueError("Codex command has model_reasoning_effort without a value")
        if (
            self.reasoning_effort is not None
            and command_reasoning_effort is not None
            and command_reasoning_effort != self.reasoning_effort
        ):
            raise ValueError(
                "Codex provider specifies both reasoning_effort "
                f"{self.reasoning_effort!r} and conflicting model_reasoning_effort "
                f"{command_reasoning_effort!r} in command"
            )
        return self


def _command_model(command: list[str]) -> str | None:
    """Read the Codex CLI model flag without rebuilding command arguments."""
    for index, argument in enumerate(command):
        if argument in {"--model", "-m"}:
            if index + 1 < len(command):
                return command[index + 1]
            return None
        if argument.startswith("--model="):
            return argument.partition("=")[2] or None
    return None


def _has_command_model_flag(command: list[str]) -> bool:
    return any(
        argument in {"--model", "-m"} or argument.startswith("--model=")
        for argument in command
    )


def _command_reasoning_effort(command: list[str]) -> str | None:
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


def _has_command_reasoning_effort(command: list[str]) -> bool:
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


class JulesIntegrationSettings(BaseModel):
    source: str | None = None
    poll_interval_seconds: float = Field(default=15.0, gt=0)
    timeout_seconds: int = Field(default=1800, gt=0)
    poll_retry_attempts: int = Field(default=3, ge=0)
    poll_retry_backoff_seconds: float = Field(default=2.0, gt=0)


class AgentProviderSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str


class JulesProviderSettings(JulesIntegrationSettings):
    model_config = ConfigDict(extra="forbid")

    type: Literal["jules"] = "jules"


class CodexCliProviderSettings(CodexIntegrationSettings):
    model_config = ConfigDict(extra="forbid")

    type: Literal["codex-cli"] = "codex-cli"


class GeminiGenerationSettings(BaseModel):
    """Small, provider-local set of generation controls for Vertex Gemini."""

    model_config = ConfigDict(extra="forbid")

    temperature: float = Field(default=0.2, ge=0, le=2)
    max_output_tokens: int | None = Field(default=None, gt=0)


class GeminiVertexProviderSettings(BaseModel):
    """Configuration for a Google Gen AI SDK client in Vertex AI mode."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["gemini-vertex"] = "gemini-vertex"
    project: str = Field(min_length=1)
    location: str = Field(default="global", min_length=1)
    model: str = Field(min_length=1)
    timeout_seconds: int = Field(default=900, gt=0)
    generation: GeminiGenerationSettings = Field(default_factory=GeminiGenerationSettings)


class IntegrationsSettings(BaseModel):
    jules: JulesIntegrationSettings = Field(default_factory=JulesIntegrationSettings)
    codex: CodexIntegrationSettings = Field(
        default_factory=lambda: CodexIntegrationSettings(
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
    github: GitHubIntegrationSettings = Field(
        default_factory=lambda: GitHubIntegrationSettings(command=["gh"], timeout_seconds=120)
    )


class ProjectConfig(BaseModel):
    path: Path
    task: TaskSettings = Field(default_factory=TaskSettings)
    repository: RepositorySettings = Field(default_factory=RepositorySettings)
    roles: RolesSettings = Field(default_factory=RolesSettings)
    agents: AgentsSettings = Field(default_factory=AgentsSettings)
    providers: dict[str, AgentProviderSettings] = Field(default_factory=dict)
    safety: SafetySettings = Field(default_factory=SafetySettings)
    validation: ValidationSettings = Field(default_factory=ValidationSettings)
    review: ReviewSettings = Field(default_factory=ReviewSettings)
    notifications: NotificationsSettings = Field(default_factory=NotificationsSettings)
    workspace: WorkspaceSettings = Field(default_factory=WorkspaceSettings)
    integrations: IntegrationsSettings = Field(default_factory=IntegrationsSettings)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_agent_configuration(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        values = deepcopy(data)
        has_agents = "agents" in values
        has_providers = "providers" in values
        if has_agents != has_providers:
            raise ValueError(
                "Modern agent configuration requires both 'agents' and 'providers'. "
                "Remove both to use legacy roles/integrations."
            )
        if has_agents:
            raw_agents = values["agents"]
            if isinstance(raw_agents, dict):
                missing_roles = {"planner", "implementer", "reviewer"} - set(raw_agents)
                if missing_roles:
                    raise ValueError(
                        "Explicit agents configuration is missing roles: "
                        f"{', '.join(sorted(missing_roles))}"
                    )
        else:
            raw_roles = values.get("roles", {})
            roles = raw_roles if isinstance(raw_roles, dict) else {}
            values["agents"] = {
                "planner": {"provider": roles.get("spec", "jules")},
                "implementer": {"provider": roles.get("implementation", "codex")},
                "reviewer": {"provider": roles.get("review", "jules")},
            }
        if not has_providers:
            raw_integrations = values.get("integrations", {})
            integrations = raw_integrations if isinstance(raw_integrations, dict) else {}
            jules = JulesIntegrationSettings.model_validate(
                integrations.get("jules", {})
            ).model_dump()
            codex = CodexIntegrationSettings.model_validate(
                integrations.get(
                    "codex",
                    {
                        "command": [
                            "codex",
                            "exec",
                            "--sandbox",
                            "workspace-write",
                            "--output-last-message",
                            "{output_file}",
                            "-",
                        ],
                        "timeout_seconds": 3600,
                    },
                )
            ).model_dump()
            values["providers"] = {
                "jules": {"type": "jules", **jules},
                "codex": {"type": "codex-cli", **codex},
            }
        return values

    @model_validator(mode="after")
    def enforce_v1_safety(self) -> ProjectConfig:
        if self.safety.allow_direct_merge:
            raise ValueError("allow_direct_merge=true is not supported by V1")
        return self

    def with_agent_providers(self, overrides: Mapping[str, str]) -> ProjectConfig:
        """Return an in-memory configuration with selected configured providers.

        Interactive clients may select a different named provider for a role,
        but selections never alter YAML and cannot introduce an unconfigured
        provider. Keeping this operation on the configuration model gives all
        entry points the same validation boundary.
        """

        selected = self.model_copy(deep=True)
        role_settings = {
            "planner": selected.agents.planner,
            "implementer": selected.agents.implementer,
            "reviewer": selected.agents.reviewer,
        }
        for role, provider_name in overrides.items():
            setting = role_settings.get(role)
            if setting is None:
                raise ConfigurationError(f"Unknown agent role: {role}")
            normalized = provider_name.strip()
            if not normalized:
                raise ConfigurationError(f"Agent provider for {role} must be non-empty")
            if normalized not in selected.providers:
                raise ConfigurationError(
                    f"Agent provider {normalized!r} selected for {role} is not configured"
                )
            setting.provider = normalized
        return selected


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


def resolve_global_config(
    config_file: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    home: Path | None = None,
    installation_dir: Path | None = None,
) -> Path:
    """Resolve the global configuration using the CLI's documented precedence."""
    working_directory = (cwd or Path.cwd()).expanduser().resolve()
    environment = os.environ if environ is None else environ

    def resolve_candidate(path: Path) -> Path:
        expanded = path.expanduser()
        if expanded.is_absolute():
            return expanded.resolve()
        return (working_directory / expanded).resolve()

    if config_file is not None:
        explicit_path = resolve_candidate(config_file)
        if explicit_path.is_file():
            return explicit_path
        raise ConfigurationError(f"Explicit configuration file not found: {explicit_path}")

    for variable in _CONFIG_ENVIRONMENT_VARIABLES:
        configured = environment.get(variable)
        if not configured:
            continue
        environment_path = resolve_candidate(Path(configured))
        if environment_path.is_file():
            return environment_path
        raise ConfigurationError(
            f"Configuration file from {variable} not found: {environment_path}"
        )

    candidates = [
        directory / GLOBAL_CONFIG_FILENAME
        for directory in (working_directory, *working_directory.parents)
    ]
    user_home = (home or Path.home()).expanduser()
    candidates.extend(
        [
            user_home / ".config" / "orchestrator" / GLOBAL_CONFIG_FILENAME,
            user_home / ".orchestrator" / GLOBAL_CONFIG_FILENAME,
        ]
    )
    package_root = installation_dir or Path(__file__).resolve().parents[3]
    candidates.append(package_root / GLOBAL_CONFIG_FILENAME)

    searched: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in searched:
            continue
        searched.append(resolved)
        if resolved.is_file():
            return resolved

    rendered = "\n  - ".join(str(path) for path in searched)
    raise ConfigurationError(f"Global configuration not found. Searched:\n  - {rendered}")


class ProjectConfigLoader:
    def __init__(self, config_file: Path | None = None) -> None:
        # Debug commands can construct a loader for stages that do not need any
        # project configuration. Resolve only when configuration is requested so
        # those paths retain that behavior while all actual loads share discovery.
        self._requested_config_file = config_file
        self._resolved_config_file: Path | None = None

    @property
    def _config_file(self) -> Path:
        if self._resolved_config_file is None:
            self._resolved_config_file = resolve_global_config(self._requested_config_file)
        return self._resolved_config_file

    def load_defaults(self) -> ProjectConfig:
        """Load the global default project configuration without selecting a repository."""
        global_config = _load_yaml(self._config_file, required=True)
        self._reject_misplaced_project_settings(global_config)
        defaults = global_config.get("defaults", {})
        if not isinstance(defaults, dict):
            raise ConfigurationError("'defaults' must be a mapping")

        configuration = deepcopy(defaults)
        configuration["path"] = self._config_file.parent
        try:
            return ProjectConfig.model_validate(configuration)
        except ValueError as exc:
            raise ConfigurationError(f"Invalid project configuration: {exc}") from exc

    def list_repositories(self) -> list[ConfiguredRepository]:
        """List registered repositories without resolving their local configuration.

        This is deliberately a discovery API: it does not open a repository or
        merge its ``.orchestrator.yml``. Callers must still use :meth:`load`
        before an operation needs a resolved project configuration.
        """

        global_config = _load_yaml(self._config_file, required=True)
        self._reject_misplaced_project_settings(global_config)
        registered = global_config.get("repositories", {})
        if not isinstance(registered, dict):
            raise ConfigurationError("'repositories' must be a mapping")

        repositories: list[ConfiguredRepository] = []
        for key, raw_entry in registered.items():
            if not isinstance(raw_entry, dict):
                raise ConfigurationError(f"Repository entry '{key}' must be a mapping")
            raw_path = raw_entry.get("path")
            if raw_path is None:
                raise ConfigurationError(f"Repository entry '{key}' has no path")
            path = Path(str(raw_path)).expanduser()
            if not path.is_absolute():
                path = (self._config_file.parent / path).resolve()
            else:
                path = path.resolve()
            repositories.append(ConfiguredRepository(key=str(key), path=path))
        return repositories

    def load(self, task: DevelopmentTask, repository_override: str | None = None) -> ProjectConfig:
        global_config = _load_yaml(self._config_file, required=True)
        self._reject_misplaced_project_settings(global_config)
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

    def save_github_app_settings(
        self,
        repository_path: Path,
        *,
        implementer_app: GitHubAppSettings,
        reviewer_app: GitHubAppSettings,
    ) -> Path:
        """Persist App *references* in the repository-local configuration.

        The TUI only writes App IDs, installation IDs, and key file/environment
        references. Private key contents and installation tokens are neither
        accepted nor serialized here.
        """
        local_path = repository_path / ".orchestrator.yml"
        configuration = _load_yaml(local_path, required=False)
        integrations = configuration.setdefault("integrations", {})
        if not isinstance(integrations, dict):
            raise ConfigurationError("'integrations' must be a mapping")
        github = integrations.setdefault("github", {})
        if not isinstance(github, dict):
            raise ConfigurationError("'integrations.github' must be a mapping")

        for key, settings in (("implementer_app", implementer_app), ("app", reviewer_app)):
            if settings.is_configured:
                github[key] = settings.model_dump(mode="json", exclude_none=True)
            else:
                github.pop(key, None)

        try:
            serialized = yaml.safe_dump(configuration, allow_unicode=True, sort_keys=False)
            temporary_path = local_path.with_suffix(".yml.tmp")
            temporary_path.write_text(serialized, encoding="utf-8")
            temporary_path.replace(local_path)
        except OSError as exc:
            raise ConfigurationError(f"Cannot write configuration {local_path}: {exc}") from exc
        return local_path

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
        self._reject_misplaced_project_settings(global_config)
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

    @staticmethod
    def _reject_misplaced_project_settings(global_config: dict[str, Any]) -> None:
        project_settings = {
            "agents",
            "providers",
            "roles",
            "integrations",
            "repository",
            "safety",
            "validation",
            "review",
            "notifications",
            "workspace",
        }
        misplaced = sorted(project_settings.intersection(global_config))
        if misplaced:
            rendered = ", ".join(misplaced)
            raise ConfigurationError(
                f"Project settings at the top level are not applied: {rendered}. "
                "Place them in 'defaults', a 'repositories.<name>' entry, or the target "
                "repository's .orchestrator.yml."
            )
