import logging
from pathlib import Path

import pytest

from orchestrator.clients.exceptions import GitHubClientError
from orchestrator.config.project_config import GitHubAppSettings
from orchestrator.models import (
    CodeReview,
    DevelopmentTask,
    RepositorySpec,
    ReviewDecision,
    StepStatus,
    ValidationResult,
    WorkflowStage,
    WorkflowStatus,
)
from orchestrator.providers.agent_provider import AgentNeedsHumanError
from orchestrator.runtime_logging import log_review_summary, log_spec_summary
from orchestrator.services.notifications import NotificationService
from orchestrator.services.task_provider import GitHubIssueTaskProvider, TrelloTaskProvider
from orchestrator.services.workflow_service import WorkflowService
from tests.unit.fakes import (
    FakeClientFactory,
    FakeConfigLoader,
    FakeTrelloClient,
    FakeWorkflowRepository,
)


@pytest.fixture
def workflow(tmp_path: Path):
    repository = FakeWorkflowRepository()
    trello = FakeTrelloClient()
    factory = FakeClientFactory()
    service = WorkflowService(
        repository, TrelloTaskProvider(trello), FakeConfigLoader(tmp_path), factory
    )
    return service, repository, trello, factory


def test_happy_path_finishes_approved_and_records_outputs(workflow) -> None:
    service, repository, _, factory = workflow

    result = service.run("card-1")

    assert result.status is WorkflowStatus.APPROVED
    assert result.working_branch is not None
    assert result.commit_sha == "a" * 40
    assert result.pr_url == "https://github.example/pr/42"
    assert result.pull_request_snapshot is not None
    assert result.validation_snapshot is not None
    assert result.review_snapshot is not None
    assert result.task_source == "trello"
    assert result.task_id == "card-1"
    assert result.task_url == "https://trello.example/card-1"
    assert result.spec_jules_session_id == "spec-123"
    assert result.review_jules_session_url == "https://jules.example/review-123"
    assert factory.jules.spec_starting_branch == "develop"
    assert factory.jules.review_starting_branch == result.working_branch
    assert factory.github.review_calls == [
        {
            "repository": "example/api",
            "pull_request_number": 42,
            "event": "APPROVE",
            "body": "## Automated review: APPROVED\n\n**Summary:** Looks good\n\n"
            "No blocking findings or non-blocking suggestions.",
        }
    ]
    assert all(step["status"] is StepStatus.SUCCEEDED for step in repository.steps.values())


def test_workflow_assigns_configured_implementer_bot_but_not_reviewer(workflow) -> None:
    service, _, _, factory = workflow
    loader = service._config_loader
    loader.config.integrations.github.implementer_app = GitHubAppSettings(
        app_id=1,
        installation_id=2,
        private_key_env="IMPLEMENTER_GITHUB_APP_PRIVATE_KEY",
        bot_name="implementer[bot]",
        bot_user_id=3,
    )
    loader.config.integrations.github.app = GitHubAppSettings(
        app_id=4,
        installation_id=5,
        private_key_env="REVIEWER_GITHUB_APP_PRIVATE_KEY",
        bot_name="reviewer[bot]",
        bot_user_id=6,
    )

    result = service.run("card-implementer-assignment")

    assert result.status is WorkflowStatus.APPROVED
    assert factory.github.assignment_calls == [
        {
            "pr_number": 42,
            "assignees": ["implementer[bot]"],
            "cwd": factory.git.workspace_path,
        }
    ]


def test_workflow_without_implementer_app_does_not_assign_pull_request(workflow) -> None:
    service, _, _, factory = workflow

    result = service.run("card-no-implementer-assignment")

    assert result.status is WorkflowStatus.APPROVED
    assert factory.github.assignment_calls == []


def test_implementer_assignment_failure_is_logged_and_does_not_block_review(
    workflow, caplog: pytest.LogCaptureFixture
) -> None:
    service, _, _, factory = workflow
    loader = service._config_loader
    loader.config.integrations.github.implementer_app = GitHubAppSettings(
        app_id=1,
        installation_id=2,
        private_key_env="IMPLEMENTER_GITHUB_APP_PRIVATE_KEY",
        bot_name="implementer[bot]",
        bot_user_id=3,
    )
    factory.github.assignment_error = GitHubClientError(
        "GitHub assignment unavailable: GH_TOKEN=ghs_assignment_secret"
    )

    with caplog.at_level(logging.WARNING):
        result = service.run("card-implementer-assignment-failure")

    assert result.status is WorkflowStatus.APPROVED
    assert len(factory.github.assignment_calls) == 1
    assert len(factory.github.review_calls) == 1
    assert "Implementer assignment failed for pull request #42" in caplog.text
    assert "ghs_assignment_secret" not in caplog.text


def test_workflow_events_follow_persisted_transitions_and_do_not_control_execution(
    workflow,
) -> None:
    service, _, _, _ = workflow
    events = []

    result = service.run("card-events", event_listener=events.append)

    assert result.status is WorkflowStatus.APPROVED
    assert events[0].kind.value == "EXECUTION_CREATED"
    assert events[1].kind.value == "STATUS_CHANGED"
    assert any(event.kind.value == "STAGE_STARTED" for event in events)
    assert any(event.kind.value == "STAGE_FINISHED" for event in events)
    assert events[-1].kind.value == "WORKFLOW_FINISHED"


def test_workflow_event_listener_failures_do_not_control_execution(workflow) -> None:
    service, _, _, _ = workflow

    def broken_listener(*_args) -> None:
        raise RuntimeError("rendering unavailable")

    assert service.run("card-events-listener-error", event_listener=broken_listener).status is (
        WorkflowStatus.APPROVED
    )


class _RecordingNotifier:
    def __init__(self) -> None:
        self.events = []

    def notify(self, event) -> None:
        self.events.append(event)


def _workflow_with_notifications(tmp_path: Path):
    repository = FakeWorkflowRepository()
    loader = FakeConfigLoader(tmp_path)
    loader.config.notifications.enabled = True
    recorder = _RecordingNotifier()
    service = WorkflowService(
        repository,
        TrelloTaskProvider(FakeTrelloClient()),
        loader,
        FakeClientFactory(),
        notification_service=NotificationService({"desktop": recorder}),
    )
    return service, recorder


@pytest.mark.parametrize(
    ("configure", "expected_status"),
    [
        (lambda factory: None, WorkflowStatus.APPROVED),
        (lambda factory: setattr(factory.codex, "fail", True), WorkflowStatus.FAILED),
        (lambda factory: setattr(factory.jules, "ambiguous", True), WorkflowStatus.NEEDS_HUMAN),
        (
            lambda factory: setattr(factory.jules, "changes_requested", True),
            WorkflowStatus.CHANGES_REQUESTED,
        ),
    ],
)
def test_relevant_terminal_outcomes_emit_one_notification(
    tmp_path: Path, configure, expected_status: WorkflowStatus
) -> None:
    service, recorder = _workflow_with_notifications(tmp_path)
    configure(service._client_factory)

    result = service.run("card-notifications")

    assert result.status is expected_status
    assert [event.status for event in recorder.events] == [expected_status]


def test_recoverable_validation_and_review_retries_do_not_emit_notifications(
    tmp_path: Path,
) -> None:
    service, recorder = _workflow_with_notifications(tmp_path)
    factory = service._client_factory
    factory.commands.failures_remaining = 1
    factory.jules.review_decisions = ["CHANGES_REQUESTED", "APPROVED"]

    result = service.run("card-retry-notifications")

    assert result.status is WorkflowStatus.APPROVED
    assert [event.status for event in recorder.events] == [WorkflowStatus.APPROVED]


def test_trello_failure_is_persisted_as_failed(workflow) -> None:
    service, repository, trello, _ = workflow
    trello.error = RuntimeError("trello unavailable")

    result = service.run("card-2")

    assert result.status is WorkflowStatus.FAILED
    assert result.error_message == "trello unavailable"
    assert result.current_stage is WorkflowStage.LOAD_TASK
    assert repository.steps[1]["status"] is StepStatus.FAILED


def test_blocking_jules_ambiguity_stops_for_human_before_branch(workflow) -> None:
    service, _, _, factory = workflow
    factory.jules.ambiguous = True

    result = service.run("card-3")

    assert result.status is WorkflowStatus.NEEDS_HUMAN
    assert result.working_branch is None
    assert "retention" in (result.error_message or "").lower()
    assert result.human_interactions[-1].requesting_stage is WorkflowStage.GENERATE_SPEC
    assert "retention" in result.human_interactions[-1].prompt.lower()


def test_planner_human_response_resumes_same_execution_without_prior_workspace(
    workflow,
) -> None:
    service, repository, _, factory = workflow
    factory.jules.ambiguous = True

    paused = service.run("card-planner-resume")
    assert paused.workspace_path is None
    factory.jules.ambiguous = False

    resumed = service.respond_and_resume(paused.id, "Use a configurable retention period.")

    assert resumed.id == paused.id
    assert resumed.status is WorkflowStatus.APPROVED
    assert resumed.human_interactions[-1].response == "Use a configurable retention period."
    assert factory.jules.spec_calls == 2
    assert resumed.working_branch is not None
    assert repository.stage_changes.count(WorkflowStage.PREPARE_BRANCH) == 1


def test_reviewer_human_response_reuses_existing_workspace_branch_and_pr(workflow) -> None:
    service, _, _, factory = workflow
    factory.jules.malformed_review = True

    paused = service.run("card-review-resume")
    workspace = paused.workspace_path
    branch = paused.working_branch
    assert paused.status is WorkflowStatus.NEEDS_HUMAN
    assert paused.human_interactions[-1].requesting_stage is WorkflowStage.REVIEW
    factory.jules.malformed_review = False

    resumed = service.respond_and_resume(paused.id, "The expected behavior is intentional.")

    assert resumed.id == paused.id
    assert resumed.status is WorkflowStatus.APPROVED
    assert resumed.workspace_path == workspace
    assert resumed.working_branch == branch
    assert factory.github.create_calls == 1


def test_execution_can_pause_for_multiple_human_input_cycles(workflow) -> None:
    service, _, _, factory = workflow
    factory.jules.ambiguous = True

    first_pause = service.run("card-multiple-human")
    second_pause = service.respond_and_resume(first_pause.id, "First clarification.")
    assert second_pause.status is WorkflowStatus.NEEDS_HUMAN

    factory.jules.ambiguous = False
    completed = service.respond_and_resume(second_pause.id, "Second clarification.")

    assert completed.status is WorkflowStatus.APPROVED
    assert completed.id == first_pause.id
    assert [item.response for item in completed.human_interactions] == [
        "First clarification.",
        "Second clarification.",
    ]


def test_codex_failure_stops_before_validation(workflow) -> None:
    service, repository, _, factory = workflow
    factory.codex.fail = True

    result = service.run("card-4")

    assert result.status is WorkflowStatus.FAILED
    assert result.error_message == "codex exploded"
    assert WorkflowStage.VALIDATE not in repository.stage_changes
    assert result.workspace_path is not None
    assert Path(result.workspace_path).is_dir()


def test_failed_implementation_preserves_execution_workspace_and_artifacts(workflow) -> None:
    service, _, _, factory = workflow
    factory.codex.fail = True
    factory.codex.leave_artifact_on_failure = True

    result = service.run("card-preserve-workspace")

    workspace = Path(result.workspace_path or "")
    assert result.status is WorkflowStatus.FAILED
    assert (workspace / "partial-implementation.txt").read_text(encoding="utf-8") == "unfinished"


def test_mutable_workflow_stages_use_the_execution_workspace(workflow) -> None:
    service, repository, _, factory = workflow

    result = service.run("card-workspace")

    workspace = Path(result.workspace_path or "")
    assert workspace.is_dir()
    assert factory.codex.workspace_path == workspace
    assert factory.commands.working_directories == [workspace]
    assert factory.git.workspace_path == workspace
    assert factory.git.operation_paths == [workspace, workspace, workspace, workspace]
    assert result.repository != str(workspace)
    assert repository.execution is not None


def test_workspace_branch_name_contains_execution_id_and_is_not_protected(workflow) -> None:
    service, _, _, _ = workflow

    result = service.run("card-branch")

    assert result.working_branch is not None
    assert result.working_branch.startswith("orchestrator/")
    assert result.working_branch != "develop"
    assert result.working_branch.endswith("execution-1")


def test_validation_failure_stops_before_commit(workflow) -> None:
    service, repository, _, factory = workflow
    factory.commands.fail = True

    result = service.run("card-5")

    assert result.status is WorkflowStatus.FAILED
    assert result.validation_snapshot is not None
    assert result.commit_sha is None
    assert WorkflowStage.COMMIT not in repository.stage_changes
    assert WorkflowStage.PUSH not in repository.stage_changes
    assert WorkflowStage.CREATE_PR not in repository.stage_changes


def test_validation_failure_requests_in_place_implementation_correction(workflow) -> None:
    service, _, _, factory = workflow
    factory.commands.failures_remaining = 1

    result = service.run("card-correction")

    assert result.status is WorkflowStatus.APPROVED
    assert len(factory.codex.requests) == 2
    correction = factory.codex.requests[1]
    assert correction.workspace_path == factory.codex.requests[0].workspace_path
    assert "VALIDATION FAILURES FROM THE CURRENT IMPLEMENTATION" in correction.instructions
    assert "Command: pytest" in correction.instructions
    assert "Exit code: 1" in correction.instructions
    assert "assertion failed" in correction.instructions


def test_multiple_validation_corrections_are_supported(workflow) -> None:
    service, repository, _, factory = workflow
    factory.commands.failures_remaining = 2

    result = service.run("card-multiple-corrections")

    assert result.status is WorkflowStatus.APPROVED
    assert len(factory.codex.requests) == 3
    assert repository.stage_changes.count(WorkflowStage.VALIDATE) == 3
    assert repository.stage_changes.count(WorkflowStage.IMPLEMENT) == 3
    assert factory.jules.spec_calls == 1


def test_validation_retry_limit_fails_and_preserves_workspace(workflow) -> None:
    service, repository, _, factory = workflow
    factory.commands.fail = True

    result = service.run("card-exhausted-corrections")

    assert result.status is WorkflowStatus.FAILED
    assert len(factory.codex.requests) == 3
    assert result.workspace_path is not None
    assert Path(result.workspace_path).is_dir()
    assert WorkflowStage.COMMIT not in repository.stage_changes


def test_branch_creation_failure_is_terminal(workflow) -> None:
    service, repository, _, factory = workflow
    factory.git.branch_error = RuntimeError("cannot create branch")

    result = service.run("card-6")

    assert result.status is WorkflowStatus.FAILED
    assert result.error_message == "cannot create branch"
    failed = next(step for step in repository.steps.values() if step["status"] is StepStatus.FAILED)
    assert failed["stage"] is WorkflowStage.PREPARE_BRANCH


def test_push_failure_persists_error_and_does_not_create_pr(workflow) -> None:
    service, repository, _, factory = workflow
    factory.git.push_error = RuntimeError("origin rejected push")

    result = service.run("card-7")

    assert result.status is WorkflowStatus.FAILED
    assert result.error_message == "origin rejected push"
    assert result.pr_url is None
    assert WorkflowStage.CREATE_PR not in repository.stage_changes


def test_pull_request_failure_is_terminal(workflow) -> None:
    service, _, _, factory = workflow
    factory.github.error = RuntimeError("github unavailable")

    result = service.run("card-8")

    assert result.status is WorkflowStatus.FAILED
    assert result.error_message == "github unavailable"
    assert result.pr_url is None


def test_review_correction_revalidates_updates_existing_pr_and_can_be_approved(workflow) -> None:
    service, _, _, factory = workflow
    factory.jules.review_decisions = ["CHANGES_REQUESTED", "APPROVED"]

    result = service.run("card-9")

    assert result.status is WorkflowStatus.APPROVED
    assert result.review_snapshot is not None
    assert result.review_snapshot["result"] == "APPROVED"
    assert len(factory.codex.requests) == 2
    assert factory.codex.requests[1].workspace_path == factory.codex.requests[0].workspace_path
    assert "REVIEWER CHANGE REQUESTS" in factory.codex.requests[1].instructions
    assert factory.commands.working_directories[-1] == factory.codex.requests[0].workspace_path
    assert factory.git.commit_calls == 2
    assert factory.git.push_calls == 2
    assert factory.git.diff_calls == 2
    assert factory.github.create_calls == 1
    assert [call["event"] for call in factory.github.review_calls] == [
        "REQUEST_CHANGES",
        "APPROVE",
    ]
    assert "### Blocking findings" in factory.github.review_calls[0]["body"]


def test_approved_review_suggestions_can_be_declined_without_changing_approval(workflow) -> None:
    service, repository, _, factory = workflow
    factory.jules.review_suggestions = [["Add a focused regression test"]]
    factory.git.changed_files_results = [["src/audit.py"], []]

    result = service.run("card-polish-declined")

    assert result.status is WorkflowStatus.APPROVED
    assert repository.stage_changes.count(WorkflowStage.POLISH) == 1
    assert factory.jules.review_calls == 1
    assert factory.git.commit_calls == 1
    assert factory.git.push_calls == 1
    assert len(factory.codex.requests) == 2
    assert "NON-BLOCKING REVIEW SUGGESTIONS" in factory.codex.requests[1].instructions
    assert factory.commands.working_directories == [factory.codex.requests[0].workspace_path]
    assert factory.github.comment_calls == [
        {
            "pr_number": 42,
            "body": "## Optional polish\n\nThe non-blocking review suggestions were evaluated "
            "and intentionally skipped.\n\n- No small, safe, in-scope code change was warranted.",
        }
    ]


def test_applied_polish_is_validated_pushed_to_existing_pr_and_re_reviewed(workflow) -> None:
    service, repository, _, factory = workflow
    factory.jules.review_decisions = ["APPROVED", "APPROVED"]
    factory.jules.review_suggestions = [["Add a focused regression test"]]

    result = service.run("card-polish-applied")

    assert result.status is WorkflowStatus.APPROVED
    assert repository.stage_changes.count(WorkflowStage.POLISH) == 1
    assert repository.stage_changes.count(WorkflowStage.REVIEW) == 2
    assert repository.stage_changes.count(WorkflowStage.VALIDATE) == 2
    assert factory.git.commit_calls == 2
    assert factory.git.push_calls == 2
    assert factory.git.diff_calls == 2
    assert factory.github.create_calls == 1
    assert factory.jules.review_calls == 2
    assert factory.github.comment_calls == []


def test_fresh_approved_review_does_not_trigger_a_second_polish_pass(workflow) -> None:
    service, repository, _, factory = workflow
    factory.jules.review_decisions = ["APPROVED", "APPROVED"]
    factory.jules.review_suggestions = [
        ["Add a focused regression test"],
        ["Consider clearer naming"],
    ]

    result = service.run("card-polish-once")

    assert result.status is WorkflowStatus.APPROVED
    assert repository.stage_changes.count(WorkflowStage.POLISH) == 1
    assert factory.jules.review_calls == 2
    assert len(factory.codex.requests) == 2


def test_invalid_optional_polish_is_discarded_without_downgrading_approval(workflow) -> None:
    service, repository, _, factory = workflow
    factory.jules.review_suggestions = [["Add a focused regression test"]]
    factory.commands.failure_sequence = [False, True]

    result = service.run("card-polish-invalid")

    assert result.status is WorkflowStatus.APPROVED
    assert repository.stage_changes.count(WorkflowStage.POLISH) == 1
    assert repository.stage_changes.count(WorkflowStage.VALIDATE) == 2
    assert factory.git.discard_calls == 1
    assert factory.git.commit_calls == 1
    assert factory.git.push_calls == 1
    assert factory.jules.review_calls == 1


def test_changes_requested_after_polish_uses_mandatory_correction_loop(workflow) -> None:
    service, repository, _, factory = workflow
    factory.jules.review_decisions = ["APPROVED", "CHANGES_REQUESTED", "APPROVED"]
    factory.jules.review_suggestions = [["Add a focused regression test"]]

    result = service.run("card-polish-changes-requested")

    assert result.status is WorkflowStatus.APPROVED
    assert repository.stage_changes.count(WorkflowStage.POLISH) == 1
    assert repository.stage_changes.count(WorkflowStage.IMPLEMENT) == 2
    assert repository.stage_changes.count(WorkflowStage.REVIEW) == 3
    assert factory.git.commit_calls == 3
    assert factory.git.push_calls == 3
    assert factory.github.create_calls == 1


def test_approved_review_without_suggestions_does_not_enter_polish_stage(workflow) -> None:
    service, repository, _, factory = workflow

    result = service.run("card-no-polish")

    assert result.status is WorkflowStatus.APPROVED
    assert WorkflowStage.POLISH not in repository.stage_changes
    assert factory.github.comment_calls == []


def test_polish_skip_comment_publication_failure_preserves_approval(workflow, caplog) -> None:
    service, _, _, factory = workflow
    factory.jules.review_suggestions = [["Add a focused regression test"]]
    factory.git.changed_files_results = [["src/audit.py"], []]
    factory.github.comment_error = RuntimeError("GitHub unavailable")

    with caplog.at_level(logging.WARNING):
        result = service.run("card-polish-comment-failure")

    assert result.status is WorkflowStatus.APPROVED
    assert result.error_message is None
    assert len(factory.github.comment_calls) == 1
    assert "Optional polish skip comment publication failed: GitHub unavailable" in caplog.text


def test_review_publication_failure_preserves_reviewer_decision(workflow) -> None:
    service, _, _, factory = workflow
    factory.github.review_error = RuntimeError("GitHub unavailable")

    result = service.run("card-review-publish-failure")

    assert result.status is WorkflowStatus.APPROVED
    assert result.review_snapshot is not None
    assert result.review_snapshot["result"] == "APPROVED"
    assert result.error_message == "GitHub review publication failed: GitHub unavailable"
    assert len(factory.github.review_calls) == 1


def test_review_correction_limit_finishes_changes_requested(workflow) -> None:
    service, repository, _, factory = workflow
    factory.jules.changes_requested = True

    result = service.run("card-review-exhausted")

    assert result.status is WorkflowStatus.CHANGES_REQUESTED
    assert result.review_snapshot is not None
    assert result.review_snapshot["result"] == "CHANGES_REQUESTED"
    assert factory.jules.review_calls == 2
    assert len(factory.codex.requests) == 2
    assert repository.stage_changes.count(WorkflowStage.REVIEW) == 2
    assert factory.github.create_calls == 1


def test_review_correction_uses_validation_retry_handling(workflow) -> None:
    service, repository, _, factory = workflow
    factory.jules.review_decisions = ["CHANGES_REQUESTED", "APPROVED"]
    factory.commands.failure_sequence = [False, True, False]

    result = service.run("card-review-validation-retry")

    assert result.status is WorkflowStatus.APPROVED
    assert len(factory.codex.requests) == 3
    assert repository.stage_changes.count(WorkflowStage.IMPLEMENT) == 3
    assert repository.stage_changes.count(WorkflowStage.VALIDATE) == 3
    assert factory.github.create_calls == 1


def test_malformed_jules_review_requires_human_instead_of_assuming_approval(workflow) -> None:
    service, _, _, factory = workflow
    factory.jules.malformed_review = True

    result = service.run("card-malformed-review")

    assert result.status is WorkflowStatus.NEEDS_HUMAN
    assert result.review_snapshot is not None
    assert result.review_snapshot["result"] == "NEEDS_HUMAN"
    assert "could not be interpreted safely" in result.review_snapshot["summary"]
    assert "could not be interpreted safely" in (result.error_message or "")
    assert factory.github.review_calls[0]["event"] == "COMMENT"


def test_jules_human_intervention_stops_workflow_and_keeps_session_reference(workflow) -> None:
    service, _, _, factory = workflow
    factory.jules.spec_error = AgentNeedsHumanError(
        "Jules session is awaiting user feedback and requires human intervention", "spec-123"
    )

    result = service.run("card-needs-human")

    assert result.status is WorkflowStatus.NEEDS_HUMAN
    assert result.spec_jules_session_id == "spec-123"
    assert result.working_branch is None
    assert "awaiting user feedback" in (result.error_message or "")


def test_every_stage_transition_is_persisted_in_order(workflow) -> None:
    service, repository, _, _ = workflow

    service.run("card-10")

    assert repository.stage_changes == [
        WorkflowStage.LOAD_TASK,
        WorkflowStage.LOAD_CONFIG,
        WorkflowStage.GENERATE_SPEC,
        WorkflowStage.PREPARE_BRANCH,
        WorkflowStage.IMPLEMENT,
        WorkflowStage.VALIDATE,
        WorkflowStage.COMMIT,
        WorkflowStage.PUSH,
        WorkflowStage.CREATE_PR,
        WorkflowStage.REVIEW,
    ]


def test_workflow_logs_stage_entry_and_completion(
    workflow, caplog: pytest.LogCaptureFixture
) -> None:
    service, _, _, _ = workflow
    target_logger = logging.getLogger("orchestrator.services.workflow_service")
    target_logger.addHandler(caplog.handler)
    previous_propagation = target_logger.propagate
    target_logger.propagate = False
    try:
        with caplog.at_level(logging.INFO, logger=target_logger.name):
            service.run("card-logs")
    finally:
        target_logger.removeHandler(caplog.handler)
        target_logger.propagate = previous_propagation

    messages = [record.getMessage() for record in caplog.records]
    assert "[workflow] Loading trello task card-logs" in messages
    assert "[workflow] Entering task loading" in messages
    assert "[workflow] Completed task loading" in messages
    assert "[workflow] Repository resolved:" in " ".join(messages)


def test_verbose_workflow_logs_parsed_spec_and_review_summaries(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repository = FakeWorkflowRepository()
    service = WorkflowService(
        repository,
        TrelloTaskProvider(FakeTrelloClient()),
        FakeConfigLoader(tmp_path),
        FakeClientFactory(),
        verbose=True,
    )
    target_logger = logging.getLogger("orchestrator.services.workflow_service")
    target_logger.addHandler(caplog.handler)
    previous_propagation = target_logger.propagate
    target_logger.propagate = False
    try:
        with caplog.at_level(logging.DEBUG, logger=target_logger.name):
            service.run("card-verbose-logs")
    finally:
        target_logger.removeHandler(caplog.handler)
        target_logger.propagate = previous_propagation

    messages = [record.getMessage() for record in caplog.records]
    assert "[spec] safe_to_implement=true" in messages
    assert "[spec] proposal: Write audit record transactionally" in messages
    assert "[spec] artifacts: 1" in messages
    assert "[spec] acceptance_criteria: 1" in messages
    assert "[spec] expected_tests: 1" in messages
    assert "[spec] ambiguities: 0" in messages
    assert "[review] result=APPROVED" in messages
    assert "[review] summary: Looks good" in messages
    assert "[review] blocking_findings: 0" in messages
    assert "[review] suggestions: 0" in messages
    assert '{"technical_context"' not in caplog.text
    assert '"decision"' not in caplog.text


def test_non_verbose_workflow_does_not_log_agent_summaries(
    workflow, caplog: pytest.LogCaptureFixture
) -> None:
    service, _, _, _ = workflow
    target_logger = logging.getLogger("orchestrator.services.workflow_service")
    target_logger.addHandler(caplog.handler)
    previous_propagation = target_logger.propagate
    target_logger.propagate = False
    try:
        with caplog.at_level(logging.DEBUG, logger=target_logger.name):
            service.run("card-no-verbose-logs")
    finally:
        target_logger.removeHandler(caplog.handler)
        target_logger.propagate = previous_propagation

    assert "[spec]" not in caplog.text
    assert "[review]" not in caplog.text


def test_structured_summaries_are_bounded_and_redact_sensitive_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    target_logger = logging.getLogger("orchestrator.services.workflow_service")
    target_logger.addHandler(caplog.handler)
    previous_propagation = target_logger.propagate
    target_logger.propagate = False
    try:
        with caplog.at_level(logging.DEBUG, logger=target_logger.name):
            log_spec_summary(
                target_logger,
                RepositorySpec(
                    technical_context="context",
                    current_behavior="behavior",
                    involved_artifacts=["a.py", "b.py"],
                    implementation_proposal=("P" * 260) + " GEMINI_API_KEY=spec-secret",
                    acceptance_criteria=["one", "two"],
                    expected_tests=["test one", "test two", "test three"],
                    ambiguities=["Needs owner decision"],
                    safe_to_implement=False,
                    raw_text='{"provider_payload": "raw-spec-secret"}',
                ),
            )
            log_review_summary(
                target_logger,
                CodeReview(
                    result=ReviewDecision.CHANGES_REQUESTED,
                    summary=(
                        "Address API_KEY=review-secret and "
                        "Authorization: Bearer review-header-secret before merging"
                    ),
                    blocking_findings=["Missing authorization test"],
                    suggestions=["Consider clearer naming"],
                    raw_text='{"provider_payload": "raw-review-secret"}',
                ),
            )
    finally:
        target_logger.removeHandler(caplog.handler)
        target_logger.propagate = previous_propagation

    assert "[spec] safe_to_implement=false" in caplog.text
    assert "[spec] artifacts: 2" in caplog.text
    assert "[spec] acceptance_criteria: 2" in caplog.text
    assert "[spec] expected_tests: 3" in caplog.text
    assert "[spec] ambiguities: 1" in caplog.text
    assert "[spec] ambiguity: Needs owner decision" in caplog.text
    assert "[review] result=CHANGES_REQUESTED" in caplog.text
    assert "[review] blocking_findings: 1" in caplog.text
    assert "[review] suggestions: 1" in caplog.text
    assert "[review] blocking_finding: Missing authorization test" in caplog.text
    assert "[review] suggestion: Consider clearer naming" in caplog.text
    assert "..." in caplog.text
    assert "spec-secret" not in caplog.text
    assert "review-secret" not in caplog.text
    assert "review-header-secret" not in caplog.text
    assert "raw-spec-secret" not in caplog.text
    assert "raw-review-secret" not in caplog.text


def test_command_logging_redacts_github_api_review_body() -> None:
    from orchestrator.runtime_logging import format_command

    assert format_command(["gh", "api", "-f", "body=GITHUB_TOKEN=secret"]) == (
        "gh api -f body=[REDACTED]"
    )


def test_pull_request_closes_github_issue_in_same_repository() -> None:
    task = DevelopmentTask(
        id="42",
        source="github",
        title="Fix timeout",
        description="",
        url="https://github.com/owner/api/issues/42",
        repository="owner/api",
    )

    provider = GitHubIssueTaskProvider(object(), "owner/api")  # type: ignore[arg-type]
    reference = provider.pull_request_reference(task, "owner/api")
    body = WorkflowService._pull_request_body(task, ValidationResult(success=True), reference)

    assert "Closes #42" in body


def test_pull_request_only_references_cross_repository_issue() -> None:
    task = DevelopmentTask(
        id="42",
        source="github",
        title="Fix timeout",
        description="",
        url="https://github.com/product/backlog/issues/42",
        repository="product/backlog",
    )

    provider = GitHubIssueTaskProvider(object(), "product/backlog")  # type: ignore[arg-type]
    reference = provider.pull_request_reference(task, "owner/api")
    body = WorkflowService._pull_request_body(task, ValidationResult(success=True), reference)

    assert "Related issue: product/backlog#42" in body
    assert "Closes" not in body
