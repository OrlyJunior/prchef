from datetime import UTC, datetime
from unittest.mock import Mock

from orchestrator.models import DevelopmentTask, WorkflowExecution, WorkflowStage, WorkflowStatus
from orchestrator.models.human_interaction import HumanInteraction
from orchestrator.services.workflow_stage_sequencer import StageSequencer
from orchestrator.services.workflow_stage_values import StageValues


def test_recovered_human_answer_keeps_missing_provider_retryable(monkeypatch) -> None:
    execution = WorkflowExecution(id="execution", status=WorkflowStatus.PENDING)
    interaction = HumanInteraction(
        id=1, workflow_execution_id=execution.id,
        requesting_stage=WorkflowStage.REVIEW, prompt="Clarify", requested_at=datetime.now(UTC),
        response="Answer",
    )
    sequencer = object.__new__(StageSequencer)
    sequencer._lifecycle = Mock()
    sequencer._repository = Mock()
    sequencer._repository.reinterrupt.return_value = WorkflowExecution(
        id=execution.id, status=WorkflowStatus.INTERRUPTED,
    )
    sequencer._config_loader = Mock()
    sequencer._config_loader.load.side_effect = ValueError("Configured provider is missing")
    monkeypatch.setattr(
        StageValues, "_task_from_snapshot",
        lambda execution: DevelopmentTask(
            id="task", source="trello", title="Task", description="", url=""
        ),
    )

    result = sequencer._execute_response(execution, interaction, "Answer", recovering=True)

    assert result.status is WorkflowStatus.INTERRUPTED
    sequencer._repository.reinterrupt.assert_called_once_with(
        execution.id, "Configured provider is missing"
    )
    sequencer._repository.set_resume_mode.assert_not_called()
