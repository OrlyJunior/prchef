from __future__ import annotations

import logging

from prchef.clients.factory import ClientFactory
from prchef.config.project_config import ConfigurationError, ProjectConfig
from prchef.models.agent import AgentRole
from prchef.models.workflow_event import WorkflowEventKind
from prchef.models.workflow_status import WorkflowStatus
from prchef.providers.agent_provider import AgentProvider, AgentProviderError
from prchef.providers.concurrency import (
    ConcurrencyLimitedAgentProvider,
    ProviderConcurrencyCoordinator,
)
from prchef.providers.diagnostic_provider import DiagnosticAgentProvider
from prchef.providers.fallback_provider import FallbackAgentProvider
from prchef.repositories.workflow_repository import WorkflowRepository
from prchef.services.workflow_stage_events import StageEvents

logger = logging.getLogger("prchef.services.workflow_service")


_PROVIDER_WAIT_DIAGNOSTIC_KIND = "PROVIDER_SLOT_WAIT"
_PROVIDER_ADMITTED_DIAGNOSTIC_KIND = "PROVIDER_SLOT_ADMITTED"


class WorkflowAgentRuntime:
    """Wrap providers with diagnostics and shared admission limits."""

    def __init__(
        self,
        repository: WorkflowRepository,
        events: StageEvents,
        client_factory: ClientFactory,
        coordinator: ProviderConcurrencyCoordinator,
    ) -> None:
        self._repository = repository
        self._events = events
        self._client_factory = client_factory
        self._provider_coordinator = coordinator

    def _diagnostic_agent(
        self,
        provider: AgentProvider,
        config: ProjectConfig,
        role: AgentRole,
        execution_id: str,
    ) -> AgentProvider:
        configured = getattr(config.agents, role.value).provider
        provider_settings = config.providers[configured].model_dump()
        fallback_preset_name = getattr(config.agents, role.value).fallback_preset
        primary_provider_name = configured
        primary_preset_name: str | None = None
        if fallback_preset_name is not None:
            for preset_name, preset in config.presets_for_role(role.value).items():
                if not configured.startswith("__tui_runtime_") and preset.provider != configured:
                    continue
                try:
                    candidate = config.with_agent_preset(role.value, preset_name)
                except ConfigurationError:
                    continue
                candidate_name = getattr(candidate.agents, role.value).provider
                if candidate.providers[candidate_name].model_dump() == provider_settings:
                    primary_provider_name = preset.provider
                    primary_preset_name = preset_name
                    break
        primary = self._admitted_agent(
            DiagnosticAgentProvider(
                provider,
                role=role,
                provider_config={
                    "name": primary_provider_name,
                    "preset": primary_preset_name,
                    **provider_settings,
                },
                record=lambda category, event_role, payload: self._events._record_diagnostic(
                    execution_id,
                    category,
                    payload,
                    stage=self._events._active_stage_of(execution_id),
                    role=event_role,
                ),
                cancellation_check=lambda: self._events._raise_if_cancellation_requested(
                    execution_id
                ),
            ),
            config=config,
            provider_name=configured,
            execution_id=execution_id,
        )
        if fallback_preset_name is None:
            return primary

        fallback_preset = config.presets_for_role(role.value)[fallback_preset_name]
        fallback_config = config.with_agent_preset(role.value, fallback_preset_name)
        fallback_configured = getattr(fallback_config.agents, role.value).provider
        fallback_settings = fallback_config.providers[fallback_configured].model_dump()
        primary_values = config.provider_runtime_values(configured)
        fallback_values = fallback_config.provider_runtime_values(fallback_configured)

        def create_fallback() -> AgentProvider:
            fallback_provider = self._client_factory.create(
                fallback_config, agent_roles={role}
            ).agent(role)
            # The fallback is charged to its own configured provider name; the
            # primary's slot is already released by the time this runs.
            return self._admitted_agent(
                DiagnosticAgentProvider(
                    fallback_provider,
                    role=role,
                    provider_config={
                        "name": fallback_preset.provider,
                        "preset": fallback_preset_name,
                        **fallback_settings,
                    },
                    record=lambda category, event_role, payload: self._events._record_diagnostic(
                        execution_id,
                        category,
                        payload,
                        stage=self._events._active_stage_of(execution_id),
                        role=event_role,
                    ),
                    cancellation_check=lambda: self._events._raise_if_cancellation_requested(
                        execution_id
                    ),
                ),
                config=fallback_config,
                provider_name=fallback_configured,
                execution_id=execution_id,
            )

        def record_fallback(error: AgentProviderError) -> None:
            payload = {
                "role": role.value,
                "reason": error.category,
                "primary_provider": primary_provider_name,
                "primary_preset": primary_preset_name,
                "primary_model": primary_values.get("model"),
                "fallback_provider": fallback_preset.provider,
                "fallback_preset": fallback_preset_name,
                "fallback_model": fallback_values.get("model"),
            }
            logger.warning(
                "[workflow] %s agent capacity failure; using fallback %s -> %s/%s",
                role.value,
                primary_provider_name,
                fallback_preset.provider,
                fallback_preset_name,
            )
            self._events._emit(
                execution_id,
                WorkflowEventKind.AGENT_FALLBACK,
                stage=self._events._active_stage_of(execution_id),
                message=(
                    f"{role.value.title()} switched from {primary_provider_name} to "
                    f"{fallback_preset.provider}/{fallback_preset_name}"
                ),
                payload=payload,
            )

        return FallbackAgentProvider(
            primary, fallback_factory=create_fallback, on_fallback=record_fallback
        )

    def _admitted_agent(
        self,
        provider: AgentProvider,
        *,
        config: ProjectConfig,
        provider_name: str,
        execution_id: str,
    ) -> AgentProvider:
        """Bound one agent call by its provider's shared concurrency ceiling.

        The slot is acquired immediately before the call and released right
        after it, so a workflow never reserves a provider while it is doing
        Git, validation, or GitHub work. Role-local runtime variants are
        charged to the configured provider they were derived from.
        """

        concurrency_name = config.provider_concurrency_name(provider_name)
        return ConcurrencyLimitedAgentProvider(
            provider,
            provider_name=concurrency_name,
            coordinator=self._provider_coordinator,
            limit=config.provider_max_concurrency(provider_name),
            execution_id=execution_id,
            on_wait=lambda name: self._record_provider_wait(execution_id, name),
            on_admitted=lambda name: self._record_provider_admission(execution_id, name),
            cancellation_check=lambda: self._events._raise_if_cancellation_requested(execution_id),
        )

    def _record_provider_wait(self, execution_id: str, provider_name: str) -> None:
        """Persist that started work is blocked on shared provider capacity."""

        stage = self._events._active_stage_of(execution_id)
        self._repository.update_status(execution_id, WorkflowStatus.WAITING)
        self._events._emit(
            execution_id,
            WorkflowEventKind.STATUS_CHANGED,
            stage=stage,
            status=WorkflowStatus.WAITING,
            message=f"Waiting for {provider_name} capacity",
            payload={"kind": _PROVIDER_WAIT_DIAGNOSTIC_KIND, "provider": provider_name},
        )

    def _record_provider_admission(self, execution_id: str, provider_name: str) -> None:
        stage = self._events._active_stage_of(execution_id)
        self._repository.update_status(execution_id, WorkflowStatus.RUNNING)
        self._events._emit(
            execution_id,
            WorkflowEventKind.STATUS_CHANGED,
            stage=stage,
            status=WorkflowStatus.RUNNING,
            message=f"Admitted to {provider_name}",
            payload={"kind": _PROVIDER_ADMITTED_DIAGNOSTIC_KIND, "provider": provider_name},
        )
