"""Thin presentation controller over configuration, tasks, and workflow services."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.clients.factory import ClientFactory
from orchestrator.config.project_config import (
    AgentPresetConfig,
    ConfiguredRepository,
    GitHubAppSettings,
    ProjectConfig,
    ProjectConfigLoader,
    ProviderRuntimeCapabilities,
)
from orchestrator.models.agent import AgentRole
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.workflow_event import WorkflowEventListener
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.models.workflow_step import WorkflowStep
from orchestrator.repositories.workflow_repository import WorkflowRepository
from orchestrator.services.project_context import (
    IMPLEMENTER_INSTRUCTIONS_FILE,
    REVIEWER_INSTRUCTIONS_FILE,
    load_implementer_instructions,
    load_reviewer_instructions,
)
from orchestrator.services.task_provider import TaskProvider
from orchestrator.services.workflow_service import WorkflowService

TaskProviderFactory = Callable[[str, str], TaskProvider]
WorkflowServiceFactory = Callable[[TaskProvider], WorkflowService]


@dataclass(frozen=True, slots=True)
class WorkflowLaunch:
    """Selections made by the terminal UI before a workflow is started."""

    repository: str
    task_provider: str
    task_id: str
    agent_providers: Mapping[AgentRole, str]
    # These values affect this invocation only. They are never written through
    # ProjectConfigLoader and are validated by ProjectConfig before execution.
    agent_runtime_options: Mapping[AgentRole, Mapping[str, Any]] = field(default_factory=dict)


class WorkflowController:
    """Expose existing orchestration services in forms useful to terminal views.

    The controller has no workflow transitions or persistence logic. It only
    collects selections, creates the already-existing ``WorkflowService``, and
    reads persisted executions for inspection.
    """

    def __init__(
        self,
        *,
        workflow_repository: WorkflowRepository,
        config_loader: ProjectConfigLoader,
        client_factory: ClientFactory,
        task_provider_factory: TaskProviderFactory,
        verbose: bool = False,
        workflow_service_factory: WorkflowServiceFactory | None = None,
    ) -> None:
        self._repository = workflow_repository
        self._config_loader = config_loader
        self._client_factory = client_factory
        self._task_provider_factory = task_provider_factory
        self._verbose = verbose
        self._workflow_service_factory = workflow_service_factory
        self._selected_repository: str | None = None

    def repositories(self) -> list[ConfiguredRepository]:
        repositories = self._config_loader.list_repositories()
        configured_keys = {repository.key for repository in repositories}
        if self._selected_repository not in configured_keys:
            self._selected_repository = repositories[0].key if repositories else None
        return repositories

    @property
    def selected_repository(self) -> str | None:
        """The configured repository retained while the user navigates the TUI."""

        if self._selected_repository is None:
            self.repositories()
        return self._selected_repository

    def select_repository(self, repository: str) -> None:
        """Retain a repository selection after validating it against current config."""

        if repository not in {configured.key for configured in self.repositories()}:
            raise ValueError(f"Unknown configured repository: {repository}")
        self._selected_repository = repository

    def project_config(self, repository: str) -> ProjectConfig:
        selection = DevelopmentTask(
            id="tui-selection",
            source="system",
            title="Interactive workflow selection",
            description="Configuration resolution for terminal UI selections.",
            url="",
            repository=repository,
        )
        return self._config_loader.load(selection, repository)

    def task_providers(self, repository: str) -> list[str]:
        configured = self.project_config(repository).task.provider
        providers = ["github", "trello"]
        if configured is not None:
            providers.remove(configured)
            providers.insert(0, configured)
        return providers

    def agent_providers(self, repository: str) -> dict[AgentRole, str]:
        config = self.project_config(repository)
        return {
            AgentRole.PLANNER: config.agents.planner.provider,
            AgentRole.IMPLEMENTER: config.agents.implementer.provider,
            AgentRole.REVIEWER: config.agents.reviewer.provider,
        }

    def configured_agent_providers(self, repository: str) -> list[str]:
        return list(self.project_config(repository).providers)

    def provider_runtime_capabilities(
        self, repository: str, provider: str
    ) -> ProviderRuntimeCapabilities:
        return self.project_config(repository).provider_runtime_capabilities(provider)

    def provider_runtime_values(self, repository: str, provider: str) -> dict[str, Any]:
        return self.project_config(repository).provider_runtime_values(provider)

    def agent_presets(self, repository: str, role: AgentRole) -> dict[str, AgentPresetConfig]:
        return self.project_config(repository).presets_for_role(role.value)

    def save_agent_preset(
        self,
        repository: str,
        role: AgentRole,
        name: str,
        preset: AgentPresetConfig,
        *,
        overwrite: bool = False,
    ) -> Path:
        return self._config_loader.save_agent_preset(
            repository, role.value, name, preset, overwrite=overwrite
        )

    def rename_agent_preset(
        self, repository: str, role: AgentRole, old_name: str, new_name: str
    ) -> Path:
        return self._config_loader.rename_agent_preset(
            repository, role.value, old_name, new_name
        )

    def delete_agent_preset(self, repository: str, role: AgentRole, name: str) -> Path:
        return self._config_loader.delete_agent_preset(repository, role.value, name)

    def github_app_settings(
        self, repository: str
    ) -> tuple[GitHubAppSettings, GitHubAppSettings]:
        github = self.project_config(repository).integrations.github
        return github.implementer_app, github.app

    def agent_instructions(self, repository: str) -> tuple[str | None, str | None]:
        """Load role-specific local guidance from a configured repository root."""
        repository_path = self.repository_path(repository)
        return (
            load_implementer_instructions(repository_path),
            load_reviewer_instructions(repository_path),
        )

    def save_agent_instructions(self, repository: str, role: AgentRole, content: str) -> Path:
        """Save one role's local instructions without changing Git state."""
        filename = {
            AgentRole.IMPLEMENTER: IMPLEMENTER_INSTRUCTIONS_FILE,
            AgentRole.REVIEWER: REVIEWER_INSTRUCTIONS_FILE,
        }.get(role)
        if filename is None:
            raise ValueError(
                "Agent instructions are only supported for implementer and reviewer"
            )
        path = self.repository_path(repository) / filename
        path.write_text(content, encoding="utf-8")
        return path

    def repository_path(self, repository: str) -> Path:
        """Resolve a registered repository key to its configured root."""
        for configured in self.repositories():
            if configured.key == repository:
                return configured.path
        raise ValueError(f"Unknown configured repository: {repository}")

    def save_github_app_settings(
        self,
        repository: str,
        *,
        implementer_app: GitHubAppSettings,
        reviewer_app: GitHubAppSettings,
    ) -> Path:
        return self._config_loader.save_github_app_settings(
            repository,
            implementer_app=implementer_app,
            reviewer_app=reviewer_app,
        )

    def list_tasks(
        self, *, repository: str, task_provider: str, limit: int = 50
    ) -> list[DevelopmentTask]:
        provider = self._task_provider_factory(task_provider, repository)
        return provider.list_tasks(limit=limit)

    def start_workflow(
        self,
        launch: WorkflowLaunch,
        *,
        event_listener: WorkflowEventListener | None = None,
    ) -> WorkflowExecution:
        task_id = launch.task_id.strip()
        if not task_id:
            raise ValueError("Select a task before starting a workflow")
        if launch.task_provider not in self.task_providers(launch.repository):
            raise ValueError(f"Unsupported task provider: {launch.task_provider}")

        # Validate selections through the core configuration model before a
        # potentially long-running workflow begins. WorkflowService resolves
        # the configuration again as part of its normal persisted pipeline.
        self.project_config(launch.repository).with_agent_runtime_overrides(
            {role.value: provider for role, provider in launch.agent_providers.items()},
            {role.value: options for role, options in launch.agent_runtime_options.items()},
        )
        provider = self._task_provider_factory(launch.task_provider, launch.repository)
        service = (
            self._workflow_service_factory(provider)
            if self._workflow_service_factory is not None
            else WorkflowService(
                workflow_repository=self._repository,
                task_provider=provider,
                config_loader=self._config_loader,
                client_factory=self._client_factory,
                verbose=self._verbose,
            )
        )
        return service.run(
            task_id,
            launch.repository,
            agent_providers=launch.agent_providers,
            agent_runtime_options=launch.agent_runtime_options,
            event_listener=event_listener,
        )

    def executions(self, query: str = "") -> list[WorkflowExecution]:
        normalized = query.strip().casefold()
        executions = list(reversed(self._repository.list_executions()))
        if not normalized:
            return executions
        return [
            execution
            for execution in executions
            if normalized
            in " ".join(
                [
                    execution.id,
                    execution.task_id,
                    execution.task_source,
                    execution.repository or "",
                    execution.status.value,
                    execution.current_stage.value,
                    str((execution.task_snapshot or {}).get("title", "")),
                ]
            ).casefold()
        ]

    def execution_detail(self, execution_id: str) -> tuple[WorkflowExecution, list[WorkflowStep]]:
        return (
            self._repository.get_execution(execution_id),
            self._repository.list_steps(execution_id),
        )

    def respond_and_resume(
        self,
        execution_id: str,
        response: str,
        *,
        event_listener: WorkflowEventListener | None = None,
    ) -> WorkflowExecution:
        execution = self._repository.get_execution(execution_id)
        provider = self._task_provider_factory(
            execution.task_source, execution.repository_ref or execution.repository
        )
        service = (
            self._workflow_service_factory(provider)
            if self._workflow_service_factory is not None
            else WorkflowService(
                workflow_repository=self._repository,
                task_provider=provider,
                config_loader=self._config_loader,
                client_factory=self._client_factory,
                verbose=self._verbose,
            )
        )
        return service.respond_and_resume(
            execution_id, response, event_listener=event_listener
        )
