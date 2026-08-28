from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from orchestrator.models.workflow_status import WorkflowStage, WorkflowStatus


@dataclass(slots=True)
class WorkflowExecution:
    id: str
    trello_card_id: str
    trello_card_url: str = ""
    repository: str = ""
    base_branch: str = ""
    working_branch: str | None = None
    commit_sha: str | None = None
    pr_url: str | None = None
    current_stage: WorkflowStage = WorkflowStage.LOAD_TASK
    status: WorkflowStatus = WorkflowStatus.PENDING
    error_message: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    task_snapshot: dict[str, Any] | None = None
    spec_snapshot: dict[str, Any] | None = None
    implementation_snapshot: dict[str, Any] | None = None
    validation_snapshot: dict[str, Any] | None = None
    pull_request_snapshot: dict[str, Any] | None = None
    spec_jules_session_id: str | None = None
    spec_jules_session_url: str | None = None
    review_jules_session_id: str | None = None
    review_jules_session_url: str | None = None
    review_snapshot: dict[str, Any] | None = None
