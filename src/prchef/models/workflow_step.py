from dataclasses import dataclass
from datetime import datetime

from prchef.models.workflow_status import StepStatus, WorkflowStage


@dataclass(slots=True)
class WorkflowStep:
    id: int | None
    workflow_execution_id: str
    stage: WorkflowStage
    status: StepStatus
    started_at: datetime
    finished_at: datetime | None = None
    error_message: str | None = None

