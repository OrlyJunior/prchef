import logging
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import orchestrator.services.workflow_service as workflow_service_module
from orchestrator.clients.exceptions import (
    CommandProcessTreeUnconfirmedError,
    CommandTimeoutError,
    GitClientError,
    GitHubClientError,
)
from orchestrator.config.project_config import AgentProviderSettings, GitHubAppSettings
from orchestrator.models import (
    AgentRole,
    CodeReview,
    DevelopmentTask,
    ImplementationResult,
    RepositorySpec,
    ReviewDecision,
    StepStatus,
    ValidationResult,
    WorkflowStage,
    WorkflowStatus,
)
from orchestrator.models.execution_diagnostic import DiagnosticCategory
from orchestrator.models.workflow_event import WorkflowEventKind
from orchestrator.providers.agent_provider import AgentNeedsHumanError
from orchestrator.runtime_logging import log_review_summary, log_spec_summary
from orchestrator.services.notifications import NotificationService
from orchestrator.services.project_context import MAX_PROJECT_CONTEXT_CHARS
from orchestrator.services.repository_context import RepositoryContextRequest
from orchestrator.services.task_provider import GitHubIssueTaskProvider, TrelloTaskProvider
from orchestrator.services.workflow_service import WorkflowService
from orchestrator.services.workflow_stage_sequencer import _DiagnosedRepositoryContext
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
    assert result.base_commit == "b" * 40
    assert factory.git.fetch_calls == 1
    assert result.review_jules_session_url == "https://provider.example/review-123"
    assert factory.planner.spec_starting_branch == "develop"
    assert factory.planner.review_starting_branch == result.working_branch
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


def test_review_retry_fetches_changed_description_each_round(workflow) -> None:
    service, _, _, factory = workflow
    factory.planner.review_decisions = ["CHANGES_REQUESTED", "APPROVED"]
    factory.github.descriptions = ["First criteria", "Edited criteria"]

    result = service.run("card-description-retry")

    assert result.status is WorkflowStatus.APPROVED
    assert factory.github.description_calls == [("example/api", 42)] * 2
    assert "First criteria" in factory.planner.review_prompts[0]
    assert "Edited criteria" in factory.planner.review_prompts[1]
    assert "First criteria" not in factory.planner.review_prompts[1]
    assert "description" not in result.pull_request_snapshot


@pytest.mark.parametrize(
    "error",
    [
        GitHubClientError("GH_TOKEN=ghs_secret permission denied"),
        CommandTimeoutError(
            "GH_TOKEN=ghs_secret timed out", timeout_seconds=30
        ),
    ],
)
def test_review_description_failure_warns_without_changing_outcome(
    workflow, error: Exception
) -> None:
    service, repository, _, factory = workflow
    factory.github.description_error = error

    result = service.run("card-description-failure")

    assert result.status is WorkflowStatus.APPROVED
    assert "[unavailable: description could not be fetched]" in factory.planner.review_prompts[0]
    warning = next(
        item for item in repository.diagnostics
        if item.payload.get("kind") == "PULL_REQUEST_DESCRIPTION_UNAVAILABLE"
    )
    assert warning.stage is WorkflowStage.REVIEW
    assert (
        "permission denied" in warning.payload["reason"]
        or "timed out" in warning.payload["reason"]
    )
    assert "ghs_secret" not in str(warning.payload)


def test_review_description_unconfirmed_process_tree_fails_before_reviewer(workflow) -> None:
    service, repository, _, factory = workflow
    factory.github.description_error = CommandProcessTreeUnconfirmedError(
        "Command timed out and its process tree could not be confirmed terminated"
    )

    result = service.run("card-description-unconfirmed-process")

    assert result.status is WorkflowStatus.FAILED
    assert factory.github.description_calls == [("example/api", 42)]
    assert factory.planner.review_prompts == []
    assert not any(
        item.payload.get("kind") == "PULL_REQUEST_DESCRIPTION_UNAVAILABLE"
        for item in repository.diagnostics
    )


def test_resumed_review_fetches_edited_description(workflow) -> None:
    service, _, _, factory = workflow
    factory.planner.malformed_review = True
    factory.github.description = "Before clarification"
    paused = service.run("card-description-resume")
    assert paused.status is WorkflowStatus.NEEDS_HUMAN

    factory.planner.malformed_review = False
    factory.github.description = "After clarification"
    resumed = service.respond_and_resume(paused.id, "Proceed with review.")

    assert resumed.status is WorkflowStatus.APPROVED
    assert factory.github.description_calls == [("example/api", 42)] * 2
    assert "Before clarification" in factory.planner.review_prompts[0]
    assert "After clarification" in factory.planner.review_prompts[1]


def test_implementation_continuation_fetches_edited_description(workflow) -> None:
    service, _, _, factory = workflow
    factory.planner.changes_requested = True
    factory.github.description = "Original description"
    paused = service.run("card-description-continuation")
    assert paused.status is WorkflowStatus.CHANGES_REQUESTED

    factory.planner.changes_requested = False
    factory.github.description = "Updated description"
    resumed = service.continue_execution(paused.id, "Address the review.")

    assert resumed.status is WorkflowStatus.APPROVED
    assert "Updated description" in factory.planner.review_prompts[-1]
    assert factory.github.description_calls[-1] == ("example/api", 42)


def test_index_mismatch_is_reported_before_retrieval_changes_metadata() -> None:
    calls: list[object] = []

    class Strategy:
        def inspect_index(self) -> SimpleNamespace:
            calls.append("inspect")
            return SimpleNamespace(last_indexed_commit="a" * 40)

        def render(self, request: object) -> str:
            calls.append("render")
            return "context"

    strategy = _DiagnosedRepositoryContext(
        Strategy(), lambda commit: calls.append(("mismatch", commit)), "b" * 40
    )
    assert strategy.render(SimpleNamespace(role="planner")) == "context"  # type: ignore[arg-type]
    assert calls == ["inspect", ("mismatch", "a" * 40), "render"]
    calls.clear()
    assert strategy.render(SimpleNamespace(role="reviewer")) == "context"  # type: ignore[arg-type]
    assert calls == ["render"]


def test_index_mismatch_uses_lightweight_provenance_when_available() -> None:
    calls: list[object] = []

    class Strategy:
        def inspect_indexed_commit(self) -> str:
            calls.append("commit")
            return "a" * 40

        def inspect_index(self) -> SimpleNamespace:
            raise AssertionError("full index inspection is unnecessary")

        def render(self, request: object) -> str:
            calls.append("render")
            return "context"

    strategy = _DiagnosedRepositoryContext(
        Strategy(), lambda commit: calls.append(("mismatch", commit)), "b" * 40
    )
    assert strategy.render(SimpleNamespace(role="planner")) == "context"  # type: ignore[arg-type]
    assert calls == ["commit", ("mismatch", "a" * 40), "render"]


def test_index_mismatch_is_persisted_on_workflow_diagnostics(workflow) -> None:
    service, repository, _, _ = workflow
    execution = service.run("card-index-provenance")

    class Strategy:
        def inspect_index(self) -> SimpleNamespace:
            return SimpleNamespace(last_indexed_commit="a" * 40)

        def render(self, request: object) -> str:
            return "context"

    service._sequencer._context_resolver = lambda *args, **kwargs: Strategy()
    config = SimpleNamespace(
        path=Path("repo"),
        configured_repository_key="repo",
        repository_context=SimpleNamespace(strategy="qdrant"),
    )
    strategy = service._sequencer._repository_context_strategy(config, execution.id)
    strategy.render(SimpleNamespace(role="planner"))  # type: ignore[arg-type]

    mismatch = repository.diagnostics[-1]
    assert mismatch.payload["kind"] == "REPOSITORY_INDEX_BASE_MISMATCH"
    assert mismatch.payload["base_commit"] == execution.base_commit
    assert mismatch.payload["indexed_commit"] == "a" * 40


def test_semantic_progress_and_fallback_use_workflow_diagnostics(
    workflow, caplog: pytest.LogCaptureFixture
) -> None:
    service, repository, _, _ = workflow
    execution = service.run("card-context-progress")

    class Strategy:
        def render(self, request: object) -> str:
            callbacks["on_progress"]("Embedded 1/2 chunks")
            callbacks["on_diagnostic"]({
                "kind": "REPOSITORY_CONTEXT_FALLBACK",
                "reason": "Model download unavailable.",
                "remedy": "Retry online.",
            })
            return "LOCAL REPOSITORY CONTEXT"

    callbacks: dict[str, Any] = {}

    def resolve(*args: object, **kwargs: Any) -> Strategy:
        callbacks.update(kwargs)
        return Strategy()

    service._sequencer._context_resolver = resolve
    service._stage_events._verbose = True
    config = SimpleNamespace(
        path=Path("repo"), configured_repository_key="repo",
        repository_context=SimpleNamespace(strategy="qdrant"),
    )
    strategy = service._sequencer._repository_context_strategy(config, execution.id)

    with caplog.at_level(logging.DEBUG, logger="orchestrator.services.workflow_service"):
        assert strategy.render(  # type: ignore[arg-type]
            SimpleNamespace(role="planner")
        ) == "LOCAL REPOSITORY CONTEXT"
    assert [item.payload["kind"] for item in repository.diagnostics[-2:]] == [
        "REPOSITORY_INDEX_PROGRESS", "REPOSITORY_CONTEXT_FALLBACK"
    ]
    assert "Embedded 1/2 chunks" in caplog.text
    assert "Retry online" in caplog.text
    assert execution.status is WorkflowStatus.APPROVED


def test_oversized_context_warns_in_fresh_and_resumed_role_prompts(
    workflow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    service, repository, _, factory = workflow
    context_file = tmp_path / "docs" / "PROJECT_CONTEXT.md"
    context_file.parent.mkdir()
    context_file.write_text("x" * (MAX_PROJECT_CONTEXT_CHARS + 1), encoding="utf-8")
    original_create = factory.git.create_worktree

    def create_with_context(*args: object) -> None:
        original_create(*args)
        workspace = args[0]
        assert isinstance(workspace, Path)
        (workspace / "docs").mkdir()
        shutil.copyfile(context_file, workspace / "docs" / "PROJECT_CONTEXT.md")

    monkeypatch.setattr(factory.git, "create_worktree", create_with_context)
    service._stage_events._verbose = True
    factory.planner.ambiguous = True
    events = []
    with caplog.at_level(logging.DEBUG, logger="orchestrator.services.workflow_service"):
        paused = service.run("card-context-cap", event_listener=events.append)
        factory.planner.ambiguous = False
        resumed = service.respond_and_resume(
            paused.id, "Use the stated behavior.", event_listener=events.append
        )

    assert resumed.status is WorkflowStatus.APPROVED
    warnings = [
        diagnostic for diagnostic in repository.diagnostics
        if diagnostic.category is DiagnosticCategory.WORKFLOW_EVENT
        and diagnostic.payload.get("kind") == "PROJECT_CONTEXT_TRUNCATED"
    ]
    assert [item.stage for item in warnings] == [
        WorkflowStage.GENERATE_SPEC,
        WorkflowStage.GENERATE_SPEC,
        WorkflowStage.IMPLEMENT,
        WorkflowStage.REVIEW,
    ]
    assert warnings[0].payload == {
        "kind": "PROJECT_CONTEXT_TRUNCATED",
        "severity": "warning",
        "path": str(context_file),
        "actual_chars": 12_001,
        "cap_chars": 12_000,
    }
    assert warnings[2].payload["path"] == str(
        Path(resumed.workspace_path) / "docs" / "PROJECT_CONTEXT.md"
    )
    warning_events = [
        event for event in events
        if event.kind is WorkflowEventKind.DIAGNOSTIC
        and (event.payload or {}).get("kind") == "PROJECT_CONTEXT_TRUNCATED"
    ]
    assert len(warning_events) == 4
    assert "PROJECT_CONTEXT_TRUNCATED" in caplog.text
    assert "12001" in caplog.text


def test_workflow_resolves_once_and_reuses_strategy_during_review_loop(
    workflow, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, _, factory = workflow
    resolved_names: list[str] = []
    context_requests: list[RepositoryContextRequest] = []

    class Strategy:
        def render(self, request: RepositoryContextRequest) -> str:
            context_requests.append(request)
            return f"WORKFLOW {request.role.upper()} CONTEXT\n\n"

    strategy = Strategy()

    def resolve(name: str, **_: object) -> Strategy:
        resolved_names.append(name)
        return strategy

    monkeypatch.setattr(workflow_service_module, "resolve_repository_context_strategy", resolve)
    factory.planner.review_decisions = ["CHANGES_REQUESTED", "APPROVED"]

    result = service.run("card-context-strategy")

    assert result.status is WorkflowStatus.APPROVED
    assert resolved_names == ["heuristic"]
    assert [request.role for request in context_requests] == [
        "planner",
        "reviewer",
        "reviewer",
    ]


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


def test_cancellation_stops_later_stages_and_preserves_workspace_artifacts(workflow) -> None:
    service, repository, _, _ = workflow
    events = []
    original_record_implementation = repository.record_implementation

    def record_implementation_and_cancel(*args, **kwargs) -> None:
        original_record_implementation(*args, **kwargs)
        assert repository.execution is not None
        assert service.cancel_execution(repository.execution.id) is True

    repository.record_implementation = record_implementation_and_cancel  # type: ignore[method-assign]

    result = service.run("card-cancelled", event_listener=events.append)

    assert result.status is WorkflowStatus.CANCELLED
    assert result.error_message is None
    assert result.workspace_path is not None
    assert result.working_branch is not None
    assert WorkflowStage.VALIDATE not in repository.stage_changes
    assert WorkflowStage.COMMIT not in repository.stage_changes
    assert any(
        step["stage"] is WorkflowStage.IMPLEMENT and step["status"] is StepStatus.CANCELLED
        for step in repository.steps.values()
    )
    assert not any(event.step_status is StepStatus.FAILED for event in events)
    assert events[-1].status is WorkflowStatus.CANCELLED


def test_cancellation_at_finish_is_emitted_as_cancelled(workflow) -> None:
    service, repository, _, _ = workflow
    events = []
    original_finish = repository.finish

    def cancel_during_finish(execution_id: str, status: WorkflowStatus) -> None:
        # _finish has already checked the request by the time it calls finish.
        assert status is WorkflowStatus.APPROVED
        assert repository.request_cancellation(execution_id)
        original_finish(execution_id, status)

    repository.finish = cancel_during_finish  # type: ignore[method-assign]

    result = service.run("card-cancel-at-finish", event_listener=events.append)

    assert result.status is WorkflowStatus.CANCELLED
    assert result.current_stage is not WorkflowStage.COMPLETED
    assert [event.status for event in events[-2:]] == [
        WorkflowStatus.CANCELLED, WorkflowStatus.CANCELLED,
    ]


def test_cancellation_is_a_terminal_notification_outcome(tmp_path: Path) -> None:
    service, recorder = _workflow_with_notifications(tmp_path)
    repository = service._repository
    original_record_implementation = repository.record_implementation

    def record_implementation_and_cancel(*args, **kwargs) -> None:
        original_record_implementation(*args, **kwargs)
        assert repository.execution is not None
        service.cancel_execution(repository.execution.id)

    repository.record_implementation = record_implementation_and_cancel  # type: ignore[method-assign]

    assert service.run("card-cancel-notification").status is WorkflowStatus.CANCELLED
    assert [event.status for event in recorder.events] == [WorkflowStatus.CANCELLED]


def test_cancellation_retains_already_created_pull_request_artifacts(workflow) -> None:
    service, repository, _, _ = workflow
    original_record_pull_request = repository.record_pull_request

    def record_pull_request_and_cancel(*args, **kwargs) -> None:
        original_record_pull_request(*args, **kwargs)
        assert repository.execution is not None
        service.cancel_execution(repository.execution.id)

    repository.record_pull_request = record_pull_request_and_cancel  # type: ignore[method-assign]

    result = service.run("card-cancel-after-pr")

    assert result.status is WorkflowStatus.CANCELLED
    assert result.workspace_path is not None
    assert result.working_branch is not None
    assert result.commit_sha == "a" * 40
    assert result.pr_url == "https://github.example/pr/42"
    assert WorkflowStage.REVIEW not in repository.stage_changes


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
        (lambda factory: setattr(factory.planner, "ambiguous", True), WorkflowStatus.NEEDS_HUMAN),
        (
            lambda factory: setattr(factory.planner, "changes_requested", True),
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
    factory.planner.review_decisions = ["CHANGES_REQUESTED", "APPROVED"]

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


def test_blocking_planner_ambiguity_stops_for_human_before_branch(workflow) -> None:
    service, _, _, factory = workflow
    factory.planner.ambiguous = True

    result = service.run("card-3")

    assert result.status is WorkflowStatus.NEEDS_HUMAN
    assert result.working_branch is None
    assert "retention" in (result.error_message or "").lower()
    assert result.human_interactions[-1].requesting_stage is WorkflowStage.GENERATE_SPEC
    assert "retention" in result.human_interactions[-1].prompt.lower()


def test_planner_human_response_resumes_same_execution_without_prior_workspace(
    workflow, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, repository, _, factory = workflow
    resolved_names: list[str] = []
    context_roles: list[str] = []
    context_temporary: list[bool] = []

    class Strategy:
        def render(self, request: RepositoryContextRequest) -> str:
            context_roles.append(request.role)
            context_temporary.append(request.temporary_source)
            return "RESUME CONTEXT\n\n"

    def resolve(name: str, **_: object) -> Strategy:
        resolved_names.append(name)
        return Strategy()

    monkeypatch.setattr(workflow_service_module, "resolve_repository_context_strategy", resolve)
    factory.planner.ambiguous = True

    paused = service.run("card-planner-resume")
    assert paused.workspace_path is None
    factory.planner.ambiguous = False

    resumed = service.respond_and_resume(paused.id, "Use a configurable retention period.")

    assert resumed.id == paused.id
    assert resumed.status is WorkflowStatus.APPROVED
    assert resumed.human_interactions[-1].response == "Use a configurable retention period."
    assert factory.planner.spec_calls == 2
    assert resumed.working_branch is not None
    assert repository.stage_changes.count(WorkflowStage.PREPARE_BRANCH) == 1
    assert resolved_names == ["heuristic", "heuristic"]
    assert context_roles == ["planner", "planner", "reviewer"]
    assert context_temporary == [True, True, False]


def test_planning_and_resume_use_comments_from_task_snapshot(workflow) -> None:
    service, _, trello, factory = workflow
    original_get_card = trello.get_card

    def get_card_with_comments(card_id: str) -> DevelopmentTask:
        task = original_get_card(card_id)
        task.metadata["comments"] = ["Human clarification", "Automated finding"]
        return task

    trello.get_card = get_card_with_comments
    prompts: list[str] = []
    original_execute = factory.planner.execute

    def record_planner(request, *, on_started=None):
        if request.structured_output and request.structured_output.name == "repository-spec":
            prompts.append(request.instructions)
        return original_execute(request, on_started=on_started)

    factory.planner.execute = record_planner
    factory.planner.ambiguous = True
    paused = service.run("card-comments-resume")
    assert paused.task_snapshot["metadata"]["comments"] == [
        "Human clarification", "Automated finding",
    ]
    factory.planner.ambiguous = False
    trello.get_card = lambda card_id: (_ for _ in ()).throw(AssertionError("must use snapshot"))
    service.respond_and_resume(paused.id, "Proceed with the proposal.")

    assert len(prompts) == 2
    assert all("Human clarification\n\nAutomated finding" in prompt for prompt in prompts)


def test_human_response_rejects_cancellation_after_initial_status_read(
    workflow, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, repository, _, factory = workflow
    factory.planner.ambiguous = True
    paused = service.run("card-cancelled-before-answer")
    assert paused.status is WorkflowStatus.NEEDS_HUMAN
    original = repository.respond_to_human_input

    def cancel_before_answer(execution_id: str, response: str):
        repository.execution.status = WorkflowStatus.CANCELLED
        return original(execution_id, response)

    monkeypatch.setattr(repository, "respond_to_human_input", cancel_before_answer)
    with pytest.raises(ValueError, match="not awaiting human input"):
        service.submit_response(paused.id, "Proceed")

    assert repository.execution.status is WorkflowStatus.CANCELLED
    assert repository.execution.human_interactions[-1].response is None


def test_human_resume_preserves_role_local_claude_effort(workflow) -> None:
    service, _, _, factory = workflow
    loader = service._config_loader
    loader.config.providers["claude"] = AgentProviderSettings.model_validate(
        {"type": "claude-code", "effort": "medium"}
    )
    loader.config.agents.planner.provider = "claude"
    factory.planner.ambiguous = True

    paused = service.run(
        "card-claude-effort-resume",
        agent_providers={AgentRole.PLANNER: "claude"},
        agent_runtime_options={
            AgentRole.PLANNER: {"model": None, "reasoning_effort": "xhigh"}
        },
    )

    assert paused.status is WorkflowStatus.NEEDS_HUMAN
    first_config = factory.configs[-1]
    first_provider = first_config.providers[first_config.agents.planner.provider]
    assert first_provider.effort == "xhigh"

    factory.planner.ambiguous = False
    resumed = service.respond_and_resume(paused.id, "Use a configurable retention period.")

    assert resumed.status is WorkflowStatus.APPROVED
    resumed_config = factory.configs[-1]
    resumed_provider = resumed_config.providers[resumed_config.agents.planner.provider]
    assert resumed_provider.effort == "xhigh"
    assert loader.config.providers["claude"].effort == "medium"


def test_unpersistable_runtime_overrides_stop_launch_before_agents_run(
    workflow, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, repository, _, factory = workflow
    loader = service._config_loader
    loader.config.providers["claude"] = AgentProviderSettings.model_validate(
        {"type": "claude-code", "effort": "medium"}
    )
    loader.config.agents.planner.provider = "claude"
    record_diagnostic = repository.record_diagnostic

    def failing_record(diagnostic):
        if diagnostic.payload.get("kind") == "RUNTIME_OVERRIDES_APPLIED":
            raise RuntimeError("database is locked")
        return record_diagnostic(diagnostic)

    monkeypatch.setattr(repository, "record_diagnostic", failing_record)

    result = service.run(
        "card-claude-effort-unpersisted",
        agent_providers={AgentRole.PLANNER: "claude"},
        agent_runtime_options={
            AgentRole.PLANNER: {"model": None, "reasoning_effort": "xhigh"}
        },
    )

    assert result.status is WorkflowStatus.FAILED
    assert "Could not persist the selected agent runtime overrides" in (
        result.error_message or ""
    )
    assert factory.configs == []
    assert result.spec_snapshot is None


def test_manual_continuation_preserves_role_local_runtime_overrides(workflow) -> None:
    service, _, _, factory = workflow
    loader = service._config_loader
    loader.config.providers["claude"] = AgentProviderSettings.model_validate(
        {"type": "claude-code", "effort": "medium"}
    )
    loader.config.agents.planner.provider = "claude"
    factory.planner.changes_requested = True

    paused = service.run(
        "card-claude-effort-continuation",
        agent_providers={AgentRole.PLANNER: "claude"},
        agent_runtime_options={
            AgentRole.PLANNER: {"model": None, "reasoning_effort": "xhigh"}
        },
    )
    assert paused.status is WorkflowStatus.CHANGES_REQUESTED
    factory.planner.changes_requested = False

    resumed = service.continue_execution(paused.id, "Also cover the boundary case.")

    assert resumed.status is WorkflowStatus.APPROVED
    continued_config = factory.configs[-1]
    continued_provider = continued_config.providers[continued_config.agents.planner.provider]
    assert continued_provider.effort == "xhigh"
    assert loader.config.providers["claude"].effort == "medium"


def test_reviewer_human_response_reuses_existing_workspace_branch_and_pr(
    workflow, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, _, factory = workflow
    resolved_names: list[str] = []
    context_roles: list[str] = []

    class Strategy:
        def render(self, request: RepositoryContextRequest) -> str:
            context_roles.append(request.role)
            return "RESUME CONTEXT\n\n"

    def resolve(name: str, **_: object) -> Strategy:
        resolved_names.append(name)
        return Strategy()

    monkeypatch.setattr(workflow_service_module, "resolve_repository_context_strategy", resolve)
    factory.planner.malformed_review = True

    paused = service.run("card-review-resume")
    workspace = paused.workspace_path
    branch = paused.working_branch
    assert paused.status is WorkflowStatus.NEEDS_HUMAN
    assert paused.human_interactions[-1].requesting_stage is WorkflowStage.REVIEW
    factory.planner.malformed_review = False

    resumed = service.respond_and_resume(paused.id, "The expected behavior is intentional.")

    assert resumed.id == paused.id
    assert resumed.status is WorkflowStatus.APPROVED
    assert resumed.workspace_path == workspace
    assert resumed.working_branch == branch
    assert factory.github.create_calls == 1
    assert resolved_names == ["heuristic", "heuristic"]
    assert context_roles == ["planner", "reviewer", "reviewer"]


def test_execution_can_pause_for_multiple_human_input_cycles(workflow) -> None:
    service, _, _, factory = workflow
    factory.planner.ambiguous = True

    first_pause = service.run("card-multiple-human")
    second_pause = service.respond_and_resume(first_pause.id, "First clarification.")
    assert second_pause.status is WorkflowStatus.NEEDS_HUMAN

    factory.planner.ambiguous = False
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
    assert factory.git.operation_paths == [workspace] * 7
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
    assert factory.planner.spec_calls == 1


def test_validation_retry_limit_fails_and_preserves_workspace(workflow) -> None:
    service, repository, _, factory = workflow
    factory.commands.fail = True

    result = service.run("card-exhausted-corrections")

    assert result.status is WorkflowStatus.FAILED
    assert len(factory.codex.requests) == 3
    assert result.workspace_path is not None
    assert Path(result.workspace_path).is_dir()
    assert WorkflowStage.COMMIT not in repository.stage_changes


def test_validation_timeout_requests_correction_then_proceeds(workflow) -> None:
    service, repository, _, factory = workflow
    factory.commands.timeout_sequence = [True]

    result = service.run("card-timeout-correction")

    assert result.status is WorkflowStatus.APPROVED
    assert len(factory.codex.requests) == 2
    assert repository.stage_changes.count(WorkflowStage.VALIDATE) == 2
    correction = factory.codex.requests[1].instructions
    assert "VALIDATION FAILURES FROM THE CURRENT IMPLEMENTATION" in correction
    assert "Command: pytest" in correction
    assert "timed out after 900s" in correction
    assert "Stdout captured before timeout:\ncollected 12 items" in correction
    assert "Stderr captured before timeout:\nwaiting on lock" in correction
    assert factory.git.commit_calls == 1
    failure = next(
        diagnostic.payload
        for diagnostic in repository.diagnostics
        if diagnostic.payload.get("operation") == "validation"
    )
    assert failure["failing_commands"] == [
        {
            "command": ["pytest"],
            "exit_code": None,
            "stdout": "collected 12 items",
            "stderr": "waiting on lock",
            "timed_out": True,
            "timeout_seconds": 900,
        }
    ]


def test_repeated_validation_timeouts_exhaust_the_normal_retry_cap(workflow) -> None:
    service, repository, _, factory = workflow
    factory.commands.time_out = True

    result = service.run("card-timeout-exhausted")

    assert result.status is WorkflowStatus.FAILED
    # One implementation plus exactly max_validation_retries (2) corrections.
    assert len(factory.codex.requests) == 3
    assert repository.stage_changes.count(WorkflowStage.VALIDATE) == 3
    assert result.validation_snapshot is not None
    assert result.validation_snapshot["checks"][0]["timed_out"] is True
    assert result.validation_snapshot["checks"][0]["exit_code"] is None
    assert factory.git.commit_calls == 0
    assert factory.git.push_calls == 0
    assert WorkflowStage.COMMIT not in repository.stage_changes
    assert WorkflowStage.PUSH not in repository.stage_changes


def test_optional_polish_timeout_is_discarded_without_correction(workflow) -> None:
    service, repository, _, factory = workflow
    factory.planner.review_suggestions = [["Add a focused regression test"]]
    factory.commands.timeout_sequence = [False, True]

    result = service.run("card-polish-timeout")

    assert result.status is WorkflowStatus.APPROVED
    assert repository.stage_changes.count(WorkflowStage.VALIDATE) == 2
    assert len(factory.codex.requests) == 2
    assert factory.git.discard_calls == 1
    assert factory.git.commit_calls == 1


def test_validation_snapshot_without_timeout_fields_still_resumes() -> None:
    validation = WorkflowService._validation_from_snapshot(
        {
            "success": True,
            "checks": [
                {
                    "name": "pytest",
                    "command": ["pytest"],
                    "success": True,
                    "exit_code": 0,
                    "stdout": "ok",
                    "stderr": "",
                    "duration_seconds": 0.1,
                }
            ],
            "error_message": None,
        }
    )

    check = validation.checks[0]
    assert check.exit_code == 0
    assert check.timed_out is False
    assert check.timeout_seconds is None


def test_branch_creation_failure_is_terminal(workflow) -> None:
    service, repository, _, factory = workflow
    factory.git.branch_error = RuntimeError("cannot create branch")

    result = service.run("card-6")

    assert result.status is WorkflowStatus.FAILED
    assert result.error_message == "cannot create branch"
    failed = next(step for step in repository.steps.values() if step["status"] is StepStatus.FAILED)
    assert failed["stage"] is WorkflowStage.PREPARE_BRANCH


def test_disconnected_history_fails_preparation_before_implementation(workflow) -> None:
    service, repository, _, factory = workflow
    factory.git.merge_base_error = GitClientError(
        "Worktree HEAD has disconnected history from local base branch develop"
    )

    result = service.run("card-disconnected-prepare")

    assert result.status is WorkflowStatus.FAILED
    assert result.working_branch is not None
    assert result.workspace_path is not None
    assert Path(result.workspace_path).is_dir()
    assert "disconnected history" in (result.error_message or "")
    failed = next(step for step in repository.steps.values() if step["status"] is StepStatus.FAILED)
    assert failed["stage"] is WorkflowStage.PREPARE_BRANCH
    assert factory.git.merge_base_calls == ["b" * 40]
    assert not factory.codex.requests
    assert not factory.commands.commands
    assert factory.git.commit_calls == factory.git.push_calls == factory.git.diff_calls == 0
    assert factory.github.create_calls == 0
    assert not factory.github.review_calls


def test_disconnected_history_after_implementation_stops_before_commit(workflow) -> None:
    service, repository, _, factory = workflow
    factory.git.merge_base_errors = [
        None,
        GitClientError("Worktree HEAD has disconnected history from develop"),
    ]

    result = service.run("card-disconnected-commit")

    assert result.status is WorkflowStatus.FAILED
    assert result.current_stage is WorkflowStage.COMMIT
    assert result.error_message and "disconnected history" in result.error_message
    assert len(factory.codex.requests) == 1
    assert factory.git.commit_calls == factory.git.push_calls == 0
    assert factory.github.create_calls == 0
    assert any(
        step["stage"] is WorkflowStage.COMMIT and step["status"] is StepStatus.FAILED
        for step in repository.steps.values()
    )


def test_disconnected_history_before_review_skips_diff_and_reviewer(workflow) -> None:
    service, _, _, factory = workflow
    factory.git.merge_base_errors = [
        None,
        None,
        GitClientError("Worktree HEAD has disconnected history from develop"),
    ]

    result = service.run("card-disconnected-review")

    assert result.status is WorkflowStatus.FAILED
    assert result.current_stage is WorkflowStage.REVIEW
    assert result.error_message and "disconnected history" in result.error_message
    assert factory.git.diff_calls == 0
    assert factory.planner.review_calls == 0


def test_disconnected_history_on_review_correction_stops_before_second_commit(workflow) -> None:
    service, _, _, factory = workflow
    factory.planner.review_decisions = ["CHANGES_REQUESTED"]
    factory.git.merge_base_errors = [
        None,
        None,
        None,
        GitClientError("Worktree HEAD has disconnected history from develop"),
    ]

    result = service.run("card-disconnected-correction")

    assert result.status is WorkflowStatus.FAILED
    assert result.current_stage is WorkflowStage.COMMIT
    assert factory.git.commit_calls == 1
    assert factory.git.push_calls == 1
    assert factory.github.create_calls == 1
    assert factory.git.diff_calls == 1


def test_disconnected_history_on_polish_stops_before_second_commit(workflow) -> None:
    service, _, _, factory = workflow
    factory.planner.review_suggestions = [["Add a focused regression test"]]
    factory.git.merge_base_errors = [
        None,
        None,
        None,
        GitClientError("Worktree HEAD has disconnected history from develop"),
    ]

    result = service.run("card-disconnected-polish")

    assert result.status is WorkflowStatus.FAILED
    assert result.current_stage is WorkflowStage.COMMIT
    assert factory.git.commit_calls == 1
    assert factory.git.push_calls == 1
    assert factory.github.create_calls == 1


def test_disconnected_history_on_planner_resume_fails_preparation(workflow) -> None:
    service, _, _, factory = workflow
    factory.planner.ambiguous = True
    paused = service.run("card-disconnected-planner-resume")
    factory.planner.ambiguous = False
    factory.git.merge_base_error = GitClientError("disconnected history from develop")

    result = service.respond_and_resume(paused.id, "Use a configurable retention period.")

    assert result.status is WorkflowStatus.FAILED
    assert factory.git.fetch_calls == 1
    assert factory.git.merge_base_calls[-1] == paused.base_commit
    assert result.current_stage is WorkflowStage.PREPARE_BRANCH
    assert not factory.codex.requests
    assert factory.git.commit_calls == factory.git.push_calls == 0


def test_disconnected_history_on_review_resume_skips_diff(workflow) -> None:
    service, _, _, factory = workflow
    factory.planner.malformed_review = True
    paused = service.run("card-disconnected-review-resume")
    assert paused.status is WorkflowStatus.NEEDS_HUMAN
    diff_calls = factory.git.diff_calls
    review_calls = factory.planner.review_calls
    factory.planner.malformed_review = False
    factory.git.merge_base_error = GitClientError("disconnected history from develop")

    result = service.respond_and_resume(paused.id, "The implementation is intentional.")

    assert result.status is WorkflowStatus.FAILED
    assert factory.git.fetch_calls == 1
    assert factory.git.merge_base_calls[-1] == paused.base_commit
    assert result.current_stage is WorkflowStage.REVIEW
    assert result.error_message and "disconnected history" in result.error_message
    assert factory.git.diff_calls == diff_calls
    assert factory.planner.review_calls == review_calls


def test_disconnected_history_on_manual_continuation_stops_before_commit(workflow) -> None:
    service, _, _, factory = workflow
    factory.planner.changes_requested = True
    paused = service.run("card-disconnected-continuation")
    factory.planner.changes_requested = False
    commits = factory.git.commit_calls
    pushes = factory.git.push_calls
    factory.git.merge_base_error = GitClientError("disconnected history from develop")

    result = service.continue_execution(paused.id, "Address the review.")

    assert result.status is WorkflowStatus.FAILED
    assert result.current_stage is WorkflowStage.COMMIT
    assert factory.git.commit_calls == commits
    assert factory.git.push_calls == pushes


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
    factory.planner.review_decisions = ["CHANGES_REQUESTED", "APPROVED"]

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
    assert factory.git.fetch_calls == 1
    assert set(factory.git.merge_base_calls) == {result.base_commit}
    assert factory.github.create_calls == 1
    assert [call["event"] for call in factory.github.review_calls] == [
        "REQUEST_CHANGES",
        "APPROVE",
    ]
    assert "### Blocking findings" in factory.github.review_calls[0]["body"]


def test_approved_review_suggestions_can_be_declined_without_changing_approval(workflow) -> None:
    service, repository, _, factory = workflow
    factory.planner.review_suggestions = [["Add a focused regression test"]]
    factory.git.changed_files_results = [["src/audit.py"], []]

    result = service.run("card-polish-declined")

    assert result.status is WorkflowStatus.APPROVED
    assert repository.stage_changes.count(WorkflowStage.POLISH) == 1
    assert factory.planner.review_calls == 1
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
    factory.planner.review_decisions = ["APPROVED", "APPROVED"]
    factory.planner.review_suggestions = [["Add a focused regression test"]]

    result = service.run("card-polish-applied")

    assert result.status is WorkflowStatus.APPROVED
    assert repository.stage_changes.count(WorkflowStage.POLISH) == 1
    assert repository.stage_changes.count(WorkflowStage.REVIEW) == 2
    assert repository.stage_changes.count(WorkflowStage.VALIDATE) == 2
    assert factory.git.commit_calls == 2
    assert factory.git.push_calls == 2
    assert factory.git.diff_calls == 2
    assert factory.github.create_calls == 1
    assert factory.planner.review_calls == 2
    assert factory.github.description_calls == [("example/api", 42)] * 2
    assert factory.github.comment_calls == []


def test_fresh_approved_review_does_not_trigger_a_second_polish_pass(workflow) -> None:
    service, repository, _, factory = workflow
    factory.planner.review_decisions = ["APPROVED", "APPROVED"]
    factory.planner.review_suggestions = [
        ["Add a focused regression test"],
        ["Consider clearer naming"],
    ]

    result = service.run("card-polish-once")

    assert result.status is WorkflowStatus.APPROVED
    assert repository.stage_changes.count(WorkflowStage.POLISH) == 1
    assert factory.planner.review_calls == 2
    assert len(factory.codex.requests) == 2


def test_invalid_optional_polish_is_discarded_without_downgrading_approval(workflow) -> None:
    service, repository, _, factory = workflow
    factory.planner.review_suggestions = [["Add a focused regression test"]]
    factory.commands.failure_sequence = [False, True]

    result = service.run("card-polish-invalid")

    assert result.status is WorkflowStatus.APPROVED
    assert repository.stage_changes.count(WorkflowStage.POLISH) == 1
    assert repository.stage_changes.count(WorkflowStage.VALIDATE) == 2
    assert factory.git.discard_calls == 1
    assert factory.git.commit_calls == 1
    assert factory.git.push_calls == 1
    assert factory.planner.review_calls == 1
    assert len(factory.github.comment_calls) == 1
    assert "did not pass validation" in factory.github.comment_calls[0]["body"]


def test_changes_requested_after_polish_uses_mandatory_correction_loop(workflow) -> None:
    service, repository, _, factory = workflow
    factory.planner.review_decisions = ["APPROVED", "CHANGES_REQUESTED", "APPROVED"]
    factory.planner.review_suggestions = [["Add a focused regression test"]]

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
    factory.planner.review_suggestions = [["Add a focused regression test"]]
    factory.git.changed_files_results = [["src/audit.py"], []]
    factory.github.comment_error = RuntimeError("GitHub unavailable")

    with caplog.at_level(logging.WARNING):
        result = service.run("card-polish-comment-failure")

    assert result.status is WorkflowStatus.APPROVED
    assert result.error_message is None
    assert result.polish_skip_comment_attempted is True
    assert len(factory.github.comment_calls) == 1
    assert "Optional polish skip comment publication failed: GitHub unavailable" in caplog.text


def test_resumed_approved_review_with_suggestions_runs_polish_before_completion(workflow) -> None:
    service, repository, _, factory = workflow
    factory.planner.malformed_review = True

    paused = service.run("card-resumed-polish")

    factory.planner.malformed_review = False
    factory.planner.review_suggestions = [["Add a focused regression test"]]
    # The initial implementation was committed before the review paused.  On
    # resume, this is the polish pass's post-agent worktree state, so an empty
    # result models the implementer declining the optional suggestion.
    factory.git.changed_files_results = [[]]
    resumed = service.respond_and_resume(paused.id, "The implementation is intentional.")

    assert resumed.status is WorkflowStatus.APPROVED
    assert repository.stage_changes.count(WorkflowStage.POLISH) == 1
    assert len(factory.github.comment_calls) == 1


def test_persisted_polish_comment_attempt_prevents_duplicate_comment_on_retry(workflow) -> None:
    service, _, _, factory = workflow
    factory.planner.review_suggestions = [["Add a focused regression test"]]
    factory.git.changed_files_results = [["src/audit.py"], []]

    result = service.run("card-polish-comment-retry")
    execution_clients = factory.create(service._config_loader.config).at_path(
        Path(result.workspace_path or "")
    )
    service._publish_polish_skip_comment(
        execution_id=result.id,
        execution_clients=execution_clients,
        pull_request_number=42,
        implementation=ImplementationResult(success=True),
    )

    assert result.polish_skip_comment_attempted is True
    assert len(factory.github.comment_calls) == 1


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
    factory.planner.changes_requested = True

    result = service.run("card-review-exhausted")

    assert result.status is WorkflowStatus.CHANGES_REQUESTED
    assert result.review_snapshot is not None
    assert result.review_snapshot["result"] == "CHANGES_REQUESTED"
    assert factory.planner.review_calls == 2
    assert len(factory.codex.requests) == 2
    assert repository.stage_changes.count(WorkflowStage.REVIEW) == 2
    assert factory.github.create_calls == 1


def test_manual_continuation_reuses_open_pr_and_records_feedback(workflow) -> None:
    service, repository, _, factory = workflow
    factory.planner.changes_requested = True

    paused = service.run("card-manual-continuation")
    assert paused.status is WorkflowStatus.CHANGES_REQUESTED
    factory.planner.changes_requested = False

    resumed = service.continue_execution(paused.id, "Also cover the boundary case.")

    assert resumed.status is WorkflowStatus.APPROVED
    assert factory.github.create_calls == 1
    assert factory.github.pr_open_calls == [
        {"repository": "example/api", "pull_request_number": 42}
    ]
    assert "OPERATOR FEEDBACK FOR CONTINUING" in factory.codex.requests[-1].instructions
    assert "Also cover the boundary case." in factory.codex.requests[-1].instructions
    assert any(
        diagnostic.payload.get("kind") == "MANUAL_CONTINUATION"
        and diagnostic.payload["feedback"] == "Also cover the boundary case."
        for diagnostic in repository.diagnostics
    )


def test_manual_continuation_stops_before_implementer_when_workspace_is_missing(workflow) -> None:
    service, _, _, factory = workflow
    factory.planner.changes_requested = True
    paused = service.run("card-missing-continuation-worktree")
    assert paused.workspace_path is not None
    Path(paused.workspace_path).rmdir()
    initial_requests = len(factory.codex.requests)

    with pytest.raises(Exception, match="workspace"):
        service.continue_execution(paused.id, "Please continue.")

    assert len(factory.codex.requests) == initial_requests


def test_manual_continuation_blocks_a_closed_persisted_pull_request(workflow) -> None:
    service, _, _, factory = workflow
    factory.planner.changes_requested = True
    paused = service.run("card-closed-continuation-pr")
    factory.planner.changes_requested = False
    factory.github.pr_open = False
    pushes_before = factory.git.push_calls

    resumed = service.continue_execution(paused.id, "Please address the review.")

    assert resumed.status is WorkflowStatus.FAILED
    assert "closed or unavailable" in (resumed.error_message or "")
    assert factory.git.push_calls == pushes_before
    assert factory.github.create_calls == 1


def test_manual_continuation_creates_pr_when_original_failure_had_none(workflow) -> None:
    service, _, _, factory = workflow
    factory.github.error = RuntimeError("PR creation failed")
    failed = service.run("card-continuation-without-pr")
    assert failed.status is WorkflowStatus.FAILED
    assert failed.pull_request_snapshot is None
    factory.github.error = None

    resumed = service.continue_execution(failed.id, "Retry PR creation after implementation.")

    assert resumed.status is WorkflowStatus.APPROVED
    assert resumed.pull_request_snapshot is not None
    assert factory.github.create_calls == 2


def test_review_correction_uses_validation_retry_handling(workflow) -> None:
    service, repository, _, factory = workflow
    factory.planner.review_decisions = ["CHANGES_REQUESTED", "APPROVED"]
    factory.commands.failure_sequence = [False, True, False]

    result = service.run("card-review-validation-retry")

    assert result.status is WorkflowStatus.APPROVED
    assert len(factory.codex.requests) == 3
    assert repository.stage_changes.count(WorkflowStage.IMPLEMENT) == 3
    assert repository.stage_changes.count(WorkflowStage.VALIDATE) == 3
    assert factory.github.create_calls == 1


def test_malformed_review_requires_human_instead_of_assuming_approval(workflow) -> None:
    service, _, _, factory = workflow
    factory.planner.malformed_review = True

    result = service.run("card-malformed-review")

    assert result.status is WorkflowStatus.NEEDS_HUMAN
    assert result.review_snapshot is not None
    assert result.review_snapshot["result"] == "NEEDS_HUMAN"
    assert "could not be interpreted safely" in result.review_snapshot["summary"]
    assert "could not be interpreted safely" in (result.error_message or "")
    assert factory.github.review_calls[0]["event"] == "COMMENT"


def test_agent_human_intervention_stops_workflow_and_keeps_session_reference(workflow) -> None:
    service, _, _, factory = workflow
    factory.planner.spec_error = AgentNeedsHumanError(
        "Agent is awaiting user feedback and requires human intervention", "spec-123"
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
    # Verbose mode uses the persisted diagnostic stream, so it includes the
    # same full, sanitized request schemas available in Detailed Logs.
    assert "[diagnostic]" in caplog.text
    assert '"technical_context"' in caplog.text
    assert '"decision"' in caplog.text


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
