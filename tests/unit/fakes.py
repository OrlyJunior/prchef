from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from orchestrator.clients.command_client import CommandResult
from orchestrator.config.project_config import ProjectConfig
from orchestrator.models import (
    AgentExecutionReference,
    AgentRequest,
    AgentResult,
    AgentRole,
    CodeReview,
    DevelopmentTask,
    ImplementationResult,
    PullRequest,
    RepositorySpec,
    StepStatus,
    ValidationResult,
    WorkflowExecution,
    WorkflowStage,
    WorkflowStatus,
)


class FakeWorkflowRepository:
    def __init__(self) -> None:
        self.execution: WorkflowExecution | None = None
        self.steps: dict[int, dict[str, Any]] = {}
        self.stage_changes: list[WorkflowStage] = []
        self.next_step = 1

    def create_execution(self, task_id: str, task_source: str = "trello") -> WorkflowExecution:
        self.execution = WorkflowExecution(
            id="execution-1",
            task_id=task_id,
            task_source=task_source,
            trello_card_id=task_id if task_source == "trello" else "",
        )
        return self.execution

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

    def update_project(self, execution_id: str, repository: str, base_branch: str) -> None:
        execution = self._execution()
        execution.repository = repository
        execution.base_branch = base_branch

    def record_workspace(self, execution_id: str, workspace_path: str) -> None:
        self._execution().workspace_path = workspace_path

    def update_stage(self, execution_id: str, stage: WorkflowStage) -> None:
        self._execution().current_stage = stage
        self.stage_changes.append(stage)

    def update_status(self, execution_id: str, status: WorkflowStatus) -> None:
        self._execution().status = status

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

    def record_review_jules_session(
        self, execution_id: str, session_id: str, session_url: str | None
    ) -> None:
        execution = self._execution()
        execution.review_jules_session_id = session_id
        execution.review_jules_session_url = session_url

    def record_error(self, execution_id: str, message: str) -> None:
        self._execution().error_message = message

    def finish(self, execution_id: str, status: WorkflowStatus) -> None:
        execution = self._execution()
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
            }
        )

    def load(self, task: DevelopmentTask, repository_override: str | None = None) -> ProjectConfig:
        return self.config


class FakeJulesClient:
    def __init__(self) -> None:
        self.ambiguous = False
        self.changes_requested = False
        self.malformed_review = False
        self.spec_error: Exception | None = None
        self.review_error: Exception | None = None
        self.spec_starting_branch: str | None = None
        self.review_starting_branch: str | None = None

    def execute(
        self, request: AgentRequest, *, on_started: Any = None
    ) -> AgentResult:
        is_spec = (
            request.structured_output is not None
            and request.structured_output.name == "repository-spec"
        )
        external_id = "spec-123" if is_spec else "review-123"
        reference = AgentExecutionReference(
            provider="jules",
            external_id=external_id,
            url=f"https://jules.example/{external_id}",
            metadata={"state": "QUEUED", "resource_name": None},
        )
        if on_started is not None:
            on_started(reference)
        if is_spec:
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
        if self.review_error:
            raise self.review_error
        if self.malformed_review:
            return AgentResult(output="not valid JSON", execution=reference)
        return AgentResult(
            output=json.dumps(
                {
                    "decision": (
                        "CHANGES_REQUESTED" if self.changes_requested else "APPROVED"
                    ),
                    "summary": (
                        "An edge case needs coverage"
                        if self.changes_requested
                        else "Looks good"
                    ),
                    "findings": ["Missing edge case"] if self.changes_requested else [],
                    "blocking_findings": (
                        ["Missing edge case"] if self.changes_requested else []
                    ),
                    "suggestions": [],
                }
            ),
            execution=reference,
        )


class FakeCodexClient:
    def __init__(self) -> None:
        self.fail = False
        self.leave_artifact_on_failure = False
        self.workspace_path: Path | None = None

    def execute(
        self, request: AgentRequest, *, on_started: Any = None
    ) -> AgentResult:
        del on_started
        self.workspace_path = request.workspace_path
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

    def changed_files(self) -> list[str]:
        self.operation_paths.append(self.workspace_path)
        return ["src/audit.py", "tests/test_audit.py"]

    def github_repository(self) -> str:
        return "example/api"

    def commit_all(self, message: str, working_branch: str, protected_branches: list[str]) -> str:
        self.operation_paths.append(self.workspace_path)
        return "a" * 40

    def push(self, working_branch: str, protected_branches: list[str]) -> None:
        self.operation_paths.append(self.workspace_path)
        if self.push_error:
            raise self.push_error

    def diff(self, base_branch: str) -> str:
        self.operation_paths.append(self.workspace_path)
        return "diff --git a/src/audit.py b/src/audit.py"


class FakeGitHubClient:
    def __init__(self) -> None:
        self.error: Exception | None = None
        self.last_pull_request_body: str | None = None

    def create_pull_request(self, **kwargs: Any) -> PullRequest:
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

    def at_path(self, repository_path: Path) -> FakeGitHubClient:
        del repository_path
        return self


class FakeCommandClient:
    def __init__(self) -> None:
        self.fail = False
        self.working_directories: list[Path] = []

    def run(self, command: list[str], **kwargs: Any) -> CommandResult:
        cwd = kwargs.get("cwd")
        if isinstance(cwd, Path):
            self.working_directories.append(cwd)
        return CommandResult(
            command=command,
            exit_code=1 if self.fail else 0,
            stdout="tests completed",
            stderr="assertion failed" if self.fail else "",
            duration_seconds=0.01,
        )


class FakeClientFactory:
    def __init__(self) -> None:
        self.jules = FakeJulesClient()
        self.codex = FakeCodexClient()
        self.git = FakeGitClient()
        self.github = FakeGitHubClient()
        self.commands = FakeCommandClient()

    def create(
        self,
        config: ProjectConfig,
        *,
        agent_roles: set[AgentRole] | None = None,
    ) -> SimpleNamespace:
        selected = agent_roles if agent_roles is not None else set(AgentRole)
        provider_by_name = {"jules": self.jules, "codex": self.codex}
        role_names = {
            AgentRole.PLANNER: config.agents.planner.provider,
            AgentRole.IMPLEMENTER: config.agents.implementer.provider,
            AgentRole.REVIEWER: config.agents.reviewer.provider,
        }
        agents = {
            role: provider_by_name[role_names[role]]
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
