from __future__ import annotations

from collections.abc import Callable

from pydantic import ValidationError

from orchestrator.clients.codex_client import CodexClient
from orchestrator.clients.command_client import CommandClient
from orchestrator.clients.jules_client import JulesClient
from orchestrator.config.project_config import (
    AgentProviderSettings,
    CodexCliProviderSettings,
    ConfigurationError,
    GeminiVertexProviderSettings,
    JulesProviderSettings,
)
from orchestrator.providers.agent_provider import AgentProvider
from orchestrator.providers.codex_provider import CodexAgentProvider
from orchestrator.providers.gemini_vertex_provider import GeminiVertexAgentProvider
from orchestrator.providers.jules_provider import JulesAgentProvider

ProviderBuilder = Callable[[str, AgentProviderSettings, CommandClient], AgentProvider]


class AgentProviderRegistry:
    """Explicit provider-type registry; transport details stay in each builder."""

    def __init__(self, jules_api_key: str | None) -> None:
        self._jules_api_key = jules_api_key
        self._builders: dict[str, ProviderBuilder] = {
            "jules": self._build_jules,
            "codex-cli": self._build_codex,
            "gemini-vertex": self._build_gemini_vertex,
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

    def _build_jules(
        self, name: str, settings: AgentProviderSettings, commands: CommandClient
    ) -> AgentProvider:
        del commands
        options = JulesProviderSettings.model_validate(settings.model_dump())
        return JulesAgentProvider(name, JulesClient(self._jules_api_key, options))

    @staticmethod
    def _build_codex(
        name: str, settings: AgentProviderSettings, commands: CommandClient
    ) -> AgentProvider:
        del name
        options = CodexCliProviderSettings.model_validate(settings.model_dump())
        return CodexAgentProvider(
            CodexClient(
                commands,
                options.command,
                options.timeout_seconds,
                approval_policy=options.approval_policy,
            )
        )

    @staticmethod
    def _build_gemini_vertex(
        name: str, settings: AgentProviderSettings, commands: CommandClient
    ) -> AgentProvider:
        del commands
        options = GeminiVertexProviderSettings.model_validate(settings.model_dump())
        return GeminiVertexAgentProvider(name, options)
