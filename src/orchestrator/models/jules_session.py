from dataclasses import dataclass
from enum import StrEnum


class JulesSessionState(StrEnum):
    STATE_UNSPECIFIED = "STATE_UNSPECIFIED"
    QUEUED = "QUEUED"
    PLANNING = "PLANNING"
    AWAITING_PLAN_APPROVAL = "AWAITING_PLAN_APPROVAL"
    AWAITING_USER_FEEDBACK = "AWAITING_USER_FEEDBACK"
    IN_PROGRESS = "IN_PROGRESS"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class JulesSession:
    id: str
    url: str | None
    state: JulesSessionState
    resource_name: str | None = None


@dataclass(frozen=True, slots=True)
class JulesOperationResult:
    session: JulesSession
    result_text: str
