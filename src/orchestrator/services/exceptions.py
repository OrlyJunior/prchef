class WorkflowError(RuntimeError):
    pass


class WorkflowCancelledError(WorkflowError):
    """Raised when a persisted user cancellation request reaches a safe boundary."""


class StructuredOutputError(WorkflowError):
    pass

