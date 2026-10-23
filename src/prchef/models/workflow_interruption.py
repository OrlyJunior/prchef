class WorkflowInterruptedError(RuntimeError):
    """The owning app session is ending while a workflow stage is active."""
