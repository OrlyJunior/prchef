from enum import StrEnum


class WorkflowStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    # Admitted work that made progress but is queued for a provider concurrency
    # slot. It is deliberately distinct from PENDING (never started) so history
    # can tell "waiting to begin" from "waiting for shared provider capacity".
    WAITING = "WAITING"
    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


# Non-terminal statuses: queued for a worker, queued for shared provider
# capacity, or genuinely running. All of them remain cancellable and keep their
# workspace. Defined once so presentation, cleanup, and persistence cannot
# disagree about what "active" means.
ACTIVE_WORKFLOW_STATUSES = frozenset(
    {WorkflowStatus.PENDING, WorkflowStatus.WAITING, WorkflowStatus.RUNNING}
)


class WorkflowStage(StrEnum):
    LOAD_TASK = "LOAD_TASK"
    LOAD_CONFIG = "LOAD_CONFIG"
    GENERATE_SPEC = "GENERATE_SPEC"
    PREPARE_BRANCH = "PREPARE_BRANCH"
    IMPLEMENT = "IMPLEMENT"
    POLISH = "POLISH"
    VALIDATE = "VALIDATE"
    COMMIT = "COMMIT"
    PUSH = "PUSH"
    CREATE_PR = "CREATE_PR"
    REVIEW = "REVIEW"
    COMPLETED = "COMPLETED"


class StepStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"


class ReviewDecision(StrEnum):
    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    NEEDS_HUMAN = "NEEDS_HUMAN"


class PolishOutcome(StrEnum):
    """The single evaluated result of optional approved-review polish."""

    APPLIED = "APPLIED"
    SKIPPED = "SKIPPED"
