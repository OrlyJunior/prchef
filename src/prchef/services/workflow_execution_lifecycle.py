from __future__ import annotations

import json
import logging
from collections.abc import Mapping

from prchef.config.project_config import ProjectConfig
from prchef.models.agent import AgentRole
from prchef.models.execution_diagnostic import DiagnosticCategory, ExecutionDiagnostic
from prchef.models.workflow_event import WorkflowEventKind
from prchef.models.workflow_execution import WorkflowExecution
from prchef.models.workflow_status import WorkflowStage, WorkflowStatus
from prchef.providers.agent_provider import AgentProviderError
from prchef.repositories.workflow_repository import WorkflowRepository
from prchef.services.exceptions import WorkflowCancelledError, WorkflowError
from prchef.services.notifications import NotificationService
from prchef.services.workflow_stage_events import StageEvents

logger = logging.getLogger("prchef.services.workflow_service")


_RUNTIME_OVERRIDES_DIAGNOSTIC_KIND = "RUNTIME_OVERRIDES_APPLIED"
_QUOTA_EXHAUSTED_DIAGNOSTIC_KIND = "PROVIDER_QUOTA_EXHAUSTED"


class ExecutionLifecycle:
    """Persist admission, terminal outcomes and resumable runtime choices."""

    def __init__(
        self,
        repository: WorkflowRepository,
        events: StageEvents,
        notifications: NotificationService,
    ) -> None:
        self._repository = repository
        self._events = events
        self._notification_service = notifications

    def _start_admitted(self, execution_id: str) -> None:
        """Move admitted work from the queue into a genuinely running state."""

        self._events._raise_if_cancellation_requested(execution_id)
        self._repository.update_status(execution_id, WorkflowStatus.RUNNING)
        self._events._emit(
            execution_id, WorkflowEventKind.STATUS_CHANGED, status=WorkflowStatus.RUNNING
        )

    def _record_quota_failure(self, execution_id: str, exc: Exception) -> None:
        if not isinstance(exc, AgentProviderError) or exc.category != "quota":
            return
        if not exc.provider_name:
            return
        try:
            stage = self._repository.get_execution(execution_id).current_stage
        except Exception:
            stage = None
        stage_roles = {
            WorkflowStage.GENERATE_SPEC: AgentRole.PLANNER,
            WorkflowStage.IMPLEMENT: AgentRole.IMPLEMENTER,
            WorkflowStage.POLISH: AgentRole.IMPLEMENTER,
            WorkflowStage.REVIEW: AgentRole.REVIEWER,
        }
        role = stage_roles.get(stage) if stage is not None else None
        payload = {"kind": _QUOTA_EXHAUSTED_DIAGNOSTIC_KIND, "provider": exc.provider_name}
        if role is not None:
            payload["role"] = role.value
        self._events._record_diagnostic(
            execution_id, DiagnosticCategory.WORKFLOW_EVENT, payload, stage=stage, role=role
        )

    def _pause_for_human(
        self,
        execution_id: str,
        stage: WorkflowStage,
        prompt: str,
        config: ProjectConfig | None,
    ) -> WorkflowExecution:
        if self._repository.cancellation_requested(execution_id):
            return self._finish(execution_id, WorkflowStatus.CANCELLED, config)
        self._repository.record_error(execution_id, prompt)
        self._repository.request_human_input(execution_id, stage, prompt)
        return self._finish(execution_id, WorkflowStatus.NEEDS_HUMAN, config)

    def _record_runtime_overrides(
        self,
        execution_id: str,
        provider_overrides: Mapping[str, str],
        runtime_overrides: Mapping[str, Mapping[str, object]],
    ) -> None:
        """Durably persist validated one-run selections for restart-safe resume.

        Unlike ordinary diagnostics this record is load-bearing: human resume and
        manual continuation rebuild the role-local provider/model/effort choices
        from it. It is written before any agent starts, and a failed or lossy
        write stops the launch with an explicit error instead of letting a later
        resume silently fall back to YAML defaults.
        """

        providers = dict(provider_overrides)
        options = {role: dict(values) for role, values in runtime_overrides.items()}
        failure = (
            "Could not persist the selected agent runtime overrides "
            "(provider/model/effort); the run was stopped before any agent started "
            "so a later resume cannot silently use different settings. "
            "Retry the launch."
        )
        diagnostic = ExecutionDiagnostic(
            execution_id=execution_id,
            category=DiagnosticCategory.WORKFLOW_EVENT,
            payload={
                "kind": _RUNTIME_OVERRIDES_DIAGNOSTIC_KIND,
                "provider_overrides": providers,
                "runtime_overrides": options,
            },
            stage=WorkflowStage.LOAD_CONFIG,
        )
        try:
            self._repository.record_diagnostic(diagnostic)
            persisted = self._runtime_overrides_for_execution(execution_id)
        except Exception as exc:
            logger.warning("[workflow] Could not persist runtime overrides", exc_info=True)
            raise WorkflowError(failure) from exc
        expected = json.loads(json.dumps((providers, options), default=str))
        if json.loads(json.dumps(persisted, default=str)) != expected:
            raise WorkflowError(failure)

    def _runtime_overrides_for_execution(
        self, execution_id: str
    ) -> tuple[dict[str, str], dict[str, dict[str, object]]]:
        """Return the launch selections recorded for a resumable execution."""

        list_diagnostics = getattr(self._repository, "list_diagnostics", None)
        if not callable(list_diagnostics):
            return {}, {}
        for diagnostic in reversed(list_diagnostics(execution_id)):
            payload = diagnostic.payload
            if payload.get("kind") != _RUNTIME_OVERRIDES_DIAGNOSTIC_KIND:
                continue
            raw_providers = payload.get("provider_overrides")
            raw_options = payload.get("runtime_overrides")
            if not isinstance(raw_providers, Mapping) or not isinstance(raw_options, Mapping):
                raise WorkflowError("Persisted runtime overrides are invalid")
            providers = {
                role: provider
                for role, provider in raw_providers.items()
                if isinstance(role, str) and isinstance(provider, str)
            }
            options = {
                role: dict(value)
                for role, value in raw_options.items()
                if isinstance(role, str) and isinstance(value, Mapping)
            }
            if len(providers) != len(raw_providers) or len(options) != len(raw_options):
                raise WorkflowError("Persisted runtime overrides are invalid")
            return providers, options
        return {}, {}

    def _finish(
        self,
        execution_id: str,
        status: WorkflowStatus,
        config: ProjectConfig | None,
    ) -> WorkflowExecution:
        if status is not WorkflowStatus.CANCELLED:
            try:
                self._events._raise_if_cancellation_requested(execution_id)
            except WorkflowCancelledError:
                status = WorkflowStatus.CANCELLED
        self._repository.finish(execution_id, status)
        execution = self._repository.get_execution(execution_id)
        status = execution.status
        self._events._emit(
            execution_id,
            WorkflowEventKind.STATUS_CHANGED,
            stage=execution.current_stage,
            status=status,
        )
        self._events._emit(
            execution_id,
            WorkflowEventKind.WORKFLOW_FINISHED,
            stage=execution.current_stage,
            status=status,
        )
        if config is not None:
            self._notification_service.notify_outcome(config.notifications, execution)
        return execution
