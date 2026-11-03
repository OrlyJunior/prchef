"""Workflow stop signals shared by repositories and service safe boundaries."""


class WorkflowError(RuntimeError):
    pass


class WorkflowCancelledError(WorkflowError):
    """A persisted cancellation request reached a safe boundary."""


class ExecutionOwnershipLostError(WorkflowCancelledError):
    """This instance may no longer write or work on the execution."""


class WorkflowInterruptedError(RuntimeError):
    """The owning app session is ending while a workflow stage is active."""
