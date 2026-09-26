from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orchestrator.clients.factory import WorkflowClients
from orchestrator.config.project_config import ProjectConfig
from orchestrator.models.code_review import CodeReview
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.execution_diagnostic import DiagnosticCategory
from orchestrator.models.implementation_result import ImplementationResult
from orchestrator.models.pull_request import PullRequest
from orchestrator.models.repository_spec import RepositorySpec
from orchestrator.models.validation_result import ValidationCheck, ValidationResult
from orchestrator.models.workflow_status import PolishOutcome, ReviewDecision, WorkflowStage
from orchestrator.repositories.workflow_repository import WorkflowRepository
from orchestrator.runtime_logging import redact_text
from orchestrator.services.exceptions import WorkflowError
from orchestrator.services.implementation_service import ImplementationService
from orchestrator.services.validation_service import ValidationService
from orchestrator.services.workflow_stage_actions import StageActions
from orchestrator.services.workflow_stage_events import StageEvents

logger = logging.getLogger("orchestrator.services.workflow_service")


class _ValidationFailed(WorkflowError):
    def __init__(self, validation: ValidationResult) -> None:
        super().__init__(validation.error_message or "Validation failed")
        self.validation = validation


class CorrectionPolicy:
    """Own validation retries and optional polish policy."""

    def __init__(
        self, repository: WorkflowRepository, events: StageEvents, actions: StageActions
    ) -> None:
        self._repository = repository
        self._events = events
        self._actions = actions

    @staticmethod
    def _review_retry_available(review: CodeReview, attempted: int, config: ProjectConfig) -> bool:
        return (
            review.result is ReviewDecision.CHANGES_REQUESTED
            and attempted < config.review.max_review_retries
        )

    def _correct_review_findings(
        self,
        *,
        execution_id: str,
        execution_clients: WorkflowClients,
        implementation_service: ImplementationService,
        task: DevelopmentTask,
        spec: RepositorySpec,
        review: CodeReview,
        workspace_path: Path,
        working_branch: str,
        config: ProjectConfig,
        attempt: int,
        log_retry: bool = False,
        before_push: Callable[[], None] | None = None,
    ) -> ValidationResult:
        if log_retry:
            logger.info(
                "[workflow] Review requested changes; requesting implementation correction %s/%s",
                attempt,
                config.review.max_review_retries,
            )
        with self._events._step(execution_id, WorkflowStage.IMPLEMENT):
            implementation = implementation_service.correct_review_findings(
                task,
                spec,
                review,
                workspace_path,
                instructions_repository_path=config.path,
            )
            self._repository.record_implementation(execution_id, implementation)
            if not implementation.success:
                raise WorkflowError(
                    implementation.error_message or "Review correction provider failed"
                )
        validation = self._validate_with_corrections(
            execution_id=execution_id,
            execution_clients=execution_clients,
            implementation_service=implementation_service,
            task=task,
            spec=spec,
            workspace_path=workspace_path,
            config=config,
        )
        self._actions._commit_changes(execution_id, execution_clients, task, working_branch, config)
        with self._events._step(execution_id, WorkflowStage.PUSH):
            if before_push is not None:
                before_push()
            execution_clients.git.push(working_branch, config.repository.protected_branches)
        return validation

    def _validate_with_corrections(
        self,
        *,
        execution_id: str,
        execution_clients: WorkflowClients,
        implementation_service: ImplementationService,
        task: DevelopmentTask,
        spec: RepositorySpec,
        workspace_path: Path,
        config: ProjectConfig,
    ) -> ValidationResult:
        validation_attempt = 0
        while True:
            try:
                with self._events._step(execution_id, WorkflowStage.VALIDATE):
                    validation = ValidationService(execution_clients.commands).validate(
                        workspace_path,
                        config.validation,
                        require_tests=config.safety.require_tests,
                        on_check_complete=lambda payload: self._events._record_validation_check(
                            execution_id, payload
                        ),
                    )
                    self._repository.record_validation(execution_id, validation)
                    if not validation.success:
                        self._events._record_diagnostic(
                            execution_id,
                            DiagnosticCategory.ERROR,
                            {
                                "operation": "validation",
                                "message": validation.error_message,
                                "failing_commands": [
                                    self._failing_command_diagnostic(check)
                                    for check in validation.checks
                                    if not check.success
                                ],
                            },
                            stage=WorkflowStage.VALIDATE,
                        )
                        raise _ValidationFailed(validation)
            except _ValidationFailed as exc:
                if validation_attempt >= config.validation.max_validation_retries:
                    raise WorkflowError(str(exc)) from exc
                validation_attempt += 1
                logger.info(
                    "[workflow] Validation failed; requesting implementation correction %s/%s",
                    validation_attempt,
                    config.validation.max_validation_retries,
                )
                with self._events._step(execution_id, WorkflowStage.IMPLEMENT):
                    implementation = implementation_service.correct_validation_failures(
                        task,
                        spec,
                        exc.validation,
                        workspace_path,
                        instructions_repository_path=config.path,
                    )
                    self._repository.record_implementation(execution_id, implementation)
                    if not implementation.success:
                        raise WorkflowError(
                            implementation.error_message
                            or "Implementation correction provider failed"
                        ) from exc
                continue
            return validation

    def _validate_polish_changes(
        self,
        *,
        execution_id: str,
        execution_clients: WorkflowClients,
        workspace_path: Path,
        config: ProjectConfig,
    ) -> ValidationResult | None:
        """Validate optional changes once and discard them if they are not safe to push."""
        try:
            with self._events._step(execution_id, WorkflowStage.VALIDATE):
                validation = ValidationService(execution_clients.commands).validate(
                    workspace_path,
                    config.validation,
                    require_tests=config.safety.require_tests,
                    on_check_complete=lambda payload: self._events._record_validation_check(
                        execution_id, payload
                    ),
                )
                self._repository.record_validation(execution_id, validation)
                if not validation.success:
                    self._events._record_diagnostic(
                        execution_id,
                        DiagnosticCategory.ERROR,
                        {
                            "operation": "optional_polish_validation",
                            "message": validation.error_message,
                            "failing_commands": [
                                self._failing_command_diagnostic(check)
                                for check in validation.checks
                                if not check.success
                            ],
                        },
                        stage=WorkflowStage.VALIDATE,
                    )
                    raise _ValidationFailed(validation)
        except _ValidationFailed:
            logger.warning(
                "[workflow] Optional polish failed validation; discarding polish changes"
            )
            self._discard_polish_changes(execution_clients)
            return None

        return validation

    def _evaluate_optional_polish(
        self,
        *,
        execution_id: str,
        execution_clients: WorkflowClients,
        implementation_service: ImplementationService,
        task: DevelopmentTask,
        spec: RepositorySpec,
        suggestions: list[str],
        workspace_path: Path,
        pull_request: PullRequest,
        working_branch: str,
        config: ProjectConfig,
        force: bool = False,
        before_push: Callable[[], None] | None = None,
    ) -> tuple[PolishOutcome, ValidationResult | None]:
        """Evaluate approved-review suggestions once and persist an auditable outcome."""
        persisted = self._repository.get_execution(execution_id).polish_outcome
        if persisted is not None and not force:
            return persisted, None

        with self._events._step(execution_id, WorkflowStage.POLISH):
            implementation = implementation_service.run_polish_pass(
                task,
                spec,
                suggestions,
                workspace_path,
                pull_request,
                instructions_repository_path=config.path,
            )
            self._repository.record_implementation(execution_id, implementation)

        if not implementation.success:
            logger.warning("[workflow] Optional polish did not complete; retaining approved review")
            self._discard_polish_changes(execution_clients)
            self._record_skipped_polish(
                execution_id=execution_id,
                execution_clients=execution_clients,
                pull_request_number=pull_request.number,
                implementation=implementation,
                fallback_reason=(
                    "The optional polish pass did not complete safely, so no changes were applied."
                ),
            )
            return PolishOutcome.SKIPPED, None

        if not implementation.changed_files:
            logger.info("[workflow] Implementer declined optional polish suggestions")
            self._record_skipped_polish(
                execution_id=execution_id,
                execution_clients=execution_clients,
                pull_request_number=pull_request.number,
                implementation=implementation,
            )
            return PolishOutcome.SKIPPED, None

        validation = self._validate_polish_changes(
            execution_id=execution_id,
            execution_clients=execution_clients,
            workspace_path=workspace_path,
            config=config,
        )
        if validation is None:
            self._record_skipped_polish(
                execution_id=execution_id,
                execution_clients=execution_clients,
                pull_request_number=pull_request.number,
                implementation=implementation,
                fallback_reason=(
                    "The optional changes did not pass validation, so no changes were applied."
                ),
            )
            return PolishOutcome.SKIPPED, None

        self._actions._commit_changes(execution_id, execution_clients, task, working_branch, config)
        with self._events._step(execution_id, WorkflowStage.PUSH):
            if before_push is not None:
                before_push()
            execution_clients.git.push(working_branch, config.repository.protected_branches)

        self._repository.record_polish_outcome(execution_id, PolishOutcome.APPLIED)
        return PolishOutcome.APPLIED, validation

    @staticmethod
    def _failing_command_diagnostic(check: ValidationCheck) -> dict[str, Any]:
        diagnostic: dict[str, Any] = {
            "command": check.command,
            "exit_code": check.exit_code,
            "stdout": check.stdout,
            "stderr": check.stderr,
        }
        if check.timed_out:
            diagnostic["timed_out"] = True
            diagnostic["timeout_seconds"] = check.timeout_seconds
        return diagnostic

    def _record_skipped_polish(
        self,
        *,
        execution_id: str,
        execution_clients: WorkflowClients,
        pull_request_number: int,
        implementation: ImplementationResult,
        fallback_reason: str | None = None,
    ) -> None:
        self._repository.record_polish_outcome(execution_id, PolishOutcome.SKIPPED)
        self._publish_polish_skip_comment(
            execution_id=execution_id,
            execution_clients=execution_clients,
            pull_request_number=pull_request_number,
            implementation=implementation,
            fallback_reason=fallback_reason,
        )

    @staticmethod
    def _discard_polish_changes(execution_clients: WorkflowClients) -> None:
        execution_clients.git.discard_uncommitted_changes()

    @staticmethod
    def _format_polish_skip_comment(
        implementation: ImplementationResult, fallback_reason: str | None = None
    ) -> str:
        """Render a concise, safe explanation for an intentionally skipped polish pass."""
        lines = [
            "## Optional polish",
            "",
            "The non-blocking review suggestions were evaluated and intentionally skipped.",
            "",
        ]
        if implementation.polish_skip_reasons:
            lines.extend(f"- {reason}" for reason in implementation.polish_skip_reasons)
        else:
            reason = fallback_reason or "No small, safe, in-scope code change was warranted."
            lines.append(f"- {reason}")
        return "\n".join(lines)

    def _publish_polish_skip_comment(
        self,
        *,
        execution_id: str,
        execution_clients: WorkflowClients,
        pull_request_number: int,
        implementation: ImplementationResult,
        fallback_reason: str | None = None,
    ) -> None:
        execution = self._repository.get_execution(execution_id)
        if execution.polish_skip_comment_attempted:
            logger.info("[github] Optional polish skip comment was already attempted")
            return
        # Persist before the external side effect so a retry after a process interruption cannot
        # publish a second timeline comment. Publication itself remains deliberately non-fatal.
        self._repository.record_polish_skip_comment_attempt(execution_id)
        try:
            execution_clients.github.comment_pull_request(
                pr_number=pull_request_number,
                body=self._format_polish_skip_comment(implementation, fallback_reason),
            )
        except Exception as exc:
            # A skip explanation is audit context, not a new review decision. Keep the existing
            # approval intact if GitHub is temporarily unavailable.
            logger.warning(
                "[github] Optional polish skip comment publication failed: %s",
                redact_text(str(exc)),
            )
