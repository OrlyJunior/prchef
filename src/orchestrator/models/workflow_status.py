from enum import StrEnum


class WorkflowStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    # Admitted work that made progress but is queued for a provider concurrency
    # slot. It is deliberately distinct from PENDING (never started) so history
    # can tell "waiting to begin" from "waiting for shared provider capacity".
    WAITING = "WAITING"
    INTERRUPTED = "INTERRUPTED"
    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


# Work admitted to this session: queued for a worker, queued for shared
# provider capacity, or genuinely running. INTERRUPTED is recoverable but has
# no worker in this session.
ACTIVE_WORKFLOW_STATUSES = frozenset(
    {WorkflowStatus.PENDING, WorkflowStatus.WAITING, WorkflowStatus.RUNNING}
)


FRESH_RUN_STATUSES = frozenset({
    WorkflowStatus.FAILED,
    WorkflowStatus.CANCELLED,
    WorkflowStatus.NEEDS_HUMAN,
    WorkflowStatus.CHANGES_REQUESTED,
})


def can_start_fresh_run(latest: WorkflowStatus | None) -> bool:
    """Whether a task may acquire another execution in the same repository."""

    return latest is None or latest in FRESH_RUN_STATUSES


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
    INTERRUPTED = "INTERRUPTED"
    SKIPPED = "SKIPPED"


class ReviewDecision(StrEnum):
    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    NEEDS_HUMAN = "NEEDS_HUMAN"


class PolishOutcome(StrEnum):
    """The single evaluated result of optional approved-review polish."""

    APPLIED = "APPLIED"
    SKIPPED = "SKIPPED"
