"""Thin presentation controller over configuration, tasks, and workflow services."""

from __future__ import annotations

import logging
import os
import stat
import tempfile
import threading
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
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
    RepositoryConfiguration,
)
from orchestrator.models.agent import AgentRole
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.execution_diagnostic import ExecutionDiagnostic
from orchestrator.models.repository_context import IndexLifecycleState, RepositoryIndexStatus
from orchestrator.models.workflow_batch import WorkflowBatch
from orchestrator.models.workflow_event import (
    WorkflowEvent,
    WorkflowEventKind,
    WorkflowEventListener,
)
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.models.workflow_status import (
    ACTIVE_WORKFLOW_STATUSES,
    WorkflowStage,
    WorkflowStatus,
)
from orchestrator.models.workflow_step import WorkflowStep
from orchestrator.providers.concurrency import ProviderConcurrencyCoordinator
from orchestrator.repositories.workflow_repository import WorkflowRepository
from orchestrator.services.execution_dispatcher import ExecutionDispatcher, ExecutionHandle
from orchestrator.services.project_context import (
    IMPLEMENTER_INSTRUCTIONS_FILE,
    PLANNER_INSTRUCTIONS_FILE,
    REVIEWER_INSTRUCTIONS_FILE,
    load_implementer_instructions,
    load_planner_instructions,
    load_reviewer_instructions,
)
from orchestrator.services.repository_context import resolve_repository_context_strategy
from orchestrator.services.task_provider import LabelCatalogProvider, TaskProvider
from orchestrator.services.workflow_service import WorkflowService

TaskProviderFactory = Callable[[str, str], TaskProvider]
WorkflowServiceFactory = Callable[[TaskProvider], WorkflowService]
logger = logging.getLogger("orchestrator")


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


@dataclass(frozen=True, slots=True)
class WorkflowBatchLaunch:
    """One set of role selections applied to an ordered group of tasks."""

    repository: str
    task_provider: str
    task_ids: tuple[str, ...]
    agent_providers: Mapping[AgentRole, str]
    agent_runtime_options: Mapping[AgentRole, Mapping[str, Any]] = field(default_factory=dict)


@dataclass(slots=True)
class _BatchRun:
    batch_id: str
    size: int
    required_providers: frozenset[str]
    listener: WorkflowEventListener | None
    condition: threading.Condition = field(default_factory=threading.Condition)
    unavailable: set[str] = field(default_factory=set)
    positions: dict[str, int] = field(default_factory=dict)
    statuses: dict[int, WorkflowStatus] = field(default_factory=dict)
    generations: dict[int, int] = field(default_factory=dict)
    skipped: set[int] = field(default_factory=set)
    stopping: bool = False
    submissions_done: bool = False

    def ready(self) -> bool:
        # Dispatcher evaluates this under its own lock. Never acquire a
        # controller lock or call back into the dispatcher here.
        return not any(provider in self.unavailable for provider in self.required_providers)

    def complete(self) -> bool:
        if not self.submissions_done:
            return False
        final = {
            WorkflowStatus.APPROVED, WorkflowStatus.CHANGES_REQUESTED,
            WorkflowStatus.FAILED, WorkflowStatus.CANCELLED,
        }
        if self.stopping:
            final.add(WorkflowStatus.NEEDS_HUMAN)
        return all(
            position in self.skipped or self.statuses.get(position) in final
            for position in range(self.size)
        )


class WorkflowController:
    """Expose existing orchestration services in forms useful to terminal views.

    The controller owns launch orchestration (including ordered batches), but
    no workflow-stage transitions. It delegates durable records to the
    repository and every execution to the existing ``WorkflowService`` seam.
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
        dispatcher: ExecutionDispatcher | None = None,
        provider_coordinator: ProviderConcurrencyCoordinator | None = None,
        repository_index_clients: Any | None = None,
    ) -> None:
        self._repository = workflow_repository
        self._config_loader = config_loader
        self._client_factory = client_factory
        self._task_provider_factory = task_provider_factory
        self._verbose = verbose
        self._workflow_service_factory = workflow_service_factory
        # Process-scoped so every execution the UI starts, resumes, or
        # continues competes for the same bounded capacity.
        self._dispatcher = dispatcher or ExecutionDispatcher()
        self._provider_coordinator = provider_coordinator or ProviderConcurrencyCoordinator()
        self._repository_index_clients = repository_index_clients
        self._selected_repository: str | None = None
        self._active_services: dict[str, WorkflowService] = {}
        self._active_batches: dict[str, _BatchRun] = {}
        self._execution_batches: dict[str, str] = {}
        self._index_operations: set[str] = set()
        self._index_operation_lock = threading.RLock()

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

    def configured_agent_provider_options(self, repository: str) -> list[tuple[str, str]]:
        """Return selectable providers with deprecated backends visibly labeled."""

        return self.project_config(repository).provider_options()

    def provider_runtime_capabilities(
        self, repository: str, provider: str
    ) -> ProviderRuntimeCapabilities:
        return self.project_config(repository).provider_runtime_capabilities(provider)

    def provider_runtime_values(self, repository: str, provider: str) -> dict[str, Any]:
        return self.project_config(repository).provider_runtime_values(provider)

    def agent_presets(self, repository: str, role: AgentRole) -> dict[str, AgentPresetConfig]:
        return self.project_config(repository).presets_for_role(role.value)

    def agent_fallback_preset(self, repository: str, role: AgentRole) -> str | None:
        config = self.project_config(repository)
        return {
            AgentRole.PLANNER: config.agents.planner,
            AgentRole.IMPLEMENTER: config.agents.implementer,
            AgentRole.REVIEWER: config.agents.reviewer,
        }[role].fallback_preset

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

    def save_agent_fallback_preset(
        self, repository: str, role: AgentRole, preset_name: str | None
    ) -> Path:
        return self._config_loader.save_agent_fallback_preset(
            repository, role.value, preset_name
        )

    def github_app_settings(
        self, repository: str
    ) -> tuple[GitHubAppSettings, GitHubAppSettings]:
        github = self.project_config(repository).integrations.github
        return github.implementer_app, github.app

    def agent_instructions(self, repository: str) -> tuple[str | None, str | None, str | None]:
        """Load role-specific local guidance from a configured repository root."""
        repository_path = self.repository_path(repository)
        return (
            load_planner_instructions(repository_path),
            load_implementer_instructions(repository_path),
            load_reviewer_instructions(repository_path),
        )

    def save_agent_instructions(self, repository: str, role: AgentRole, content: str) -> Path:
        """Save one role's local instructions without changing Git state."""
        filename = {
            AgentRole.PLANNER: PLANNER_INSTRUCTIONS_FILE,
            AgentRole.IMPLEMENTER: IMPLEMENTER_INSTRUCTIONS_FILE,
            AgentRole.REVIEWER: REVIEWER_INSTRUCTIONS_FILE,
        }.get(role)
        if filename is None:
            raise ValueError(
                "Agent instructions are only supported for planner, implementer, and reviewer"
            )
        path = self.repository_path(repository) / filename
        if path.is_symlink():
            raise ValueError(f"Cannot save instructions through a symlink: {filename}")
        existing_mode: int | None = None
        if os.name == "posix":
            try:
                existing_status = path.lstat()
            except FileNotFoundError:
                pass
            else:
                if stat.S_ISREG(existing_status.st_mode):
                    existing_mode = stat.S_IMODE(existing_status.st_mode)
        # Replacing the directory entry avoids following a link introduced after the check.
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, prefix=f".{filename}.",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(content)
            if existing_mode is not None:
                temporary_path.chmod(existing_mode)
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return path

    def repository_path(self, repository: str) -> Path:
        """Resolve a registered repository key to its configured root."""
        for configured in self.repositories():
            if configured.key == repository:
                return configured.path
        raise ValueError(f"Unknown configured repository: {repository}")

    def repository_settings(self, repository: str) -> RepositoryConfiguration:
        """Load the repository fields supported by the dedicated settings screen."""

        return self._config_loader.repository_settings(repository)

    def repository_index_status(self, repository: str) -> RepositoryIndexStatus:
        """Return a repository-scoped index status without starting a workflow."""

        config = self.project_config(repository)
        if config.repository_context.strategy == "heuristic":
            status = RepositoryIndexStatus(
                strategy="heuristic",
                state=IndexLifecycleState.READY,
            )
        else:
            strategy = resolve_repository_context_strategy(
                config.repository_context.strategy,
                settings=config.repository_context,
                repository_key=config.configured_repository_key,
                configured_root=config.path,
                client_manager=self._repository_index_clients,
            )
            inspect = getattr(strategy, "inspect_index", None)
            if not callable(inspect):
                raise ValueError(
                    f"Repository context strategy {config.repository_context.strategy!r} "
                    "does not expose index lifecycle management"
                )
            status = inspect()
        with self._index_operation_lock:
            if repository in self._index_operations:
                return replace(status, state=IndexLifecycleState.INDEXING, error_message=None)
        return status

    def build_repository_index(self, repository: str) -> RepositoryIndexStatus:
        """Build or rebuild one configured repository's semantic index."""

        return self._run_repository_index_operation(repository, "build_index")

    def refresh_repository_index(self, repository: str) -> RepositoryIndexStatus:
        """Synchronize changed files for one configured repository index."""

        return self._run_repository_index_operation(repository, "refresh_changed_files")

    def clear_repository_index(self, repository: str) -> RepositoryIndexStatus:
        """Drop one configured repository's semantic collection."""

        return self._run_repository_index_operation(repository, "clear_index")

    def _run_repository_index_operation(
        self, repository: str, operation: str
    ) -> RepositoryIndexStatus:
        config = self.project_config(repository)
        if config.repository_context.strategy != "qdrant":
            raise ValueError(
                "Semantic index actions require repository_context.strategy: qdrant; "
                "heuristic context is already available without an index."
            )
        with self._index_operation_lock:
            if repository in self._index_operations:
                raise RuntimeError(f"An index operation is already running for {repository!r}")
            self._index_operations.add(repository)
        try:
            strategy = resolve_repository_context_strategy(
                config.repository_context.strategy,
                settings=config.repository_context,
                repository_key=config.configured_repository_key,
                configured_root=config.path,
                client_manager=self._repository_index_clients,
            )
            action = getattr(strategy, operation, None)
            if not callable(action):
                raise ValueError("The configured context strategy cannot manage an index")
            result = action()
            if isinstance(result, RepositoryIndexStatus):
                return result
            raise ValueError("The configured context strategy returned an invalid index status")
        finally:
            with self._index_operation_lock:
                self._index_operations.discard(repository)

    def resolve_repository_path(self, path: str) -> Path:
        """Resolve an editor path exactly as normal configuration loading will."""

        return self._config_loader.resolve_repository_path(path)

    def save_repository(
        self,
        repository_key: str,
        path: str,
        auto_detect: bool,
        commands: list[list[str]],
        original_key: str | None = None,
    ) -> Path:
        """Persist repository settings and retain the resulting key as selected."""

        saved = self._config_loader.save_repository(
            repository_key,
            path,
            auto_detect,
            commands,
            original_key=original_key,
        )
        self._selected_repository = repository_key.strip()
        self.repositories()
        return saved

    def delete_repository(self, repository: str) -> Path:
        """Delete repository configuration without performing filesystem operations."""

        deleted = self._config_loader.delete_repository(repository)
        if self._selected_repository == repository:
            self._selected_repository = None
        self.repositories()
        return deleted

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

    def list_task_labels(
        self, *, repository: str, task_provider: str
    ) -> tuple[str, ...] | None:
        """Return the provider's label names, or ``None`` without label support.

        ``None`` is deliberately distinct from an empty tuple: an empty catalog
        is a supported filter with nothing to choose, while ``None`` means the
        launcher offers no label filter. Lookup failures propagate.
        """

        provider = self._task_provider_factory(task_provider, repository)
        if not isinstance(provider, LabelCatalogProvider):
            return None
        return tuple(provider.list_labels())

    def submit_workflow(
        self,
        launch: WorkflowLaunch,
        *,
        event_listener: WorkflowEventListener | None = None,
        admission_gate: Callable[[], bool] | None = None,
    ) -> ExecutionHandle:
        """Submit a launch through the shared dispatcher and return its handle."""

        task_id = launch.task_id.strip()
        if not task_id:
            raise ValueError("Select a task before starting a workflow")
        if launch.task_provider not in self.task_providers(launch.repository):
            raise ValueError(f"Unsupported task provider: {launch.task_provider}")

        # Validate selections through the core configuration model before a
        # potentially long-running workflow begins. WorkflowService resolves
        # the configuration again as part of its normal persisted pipeline.
        base_config = self.project_config(launch.repository)
        resolved_config = base_config.with_agent_runtime_overrides(
            {role.value: provider for role, provider in launch.agent_providers.items()},
            {role.value: options for role, options in launch.agent_runtime_options.items()},
        )
        if self._verbose:
            resolutions: list[str] = []
            for role in AgentRole:
                configured = launch.agent_providers.get(
                    role, getattr(base_config.agents, role.value).provider
                )
                effective = getattr(resolved_config.agents, role.value).provider
                provider_type = resolved_config.providers[effective].type
                runtime_note = (
                    f" [runtime={effective}]" if effective != configured else ""
                )
                resolutions.append(
                    f"{role.value} -> {configured} ({provider_type}){runtime_note}"
                )
            logger.debug("[config] TUI agent resolution: %s", "; ".join(resolutions))
        provider = self._task_provider_factory(launch.task_provider, launch.repository)
        service = self._create_service(provider)
        submission_options: dict[str, Any] = {}
        if admission_gate is not None:
            submission_options["admission_gate"] = admission_gate
        handle = service.submit_execution(
            task_id,
            launch.repository,
            agent_providers=launch.agent_providers,
            agent_runtime_options=launch.agent_runtime_options,
            event_listener=self._tracking_listener(service, event_listener),
            **submission_options,
        )
        record_repository = getattr(self._repository, "record_submission_repository", None)
        if callable(record_repository):
            record_repository(handle.execution_id, launch.repository)
        return handle

    def start_workflow(
        self,
        launch: WorkflowLaunch,
        *,
        event_listener: WorkflowEventListener | None = None,
    ) -> WorkflowExecution:
        """Submit a launch and wait for it, preserving the blocking TUI worker."""

        return self.submit_workflow(launch, event_listener=event_listener).result()

    def start_batch(
        self,
        launch: WorkflowBatchLaunch,
        *,
        event_listener: WorkflowEventListener | None = None,
        on_batch_created: Callable[[WorkflowBatch], None] | None = None,
    ) -> WorkflowBatch | None:
        """Persist a batch, submit its items in order, and wait until it closes."""

        task_ids = tuple(
            dict.fromkeys(
                task_id.strip() for task_id in launch.task_ids if task_id.strip()
            )
        )
        if not task_ids:
            return None
        if launch.task_provider not in self.task_providers(launch.repository):
            raise ValueError(f"Unsupported task provider: {launch.task_provider}")

        # Validate the shared role/provider/runtime selections before the
        # durable batch row is created. Individual submissions validate again
        # through their normal path, but a shared configuration error should
        # fail once rather than leave a batch with every item unstarted.
        config = self.project_config(launch.repository).with_agent_runtime_overrides(
            {role.value: provider for role, provider in launch.agent_providers.items()},
            {
                role.value: options
                for role, options in launch.agent_runtime_options.items()
            },
        )

        tasks_by_id = {
            task.id: task
            for task in self.list_tasks(
                repository=launch.repository,
                task_provider=launch.task_provider,
            )
        }
        missing = [task_id for task_id in task_ids if task_id not in tasks_by_id]
        if missing:
            raise ValueError(f"Selected tasks are no longer open: {', '.join(missing)}")
        existing = self._repository.executions_for_tasks(
            launch.task_provider, list(task_ids), launch.repository
        )
        if existing:
            raise ValueError(
                "Tasks already have executions: " + ", ".join(existing)
            )

        ordered_tasks = [tasks_by_id[task_id] for task_id in task_ids]
        batch = self._repository.create_batch(
            launch.repository, launch.task_provider, ordered_tasks
        )
        required: set[str] = set()
        for role in AgentRole:
            settings = getattr(config.agents, role.value)
            required.add(config.provider_concurrency_name(settings.provider))
            if settings.fallback_preset:
                preset = config.presets_for_role(role.value)[settings.fallback_preset]
                required.add(config.provider_concurrency_name(preset.provider))
        run = _BatchRun(batch.id, len(ordered_tasks), frozenset(required), event_listener)
        self._active_batches[batch.id] = run
        try:
            if on_batch_created is not None:
                on_batch_created(batch)
            for position, task in enumerate(ordered_tasks):
                if run.stopping:
                    with run.condition:
                        run.skipped.update(range(position, len(ordered_tasks)))
                        run.condition.notify_all()
                    break
                # Close the list/start race as far as the repository boundary
                # permits. A task that gained an execution is skipped.
                if self._repository.executions_for_tasks(
                    launch.task_provider, [task.id], launch.repository
                ):
                    with run.condition:
                        run.skipped.add(position)
                        run.condition.notify_all()
                    continue
                item_launch = WorkflowLaunch(
                    repository=launch.repository,
                    task_provider=launch.task_provider,
                    task_id=task.id,
                    agent_providers=launch.agent_providers,
                    agent_runtime_options=launch.agent_runtime_options,
                )

                def observe_item(event: WorkflowEvent, *, item_position: int = position) -> None:
                    self._observe_batch_event(run, item_position, event)

                try:
                    handle = self.submit_workflow(
                        item_launch, event_listener=observe_item, admission_gate=run.ready
                    )
                    # Test doubles and legacy-compatible services may not emit
                    # EXECUTION_CREATED, while the handle still exposes the
                    # synchronously persisted execution identity.
                    self._repository.record_batch_item_execution(
                        batch.id, position, handle.execution_id
                    )
                    self._execution_batches[handle.execution_id] = batch.id
                    with run.condition:
                        run.positions[handle.execution_id] = position
                        run.statuses.setdefault(position, WorkflowStatus.PENDING)
                        run.generations.setdefault(position, 0)
                    threading.Thread(
                        target=self._watch_batch_handle,
                        args=(run, position, 0, handle),
                        daemon=True,
                    ).start()
                    if run.stopping:
                        self.cancel_workflow(handle.execution_id)
                except Exception:
                    logger.exception(
                        "Batch %s task %s failed; continuing with the next task",
                        batch.id,
                        task.id,
                    )
                    with run.condition:
                        run.skipped.add(position)
                        run.condition.notify_all()
                    continue
            with run.condition:
                run.submissions_done = True
                run.condition.notify_all()
                while not run.complete():
                    self._reconcile_batch_statuses(run)
                    if not run.complete():
                        # A different controller/process can cancel a settled
                        # NEEDS_HUMAN item without emitting an event here.
                        run.condition.wait(timeout=1.0)
            return self._repository.get_batch(batch.id)
        finally:
            self._active_batches.pop(batch.id, None)
            for execution_id, owning_batch in list(self._execution_batches.items()):
                if owning_batch == batch.id:
                    self._execution_batches.pop(execution_id, None)

    def _reconcile_batch_statuses(self, run: _BatchRun) -> None:
        """Refresh unresolved items from durable state while holding the run condition."""
        final = {
            WorkflowStatus.APPROVED, WorkflowStatus.CHANGES_REQUESTED,
            WorkflowStatus.FAILED, WorkflowStatus.CANCELLED,
        }
        unresolved = {
            execution_id: position
            for execution_id, position in run.positions.items()
            if run.statuses.get(position) not in final
        }
        if not unresolved:
            return
        try:
            statuses = self._repository.execution_statuses(list(unresolved))
        except Exception:
            logger.exception("Could not reconcile batch %s execution statuses", run.batch_id)
            return
        for execution_id, position in unresolved.items():
            persisted = statuses.get(execution_id)
            if persisted is None:
                logger.warning(
                    "Could not reconcile batch %s execution %s: missing status",
                    run.batch_id, execution_id,
                )
                continue
            if persisted not in ACTIVE_WORKFLOW_STATUSES:
                run.statuses[position] = persisted

    def _observe_batch_event(
        self, run: _BatchRun, position: int, event: WorkflowEvent
    ) -> None:
        """Keep batch bookkeeping isolated from workflow observer failures."""
        try:
            if event.kind is WorkflowEventKind.EXECUTION_CREATED:
                self._repository.record_batch_item_execution(
                    run.batch_id, position, event.execution_id
                )
                self._execution_batches[event.execution_id] = run.batch_id
            with run.condition:
                run.positions[event.execution_id] = position
                if (
                    event.status is not None
                    and run.statuses.get(position) is not WorkflowStatus.CANCELLED
                ):
                    run.statuses[position] = event.status
                payload = event.payload or {}
                if (
                    event.kind is WorkflowEventKind.DIAGNOSTIC
                    and payload.get("kind") == "PROVIDER_QUOTA_EXHAUSTED"
                    and isinstance(payload.get("provider"), str)
                ):
                    run.unavailable.add(payload["provider"])
                run.condition.notify_all()
        except Exception:
            logger.exception("Could not update batch %s from workflow event", run.batch_id)
        if run.listener is not None:
            try:
                payload = dict(event.payload or {})
                payload.update({
                    "batch_id": run.batch_id,
                    "batch_position": position,
                    "batch_size": run.size,
                })
                run.listener(replace(event, payload=payload))
            except Exception:
                logger.exception("Batch %s listener failed", run.batch_id)

    def _watch_batch_handle(
        self, run: _BatchRun, position: int, generation: int, handle: ExecutionHandle
    ) -> None:
        try:
            status = handle.result().status
        except Exception:
            logger.exception("Batch %s execution %s failed", run.batch_id, handle.execution_id)
            status = WorkflowStatus.FAILED
        with run.condition:
            if run.generations.get(position, 0) != generation:
                return
            # The handle has settled, so this generation will not report again.
            # A persisted status may refine the outcome (for example a
            # cancellation recorded after the result), but an active one is a
            # stale read: recording it would leave the item open forever and
            # start_batch would never be woken to observe completion.
            try:
                persisted = self._repository.get_execution(handle.execution_id).status
            except Exception:
                pass
            else:
                if persisted not in ACTIVE_WORKFLOW_STATUSES:
                    status = persisted
            run.statuses[position] = status
            run.condition.notify_all()

    def _create_service(self, provider: TaskProvider) -> WorkflowService:
        if self._workflow_service_factory is not None:
            return self._workflow_service_factory(provider)
        return WorkflowService(
            workflow_repository=self._repository,
            task_provider=provider,
            config_loader=self._config_loader,
            client_factory=self._client_factory,
            verbose=self._verbose,
            dispatcher=self._dispatcher,
            provider_coordinator=self._provider_coordinator,
            repository_index_clients=self._repository_index_clients,
        )

    def _tracking_listener(
        self, service: WorkflowService, event_listener: WorkflowEventListener | None
    ) -> WorkflowEventListener:
        """Track which service owns an execution so cancellation can reach it."""

        def observe(event: WorkflowEvent) -> None:
            if event.kind is WorkflowEventKind.EXECUTION_CREATED:
                self._active_services[event.execution_id] = service
            elif event.kind is WorkflowEventKind.WORKFLOW_FINISHED:
                self._active_services.pop(event.execution_id, None)
            if event_listener is not None:
                event_listener(event)

        return observe

    def cancel_workflow(self, execution_id: str) -> bool:
        """Request cancellation through the service boundary when it is live.

        Falling back to the repository keeps a request durable when the TUI was
        restarted while an execution is still marked running.
        """
        service = self._active_services.get(execution_id)
        if service is not None:
            accepted = service.cancel_execution(execution_id)
        else:
            accepted = self._repository.request_cancellation(execution_id)
        if accepted:
            batch_id = self._execution_batches.get(execution_id)
            run = self._active_batches.get(batch_id) if batch_id is not None else None
            if run is not None:
                with run.condition:
                    position = run.positions.get(execution_id)
                    if (
                        position is not None
                        and self._repository.get_execution(execution_id).status
                        is WorkflowStatus.CANCELLED
                    ):
                        run.statuses[position] = WorkflowStatus.CANCELLED
                        run.condition.notify_all()
        return accepted

    def cancel_batch(self, batch_id: str) -> bool:
        run = self._active_batches.get(batch_id)
        if run is None:
            return False
        with run.condition:
            run.stopping = True
            active_ids = [
                execution_id for execution_id, position in run.positions.items()
                if run.statuses.get(position) in ACTIVE_WORKFLOW_STATUSES
            ]
            run.condition.notify_all()
        for execution_id in active_ids:
            try:
                self.cancel_workflow(execution_id)
            except Exception:
                logger.exception("Could not cancel batch %s execution %s", batch_id, execution_id)
        return True

    def batch_unavailable_providers(self, batch_id: str) -> tuple[str, ...]:
        run = self._active_batches.get(batch_id)
        if run is None:
            return ()
        with run.condition:
            return tuple(sorted(run.unavailable))

    def batch_required_providers(self, batch_id: str) -> frozenset[str]:
        run = self._active_batches.get(batch_id)
        return run.required_providers if run is not None else frozenset()

    def mark_batch_provider_available(self, batch_id: str, provider: str) -> None:
        run = self._active_batches.get(batch_id)
        if run is None:
            raise ValueError("The batch is no longer running")
        with run.condition:
            run.unavailable.discard(provider)
            run.condition.notify_all()
        self._dispatcher.reevaluate()

    def execution_in_live_batch(self, execution_id: str) -> bool:
        run = self._active_batches.get(self._execution_batches.get(execution_id, ""))
        return run is not None and not run.stopping and not run.complete()

    def _batch_submission_options(
        self, execution_id: str, listener: WorkflowEventListener | None
    ) -> tuple[WorkflowEventListener | None, dict[str, Any]]:
        run = self._active_batches.get(self._execution_batches.get(execution_id, ""))
        if run is None or run.stopping or run.complete():
            return listener, {}
        position = run.positions[execution_id]

        def observe(event: WorkflowEvent) -> None:
            self._observe_batch_event(run, position, event)
            if listener is not None:
                try:
                    listener(event)
                except Exception:
                    logger.exception("Execution %s listener failed", execution_id)

        return observe, {"admission_gate": run.ready}

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

    def task_execution_status(
        self,
        repository: str,
        task_provider: str,
        tasks: list[DevelopmentTask],
    ) -> dict[str, WorkflowExecution]:
        return self._repository.executions_for_tasks(
            task_provider, [task.id for task in tasks], repository
        )

    def batches(self) -> list[WorkflowBatch]:
        return list(reversed(self._repository.list_batches()))

    def batch_detail(self, batch_id: str) -> WorkflowBatch:
        return self._repository.get_batch(batch_id)

    def execution_detail(self, execution_id: str) -> tuple[WorkflowExecution, list[WorkflowStep]]:
        return (
            self._repository.get_execution(execution_id),
            self._repository.list_steps(execution_id),
        )

    def execution_snapshot(self, execution_id: str) -> WorkflowExecution:
        """Read status even when related detail rows cannot be loaded."""
        return self._repository.get_execution(execution_id)

    def execution_diagnostics(self, execution_id: str) -> list[ExecutionDiagnostic]:
        method = getattr(self._repository, "list_diagnostics", None)
        return method(execution_id) if callable(method) else []

    def submit_response(
        self,
        execution_id: str,
        response: str,
        *,
        event_listener: WorkflowEventListener | None = None,
    ) -> ExecutionHandle:
        """Queue a human-response resume through the shared dispatcher."""

        service = self._service_for_execution(self._repository.get_execution(execution_id))
        # Register before submitting so a queued resume is already cancellable
        # through its own service; a rejected submission is rolled back.
        self._active_services[execution_id] = service
        try:
            run = self._active_batches.get(self._execution_batches.get(execution_id, ""))
            with run.condition if run is not None else nullcontext():
                batch_listener, options = self._batch_submission_options(
                    execution_id, event_listener
                )
                handle = service.submit_response(
                    execution_id,
                    response,
                    event_listener=self._tracking_listener(service, batch_listener),
                    **options,
                )
                if options and run is not None:
                    position = run.positions[execution_id]
                    generation = run.generations.get(position, 0) + 1
                    run.generations[position] = generation
            if options and run is not None:
                threading.Thread(
                    target=self._watch_batch_handle,
                    args=(run, position, generation, handle), daemon=True,
                ).start()
            return handle
        except Exception:
            self._active_services.pop(execution_id, None)
            raise

    def respond_and_resume(
        self,
        execution_id: str,
        response: str,
        *,
        event_listener: WorkflowEventListener | None = None,
    ) -> WorkflowExecution:
        return self.submit_response(
            execution_id, response, event_listener=event_listener
        ).result()

    def _service_for_execution(self, execution: WorkflowExecution) -> WorkflowService:
        provider = self._task_provider_factory(
            execution.task_source, execution.repository_ref or execution.repository
        )
        return self._create_service(provider)

    def continuation_stage(self, execution: WorkflowExecution) -> WorkflowStage | None:
        """Where a manual continuation of this execution would restart, if anywhere."""

        return WorkflowService.continuation_stage(
            execution, lambda: self.execution_diagnostics(execution.id)
        )

    def continuation_feedback(self, execution_id: str) -> str:
        return self._service_for_execution(
            self._repository.get_execution(execution_id)
        ).continuation_feedback(execution_id)

    def submit_continuation(
        self,
        execution_id: str,
        feedback: str,
        *,
        event_listener: WorkflowEventListener | None = None,
    ) -> ExecutionHandle:
        """Queue a manual continuation through the shared dispatcher."""

        service = self._service_for_execution(self._repository.get_execution(execution_id))
        self._active_services[execution_id] = service
        try:
            run = self._active_batches.get(self._execution_batches.get(execution_id, ""))
            with run.condition if run is not None else nullcontext():
                batch_listener, options = self._batch_submission_options(
                    execution_id, event_listener
                )
                handle = service.submit_continuation(
                    execution_id,
                    feedback,
                    event_listener=self._tracking_listener(service, batch_listener),
                    **options,
                )
                if options and run is not None:
                    position = run.positions[execution_id]
                    generation = run.generations.get(position, 0) + 1
                    run.generations[position] = generation
            if options and run is not None:
                threading.Thread(
                    target=self._watch_batch_handle,
                    args=(run, position, generation, handle), daemon=True,
                ).start()
            return handle
        except Exception:
            self._active_services.pop(execution_id, None)
            raise

    def continue_execution(
        self,
        execution_id: str,
        feedback: str,
        *,
        event_listener: WorkflowEventListener | None = None,
    ) -> WorkflowExecution:
        return self.submit_continuation(
            execution_id, feedback, event_listener=event_listener
        ).result()
