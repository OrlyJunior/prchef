from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from orchestrator.clients.command_client import CommandResult
from orchestrator.clients.exceptions import CommandTimeoutError
from orchestrator.config.project_config import ProjectConfig
from orchestrator.models import (
    AgentExecutionReference,
    AgentRequest,
    AgentResult,
    AgentRole,
    CodeReview,
    DevelopmentTask,
    HumanInteraction,
    ImplementationResult,
    PolishOutcome,
    PullRequest,
    RepositorySpec,
    StepStatus,
    ValidationResult,
    WorkflowBatch,
    WorkflowBatchItem,
    WorkflowExecution,
    WorkflowStage,
    WorkflowStatus,
)

# Queued, provider-blocked, and running work are all cancellable, matching the
# real repository's active-status contract.
_ACTIVE_STATUSES = frozenset(
    {WorkflowStatus.PENDING, WorkflowStatus.WAITING, WorkflowStatus.RUNNING}
)


class FakeWorkflowRepository:
    def __init__(self) -> None:
        self.execution: WorkflowExecution | None = None
        self.steps: dict[int, dict[str, Any]] = {}
        self.stage_changes: list[WorkflowStage] = []
        self.next_step = 1
        self.diagnostics: list[Any] = []
        self.batches: list[WorkflowBatch] = []

    def create_execution(self, task_id: str, task_source: str = "trello") -> WorkflowExecution:
        self.execution = WorkflowExecution(
            id="execution-1",
            task_id=task_id,
            task_source=task_source,
            trello_card_id=task_id if task_source == "trello" else "",
        )
        return self.execution

    def create_batch(
        self, repository: str, task_source: str, tasks: list[DevelopmentTask]
    ) -> WorkflowBatch:
        batch = WorkflowBatch(
            id=f"batch-{len(self.batches) + 1}",
            repository=repository,
            task_source=task_source,
            items=tuple(
                WorkflowBatchItem(position, task.id, task.title)
                for position, task in enumerate(tasks)
            ),
        )
        self.batches.append(batch)
        return batch

    def record_batch_item_execution(
        self, batch_id: str, position: int, execution_id: str
    ) -> None:
        batch = self.get_batch(batch_id)
        items = tuple(
            WorkflowBatchItem(item.position, item.task_id, item.task_title, execution_id)
            if item.position == position
            else item
            for item in batch.items
        )
        replacement = WorkflowBatch(
            batch.id, batch.repository, batch.task_source, batch.created_at, items
        )
        self.batches[self.batches.index(batch)] = replacement

    def get_batch(self, batch_id: str) -> WorkflowBatch:
        return next(batch for batch in self.batches if batch.id == batch_id)

    def list_batches(self) -> list[WorkflowBatch]:
        return list(self.batches)

    def executions_for_tasks(
        self, task_source: str, task_ids: list[str], repository: str
    ) -> dict[str, WorkflowExecution]:
        if self.execution is None:
            return {}
        if (
            self.execution.task_source == task_source
            and self.execution.task_id in task_ids
            and repository in {self.execution.repository, self.execution.repository_ref}
        ):
            return {self.execution.task_id: self.execution}
        return {}

    def _execution(self) -> WorkflowExecution:
        assert self.execution is not None
        self.execution.updated_at = datetime.now(UTC)
        return self.execution

    def update_task(self, execution_id: str, task: DevelopmentTask) -> None:
        execution = self._execution()
        execution.task_id = task.id
        execution.task_source = task.source
        execution.task_url = task.url
        if task.source == "trello":
            execution.trello_card_id = task.id
            execution.trello_card_url = task.url
        execution.task_snapshot = asdict(task)

    def update_project(
        self,
        execution_id: str,
        repository: str,
        base_branch: str,
        repository_ref: str = "",
    ) -> None:
        execution = self._execution()
        execution.repository = repository
        execution.base_branch = base_branch
        execution.repository_ref = repository_ref

    def record_base_commit(self, execution_id: str, base_commit: str) -> None:
        self._execution().base_commit = base_commit

    def record_workspace(self, execution_id: str, workspace_path: str) -> None:
        self._execution().workspace_path = workspace_path

    def update_stage(self, execution_id: str, stage: WorkflowStage) -> None:
        self._execution().current_stage = stage
        self.stage_changes.append(stage)

    def update_status(self, execution_id: str, status: WorkflowStatus) -> None:
        self._execution().status = status

    def request_cancellation(self, execution_id: str) -> bool:
        execution = self._execution()
        if execution.status not in _ACTIVE_STATUSES:
            return False
        if execution.cancellation_requested_at is None:
            execution.cancellation_requested_at = datetime.now(UTC)
        return True

    def cancellation_requested(self, execution_id: str) -> bool:
        execution = self._execution()
        return (
            execution.status in _ACTIVE_STATUSES
            and execution.cancellation_requested_at is not None
        )

    def record_spec(self, execution_id: str, spec: RepositorySpec) -> None:
        self._execution().spec_snapshot = asdict(spec)

    def record_spec_jules_session(
        self, execution_id: str, session_id: str, session_url: str | None
    ) -> None:
        execution = self._execution()
        execution.spec_jules_session_id = session_id
        execution.spec_jules_session_url = session_url

    def record_branch(self, execution_id: str, branch: str) -> None:
        self._execution().working_branch = branch

    def record_implementation(self, execution_id: str, result: ImplementationResult) -> None:
        self._execution().implementation_snapshot = asdict(result)

    def record_validation(self, execution_id: str, result: ValidationResult) -> None:
        self._execution().validation_snapshot = asdict(result)

    def record_commit(self, execution_id: str, commit_sha: str) -> None:
        self._execution().commit_sha = commit_sha

    def record_pull_request(self, execution_id: str, pull_request: PullRequest) -> None:
        execution = self._execution()
        execution.pr_url = pull_request.url
        execution.pull_request_snapshot = asdict(pull_request)

    def record_review(self, execution_id: str, review: CodeReview) -> None:
        self._execution().review_snapshot = asdict(review)

    def record_polish_outcome(self, execution_id: str, outcome: PolishOutcome) -> None:
        self._execution().polish_outcome = outcome

    def record_polish_skip_comment_attempt(self, execution_id: str) -> None:
        self._execution().polish_skip_comment_attempted = True

    def record_review_jules_session(
        self, execution_id: str, session_id: str, session_url: str | None
    ) -> None:
        execution = self._execution()
        execution.review_jules_session_id = session_id
        execution.review_jules_session_url = session_url

    def record_error(self, execution_id: str, message: str) -> None:
        self._execution().error_message = message

    def record_diagnostic(self, diagnostic: Any) -> Any:
        self.diagnostics.append(diagnostic)
        return diagnostic

    def list_diagnostics(self, execution_id: str) -> list[Any]:
        assert execution_id == self._execution().id
        return list(self.diagnostics)

    def request_human_input(
        self, execution_id: str, stage: WorkflowStage, prompt: str
    ) -> HumanInteraction:
        execution = self._execution()
        interaction = HumanInteraction(
            id=len(execution.human_interactions) + 1,
            workflow_execution_id=execution_id,
            requesting_stage=stage,
            prompt=prompt,
            requested_at=datetime.now(UTC),
        )
        execution.human_interactions.append(interaction)
        return interaction

    def respond_to_human_input(self, execution_id: str, response: str) -> HumanInteraction:
        execution = self._execution()
        if execution.status is not WorkflowStatus.NEEDS_HUMAN:
            raise ValueError(f"Execution {execution_id} is not awaiting human input")
        interaction = next(
            item for item in reversed(execution.human_interactions) if item.response is None
        )
        interaction.response = response.strip()
        interaction.responded_at = datetime.now(UTC)
        execution.status = WorkflowStatus.PENDING
        execution.error_message = None
        execution.finished_at = None
        return interaction

    def resume_execution(self, execution_id: str) -> None:
        execution = self._execution()
        execution.status = WorkflowStatus.RUNNING
        execution.error_message = None
        execution.finished_at = None

    def queue_execution(self, execution_id: str) -> None:
        execution = self._execution()
        execution.status = WorkflowStatus.PENDING
        execution.error_message = None
        execution.finished_at = None
        execution.cancellation_requested_at = None

    def finish(self, execution_id: str, status: WorkflowStatus) -> None:
        execution = self._execution()
        if execution.cancellation_requested_at is not None:
            status = WorkflowStatus.CANCELLED
        execution.status = status
        if status in {WorkflowStatus.APPROVED, WorkflowStatus.CHANGES_REQUESTED}:
            execution.current_stage = WorkflowStage.COMPLETED
        execution.finished_at = datetime.now(UTC)

    def start_step(self, execution_id: str, stage: WorkflowStage) -> int:
        step_id = self.next_step
        self.next_step += 1
        self.steps[step_id] = {"stage": stage, "status": StepStatus.RUNNING, "error": None}
        return step_id

    def finish_step(
        self, step_id: int, status: StepStatus, error_message: str | None = None
    ) -> None:
        self.steps[step_id]["status"] = status
        self.steps[step_id]["error"] = error_message

    def get_execution(self, execution_id: str) -> WorkflowExecution:
        return self._execution()


class FakeTrelloClient:
    def __init__(self) -> None:
        self.error: Exception | None = None

    def get_card(self, card_id: str) -> DevelopmentTask:
        if self.error:
            raise self.error
        return DevelopmentTask(
            id=card_id,
            source="trello",
            title="Add audit log",
            description="Persist an audit record after updates.",
            url=f"https://trello.example/{card_id}",
            repository="example",
            metadata={"custom_fields": {"Repository": "example"}},
        )


class FakeConfigLoader:
    def __init__(self, path: Path) -> None:
        self.config = ProjectConfig.model_validate(
            {
                "path": path,
                "repository": {
                    "name": "example",
                    "base_branch": "develop",
                    "protected_branches": ["main", "master", "develop"],
                },
                "validation": {"auto_detect": False, "commands": [["pytest"]]},
                "repository_context": {"strategy": "heuristic"},
                "agents": {
                    "planner": {"provider": "planner"},
                    "implementer": {"provider": "codex"},
                    "reviewer": {"provider": "planner"},
                },
                "providers": {
                    "planner": {"type": "litellm", "model": "test/model"},
                    "codex": {"type": "codex-cli", "command": ["codex", "exec", "-"]},
                },
            }
        )

    def load(self, task: DevelopmentTask, repository_override: str | None = None) -> ProjectConfig:
        return self.config


class FakeAgentProvider:
    def __init__(self) -> None:
        self.ambiguous = False
        self.changes_requested = False
        self.review_decisions: list[str] = []
        self.review_suggestions: list[list[str]] = []
        self.malformed_review = False
        self.spec_error: Exception | None = None
        self.review_error: Exception | None = None
        self.spec_starting_branch: str | None = None
        self.review_starting_branch: str | None = None
        self.spec_calls = 0
        self.review_calls = 0

    def execute(
        self, request: AgentRequest, *, on_started: Any = None
    ) -> AgentResult:
        is_spec = (
            request.structured_output is not None
            and request.structured_output.name == "repository-spec"
        )
        external_id = "spec-123" if is_spec else "review-123"
        reference = AgentExecutionReference(
            provider="planner",
            external_id=external_id,
            url=f"https://provider.example/{external_id}",
            metadata={"state": "QUEUED", "resource_name": None},
        )
        if on_started is not None:
            on_started(reference)
        if is_spec:
            self.spec_calls += 1
            self.spec_starting_branch = request.revision
            if self.spec_error:
                raise self.spec_error
            return AgentResult(
                output=json.dumps(
                    {
                        "technical_context": "Service and repository layers",
                        "current_behavior": "No audit record",
                        "involved_artifacts": ["src/audit.py"],
                        "implementation_proposal": "Write audit record transactionally",
                        "acceptance_criteria": ["Audit row is created"],
                        "expected_tests": ["Unit test"],
                        "risks": [],
                        "ambiguities": (
                            ["Audit retention is unspecified"] if self.ambiguous else []
                        ),
                        "safe_to_implement": not self.ambiguous,
                    }
                ),
                execution=reference,
            )
        self.review_starting_branch = request.revision
        self.review_calls += 1
        if self.review_error:
            raise self.review_error
        if self.malformed_review:
            return AgentResult(output="not valid JSON", execution=reference)
        decision = (
            self.review_decisions.pop(0)
            if self.review_decisions
            else ("CHANGES_REQUESTED" if self.changes_requested else "APPROVED")
        )
        changes_requested = decision == "CHANGES_REQUESTED"
        suggestions = self.review_suggestions.pop(0) if self.review_suggestions else []
        return AgentResult(
            output=json.dumps(
                {
                    "decision": decision,
                    "summary": (
                        "An edge case needs coverage"
                        if changes_requested
                        else "Looks good"
                    ),
                    "findings": ["Missing edge case"] if changes_requested else [],
                    "blocking_findings": (
                        ["Missing edge case"] if changes_requested else []
                    ),
                    "suggestions": suggestions,
                }
            ),
            execution=reference,
        )


class FakeCodexClient:
    def __init__(self) -> None:
        self.fail = False
        self.leave_artifact_on_failure = False
        self.workspace_path: Path | None = None
        self.requests: list[AgentRequest] = []

    def execute(
        self, request: AgentRequest, *, on_started: Any = None
    ) -> AgentResult:
        del on_started
        self.workspace_path = request.workspace_path
        self.requests.append(request)
        if self.fail and self.leave_artifact_on_failure and self.workspace_path is not None:
            (self.workspace_path / "partial-implementation.txt").write_text(
                "unfinished", encoding="utf-8"
            )
        return AgentResult(
            success=not self.fail,
            output="implemented" if not self.fail else "",
            error_message="codex exploded" if self.fail else None,
        )


class FakeGitClient:
    def __init__(self) -> None:
        self.branch_error: Exception | None = None
        self.push_error: Exception | None = None
        self.branch: str | None = None
        self.workspace_path: Path | None = None
        self.control_path: Path | None = None
        self.operation_paths: list[Path | None] = []
        self.commit_calls = 0
        self.push_calls = 0
        self.diff_calls = 0
        self.merge_base_calls: list[str] = []
        self.merge_base_error: Exception | None = None
        self.merge_base_errors: list[Exception | None] = []
        self.discard_calls = 0
        self.changed_files_results: list[list[str]] = []
        self.fetch_calls = 0

    def fetch_base(self, base_branch: str) -> str:
        self.fetch_calls += 1
        return "b" * 40

    @contextmanager
    def planning_snapshot(self, base_commit: str):
        # The fake repository is already the planned content.
        yield self.control_path or Path(".")

    def create_branch(
        self, base_branch: str, working_branch: str, protected_branches: list[str]
    ) -> None:
        if self.branch_error:
            raise self.branch_error
        self.branch = working_branch

    def create_worktree(
        self,
        workspace_path: Path,
        base_branch: str,
        working_branch: str,
        protected_branches: list[str],
        base_commit: str | None = None,
    ) -> None:
        del base_branch, protected_branches
        if self.branch_error:
            raise self.branch_error
        workspace_path.mkdir(parents=True, exist_ok=False)
        self.workspace_path = workspace_path
        self.branch = working_branch

    def at_path(self, repository_path: Path) -> FakeGitClient:
        self.control_path = self.control_path or repository_path
        self.workspace_path = repository_path
        return self

    def verify_worktree_branch(self, working_branch: str, expected_commit: str | None) -> None:
        del expected_commit
        if self.workspace_path is None or not self.workspace_path.is_dir():
            raise RuntimeError("Workflow workspace is unavailable")
        if self.branch != working_branch:
            raise RuntimeError("Workflow workspace is on the wrong branch")

    def changed_files(self) -> list[str]:
        self.operation_paths.append(self.workspace_path)
        if self.changed_files_results:
            return self.changed_files_results.pop(0)
        return ["src/audit.py", "tests/test_audit.py"]

    def discard_uncommitted_changes(self) -> None:
        self.discard_calls += 1

    def github_repository(self) -> str:
        return "example/api"

    def ensure_merge_base(self, base_branch: str) -> None:
        self.merge_base_calls.append(base_branch)
        self.operation_paths.append(self.workspace_path)
        error = (
            self.merge_base_errors.pop(0)
            if self.merge_base_errors
            else self.merge_base_error
        )
        if error is not None:
            raise error

    def commit_all(self, message: str, working_branch: str, protected_branches: list[str]) -> str:
        self.commit_calls += 1
        self.operation_paths.append(self.workspace_path)
        return "a" * 40

    def push(self, working_branch: str, protected_branches: list[str]) -> None:
        self.push_calls += 1
        self.operation_paths.append(self.workspace_path)
        if self.push_error:
            raise self.push_error

    def diff(self, base_branch: str) -> str:
        self.ensure_merge_base(base_branch)
        self.diff_calls += 1
        self.operation_paths.append(self.workspace_path)
        return "diff --git a/src/audit.py b/src/audit.py"


class FakeGitHubClient:
    def __init__(self) -> None:
        self.error: Exception | None = None
        self.review_error: Exception | None = None
        self.assignment_error: Exception | None = None
        self.implementer_identity: str | None = None
        self.last_pull_request_body: str | None = None
        self.create_calls = 0
        self.review_calls: list[dict[str, Any]] = []
        self.assignment_calls: list[dict[str, Any]] = []
        self.comment_error: Exception | None = None
        self.comment_calls: list[dict[str, Any]] = []
        self.pr_open = True
        self.pr_open_calls: list[dict[str, Any]] = []

    def is_pull_request_open(self, repository: str, pull_request_number: int) -> bool:
        self.pr_open_calls.append(
            {"repository": repository, "pull_request_number": pull_request_number}
        )
        return self.pr_open

    def create_pull_request(self, **kwargs: Any) -> PullRequest:
        self.create_calls += 1
        if self.error:
            raise self.error
        self.last_pull_request_body = str(kwargs["body"])
        return PullRequest(
            number=42,
            url="https://github.example/pr/42",
            title=str(kwargs["title"]),
            base_branch=str(kwargs["base_branch"]),
            head_branch=str(kwargs["head_branch"]),
        )

    def submit_pull_request_review(self, **kwargs: Any) -> None:
        self.review_calls.append(kwargs)
        if self.review_error:
            raise self.review_error

    def comment_pull_request(self, **kwargs: Any) -> None:
        self.comment_calls.append(kwargs)
        if self.comment_error:
            raise self.comment_error

    def assign_pull_request(
        self, pr_number: int, assignees: list[str] | tuple[str, ...], **kwargs: Any
    ) -> None:
        self.assignment_calls.append(
            {"pr_number": pr_number, "assignees": list(assignees), **kwargs}
        )
        if self.assignment_error:
            raise self.assignment_error

    def assign_implementer(self, pr_number: int, **kwargs: Any) -> str | None:
        if self.implementer_identity is None:
            return None
        self.assign_pull_request(pr_number, (self.implementer_identity,), **kwargs)
        return self.implementer_identity

    def at_path(self, repository_path: Path) -> FakeGitHubClient:
        del repository_path
        return self


class FakeCommandClient:
    def __init__(self) -> None:
        self.fail = False
        self.failures_remaining = 0
        self.failure_sequence: list[bool] = []
        self.time_out = False
        self.timeout_sequence: list[bool] = []
        self.working_directories: list[Path] = []
        self.commands: list[list[str]] = []

    def run(self, command: list[str], **kwargs: Any) -> CommandResult:
        cwd = kwargs.get("cwd")
        if isinstance(cwd, Path):
            self.working_directories.append(cwd)
        self.commands.append(command)
        timed_out = self.timeout_sequence.pop(0) if self.timeout_sequence else self.time_out
        if timed_out:
            timeout_seconds = kwargs.get("timeout_seconds", 900)
            raise CommandTimeoutError(
                f"Command timed out after {timeout_seconds}s: {' '.join(command)}",
                timeout_seconds=timeout_seconds,
                stdout="collected 12 items",
                stderr="waiting on lock",
                duration_seconds=float(timeout_seconds),
            )
        if self.failure_sequence:
            failed = self.failure_sequence.pop(0)
        else:
            failed = self.fail or self.failures_remaining > 0
            if self.failures_remaining > 0:
                self.failures_remaining -= 1
        return CommandResult(
            command=command,
            exit_code=1 if failed else 0,
            stdout="tests completed",
            stderr="assertion failed" if failed else "",
            duration_seconds=0.01,
        )


class FakeClientFactory:
    def __init__(self) -> None:
        self.planner = FakeAgentProvider()
        self.codex = FakeCodexClient()
        self.git = FakeGitClient()
        self.github = FakeGitHubClient()
        self.commands = FakeCommandClient()
        self.configs: list[ProjectConfig] = []

    def create(
        self,
        config: ProjectConfig,
        *,
        agent_roles: set[AgentRole] | None = None,
    ) -> SimpleNamespace:
        self.configs.append(config)
        self.git.control_path = config.path
        implementer_app = config.integrations.github.implementer_app
        self.github.implementer_identity = (
            implementer_app.bot_name or "implementer[bot]"
            if implementer_app.is_configured
            else None
        )
        selected = agent_roles if agent_roles is not None else set(AgentRole)
        provider_by_name = {"planner": self.planner, "codex": self.codex}
        role_names = {
            AgentRole.PLANNER: config.agents.planner.provider,
            AgentRole.IMPLEMENTER: config.agents.implementer.provider,
            AgentRole.REVIEWER: config.agents.reviewer.provider,
        }
        agents = {
            role: (
                self.planner
                if role_names[role].startswith("__tui_runtime_")
                else provider_by_name[role_names[role]]
            )
            for role in selected
        }
        clients = SimpleNamespace(
            agent=lambda role: agents[role],
            git=self.git,
            github=self.github,
            commands=self.commands,
        )
        clients.at_path = lambda path: SimpleNamespace(
            agent=lambda role: agents[role],
            git=self.git.at_path(path),
            github=self.github.at_path(path),
            commands=self.commands,
        )
        return clients
