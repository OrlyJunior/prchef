from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from orchestrator.models.agent import AgentRole
from orchestrator.models.execution_diagnostic import DiagnosticCategory, ExecutionDiagnostic
from orchestrator.models.workflow_event import (
    WorkflowEvent,
    WorkflowEventKind,
    WorkflowEventListener,
)
from orchestrator.models.workflow_interruption import WorkflowInterruptedError
from orchestrator.models.workflow_status import StepStatus, WorkflowStage, WorkflowStatus
from orchestrator.repositories.workflow_repository import WorkflowRepository
from orchestrator.runtime_logging import redact_payload, redact_text
from orchestrator.services.exceptions import WorkflowCancelledError

logger = logging.getLogger("orchestrator.services.workflow_service")


_STAGE_LABELS = {
    WorkflowStage.LOAD_TASK: "task loading",
    WorkflowStage.LOAD_CONFIG: "repository/config resolution",
    WorkflowStage.GENERATE_SPEC: "spec generation",
    WorkflowStage.PREPARE_BRANCH: "branch preparation",
    WorkflowStage.IMPLEMENT: "implementation",
    WorkflowStage.POLISH: "optional implementation polish",
    WorkflowStage.VALIDATE: "local validation",
    WorkflowStage.COMMIT: "commit",
    WorkflowStage.PUSH: "push",
    WorkflowStage.CREATE_PR: "pull request creation",
    WorkflowStage.REVIEW: "review",
}

@dataclass(slots=True)
class ExecutionRuntime:
    """Mutable state owned by exactly one in-flight execution.

    Concurrent executions share a service instance, so the observer and the
    stage used for diagnostic attribution can never live on the service itself.
    """

    listener: WorkflowEventListener | None = None
    active_stage: WorkflowStage | None = None


class ExecutionRuntimes:
    """Own independent listener and stage state for each execution."""

    def __init__(self) -> None:
        self._items: dict[str, ExecutionRuntime] = {}
        self._lock = threading.RLock()

    def begin(self, execution_id: str, listener: WorkflowEventListener | None) -> ExecutionRuntime:
        runtime = ExecutionRuntime(listener=listener)
        with self._lock:
            self._items[execution_id] = runtime
        return runtime

    def runtime(self, execution_id: str) -> ExecutionRuntime:
        with self._lock:
            runtime = self._items.get(execution_id)
            if runtime is None:
                runtime = ExecutionRuntime()
                self._items[execution_id] = runtime
            return runtime

    def peek(self, execution_id: str) -> ExecutionRuntime | None:
        with self._lock:
            return self._items.get(execution_id)

    def end(self, execution_id: str) -> None:
        with self._lock:
            self._items.pop(execution_id, None)


class StageEvents:
    """Persist stage transitions and deliver diagnostic and workflow events."""

    def __init__(
        self, repository: WorkflowRepository, runtimes: ExecutionRuntimes, *, verbose: bool = False
    ) -> None:
        self._repository = repository
        self._runtimes = runtimes
        self._verbose = verbose

    @contextmanager
    def _step(self, execution_id: str, stage: WorkflowStage) -> Iterator[None]:
        self._raise_if_cancellation_requested(execution_id)
        label = _STAGE_LABELS.get(stage, stage.value.casefold().replace("_", " "))
        runtime = self._runtimes.runtime(execution_id)
        previous_stage = runtime.active_stage
        runtime.active_stage = stage
        logger.info("[workflow] Entering %s", label)
        self._repository.update_stage(execution_id, stage)
        step_id = self._repository.start_step(execution_id, stage)
        self._emit(
            execution_id,
            WorkflowEventKind.STAGE_STARTED,
            stage=stage,
            step_status=StepStatus.RUNNING,
        )
        try:
            yield
            self._raise_if_cancellation_requested(execution_id)
        except WorkflowInterruptedError:
            self._repository.finish_step(step_id, StepStatus.INTERRUPTED)
            raise
        except WorkflowCancelledError as exc:
            self._repository.finish_step(step_id, StepStatus.CANCELLED, str(exc))
            self._emit(
                execution_id,
                WorkflowEventKind.STAGE_FINISHED,
                stage=stage,
                step_status=StepStatus.CANCELLED,
                message=str(exc),
            )
            logger.info("[workflow] %s cancelled", label)
            raise
        except Exception as exc:
            self._repository.finish_step(step_id, StepStatus.FAILED, str(exc))
            self._record_diagnostic(
                execution_id,
                DiagnosticCategory.ERROR,
                {
                    "operation": "workflow_stage",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
                stage=stage,
            )
            self._emit(
                execution_id,
                WorkflowEventKind.STAGE_FAILED,
                stage=stage,
                step_status=StepStatus.FAILED,
                message=redact_text(str(exc)),
            )
            logger.error("[workflow] %s failed: %s", label, redact_text(str(exc)))
            raise
        else:
            self._repository.finish_step(step_id, StepStatus.SUCCEEDED)
            self._emit(
                execution_id,
                WorkflowEventKind.STAGE_FINISHED,
                stage=stage,
                step_status=StepStatus.SUCCEEDED,
            )
            logger.info("[workflow] Completed %s", label)
        finally:
            runtime.active_stage = previous_stage

    def _record_diagnostic(
        self,
        execution_id: str,
        category: DiagnosticCategory,
        payload: dict[str, Any],
        *,
        stage: WorkflowStage | None = None,
        role: AgentRole | None = None,
        notify: bool = True,
    ) -> None:
        """Persist first, then notify observers. Diagnostics cannot affect a run."""
        sanitized = redact_payload(payload)
        if not isinstance(sanitized, dict):
            sanitized = {"value": sanitized}
        diagnostic = ExecutionDiagnostic(
            execution_id=execution_id,
            category=category,
            payload=sanitized,
            stage=stage,
            role=role,
        )
        try:
            self._repository.record_diagnostic(diagnostic)
        except AttributeError:
            # Lightweight repository fakes used by integrations predating the
            # diagnostics table retain their existing workflow behavior.
            pass
        except Exception:
            logger.warning("[workflow] Could not persist diagnostic event", exc_info=True)
        if self._verbose:
            logger.debug(
                "[diagnostic] %s",
                json.dumps(
                    {
                        "category": category.value,
                        "stage": stage.value if stage else None,
                        "role": role.value if role else None,
                        "payload": sanitized,
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            )
        if notify:
            self._notify_event(
                WorkflowEvent(
                    execution_id=execution_id,
                    kind=WorkflowEventKind.DIAGNOSTIC,
                    stage=stage,
                    category=category.value,
                    payload=sanitized,
                )
            )

    def _emit(
        self,
        execution_id: str,
        kind: WorkflowEventKind,
        *,
        stage: WorkflowStage | None = None,
        status: WorkflowStatus | None = None,
        step_status: StepStatus | None = None,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        event_payload: dict[str, Any] = {
            "kind": kind.value,
            "status": status.value if status else None,
            "step_status": step_status.value if step_status else None,
            "message": message,
        }
        if payload is not None:
            event_payload.update(payload)
        self._record_diagnostic(
            execution_id,
            DiagnosticCategory.WORKFLOW_EVENT,
            event_payload,
            stage=stage,
            notify=False,
        )
        sanitized_payload = redact_payload(payload) if payload is not None else None
        self._notify_event(
            WorkflowEvent(
                execution_id=execution_id,
                kind=kind,
                stage=stage,
                status=status,
                step_status=step_status,
                message=redact_text(message) if message is not None else None,
                payload=sanitized_payload if isinstance(sanitized_payload, dict) else None,
            )
        )

    def _notify_event(self, event: WorkflowEvent) -> None:
        runtime = self._runtimes.peek(event.execution_id)
        listener = runtime.listener if runtime is not None else None
        if listener is None:
            return
        try:
            listener(event)
        except Exception:
            # A presentation observer is never allowed to change workflow
            # semantics or make a successfully persisted transition fail.
            logger.warning("[workflow] Event listener failed")

    def _raise_if_cancellation_requested(self, execution_id: str) -> None:
        if self._repository.cancellation_requested(execution_id):
            raise WorkflowCancelledError("Cancellation requested by user")

    def _active_stage_of(self, execution_id: str) -> WorkflowStage | None:
        runtime = self._runtimes.peek(execution_id)
        return runtime.active_stage if runtime is not None else None

    def _record_validation_check(self, execution_id: str, payload: dict[str, Any]) -> None:
        self._record_diagnostic(
            execution_id, DiagnosticCategory.VALIDATION, payload, stage=WorkflowStage.VALIDATE
        )
        self._raise_if_cancellation_requested(execution_id)
