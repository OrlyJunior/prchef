from prchef.models.workflow_interruption import (
    ExecutionOwnershipLostError as ExecutionOwnershipLostError,
)
from prchef.models.workflow_interruption import (
    WorkflowCancelledError as WorkflowCancelledError,
)
from prchef.models.workflow_interruption import (
    WorkflowError as WorkflowError,
)


class StructuredOutputError(WorkflowError):
    pass

