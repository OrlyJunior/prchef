from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from io import StringIO
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationInfo,
    field_validator,
    model_validator,
)
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError as RoundTripYAMLError

from orchestrator.models.development_task import DevelopmentTask

logger = logging.getLogger(__name__)


class ConfigurationError(RuntimeError):
    pass


class ProviderSupportTier(StrEnum):
    """Published support level for a provider backend."""

    FIRST_CLASS = "first-class"
    OPTIONAL = "optional"
    UNSUPPORTED = "unsupported"


_PROVIDER_SUPPORT_TIERS: dict[str, ProviderSupportTier] = {
    "litellm": ProviderSupportTier.FIRST_CLASS,
    "codex-cli": ProviderSupportTier.FIRST_CLASS,
    "claude-code": ProviderSupportTier.FIRST_CLASS,
    "gemini-vertex": ProviderSupportTier.OPTIONAL,
}


def provider_support_tier(provider_type: str) -> ProviderSupportTier:
    """Return the support level without inferring it from a model name."""

    return _PROVIDER_SUPPORT_TIERS.get(provider_type, ProviderSupportTier.UNSUPPORTED)


def provider_option_label(name: str, provider_type: str) -> str:
    """Render a configured provider for selection while preserving its name as the value."""

    tier = provider_support_tier(provider_type)
    if tier is ProviderSupportTier.UNSUPPORTED:
        return f"{name} (unsupported)"
    return name


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
    project configuration remains the responsibility of :class:`ProjectConfigLoader.load`.
    """

    key: str
    path: Path


@dataclass(frozen=True, slots=True)
class RepositoryConfiguration:
    """Repository fields edited by the terminal settings screen.

    ``path`` retains the configured spelling so opening and saving the editor
    does not silently turn a relative path into an absolute one. ``resolved_path``
    exists only for the TUI's non-blocking path-existence indicator.
    """

    key: str
    path: str
    resolved_path: Path
    auto_detect: bool
    commands: list[list[str]]

    @property
    def path_exists(self) -> bool:
        return self.resolved_path.exists()


class RepositorySettings(BaseModel):
    name: str = ""
    base_branch: str = "develop"
    protected_branches: list[str] = Field(default_factory=lambda: ["main", "master", "develop"])
    branch_prefix: str = "orchestrator"


class EmbeddingSettings(BaseModel):
    """Provider-neutral embedding identity used by repository retrieval."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["fastembed"] = "fastembed"
    model: str = "BAAI/bge-small-en-v1.5"
    version: str = "1"
    dimension: int = Field(default=384, gt=0, le=65_536)

    @field_validator("model", "version")
    @classmethod
    def validate_identity_part(cls, value: str, info: ValidationInfo) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"Embedding {info.field_name} must be non-empty")
        return normalized


class QdrantSettings(BaseModel):
    """Connection settings shared by embedded-local and server Qdrant clients."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["local", "server"] = "local"
    path: Path | None = None
    url: str | None = None
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65_535)

    @field_validator("url", "host")
    @classmethod
    def normalize_optional_string(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_mode_fields(self) -> QdrantSettings:
        if self.mode == "local":
            if self.url is not None or self.host is not None or self.port is not None:
                raise ValueError("Local Qdrant mode accepts only 'path'")
            return self
        if self.path is not None:
            raise ValueError("Server Qdrant mode does not accept 'path'")
        if (self.url is None) == (self.host is None):
            raise ValueError("Server Qdrant mode requires exactly one of 'url' or 'host'")
        if self.port is not None and self.host is None:
            raise ValueError("Qdrant 'port' can only be used with 'host'")
        if self.url is not None:
            parsed = urlparse(self.url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("Qdrant 'url' must be an absolute http(s) URL")
        if self.host is not None and any(character in self.host for character in "/?#"):
            raise ValueError("Qdrant 'host' must be a hostname without a URL path")
        return self


class RepositoryContextSettings(BaseModel):
    """Selects orchestrator-owned repository retrieval policy."""

    model_config = ConfigDict(extra="forbid")

    strategy: str = "qdrant"
    fallback_to_heuristic: bool = False
    context_budget_chars: int = Field(default=12_000, ge=512, le=100_000)
    candidate_count: int = Field(default=12, ge=1, le=100)
    chunk_size_chars: int = Field(default=1_600, ge=256, le=16_000)
    chunk_overlap_chars: int = Field(default=200, ge=0, le=4_000)
    embedding: EmbeddingSettings = Field(default_factory=lambda: EmbeddingSettings())
    qdrant: QdrantSettings = Field(default_factory=lambda: QdrantSettings())

    @field_validator("strategy")
    @classmethod
    def validate_strategy(cls, value: str) -> str:
        # Import lazily so configuration can consult the resolver-owned registry
        # without introducing a module initialization cycle through services.
        from orchestrator.services.repository_context import (
            repository_context_strategy_names,
        )

        normalized = value.strip().casefold()
        supported = repository_context_strategy_names()
        if normalized not in supported:
            raise ValueError(
                f"Unknown repository context strategy {value!r}; "
                f"supported strategies: {', '.join(supported)}"
            )
        return normalized

    @model_validator(mode="after")
    def validate_chunking(self) -> RepositoryContextSettings:
        if self.chunk_overlap_chars >= self.chunk_size_chars:
            raise ValueError(
                "repository_context.chunk_overlap_chars must be smaller than chunk_size_chars"
            )
        return self


class TaskSettings(BaseModel):
    provider: Literal["trello", "github"] | None = None


class RolesSettings(BaseModel):
    spec: str | None = None
    implementation: str = "codex"
    review: str | None = None


class AgentRoleSettings(BaseModel):
    provider: str
    fallback_preset: str | None = None

    @field_validator("provider", "fallback_preset")
    @classmethod
    def validate_non_empty_string(
        cls, value: str | None, info: ValidationInfo
    ) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"Agent role {info.field_name} must be a non-empty string")
        return normalized


class AgentPresetConfig(BaseModel):
    """A reusable, role-local selection of a provider and runtime options.

    Presets intentionally store only the same role-local values exposed by the
    launcher.  They are templates, not provider definitions: a missing provider
    is therefore retained so the launcher can report it clearly rather than
    silently changing or deleting the preset.
    """

    provider: str
    model: str | None = None
    reasoning_effort: str | None = None
    permission_mode: Literal["dontAsk", "acceptEdits", "auto"] | None = None
    generation: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("provider", "model", "reasoning_effort")
    @classmethod
    def validate_non_empty_string(
        cls, value: str | None, info: ValidationInfo
    ) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"Agent preset {info.field_name} must be a non-empty string")
        return normalized

    @field_validator("generation")
    @classmethod
    def validate_generation(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError("Agent preset generation options must be a mapping")
        return dict(value)

    def runtime_options(self) -> dict[str, Any]:
        """Return only values that can be passed to runtime override validation."""

        options: dict[str, Any] = {}
        if self.model is not None:
            options["model"] = self.model
        if self.reasoning_effort is not None:
            options["reasoning_effort"] = self.reasoning_effort
        if self.permission_mode is not None:
            options["permission_mode"] = self.permission_mode
        if self.generation:
            options["generation"] = dict(self.generation)
        return options


class AgentsSettings(BaseModel):
    planner: AgentRoleSettings
    implementer: AgentRoleSettings
    reviewer: AgentRoleSettings


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
    """GitHub integration identities.

    ``app`` remains the reviewer identity for backwards compatibility. The
    implementer identity is deliberately separate from review credentials.
    """

    command: list[str] = Field(default_factory=lambda: ["gh"])
    timeout_seconds: int = 120
    app: GitHubAppSettings = Field(default_factory=GitHubAppSettings)
    implementer_app: GitHubAppSettings = Field(default_factory=GitHubAppSettings)
    planner_app: GitHubAppSettings = Field(default_factory=GitHubAppSettings)

    @model_validator(mode="before")
    @classmethod
    def normalize_disabled_apps(cls, value: Any) -> Any:
        if isinstance(value, dict):
            value = dict(value)
            for key in ("app", "implementer_app", "planner_app"):
                if key in value and value[key] is None:
                    value[key] = {}
        return value


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


def _max_concurrency_field() -> Any:
    """Declare the shared, provider-scoped concurrency ceiling.

    The named provider instance *is* the shared resource (one GPU, one API
    quota), so the ceiling belongs to its definition rather than to a separate
    pool abstraction. It is deliberately conservative: without an explicit
    value a provider admits exactly one agent call at a time.
    """

    return Field(default=1, gt=0)


class AgentProviderSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str
    max_concurrency: StrictInt = _max_concurrency_field()


@dataclass(frozen=True, slots=True)
class ProviderRuntimeCapabilities:
    """User-facing runtime controls exposed by one configured provider.

    This is deliberately a capability description, not a model catalogue.  The
    provider settings schemas remain the validation authority and model values
    are always entered as free-form identifiers when a provider accepts them.
    """

    supports_model: bool = False
    supports_reasoning_effort: bool = False
    supports_permission_mode: bool = False
    supports_generation: bool = False


class CodexCliProviderSettings(CodexIntegrationSettings):
    model_config = ConfigDict(extra="forbid")

    type: Literal["codex-cli"] = "codex-cli"
    max_concurrency: StrictInt = _max_concurrency_field()


class ClaudeCodeProviderSettings(BaseModel):
    """Safe, provider-local controls for Claude Code's print-mode CLI.

    The executable is intentionally a single entry rather than a free-form
    command list.  The provider constructs every supported CLI argument so
    configuration cannot opt into worktrees, session resume, remote runs, or
    permission-bypass switches that would undermine orchestrator ownership.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["claude-code"] = "claude-code"
    max_concurrency: StrictInt = _max_concurrency_field()
    executable: str = "claude"
    timeout_seconds: int = Field(default=1800, gt=0, le=86_400)
    model: str | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    permission_mode: Literal["dontAsk", "acceptEdits", "auto"] = "dontAsk"
    max_turns: int | None = Field(default=None, gt=0, le=100)
    max_budget_usd: float | None = Field(default=None, gt=0, le=10_000)
    bare_mode: bool = True

    @field_validator("executable", "model")
    @classmethod
    def validate_non_empty_string(
        cls, value: str | None, info: ValidationInfo
    ) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"Claude Code provider {info.field_name} must be a non-empty string")
        return normalized

    @field_validator("executable")
    @classmethod
    def validate_executable(cls, value: str) -> str:
        prohibited = (
            "--worktree",
            "--add-dir",
            "--resume",
            "--continue",
            "--cloud",
            "--permission-mode",
            "--allowedtools",
            "--dangerously-skip-permissions",
            "--bypass-permissions",
        )
        lowered = value.casefold()
        if any(flag in lowered for flag in prohibited):
            raise ValueError(
                "Claude Code executable must be one command entry, not a CLI command with options"
            )
        if any(character in value for character in ("\n", "\r", "\x00")):
            raise ValueError("Claude Code executable must be one command entry")
        is_path = "/" in value or "\\" in value
        if any(character.isspace() for character in value) and not is_path:
            raise ValueError(
                "Claude Code executable must be one command entry, not a CLI command with options"
            )
        return value


class GeminiGenerationSettings(BaseModel):
    """Small, provider-local set of generation controls for Vertex Gemini."""

    model_config = ConfigDict(extra="forbid")

    temperature: float = Field(default=0.2, ge=0, le=2)
    max_output_tokens: int | None = Field(default=None, gt=0)


class GeminiVertexProviderSettings(BaseModel):
    """Configuration for a Google Gen AI SDK client in Vertex AI mode."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["gemini-vertex"] = "gemini-vertex"
    max_concurrency: StrictInt = _max_concurrency_field()
    project: str = Field(min_length=1)
    location: str = Field(default="global", min_length=1)
    model: str = Field(min_length=1)
    timeout_seconds: int = Field(default=900, gt=0)
    generation: GeminiGenerationSettings = Field(default_factory=GeminiGenerationSettings)


class LiteLLMProviderSettings(BaseModel):
    """Configuration for LiteLLM's generic API-model execution backend.

    The API key is deliberately an environment-variable *reference* only. It
    is resolved by the provider at execution time and is never retained here.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["litellm"] = "litellm"
    max_concurrency: StrictInt = _max_concurrency_field()
    model: str = Field(min_length=1)
    api_key_env: str | None = None
    api_base: str | None = None
    timeout_seconds: float | None = Field(default=None, gt=0)
    temperature: float | None = None
    max_output_tokens: int | None = Field(default=None, gt=0)
    generation: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("model", "api_key_env", "api_base")
    @classmethod
    def validate_non_empty_string(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"LiteLLM provider {info.field_name} must be a non-empty string")
        return normalized

    @field_validator("generation")
    @classmethod
    def validate_generation(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        reserved = {
            "api_key",
            "api_base",
            "messages",
            "model",
            "response_format",
            "temperature",
            "timeout",
            "max_tokens",
        }
        conflicting = sorted(str(key) for key in value if str(key).casefold() in reserved)
        if conflicting:
            raise ValueError(
                "LiteLLM generation options cannot override provider settings: "
                f"{', '.join(conflicting)}"
            )
        return value


def _validate_provider_settings(
    values: Mapping[str, Any], provider_name: str
) -> (
    CodexCliProviderSettings
    | ClaudeCodeProviderSettings
    | GeminiVertexProviderSettings
    | LiteLLMProviderSettings
):
    """Validate a runtime-adjusted provider through its existing schema."""

    provider_type = values.get("type")
    try:
        if provider_type == "codex-cli":
            return CodexCliProviderSettings.model_validate(values)
        if provider_type == "claude-code":
            return ClaudeCodeProviderSettings.model_validate(values)
        if provider_type == "gemini-vertex":
            return GeminiVertexProviderSettings.model_validate(values)
        if provider_type == "litellm":
            return LiteLLMProviderSettings.model_validate(values)
    except ValueError as exc:
        raise ConfigurationError(
            f"Invalid runtime configuration for agent provider {provider_name!r}: {exc}"
        ) from exc
    raise ConfigurationError(
        f"Unknown agent provider type {provider_type!r} for provider {provider_name!r}"
    )


class IntegrationsSettings(BaseModel):
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
    configured_repository_key: str | None = Field(default=None, exclude=True)
    task: TaskSettings = Field(default_factory=TaskSettings)
    repository: RepositorySettings = Field(default_factory=RepositorySettings)
    repository_context: RepositoryContextSettings = Field(
        default_factory=RepositoryContextSettings
    )
    roles: RolesSettings = Field(default_factory=RolesSettings)
    agents: AgentsSettings
    agent_presets: dict[str, dict[str, AgentPresetConfig]] = Field(default_factory=dict)
    providers: dict[str, AgentProviderSettings] = Field(default_factory=dict)
    # Synthetic role-local provider name -> the configured provider it was
    # materialized from. Runtime overrides must never create extra capacity for
    # a shared backend, so concurrency accounting follows this back to the real
    # configured name. It is in-memory only and never serialized to YAML.
    runtime_provider_origins: dict[str, str] = Field(default_factory=dict, exclude=True)
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
        integrations = values.get("integrations", {})
        if isinstance(integrations, dict) and "jules" in integrations:
            raise ValueError(
                "integrations.jules is no longer supported; remove it and configure "
                "agents with a supported provider type such as 'codex-cli'"
            )
        raw_providers = values.get("providers", {})
        if isinstance(raw_providers, dict):
            rejected = sorted(
                name for name, settings in raw_providers.items()
                if isinstance(settings, dict) and settings.get("type") == "jules"
            )
            if rejected:
                raise ValueError(
                    "Jules provider type is no longer supported for provider(s) "
                    f"{', '.join(repr(name) for name in rejected)}; "
                    "choose a supported type such as 'codex-cli'"
                )
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
            missing_legacy_roles = [role for role in ("spec", "review") if not roles.get(role)]
            if missing_legacy_roles:
                raise ValueError(
                    "Legacy configuration requires explicit supported roles for "
                    f"{', '.join(missing_legacy_roles)}; set roles.spec and roles.review "
                    "to configured providers, or define agents and providers with a "
                    "supported type such as 'codex-cli'"
                )
            values["agents"] = {
                "planner": {"provider": roles["spec"]},
                "implementer": {"provider": roles.get("implementation", "codex")},
                "reviewer": {"provider": roles["review"]},
            }
        if not has_providers:
            integrations = integrations if isinstance(integrations, dict) else {}
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
                "codex": {"type": "codex-cli", **codex},
            }
            for role in ("planner", "implementer", "reviewer"):
                provider_name = values["agents"][role]["provider"]
                if provider_name != "codex":
                    raise ValueError(
                        f"Legacy role {role} references provider {provider_name!r}, "
                        "which cannot be resolved; define agents and providers with a "
                        "supported type such as 'codex-cli'"
                    )
        return values

    @model_validator(mode="after")
    def enforce_v1_safety(self) -> ProjectConfig:
        if self.safety.allow_direct_merge:
            raise ValueError("allow_direct_merge=true is not supported by V1")
        unknown_roles = sorted(set(self.agent_presets) - {"planner", "implementer", "reviewer"})
        if unknown_roles:
            raise ValueError(
                "Agent presets have unknown roles: " + ", ".join(unknown_roles)
            )
        for role, presets in self.agent_presets.items():
            if not presets:
                continue
            blank_names = [name for name in presets if not name.strip()]
            if blank_names:
                raise ValueError(f"Agent presets for {role} contain an empty preset name")
        for role in ("planner", "implementer", "reviewer"):
            fallback_preset = getattr(self.agents, role).fallback_preset
            if fallback_preset is None:
                continue
            preset = self.agent_presets.get(role, {}).get(fallback_preset)
            if preset is None:
                raise ValueError(
                    f"Fallback preset {fallback_preset!r} is not configured for {role}"
                )
            if preset.provider not in self.providers:
                raise ValueError(
                    f"Fallback preset {fallback_preset!r} for {role} references "
                    f"unconfigured provider {preset.provider!r}"
                )
            # Validate the preset's runtime fields through the target provider
            # schema now, rather than discovering an invalid fallback only
            # after the primary request has already failed.
            try:
                self.with_agent_preset(role, fallback_preset)
            except ConfigurationError as exc:
                raise ValueError(str(exc)) from exc
        return self

    def provider_options(self) -> list[tuple[str, str]]:
        """Return UI labels and stable provider names."""

        configured = sorted(
            self.providers.items(),
            key=lambda item: (
                {
                    ProviderSupportTier.FIRST_CLASS: 0,
                    ProviderSupportTier.OPTIONAL: 1,
                    ProviderSupportTier.UNSUPPORTED: 2,
                }[provider_support_tier(item[1].type)],
                item[0],
            ),
        )
        return [
            (provider_option_label(name, settings.type), name)
            for name, settings in configured
        ]

    def provider_concurrency_name(self, provider_name: str) -> str:
        """Return the configured provider whose capacity ``provider_name`` consumes.

        Role-local runtime overrides and presets materialize private provider
        names, but they still talk to the same backend account or GPU as the
        provider they were derived from, so they must share its ceiling.
        """

        seen: set[str] = set()
        current = provider_name
        while current in self.runtime_provider_origins and current not in seen:
            seen.add(current)
            current = self.runtime_provider_origins[current]
        return current

    def provider_max_concurrency(self, provider_name: str) -> int:
        """Return the configured ceiling enforced for one provider identity."""

        settings = self.providers.get(self.provider_concurrency_name(provider_name))
        if settings is None:
            settings = self.providers.get(provider_name)
        if settings is None:
            raise ConfigurationError(f"Agent provider {provider_name!r} is not configured")
        return settings.max_concurrency

    def presets_for_role(self, role: str) -> dict[str, AgentPresetConfig]:
        """Return a copy of the named presets available to one agent role."""

        if role not in {"planner", "implementer", "reviewer"}:
            raise ConfigurationError(f"Unknown agent role: {role}")
        return dict(self.agent_presets.get(role, {}))

    def with_agent_preset(self, role: str, preset_name: str) -> ProjectConfig:
        """Materialize a preset for one in-memory run without changing YAML."""

        preset = self.presets_for_role(role).get(preset_name)
        if preset is None:
            raise ConfigurationError(f"Agent preset {preset_name!r} is not configured for {role}")
        return self.with_agent_runtime_overrides(
            {role: preset.provider}, {role: preset.runtime_options()}
        )

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

    def provider_runtime_capabilities(self, provider_name: str) -> ProviderRuntimeCapabilities:
        """Return the interactive runtime controls supported by a provider.

        Keep this information beside the provider configuration schemas so
        presentation layers do not need to know provider implementation
        details or maintain a model allowlist.
        """

        settings = self.providers.get(provider_name)
        if settings is None:
            raise ConfigurationError(f"Agent provider {provider_name!r} is not configured")
        if settings.type == "codex-cli":
            return ProviderRuntimeCapabilities(
                supports_model=True, supports_reasoning_effort=True
            )
        if settings.type == "claude-code":
            return ProviderRuntimeCapabilities(
                supports_model=True,
                supports_reasoning_effort=True,
                supports_permission_mode=True,
            )
        if settings.type == "gemini-vertex":
            return ProviderRuntimeCapabilities(supports_model=True, supports_generation=True)
        if settings.type == "litellm":
            return ProviderRuntimeCapabilities(supports_model=True, supports_generation=True)
        return ProviderRuntimeCapabilities()

    def provider_runtime_values(self, provider_name: str) -> dict[str, Any]:
        """Return current user-facing runtime values for a configured provider."""

        settings = self.providers.get(provider_name)
        if settings is None:
            raise ConfigurationError(f"Agent provider {provider_name!r} is not configured")
        if settings.type == "codex-cli":
            codex = CodexCliProviderSettings.model_validate(settings.model_dump())
            return {
                "model": codex.model,
                "reasoning_effort": codex.reasoning_effort,
            }
        if settings.type == "claude-code":
            claude = ClaudeCodeProviderSettings.model_validate(settings.model_dump())
            return {
                "model": claude.model,
                "reasoning_effort": claude.effort,
                "permission_mode": claude.permission_mode,
            }
        if settings.type == "gemini-vertex":
            vertex = GeminiVertexProviderSettings.model_validate(settings.model_dump())
            return {
                "model": vertex.model,
                "generation": vertex.generation.model_dump(),
            }
        if settings.type == "litellm":
            litellm = LiteLLMProviderSettings.model_validate(settings.model_dump())
            generation = dict(litellm.generation)
            if litellm.temperature is not None:
                generation["temperature"] = litellm.temperature
            if litellm.max_output_tokens is not None:
                generation["max_output_tokens"] = litellm.max_output_tokens
            return {"model": litellm.model, "generation": generation}
        return {}

    def with_agent_runtime_overrides(
        self,
        provider_overrides: Mapping[str, str],
        runtime_overrides: Mapping[str, Mapping[str, Any]],
    ) -> ProjectConfig:
        """Apply role-local provider settings without changing persisted YAML.

        A named provider can serve more than one role.  Role-local options are
        therefore materialized under private in-memory names, allowing (for
        example) planner and implementer to use different Codex models in the
        same workflow.  The normal registry still constructs and validates the
        resulting provider configurations.
        """

        selected = self.with_agent_providers(provider_overrides)
        role_settings = {
            "planner": selected.agents.planner,
            "implementer": selected.agents.implementer,
            "reviewer": selected.agents.reviewer,
        }
        for role, overrides in runtime_overrides.items():
            setting = role_settings.get(role)
            if setting is None:
                raise ConfigurationError(f"Unknown agent role: {role}")
            if not isinstance(overrides, Mapping):
                raise ConfigurationError(f"Runtime options for {role} must be a mapping")
            provider_name = setting.provider
            base = selected.providers[provider_name]
            capability = selected.provider_runtime_capabilities(provider_name)
            allowed = {"model"} if capability.supports_model else set()
            if capability.supports_reasoning_effort:
                allowed.add("reasoning_effort")
            if capability.supports_permission_mode:
                allowed.add("permission_mode")
            if capability.supports_generation:
                allowed.add("generation")
            unsupported = sorted(set(overrides) - allowed)
            if unsupported:
                raise ConfigurationError(
                    f"Provider {provider_name!r} does not support runtime option(s): "
                    f"{', '.join(unsupported)}"
                )

            values = base.model_dump()
            for key, value in overrides.items():
                if key == "generation":
                    if not isinstance(value, Mapping):
                        raise ConfigurationError("Provider generation options must be a mapping")
                    if base.type == "litellm":
                        generation_overrides = dict(value)
                        for setting_name in ("temperature", "max_output_tokens"):
                            if setting_name in generation_overrides:
                                values[setting_name] = generation_overrides.pop(setting_name)
                        generation = values.get("generation", {})
                        if not isinstance(generation, dict):
                            generation = {}
                        generation.update(generation_overrides)
                        values["generation"] = generation
                        continue
                    generation = values.get("generation", {})
                    if not isinstance(generation, dict):
                        generation = {}
                    generation.update(value)
                    values["generation"] = generation
                else:
                    # The runtime/preset boundary is provider-neutral. Claude's
                    # CLI calls this native setting `effort`, while the shared
                    # launcher control is named `reasoning_effort`.
                    if base.type == "claude-code" and key == "reasoning_effort":
                        values["effort"] = value
                    else:
                        values[key] = value
            validated = _validate_provider_settings(values, provider_name)
            runtime_name = f"__tui_runtime_{role}"
            suffix = 2
            while runtime_name in selected.providers:
                runtime_name = f"__tui_runtime_{role}_{suffix}"
                suffix += 1
            selected.providers[runtime_name] = AgentProviderSettings.model_validate(
                validated.model_dump()
            )
            # Keep the concurrency identity of the provider this was derived
            # from: a temporary model/effort change does not create a second
            # quota pool or a second GPU.
            selected.runtime_provider_origins[runtime_name] = provider_name
            setting.provider = runtime_name
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


def _load_yaml_for_update(path: Path) -> dict[str, Any]:
    """Load editable YAML while retaining comments, ordering, and scalar style."""

    round_trip_yaml = YAML(typ="rt")
    round_trip_yaml.preserve_quotes = True
    try:
        content = round_trip_yaml.load(path.read_text(encoding="utf-8")) or {}
    except (OSError, RoundTripYAMLError) as exc:
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
    """Resolve the global configuration without consulting the process cwd.

    The configuration is application state, rather than state owned by the
    repository from which the command happens to be run.  Explicit ``--config``
    values retain normal command-line relative-path behavior; environment
    values and automatic discovery are anchored outside the working directory.
    """
    environment = os.environ if environ is None else environ
    user_home = (home or Path.home()).expanduser().resolve()

    def resolve_explicit_candidate(path: Path) -> Path:
        expanded = path.expanduser()
        if expanded.is_absolute():
            return expanded.resolve()
        return ((cwd or Path.cwd()).expanduser().resolve() / expanded).resolve()

    def resolve_environment_candidate(path: Path) -> Path:
        expanded = path.expanduser()
        if expanded.is_absolute():
            return expanded.resolve()
        return (user_home / expanded).resolve()

    if config_file is not None:
        explicit_path = resolve_explicit_candidate(config_file)
        if explicit_path.is_file():
            return explicit_path
        raise ConfigurationError(f"Explicit configuration file not found: {explicit_path}")

    for variable in _CONFIG_ENVIRONMENT_VARIABLES:
        configured = environment.get(variable)
        if not configured:
            continue
        environment_path = resolve_environment_candidate(Path(configured))
        if environment_path.is_file():
            return environment_path
        raise ConfigurationError(
            f"Configuration file from {variable} not found: {environment_path}"
        )

    # ``installation_dir`` remains an accepted parameter for backwards
    # compatible callers, but a pipx environment and an editable source tree
    # must never supply configuration implicitly.
    del installation_dir
    candidates = [
        user_home / ".config" / "orchestrator" / GLOBAL_CONFIG_FILENAME,
        user_home / ".orchestrator" / GLOBAL_CONFIG_FILENAME,
    ]

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

    def has_planner_github_app(self) -> bool:
        """Check whether any repository could use a Planner App without probing Git."""
        try:
            config_file = self._config_file
        except ConfigurationError:
            # Explicit GitHub issue reads worked without a global config,
            # even when the selected config path did not exist. A missing
            # file cannot contain a Planner identity.
            return False
        configuration = _load_yaml(config_file, required=True)
        defaults = configuration.get("defaults", {})
        repositories = configuration.get("repositories", {})
        entries = (
            [defaults, *repositories.values()]
            if isinstance(repositories, dict)
            else [defaults]
        )
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            integrations = entry.get("integrations", {})
            github = integrations.get("github", {}) if isinstance(integrations, dict) else {}
            if isinstance(github, dict) and github.get("planner_app"):
                return True
        return False

    def list_repositories(self) -> list[ConfiguredRepository]:
        """List registered repositories without resolving project settings.

        This is deliberately a discovery API. Callers must still use :meth:`load`
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

    def repository_settings(self, repository_key: str) -> RepositoryConfiguration:
        """Return the editable repository fields without requiring the path to exist."""

        configuration = _load_yaml(self._config_file, required=True)
        self._reject_misplaced_project_settings(configuration)
        repositories = configuration.get("repositories", {})
        if not isinstance(repositories, dict):
            raise ConfigurationError("'repositories' must be a mapping")
        repository = repositories.get(repository_key)
        if not isinstance(repository, dict):
            raise ConfigurationError(f"Repository entry '{repository_key}' must be a mapping")
        raw_path = repository.get("path")
        if raw_path is None:
            raise ConfigurationError(f"Repository entry '{repository_key}' has no path")

        configured_path = str(raw_path)
        resolved_path = self.resolve_repository_path(configured_path)

        defaults = configuration.get("defaults", {})
        default_validation = defaults.get("validation", {}) if isinstance(defaults, dict) else {}
        repository_validation = repository.get("validation", {})
        if not isinstance(default_validation, dict):
            raise ConfigurationError("'defaults.validation' must be a mapping")
        if not isinstance(repository_validation, dict):
            raise ConfigurationError(
                f"'repositories.{repository_key}.validation' must be a mapping"
            )
        try:
            validation = ValidationSettings.model_validate(
                _deep_merge(default_validation, repository_validation)
            )
        except ValueError as exc:
            raise ConfigurationError(
                f"Invalid validation configuration for repository {repository_key!r}: {exc}"
            ) from exc
        return RepositoryConfiguration(
            key=repository_key,
            path=configured_path,
            resolved_path=resolved_path,
            auto_detect=validation.auto_detect,
            commands=deepcopy(validation.commands),
        )

    def resolve_repository_path(self, path: str) -> Path:
        """Resolve a configured path using the global config directory as its anchor."""

        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self._config_file.parent / candidate
        return candidate.resolve()

    def repository_identity_aliases(self, reference: str) -> tuple[str, ...]:
        """Names that can identify the same repository in execution history.

        Include the resolved path even when the caller supplied a configured
        key, since path-only CLI runs persist their original path as the ref.
        """
        repositories = self.list_repositories()
        by_key = {repository.key: repository.path for repository in repositories}
        path = by_key.get(reference) or self.resolve_repository_path(reference)
        return tuple(
            dict.fromkeys(
                (
                    reference,
                    str(path),
                    path.as_posix(),
                    *(repo.key for repo in repositories if repo.path == path),
                )
            )
        )

    def save_repository(
        self,
        repository_key: str,
        path: str,
        auto_detect: bool,
        commands: list[list[str]],
        original_key: str | None = None,
    ) -> Path:
        """Create or update one repository entry in the global configuration.

        Existing repository-only settings are retained, including integrations,
        notifications, and presets. A rename occupies the original entry's
        position, which keeps selectors stable and diffs small.
        """

        normalized_key = repository_key.strip()
        normalized_path = path.strip()
        if not normalized_key:
            raise ConfigurationError("Repository key must be non-empty")
        if not normalized_path:
            raise ConfigurationError("Repository path must be non-empty")
        if "\0" in normalized_path:
            raise ConfigurationError("Repository path contains an invalid null character")
        try:
            resolved_path = self.resolve_repository_path(normalized_path)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ConfigurationError(f"Repository path is not usable: {exc}") from exc
        if resolved_path.exists() and not resolved_path.is_dir():
            raise ConfigurationError("Repository path exists but is not a directory")

        normalized_commands: list[list[str]] = []
        for index, command in enumerate(commands, start=1):
            if not isinstance(command, list) or not command:
                raise ConfigurationError(
                    f"Validation command {index} must contain at least one argument"
                )
            if not all(isinstance(argument, str) for argument in command):
                raise ConfigurationError(
                    f"Validation command {index} arguments must all be strings"
                )
            if not command[0].strip():
                raise ConfigurationError(
                    f"Validation command {index} must start with a program name"
                )
            normalized_commands.append(list(command))
        try:
            validation = ValidationSettings.model_validate(
                {"auto_detect": auto_detect, "commands": normalized_commands}
            )
        except ValueError as exc:
            raise ConfigurationError(f"Invalid repository validation settings: {exc}") from exc

        configuration = _load_yaml_for_update(self._config_file)
        self._reject_misplaced_project_settings(configuration)
        repositories = configuration.setdefault("repositories", {})
        if not isinstance(repositories, dict):
            raise ConfigurationError("'repositories' must be a mapping")

        source_key = original_key.strip() if original_key is not None else normalized_key
        existing = repositories.get(source_key)
        if original_key is not None and not isinstance(existing, dict):
            raise ConfigurationError(f"Repository {source_key!r} is not configured")
        if source_key != normalized_key and normalized_key in repositories:
            raise ConfigurationError(f"Repository key {normalized_key!r} is already configured")
        repository = deepcopy(existing) if isinstance(existing, dict) else {}
        repository["path"] = normalized_path
        repository_validation = repository.setdefault("validation", {})
        if not isinstance(repository_validation, dict):
            raise ConfigurationError(
                f"'repositories.{source_key}.validation' must be a mapping"
            )
        repository_validation["auto_detect"] = validation.auto_detect
        repository_validation["commands"] = deepcopy(validation.commands)

        defaults = configuration.get("defaults", {})
        candidate = _deep_merge(defaults, repository) if isinstance(defaults, dict) else repository
        candidate["path"] = Path(normalized_path)
        try:
            ProjectConfig.model_validate(candidate)
        except ValueError as exc:
            raise ConfigurationError(f"Invalid repository configuration: {exc}") from exc

        if source_key == normalized_key:
            repositories[normalized_key] = repository
        elif existing is not None:
            ordered_entries: list[tuple[Any, Any]] = []
            for key, value in repositories.items():
                ordered_entries.append(
                    (normalized_key, repository) if key == source_key else (key, value)
                )
            repositories.clear()
            repositories.update(ordered_entries)
        else:
            repositories[normalized_key] = repository
        return self._write_configuration(configuration)

    def delete_repository(self, repository_key: str) -> Path:
        """Remove only a global configuration entry; never touch its directory."""

        normalized_key = repository_key.strip()
        if not normalized_key:
            raise ConfigurationError("Repository key must be non-empty")
        configuration = _load_yaml_for_update(self._config_file)
        self._reject_misplaced_project_settings(configuration)
        repositories = configuration.get("repositories", {})
        if not isinstance(repositories, dict):
            raise ConfigurationError("'repositories' must be a mapping")
        if normalized_key not in repositories:
            raise ConfigurationError(f"Repository {normalized_key!r} is not configured")
        del repositories[normalized_key]
        return self._write_configuration(configuration)

    def load(self, task: DevelopmentTask, repository_override: str | None = None) -> ProjectConfig:
        global_config = _load_yaml(self._config_file, required=True)
        self._reject_misplaced_project_settings(global_config)
        repository_ref = repository_override or self.repository_reference_for_task(task)
        if not repository_ref:
            raise ConfigurationError(
                "Task has no repository; configure one in the task or use --repository"
            )

        registered = global_config.get("repositories", {})
        if not isinstance(registered, dict):
            raise ConfigurationError("'repositories' must be a mapping")

        entry: dict[str, Any]
        configured_repository_key: str | None = None
        if repository_ref in registered:
            raw_entry = registered[repository_ref]
            if not isinstance(raw_entry, dict):
                raise ConfigurationError(f"Repository entry '{repository_ref}' must be a mapping")
            entry = deepcopy(raw_entry)
            raw_path = entry.pop("path", None)
            if raw_path is None:
                raise ConfigurationError(f"Repository entry '{repository_ref}' has no path")
            repository_path = Path(str(raw_path)).expanduser()
            configured_repository_key = repository_ref
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
        legacy_local_config = repository_path / ".orchestrator.yml"
        if legacy_local_config.is_file():
            logger.warning(
                "Repository-local .orchestrator.yml is no longer supported and is ignored: %s",
                legacy_local_config,
            )
        merged = _deep_merge(defaults, entry)
        merged["path"] = repository_path
        merged["configured_repository_key"] = configured_repository_key
        repository_settings = merged.setdefault("repository", {})
        if not isinstance(repository_settings, dict):
            raise ConfigurationError("'repository' must be a mapping")
        repository_settings.setdefault("name", repository_ref)

        repository_context = merged.get("repository_context", {})
        if repository_context is None:
            repository_context = {}
        merged["repository_context"] = repository_context
        if not isinstance(repository_context, dict):
            raise ConfigurationError("'repository_context' must be a mapping")
        if str(repository_context.get("strategy", "qdrant")).strip().casefold() == "qdrant":
            qdrant = repository_context.setdefault("qdrant", {})
            if not isinstance(qdrant, dict):
                raise ConfigurationError("'repository_context.qdrant' must be a mapping")
            if (
                configured_repository_key is not None
                and str(qdrant.get("mode", "local")).strip().casefold() == "local"
            ):
                raw_index_path = qdrant.get("path")
                index_path = (
                    Path.home().expanduser() / ".orchestrator" / "qdrant"
                    if raw_index_path is None
                    else Path(str(raw_index_path)).expanduser()
                )
                if not index_path.is_absolute():
                    index_path = self._config_file.parent / index_path
                index_path = index_path.resolve()
                try:
                    index_path.relative_to(repository_path)
                except ValueError:
                    pass
                else:
                    raise ConfigurationError(
                        "Qdrant local index path must be outside the repository working tree"
                    )
                qdrant["path"] = index_path

        try:
            return ProjectConfig.model_validate(merged)
        except ValueError as exc:
            raise ConfigurationError(f"Invalid project configuration: {exc}") from exc

    def save_github_app_settings(
        self,
        repository_key: str,
        *,
        implementer_app: GitHubAppSettings,
        reviewer_app: GitHubAppSettings,
        planner_app: GitHubAppSettings | None = None,
    ) -> Path:
        """Persist App *references* in the resolved global configuration.

        The TUI only writes App IDs, installation IDs, and key file/environment
        references. Private key contents and installation tokens are neither
        accepted nor serialized here.
        """
        configuration = _load_yaml(self._config_file, required=True)
        self._reject_misplaced_project_settings(configuration)
        repositories = configuration.setdefault("repositories", {})
        if not isinstance(repositories, dict):
            raise ConfigurationError("'repositories' must be a mapping")
        repository = repositories.get(repository_key)
        if not isinstance(repository, dict):
            raise ConfigurationError(f"Repository entry '{repository_key}' must be a mapping")
        integrations = repository.setdefault("integrations", {})
        if not isinstance(integrations, dict):
            raise ConfigurationError("'integrations' must be a mapping")
        github = integrations.setdefault("github", {})
        if not isinstance(github, dict):
            raise ConfigurationError("'integrations.github' must be a mapping")

        defaults = configuration.get("defaults", {})
        default_integrations = (
            defaults.get("integrations", {}) if isinstance(defaults, dict) else {}
        )
        default_github = (
            default_integrations.get("github", {})
            if isinstance(default_integrations, dict)
            else {}
        )

        for key, settings in (
            ("implementer_app", implementer_app),
            ("app", reviewer_app),
            ("planner_app", planner_app),
        ):
            if settings is None:
                continue
            if settings.is_configured:
                github[key] = settings.model_dump(mode="json", exclude_none=True)
            elif isinstance(default_github, dict) and key in default_github:
                # An explicit null masks an inherited identity after the defaults merge.
                github[key] = None
            else:
                github.pop(key, None)

        return self._write_configuration(configuration)

    def save_agent_preset(
        self,
        repository_key: str,
        role: str,
        name: str,
        preset: AgentPresetConfig,
        *,
        overwrite: bool = False,
    ) -> Path:
        """Create or update one repository-scoped agent preset.

        The resolved global ``orchestrator.yml`` remains the sole persistence
        target.  A preset is intentionally written under the selected
        repository entry, preserving the established repository override
        behavior and never creating a local ``.orchestrator.yml``.
        """

        normalized_role = self._preset_role(role)
        normalized_name = self._preset_name(name)
        configuration, repository = self._repository_entry_for_write(repository_key)
        presets = repository.setdefault("agent_presets", {})
        if not isinstance(presets, dict):
            raise ConfigurationError("'agent_presets' must be a mapping")
        role_presets = presets.setdefault(normalized_role, {})
        if not isinstance(role_presets, dict):
            raise ConfigurationError(f"'agent_presets.{normalized_role}' must be a mapping")
        if normalized_name in role_presets and not overwrite:
            raise ConfigurationError(
                f"Agent preset {normalized_name!r} already exists for {normalized_role}"
            )
        role_presets[normalized_name] = preset.model_dump(
            mode="json", exclude_none=True, exclude_defaults=True
        )
        return self._write_configuration(configuration)

    def rename_agent_preset(
        self, repository_key: str, role: str, old_name: str, new_name: str
    ) -> Path:
        """Rename a role-local preset without affecting any provider definition."""

        normalized_role = self._preset_role(role)
        old = self._preset_name(old_name)
        new = self._preset_name(new_name)
        configuration, repository = self._repository_entry_for_write(repository_key)
        presets = repository.get("agent_presets", {})
        if not isinstance(presets, dict) or not isinstance(presets.get(normalized_role), dict):
            raise ConfigurationError(
                f"Agent preset {old!r} is not configured for {normalized_role}"
            )
        role_presets = presets[normalized_role]
        if old not in role_presets:
            raise ConfigurationError(
                f"Agent preset {old!r} is not configured for {normalized_role}"
            )
        if old != new and new in role_presets:
            raise ConfigurationError(f"Agent preset {new!r} already exists for {normalized_role}")
        if old != new:
            role_presets[new] = role_presets.pop(old)
            if self._effective_fallback_preset(configuration, repository, normalized_role) == old:
                self._set_repository_fallback(repository, normalized_role, new)
        return self._write_configuration(configuration)

    def delete_agent_preset(self, repository_key: str, role: str, name: str) -> Path:
        """Delete a role-local preset while leaving provider definitions intact."""

        normalized_role = self._preset_role(role)
        normalized_name = self._preset_name(name)
        configuration, repository = self._repository_entry_for_write(repository_key)
        presets = repository.get("agent_presets", {})
        if not isinstance(presets, dict) or not isinstance(presets.get(normalized_role), dict):
            raise ConfigurationError(
                f"Agent preset {normalized_name!r} is not configured for {normalized_role}"
            )
        role_presets = presets[normalized_role]
        if normalized_name not in role_presets:
            raise ConfigurationError(
                f"Agent preset {normalized_name!r} is not configured for {normalized_role}"
            )
        if (
            self._effective_fallback_preset(configuration, repository, normalized_role)
            == normalized_name
        ):
            raise ConfigurationError(
                f"Agent preset {normalized_name!r} is the configured fallback for "
                f"{normalized_role}; disable or change the fallback before deleting it"
            )
        del role_presets[normalized_name]
        if not role_presets:
            del presets[normalized_role]
        if not presets:
            repository.pop("agent_presets", None)
        return self._write_configuration(configuration)

    def save_agent_fallback_preset(
        self, repository_key: str, role: str, preset_name: str | None
    ) -> Path:
        """Persist one repository-scoped role fallback, including explicit disablement."""

        normalized_role = self._preset_role(role)
        normalized_name = self._preset_name(preset_name) if preset_name is not None else None
        configuration, repository = self._repository_entry_for_write(repository_key)
        if normalized_name is not None:
            defaults = configuration.get("defaults", {})
            default_presets = (
                defaults.get("agent_presets", {}) if isinstance(defaults, dict) else {}
            )
            repository_presets = repository.get("agent_presets", {})
            available: dict[str, Any] = {}
            for source in (default_presets, repository_presets):
                if not isinstance(source, dict):
                    continue
                role_presets = source.get(normalized_role, {})
                if isinstance(role_presets, dict):
                    available.update(role_presets)
            if normalized_name not in available:
                raise ConfigurationError(
                    f"Fallback preset {normalized_name!r} is not configured for "
                    f"{normalized_role}"
                )
        candidate_repository = deepcopy(repository)
        self._set_repository_fallback(
            candidate_repository, normalized_role, normalized_name
        )
        defaults = configuration.get("defaults", {})
        candidate = (
            _deep_merge(defaults, candidate_repository)
            if isinstance(defaults, dict)
            else candidate_repository
        )
        candidate["path"] = Path(".")
        try:
            ProjectConfig.model_validate(candidate)
        except ValueError as exc:
            raise ConfigurationError(f"Invalid fallback configuration: {exc}") from exc
        self._set_repository_fallback(repository, normalized_role, normalized_name)
        return self._write_configuration(configuration)

    @staticmethod
    def _set_repository_fallback(
        repository: dict[str, Any], role: str, preset_name: str | None
    ) -> None:
        agents = repository.setdefault("agents", {})
        if not isinstance(agents, dict):
            raise ConfigurationError("'agents' must be a mapping")
        settings = agents.setdefault(role, {})
        if not isinstance(settings, dict):
            raise ConfigurationError(f"'agents.{role}' must be a mapping")
        # Keep an explicit null so a repository can disable a fallback inherited
        # from defaults without duplicating its primary provider setting.
        settings["fallback_preset"] = preset_name

    @staticmethod
    def _effective_fallback_preset(
        configuration: dict[str, Any], repository: dict[str, Any], role: str
    ) -> str | None:
        defaults = configuration.get("defaults", {})
        merged = _deep_merge(defaults, repository) if isinstance(defaults, dict) else repository
        agents = merged.get("agents", {})
        if not isinstance(agents, dict):
            return None
        settings = agents.get(role, {})
        if not isinstance(settings, dict):
            return None
        value = settings.get("fallback_preset")
        return str(value).strip() if value is not None else None

    def _repository_entry_for_write(
        self, repository_key: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        configuration = _load_yaml(self._config_file, required=True)
        self._reject_misplaced_project_settings(configuration)
        repositories = configuration.setdefault("repositories", {})
        if not isinstance(repositories, dict):
            raise ConfigurationError("'repositories' must be a mapping")
        repository = repositories.get(repository_key)
        if not isinstance(repository, dict):
            raise ConfigurationError(f"Repository entry '{repository_key}' must be a mapping")
        return configuration, repository

    @staticmethod
    def _preset_role(role: str) -> str:
        normalized = role.strip().casefold()
        if normalized not in {"planner", "implementer", "reviewer"}:
            raise ConfigurationError(f"Unknown agent role: {role}")
        return normalized

    @staticmethod
    def _preset_name(name: str) -> str:
        normalized = name.strip()
        if not normalized:
            raise ConfigurationError("Agent preset name must be non-empty")
        return normalized

    def _write_configuration(self, configuration: dict[str, Any]) -> Path:
        try:
            round_trip_yaml = YAML(typ="rt")
            round_trip_yaml.preserve_quotes = True
            round_trip_yaml.default_flow_style = False
            round_trip_yaml.indent(mapping=2, sequence=4, offset=2)
            output = StringIO()
            round_trip_yaml.dump(configuration, output)
            temporary_path = self._config_file.with_suffix(".yml.tmp")
            temporary_path.write_text(output.getvalue(), encoding="utf-8")
            temporary_path.replace(self._config_file)
        except (OSError, RoundTripYAMLError) as exc:
            raise ConfigurationError(
                f"Cannot write configuration {self._config_file}: {exc}"
            ) from exc
        return self._config_file

    @staticmethod
    def repository_reference_for_task(task: DevelopmentTask) -> str | None:
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
            "agent_presets",
            "providers",
            "roles",
            "integrations",
            "repository",
            "repository_context",
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
                "Place them in 'defaults' or a 'repositories.<name>' entry."
            )
        ProjectConfigLoader._reject_repository_provider_concurrency(global_config)

    @staticmethod
    def _reject_repository_provider_concurrency(global_config: dict[str, Any]) -> None:
        """Keep ``max_concurrency`` authoritative in exactly one place.

        The ceiling protects a shared physical resource (a GPU's VRAM, one API
        account's quota) that does not change with the repository being worked
        on, so a per-repository override could only weaken it. Repositories
        that genuinely need independent capacity are using independent
        connections and belong under distinct provider names.
        """

        repositories = global_config.get("repositories")
        if not isinstance(repositories, dict):
            return
        offending: list[str] = []
        for key, entry in repositories.items():
            if not isinstance(entry, dict):
                continue
            providers = entry.get("providers")
            if not isinstance(providers, dict):
                continue
            offending.extend(
                f"repositories.{key}.providers.{name}.max_concurrency"
                for name, settings in providers.items()
                if isinstance(settings, dict) and "max_concurrency" in settings
            )
        if offending:
            rendered = ", ".join(sorted(offending))
            raise ConfigurationError(
                f"max_concurrency cannot be set per repository: {rendered}. "
                "Declare it once under 'defaults.providers.<name>.max_concurrency', "
                "or configure a separate provider name when a repository really "
                "needs an independent connection."
            )
