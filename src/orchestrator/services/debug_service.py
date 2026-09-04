from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from orchestrator.clients.factory import ClientFactory, WorkflowClients
from orchestrator.config.project_config import (
    ConfigurationError,
    ProjectConfig,
    ProjectConfigLoader,
)
from orchestrator.models.agent import AgentExecutionReference, AgentRole
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.pull_request import PullRequest
from orchestrator.models.repository_spec import RepositorySpec
from orchestrator.models.validation_result import ValidationCheck, ValidationResult
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.services.implementation_service import ImplementationService
from orchestrator.services.review_service import ReviewService
from orchestrator.services.spec_service import SpecService
from orchestrator.services.task_provider import TaskProvider
from orchestrator.services.validation_service import ValidationService


class DebugStage(StrEnum):
    TASK_LOADING = "task-loading"
    SPEC_GENERATION = "spec-generation"
    IMPLEMENTATION = "implementation"
    VALIDATION = "validation"
    REVIEW = "review"


class DebugExecutionError(RuntimeError):
    pass


@dataclass(slots=True)
class DebugInputs:
    """Explicit inputs for one isolated debug invocation."""

    task_source: str | None = None
    task_id: str | None = None
    repository: str | None = None
    base_branch: str | None = None
    feature_branch: str | None = None
    prompt: str | None = None
    task: DevelopmentTask | None = None
    spec: RepositorySpec | None = None
    validation: ValidationResult | None = None
    pull_request: PullRequest | None = None
    diff: str | None = None


TaskProviderFactory = Callable[[str, str | None], TaskProvider]


_STAGE_ALIASES = {
    "task": DebugStage.TASK_LOADING,
    "load-task": DebugStage.TASK_LOADING,
    "task-loading": DebugStage.TASK_LOADING,
    "spec": DebugStage.SPEC_GENERATION,
    "generate-spec": DebugStage.SPEC_GENERATION,
    "spec-generation": DebugStage.SPEC_GENERATION,
    "implementation": DebugStage.IMPLEMENTATION,
    "implement": DebugStage.IMPLEMENTATION,
    "validation": DebugStage.VALIDATION,
    "validate": DebugStage.VALIDATION,
    "review": DebugStage.REVIEW,
}


def parse_debug_stage(value: str) -> DebugStage:
    normalized = value.strip().casefold().replace("_", "-")
    try:
        return _STAGE_ALIASES[normalized]
    except KeyError as exc:
        supported = ", ".join(stage.value for stage in DebugStage)
        raise DebugExecutionError(
            f"Unsupported debug stage {value!r}; choose one of: {supported}"
        ) from exc


class DebugService:
    """Assemble inputs and run exactly one workflow/provider boundary."""

    def __init__(
        self,
        *,
        config_loader: ProjectConfigLoader,
        client_factory: ClientFactory,
        task_provider_factory: TaskProviderFactory,
    ) -> None:
        self._config_loader = config_loader
        self._client_factory = client_factory
        self._task_provider_factory = task_provider_factory

    def execute(
        self,
        execution: WorkflowExecution | None,
        stage: DebugStage,
        *,
        inputs: DebugInputs | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        values = inputs or DebugInputs()
        if dry_run:
            return self._envelope(
                execution,
                stage,
                dry_run=True,
                inputs=self._describe_inputs(execution, stage, values),
            )

        if stage is DebugStage.TASK_LOADING:
            task = self._resolve_task(execution, values, stage=stage)
            if task is None:
                raise DebugExecutionError("task-loading requires provider task input")
            return self._envelope(execution, stage, result={"task": asdict(task)})

        task = (
            None
            if stage is DebugStage.VALIDATION
            else self._resolve_task(
                execution,
                values,
                stage=stage,
                required=not (
                    stage is DebugStage.IMPLEMENTATION and values.prompt is not None
                ),
            )
        )
        config = self._resolve_config(execution, values, task)
        role_by_stage = {
            DebugStage.SPEC_GENERATION: AgentRole.PLANNER,
            DebugStage.IMPLEMENTATION: AgentRole.IMPLEMENTER,
            DebugStage.REVIEW: AgentRole.REVIEWER,
        }
        selected_role = role_by_stage.get(stage)
        clients = self._client_factory.create(
            config,
            agent_roles={selected_role} if selected_role is not None else set(),
        )
        if stage is DebugStage.SPEC_GENERATION:
            if task is None:
                raise DebugExecutionError("spec-generation requires task provider inputs")
            return self._run_spec(execution, stage, task, config, clients, values)
        if stage is DebugStage.IMPLEMENTATION:
            implementation_service = ImplementationService(
                clients.agent(AgentRole.IMPLEMENTER), clients.git
            )
            if values.prompt is not None:
                implementation_result = implementation_service.implement_prompt(
                    values.prompt, config.path
                )
            else:
                if task is None:
                    raise DebugExecutionError(
                        "implementation requires task inputs or --prompt/--prompt-file"
                    )
                spec = self._resolve_spec(execution, values)
                implementation_result = implementation_service.implement(
                    task, spec, config.path
                )
            return self._envelope(
                execution, stage, result={"implementation": asdict(implementation_result)}
            )
        if stage is DebugStage.VALIDATION:
            validation_result = ValidationService(clients.commands).validate(
                config.path,
                config.validation,
                require_tests=config.safety.require_tests,
            )
            return self._envelope(
                execution, stage, result={"validation": asdict(validation_result)}
            )
        if stage is DebugStage.REVIEW:
            if task is None:
                raise DebugExecutionError("review requires task provider inputs")
            return self._run_review(execution, stage, task, config, clients, values)
        raise DebugExecutionError(f"Unsupported debug stage: {stage}")

    def _describe_inputs(
        self,
        execution: WorkflowExecution | None,
        stage: DebugStage,
        values: DebugInputs,
    ) -> dict[str, Any]:
        if stage is DebugStage.TASK_LOADING:
            source, task_id, repository = self._provider_inputs(execution, values, stage)
            return {
                "task_source": source,
                "task_id": task_id,
                "repository": repository,
            }

        task = None if stage is DebugStage.VALIDATION else self._preview_task(execution, values)
        if stage is not DebugStage.VALIDATION and task is None:
            if stage is DebugStage.IMPLEMENTATION and values.prompt is not None:
                pass
            else:
                self._provider_inputs(execution, values, stage)
        config = self._resolve_config(execution, values, task)
        inputs: dict[str, Any] = {
            "repository_path": str(config.path),
            "base_branch": self._base_branch(execution, values, config),
        }
        if task is not None:
            inputs["task_snapshot"] = True
        elif stage is not DebugStage.VALIDATION and not (
            stage is DebugStage.IMPLEMENTATION and values.prompt is not None
        ):
            inputs["task_provider"] = self._provider_inputs(execution, values, stage)

        if stage is DebugStage.SPEC_GENERATION:
            inputs["agent_execution"] = "new read-only provider execution"
        elif stage is DebugStage.IMPLEMENTATION:
            if values.prompt is None:
                self._preview_spec(execution, values)
                inputs["spec_snapshot"] = True
            else:
                inputs["prompt"] = "explicit implementation input"
            inputs["workspace"] = "configured repository path/current checkout"
            inputs["working_branch"] = (
                execution.working_branch if execution is not None else None
            )
            inputs["requires_original_branch_checkout"] = False
        elif stage is DebugStage.VALIDATION:
            inputs.update(
                {
                    "validation_commands": config.validation.commands,
                    "auto_detect": config.validation.auto_detect,
                    "require_tests": config.safety.require_tests,
                }
            )
        elif stage is DebugStage.REVIEW:
            self._preview_spec(execution, values)
            self._preview_validation(execution, values)
            self._preview_pull_request(execution, values)
            feature_branch = self._feature_branch(execution, values)
            inputs.update(
                {
                    "spec_snapshot": True,
                    "validation_snapshot": True,
                    "pull_request_snapshot": True,
                    "working_branch": feature_branch,
                    "diff": "explicit input or configured workspace diff",
                    "agent_execution": "new read-only provider execution",
                }
            )
        return inputs

    def _run_spec(
        self,
        execution: WorkflowExecution | None,
        stage: DebugStage,
        task: DevelopmentTask,
        config: ProjectConfig,
        clients: WorkflowClients,
        values: DebugInputs,
    ) -> dict[str, Any]:
        executions: list[AgentExecutionReference] = []
        spec = SpecService(clients.agent(AgentRole.PLANNER)).generate(
            task,
            config.path,
            clients.git.github_repository(),
            self._base_branch(execution, values, config),
            executions.append,
        )
        return self._envelope(
            execution,
            stage,
            result={
                "spec": asdict(spec),
                "agent_executions": [asdict(reference) for reference in executions],
                "jules_sessions": [self._legacy_session(reference) for reference in executions],
            },
        )

    def _run_review(
        self,
        execution: WorkflowExecution | None,
        stage: DebugStage,
        task: DevelopmentTask,
        config: ProjectConfig,
        clients: WorkflowClients,
        values: DebugInputs,
    ) -> dict[str, Any]:
        spec = self._resolve_spec(execution, values)
        validation = self._resolve_validation(execution, values)
        pull_request = self._resolve_pull_request(execution, values)
        feature_branch = self._feature_branch(execution, values)
        base_branch = self._base_branch(execution, values, config)
        diff = values.diff if values.diff is not None else clients.git.diff(base_branch)
        executions: list[AgentExecutionReference] = []
        review = ReviewService(clients.agent(AgentRole.REVIEWER)).review(
            task=task,
            spec=spec,
            pull_request=pull_request,
            diff=diff,
            validation=validation,
            repository_path=config.path,
            repository_slug=clients.git.github_repository(),
            base_branch=base_branch,
            feature_branch=feature_branch,
            on_execution_started=executions.append,
        )
        return self._envelope(
            execution,
            stage,
            result={
                "review": asdict(review),
                "agent_executions": [asdict(reference) for reference in executions],
                "jules_sessions": [self._legacy_session(reference) for reference in executions],
            },
        )

    @staticmethod
    def _legacy_session(reference: AgentExecutionReference) -> dict[str, Any]:
        """Keep the existing debug response shape while persistence remains Jules-named."""
        return {
            "id": reference.external_id,
            "url": reference.url,
            "state": reference.metadata.get("state"),
            "resource_name": reference.metadata.get("resource_name"),
        }

    def _resolve_config(
        self,
        execution: WorkflowExecution | None,
        values: DebugInputs,
        task: DevelopmentTask | None,
    ) -> ProjectConfig:
        repository_ref = values.repository
        if not repository_ref and task is not None:
            repository_ref = task.repository
        if not repository_ref and execution is not None:
            repository_ref = self._task_repository(execution)
        if not repository_ref and execution is not None:
            repository_ref = execution.repository
        if not repository_ref:
            raise DebugExecutionError(
                "stage requires --repository or a repository from --from-execution"
            )
        config_task = task or DevelopmentTask(
            id=values.task_id or (execution.task_id if execution is not None else "debug"),
            source=values.task_source
            or (execution.task_source if execution is not None else "debug"),
            title="",
            description="",
            url="",
            repository=repository_ref,
        )
        try:
            config = self._config_loader.load(config_task, repository_override=repository_ref)
        except ConfigurationError as exc:
            raise DebugExecutionError(f"Cannot load repository configuration: {exc}") from exc
        return config

    def _resolve_task(
        self,
        execution: WorkflowExecution | None,
        values: DebugInputs,
        *,
        stage: DebugStage,
        required: bool = True,
    ) -> DevelopmentTask | None:
        if values.task is not None:
            return values.task

        explicit_provider = values.task_source is not None or values.task_id is not None
        if not explicit_provider and execution is not None and execution.task_snapshot:
            return self._task_from_execution(execution)
        if not required and not explicit_provider:
            return None

        source, task_id, repository = self._provider_inputs(execution, values, stage)
        if not task_id:
            if required:
                raise DebugExecutionError(
                    f"{stage.value} requires a task snapshot or provider task input"
                )
            return None
        return self._task_provider_factory(source, repository).get_task(task_id)

    def _provider_inputs(
        self,
        execution: WorkflowExecution | None,
        values: DebugInputs,
        stage: DebugStage,
    ) -> tuple[str, str | None, str | None]:
        source = values.task_source or (execution.task_source if execution is not None else None)
        task_id = values.task_id or (execution.task_id if execution is not None else None)
        repository = values.repository or (
            self._task_repository(execution) if execution is not None else None
        )
        if source is None:
            try:
                source = self._config_loader.default_task_provider()
            except ConfigurationError as exc:
                raise DebugExecutionError(str(exc)) from exc
        if not source:
            raise DebugExecutionError(
                f"{stage.value} requires --task-id with a task provider or --from-execution"
            )
        normalized_source = source.casefold()
        if normalized_source not in {"trello", "github"}:
            raise DebugExecutionError(f"Unsupported task provider: {source}")
        if normalized_source == "github" and not repository:
            raise DebugExecutionError(
                f"{stage.value} GitHub input requires --repository owner/repository"
            )
        return normalized_source, task_id, repository

    def _preview_task(
        self, execution: WorkflowExecution | None, values: DebugInputs
    ) -> DevelopmentTask | None:
        if values.task is not None:
            return values.task
        if values.task_source is not None or values.task_id is not None:
            return None
        if execution is not None and execution.task_snapshot:
            return self._task_from_execution(execution)
        return None

    def _resolve_spec(
        self, execution: WorkflowExecution | None, values: DebugInputs
    ) -> RepositorySpec:
        if values.spec is not None:
            return values.spec
        if execution is not None and execution.spec_snapshot:
            return self._spec_from_execution(execution)
        raise DebugExecutionError("implementation/review requires --spec-file or historical spec")

    def _resolve_validation(
        self, execution: WorkflowExecution | None, values: DebugInputs
    ) -> ValidationResult:
        if values.validation is not None:
            return values.validation
        if execution is not None and execution.validation_snapshot:
            return self._validation_from_execution(execution)
        raise DebugExecutionError("review requires --validation-file or historical validation")

    def _resolve_pull_request(
        self, execution: WorkflowExecution | None, values: DebugInputs
    ) -> PullRequest:
        if values.pull_request is not None:
            return values.pull_request
        if execution is not None and execution.pull_request_snapshot:
            return self._pull_request_from_execution(execution)
        raise DebugExecutionError("review requires --pull-request-file or historical pull request")

    def _preview_spec(
        self, execution: WorkflowExecution | None, values: DebugInputs
    ) -> None:
        if values.spec is None and not (execution is not None and execution.spec_snapshot):
            raise DebugExecutionError(
                "implementation/review requires --spec-file or historical spec"
            )

    def _preview_validation(
        self, execution: WorkflowExecution | None, values: DebugInputs
    ) -> None:
        if values.validation is None and not (
            execution is not None and execution.validation_snapshot
        ):
            raise DebugExecutionError("review requires --validation-file or historical validation")

    def _preview_pull_request(
        self, execution: WorkflowExecution | None, values: DebugInputs
    ) -> None:
        if values.pull_request is None and not (
            execution is not None and execution.pull_request_snapshot
        ):
            raise DebugExecutionError(
                "review requires --pull-request-file or historical pull request"
            )

    @staticmethod
    def _feature_branch(
        execution: WorkflowExecution | None, values: DebugInputs
    ) -> str:
        branch = values.feature_branch
        if not branch and execution is not None:
            branch = execution.working_branch
        if not branch and values.pull_request is not None:
            branch = values.pull_request.head_branch
        if not branch and execution is not None and execution.pull_request_snapshot:
            branch = str(execution.pull_request_snapshot.get("head_branch") or "")
        if not branch:
            raise DebugExecutionError("review requires --feature-branch or historical branch")
        return branch

    @staticmethod
    def _base_branch(
        execution: WorkflowExecution | None,
        values: DebugInputs,
        config: ProjectConfig,
    ) -> str:
        if values.base_branch:
            return values.base_branch
        if execution is not None and execution.base_branch:
            return execution.base_branch
        return config.repository.base_branch

    @staticmethod
    def _task_from_execution(execution: WorkflowExecution) -> DevelopmentTask:
        if not execution.task_snapshot:
            raise DebugExecutionError("stage requires persisted task_snapshot")
        try:
            return DevelopmentTask(**execution.task_snapshot)
        except (TypeError, ValueError) as exc:
            raise DebugExecutionError("persisted task_snapshot is invalid") from exc

    @staticmethod
    def _spec_from_execution(execution: WorkflowExecution) -> RepositorySpec:
        if not execution.spec_snapshot:
            raise DebugExecutionError("stage requires persisted spec_snapshot")
        try:
            return RepositorySpec(**execution.spec_snapshot)
        except (TypeError, ValueError) as exc:
            raise DebugExecutionError("persisted spec_snapshot is invalid") from exc

    @staticmethod
    def _validation_from_execution(execution: WorkflowExecution) -> ValidationResult:
        if not execution.validation_snapshot:
            raise DebugExecutionError("review requires persisted validation_snapshot")
        try:
            raw_checks = execution.validation_snapshot.get("checks", [])
            checks = [ValidationCheck(**check) for check in raw_checks]
            return ValidationResult(
                success=bool(execution.validation_snapshot["success"]),
                checks=checks,
                error_message=execution.validation_snapshot.get("error_message"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DebugExecutionError("persisted validation_snapshot is invalid") from exc

    @staticmethod
    def _pull_request_from_execution(execution: WorkflowExecution) -> PullRequest:
        if not execution.pull_request_snapshot:
            raise DebugExecutionError("review requires persisted pull_request_snapshot")
        try:
            return PullRequest(**execution.pull_request_snapshot)
        except (TypeError, ValueError) as exc:
            raise DebugExecutionError("persisted pull_request_snapshot is invalid") from exc

    @staticmethod
    def _task_repository(execution: WorkflowExecution | None) -> str | None:
        if execution is None:
            return None
        snapshot = execution.task_snapshot or {}
        repository = snapshot.get("repository")
        if isinstance(repository, str) and repository.strip():
            return repository.strip()
        metadata = snapshot.get("metadata")
        if isinstance(metadata, dict):
            repository = metadata.get("repository")
            if isinstance(repository, str) and repository.strip():
                return repository.strip()
        return None

    @staticmethod
    def _envelope(
        execution: WorkflowExecution | None,
        stage: DebugStage,
        *,
        dry_run: bool = False,
        inputs: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        envelope: dict[str, Any] = {
            "execution_id": execution.id if execution is not None else None,
            "stage": stage.value,
            "dry_run": dry_run,
            "execution_mutated": False,
        }
        if inputs is not None:
            envelope["inputs"] = inputs
        if result is not None:
            envelope["result"] = result
        return envelope
