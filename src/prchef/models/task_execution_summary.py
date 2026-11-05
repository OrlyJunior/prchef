from dataclasses import dataclass

from prchef.models.workflow_status import WorkflowStage, WorkflowStatus


@dataclass(frozen=True, slots=True)
class TaskExecutionSummary:
    """Latest-run information needed to browse tasks and confirm a fresh run."""

    id: str
    status: WorkflowStatus
    current_stage: WorkflowStage
    resume_stage: WorkflowStage | None
    has_pull_request: bool
