from pathlib import Path

import pytest

from orchestrator.models import (
    DevelopmentTask,
    StepStatus,
    ValidationResult,
    WorkflowStage,
    WorkflowStatus,
)
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
    service, repository, _, _ = workflow

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
    assert all(step["status"] is StepStatus.SUCCEEDED for step in repository.steps.values())


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


def test_codex_failure_stops_before_validation(workflow) -> None:
    service, repository, _, factory = workflow
    factory.codex.fail = True

    result = service.run("card-4")

    assert result.status is WorkflowStatus.FAILED
    assert result.error_message == "codex exploded"
    assert WorkflowStage.VALIDATE not in repository.stage_changes


def test_validation_failure_stops_before_commit(workflow) -> None:
    service, repository, _, factory = workflow
    factory.commands.fail = True

    result = service.run("card-5")

    assert result.status is WorkflowStatus.FAILED
    assert result.validation_snapshot is not None
    assert result.commit_sha is None
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


def test_jules_changes_requested_is_final_status(workflow) -> None:
    service, _, _, factory = workflow
    factory.jules.changes_requested = True

    result = service.run("card-9")

    assert result.status is WorkflowStatus.CHANGES_REQUESTED
    assert result.review_snapshot is not None
    assert result.review_snapshot["result"] == "CHANGES_REQUESTED"


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
