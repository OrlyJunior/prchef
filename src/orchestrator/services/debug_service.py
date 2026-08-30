from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict
from enum import StrEnum
from typing import Any

from orchestrator.clients.factory import ClientFactory, WorkflowClients
from orchestrator.config.project_config import (
    ConfigurationError,
    ProjectConfig,
    ProjectConfigLoader,
)
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
    """Run one workflow stage using a persisted execution as its input contract."""

    def __init__(
        self,
        *,
        config_loader: ProjectConfigLoader,
        client_factory: ClientFactory,
        task_provider_factory: Callable[[WorkflowExecution], TaskProvider],
    ) -> None:
        self._config_loader = config_loader
        self._client_factory = client_factory
        self._task_provider_factory = task_provider_factory

    def execute(
        self,
        execution: WorkflowExecution,
        stage: DebugStage,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        if dry_run:
            inputs = self._describe_inputs(execution, stage)
            return self._envelope(execution, stage, dry_run=True, inputs=inputs)

        if stage is DebugStage.TASK_LOADING:
            task = self._task_provider_factory(execution).get_task(execution.task_id)
            return self._envelope(execution, stage, result={"task": asdict(task)})

        task, config = self._project_context(execution)
        if stage in {DebugStage.IMPLEMENTATION, DebugStage.REVIEW}:
            self._require_working_branch(execution, stage)
        clients = self._client_factory.create(config)
        if stage is DebugStage.SPEC_GENERATION:
            return self._run_spec(execution, stage, task, config, clients)
        if stage is DebugStage.IMPLEMENTATION:
            self._assert_implementation_checkout(execution, config, clients)
            spec = self._spec_from_execution(execution)
            implementation_result = ImplementationService(clients.codex, clients.git).implement(
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
            return self._run_review(execution, stage, task, config, clients)
        raise DebugExecutionError(f"Unsupported debug stage: {stage}")

    def _describe_inputs(
        self, execution: WorkflowExecution, stage: DebugStage
    ) -> dict[str, Any]:
        if stage is DebugStage.TASK_LOADING:
            if not execution.task_source or not execution.task_id:
                raise DebugExecutionError(
                    "task-loading requires persisted task_source and task_id"
                )
            if execution.task_source.casefold() not in {"trello", "github"}:
                raise DebugExecutionError(
                    f"Unsupported persisted task source: {execution.task_source}"
                )
            repository = self._task_repository(execution)
            if execution.task_source.casefold() == "github" and not repository:
                raise DebugExecutionError(
                    "task-loading debug for GitHub requires repository in persisted task_snapshot"
                )
            return {
                "task_source": execution.task_source,
                "task_id": execution.task_id,
                "repository": repository,
            }

        task, config = self._project_context(execution)
        inputs: dict[str, Any] = {
            "task_snapshot": True,
            "repository_path": str(config.path),
            "base_branch": config.repository.base_branch,
        }
        if stage is DebugStage.SPEC_GENERATION:
            inputs["jules_session"] = "new read-only session"
        elif stage is DebugStage.IMPLEMENTATION:
            self._spec_from_execution(execution)
            self._require_working_branch(execution, stage)
            inputs.update(
                {
                    "spec_snapshot": True,
                    "working_branch": execution.working_branch,
                    "requires_existing_checkout": True,
                }
            )
        elif stage is DebugStage.VALIDATION:
            inputs.update(
                {
                    "validation_commands": config.validation.commands,
                    "auto_detect": config.validation.auto_detect,
                    "require_tests": config.safety.require_tests,
                }
            )
        elif stage is DebugStage.REVIEW:
            self._spec_from_execution(execution)
            self._validation_from_execution(execution)
            self._pull_request_from_execution(execution)
            self._require_working_branch(execution, stage)
            inputs.update(
                {
                    "validation_snapshot": True,
                    "pull_request_snapshot": True,
                    "working_branch": execution.working_branch,
                    "jules_session": "new read-only session",
                }
            )
        return inputs

    def _run_spec(
        self,
        execution: WorkflowExecution,
        stage: DebugStage,
        task: DevelopmentTask,
        config: ProjectConfig,
        clients: WorkflowClients,
    ) -> dict[str, Any]:
        sessions: list[Any] = []
        spec = SpecService(clients.jules).generate(
            task,
            clients.git.github_repository(),
            config.repository.base_branch,
            sessions.append,
        )
        return self._envelope(
            execution,
            stage,
            result={
                "spec": asdict(spec),
                "jules_sessions": [asdict(session) for session in sessions],
            },
        )

    def _run_review(
        self,
        execution: WorkflowExecution,
        stage: DebugStage,
        task: DevelopmentTask,
        config: ProjectConfig,
        clients: WorkflowClients,
    ) -> dict[str, Any]:
        spec = self._spec_from_execution(execution)
        validation = self._validation_from_execution(execution)
        pull_request = self._pull_request_from_execution(execution)
        feature_branch = self._require_working_branch(execution, DebugStage.REVIEW)
        sessions: list[Any] = []
        review = ReviewService(clients.jules).review(
            task=task,
            spec=spec,
            pull_request=pull_request,
            diff=clients.git.diff(config.repository.base_branch),
            validation=validation,
            repository_slug=clients.git.github_repository(),
            base_branch=config.repository.base_branch,
            feature_branch=feature_branch,
            on_session_created=sessions.append,
        )
        return self._envelope(
            execution,
            stage,
            result={
                "review": asdict(review),
                "jules_sessions": [asdict(session) for session in sessions],
            },
        )

    def _project_context(
        self, execution: WorkflowExecution
    ) -> tuple[DevelopmentTask, ProjectConfig]:
        task = self._task_from_execution(execution)
        repository_ref = task.repository or execution.repository
        if not repository_ref:
            raise DebugExecutionError(
                "stage requires task.repository or persisted execution.repository"
            )
        try:
            config = self._config_loader.load(task, repository_override=repository_ref)
        except ConfigurationError as exc:
            raise DebugExecutionError(f"Cannot load repository configuration: {exc}") from exc
        return task, config

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
    def _task_repository(execution: WorkflowExecution) -> str | None:
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
    def _assert_implementation_checkout(
        execution: WorkflowExecution,
        config: ProjectConfig,
        clients: WorkflowClients,
    ) -> None:
        branch = DebugService._require_working_branch(execution, DebugStage.IMPLEMENTATION)
        protected = {
            value.casefold()
            for value in ["main", "master", "develop", *config.repository.protected_branches]
        }
        if branch.casefold() in protected or not re.fullmatch(r"[A-Za-z0-9._/-]+", branch):
            raise DebugExecutionError(
                f"implementation refuses unsafe or protected working_branch: {branch}"
            )
        current = clients.git.current_branch()
        if current != branch:
            raise DebugExecutionError(
                f"implementation requires checkout on {branch!r}; current branch is {current!r}"
            )

    @staticmethod
    def _require_working_branch(
        execution: WorkflowExecution, stage: DebugStage
    ) -> str:
        branch = execution.working_branch
        if not branch:
            raise DebugExecutionError(
                f"{stage.value} requires persisted working_branch; debug does not create branches"
            )
        return branch

    @staticmethod
    def _envelope(
        execution: WorkflowExecution,
        stage: DebugStage,
        *,
        dry_run: bool = False,
        inputs: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        envelope: dict[str, Any] = {
            "execution_id": execution.id,
            "stage": stage.value,
            "dry_run": dry_run,
            "execution_mutated": False,
        }
        if inputs is not None:
            envelope["inputs"] = inputs
        if result is not None:
            envelope["result"] = result
        return envelope
