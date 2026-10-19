from dataclasses import dataclass
from datetime import datetime

from prchef.models.workflow_status import WorkflowStage


@dataclass(slots=True)
class HumanInteraction:
    """A persisted request for input during an existing workflow execution."""

    id: int | None
    workflow_execution_id: str
    requesting_stage: WorkflowStage
    prompt: str
    requested_at: datetime
    response: str | None = None
    responded_at: datetime | None = None

    @property
    def awaiting_response(self) -> bool:
        return self.response is None
