from __future__ import annotations

from collections.abc import Callable

from pydantic import ValidationError

from orchestrator.clients.claude_code_client import ClaudeCodeClient
from orchestrator.clients.codex_client import CodexClient
from orchestrator.clients.command_client import CommandClient
from orchestrator.config.project_config import (
    AgentProviderSettings,
    ClaudeCodeProviderSettings,
    CodexCliProviderSettings,
    ConfigurationError,
    GeminiVertexProviderSettings,
    LiteLLMProviderSettings,
)
from orchestrator.providers.agent_provider import AgentProvider
from orchestrator.providers.claude_code_provider import ClaudeCodeAgentProvider
from orchestrator.providers.codex_provider import CodexAgentProvider
from orchestrator.providers.gemini_vertex_provider import GeminiVertexAgentProvider
from orchestrator.providers.litellm_provider import LiteLLMAgentProvider

ProviderBuilder = Callable[[str, AgentProviderSettings, CommandClient], AgentProvider]


class AgentProviderRegistry:
    """Explicit provider-type registry; transport details stay in each builder."""

    def __init__(self) -> None:
        self._builders: dict[str, ProviderBuilder] = {
            "codex-cli": self._build_codex,
            "claude-code": self._build_claude_code,
            "gemini-vertex": self._build_gemini_vertex,
            "litellm": self._build_litellm,
        }

    def create(
        self,
        name: str,
        providers: dict[str, AgentProviderSettings],
        commands: CommandClient,
    ) -> AgentProvider:
        settings = providers.get(name)
        if settings is None:
            configured = ", ".join(sorted(providers)) or "<none>"
            raise ConfigurationError(
                f"Unknown agent provider name {name!r}; configured providers: {configured}"
            )
        builder = self._builders.get(settings.type)
        if builder is None:
            supported = ", ".join(sorted(self._builders))
            raise ConfigurationError(
                f"Unknown agent provider type {settings.type!r} for provider {name!r}; "
                f"supported types: {supported}"
            )
        try:
            return builder(name, settings, commands)
        except ValidationError as exc:
            raise ConfigurationError(
                f"Invalid configuration for agent provider {name!r}: {exc}"
            ) from exc

    @staticmethod
    def _build_codex(
        name: str, settings: AgentProviderSettings, commands: CommandClient
    ) -> AgentProvider:
        options = CodexCliProviderSettings.model_validate(settings.model_dump())
        return CodexAgentProvider(
            CodexClient(
                commands,
                options.command,
                options.timeout_seconds,
                approval_policy=options.approval_policy,
                model=options.model,
                reasoning_effort=options.reasoning_effort,
                provider_name=name,
            )
        )

    @staticmethod
    def _build_claude_code(
        name: str, settings: AgentProviderSettings, commands: CommandClient
    ) -> AgentProvider:
        options = ClaudeCodeProviderSettings.model_validate(settings.model_dump())
        return ClaudeCodeAgentProvider(
            name,
            ClaudeCodeClient(
                commands,
                options.executable,
                options.timeout_seconds,
                model=options.model,
                effort=options.effort,
                permission_mode=options.permission_mode,
                max_turns=options.max_turns,
                max_budget_usd=options.max_budget_usd,
                bare_mode=options.bare_mode,
                provider_name=name,
            ),
        )

    @staticmethod
    def _build_gemini_vertex(
        name: str, settings: AgentProviderSettings, commands: CommandClient
    ) -> AgentProvider:
        del commands
        options = GeminiVertexProviderSettings.model_validate(settings.model_dump())
        return GeminiVertexAgentProvider(name, options)

    @staticmethod
    def _build_litellm(
        name: str, settings: AgentProviderSettings, commands: CommandClient
    ) -> AgentProvider:
        del commands
        options = LiteLLMProviderSettings.model_validate(settings.model_dump())
        return LiteLLMAgentProvider(name, options)
