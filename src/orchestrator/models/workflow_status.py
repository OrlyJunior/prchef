from enum import StrEnum


class WorkflowStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    FAILED = "FAILED"


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
    SKIPPED = "SKIPPED"


class ReviewDecision(StrEnum):
    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    NEEDS_HUMAN = "NEEDS_HUMAN"


class PolishOutcome(StrEnum):
    """The single evaluated result of optional approved-review polish."""

    APPLIED = "APPLIED"
    SKIPPED = "SKIPPED"
