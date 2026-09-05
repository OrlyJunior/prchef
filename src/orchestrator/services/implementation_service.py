import json
from pathlib import Path

from orchestrator.clients.git_client import GitClient
from orchestrator.models.agent import AgentRequest
from orchestrator.models.code_review import CodeReview
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.implementation_result import ImplementationResult
from orchestrator.models.repository_spec import RepositorySpec
from orchestrator.models.validation_result import ValidationResult
from orchestrator.providers.agent_provider import AgentProvider
from orchestrator.runtime_logging import format_command, redact_text
from orchestrator.services.project_context import project_context_prompt

_CORRECTION_OUTPUT_LIMIT = 4_000



class ImplementationService:
    def __init__(self, provider: AgentProvider, git_client: GitClient) -> None:
        self._provider = provider
        self._git = git_client

    def implement(
        self,
        task: DevelopmentTask,
        spec: RepositorySpec,
        repository_path: Path,
    ) -> ImplementationResult:
        prompt = f"""ORIGINAL TASK:
{task.as_original_prompt()}

REPOSITORY SPEC:
{spec.as_prompt_context()}

{project_context_prompt(repository_path)}Keep PROJECT.md accurate when this task meaningfully
changes durable project knowledge:
architecture, workflows, domain behavior, invariants, configuration, persistence, integrations,
or important design decisions. Treat any necessary PROJECT.md update as part of the normal diff.
Do not update it for trivial implementation details. The current repository is authoritative if it
conflicts with PROJECT.md.

Implement the original task.
Use the repository spec only as supporting technical context.
Do not invent requirements.
Preserve the conventions of the target repository.
Do not commit, push, create a pull request, or merge; the orchestrator owns those operations.
"""
        return self._run_prompt(prompt, repository_path)

    def implement_prompt(self, prompt: str, repository_path: Path) -> ImplementationResult:
        """Run explicit implementation input while retaining the safety contract."""
        if not prompt.strip():
            raise ValueError("Implementation prompt must not be empty")
        safe_prompt = f"""EXPLICIT IMPLEMENTATION INPUT:
{prompt}

STRICT SAFETY RULES:
Implement only the requested input in the configured workspace.
{project_context_prompt(repository_path)}Keep PROJECT.md accurate when the requested change
meaningfully changes durable project knowledge; include any needed update in the normal diff. Do
not update it for trivial details, and treat the repository as authoritative if it conflicts with
PROJECT.md.
Do not commit, push, create a pull request, or merge; the orchestrator owns those operations.
"""
        return self._run_prompt(safe_prompt, repository_path)

    def correct_validation_failures(
        self,
        task: DevelopmentTask,
        spec: RepositorySpec,
        validation: ValidationResult,
        repository_path: Path,
    ) -> ImplementationResult:
        """Ask the implementer to repair the existing workspace after validation failed."""
        failures = self._render_validation_failures(validation)
        prompt = f"""ORIGINAL TASK:
{task.as_original_prompt()}

REPOSITORY SPEC:
{spec.as_prompt_context()}

VALIDATION FAILURES FROM THE CURRENT IMPLEMENTATION:
{failures}

{project_context_prompt(repository_path)}Keep PROJECT.md accurate if correcting these failures also
changes durable project knowledge.
Do not update it for trivial details; current repository behavior is authoritative.

Correct the existing implementation in this workspace to address the validation failures.
Do not start over or revert unrelated existing changes.
Do not commit, push, create a pull request, or merge; the orchestrator owns those operations.
"""
        return self._run_prompt(prompt, repository_path)

    def correct_review_findings(
        self,
        task: DevelopmentTask,
        spec: RepositorySpec,
        review: CodeReview,
        repository_path: Path,
    ) -> ImplementationResult:
        """Ask the implementer to address a review in the existing workspace."""
        feedback = json.dumps(
            {
                "summary": review.summary,
                "findings": review.findings,
                "blocking_findings": review.blocking_findings,
                "suggestions": review.suggestions,
            },
            ensure_ascii=False,
            indent=2,
        )
        prompt = f"""ORIGINAL TASK:
{task.as_original_prompt()}

REPOSITORY SPEC:
{spec.as_prompt_context()}

REVIEWER CHANGE REQUESTS FOR THE CURRENT IMPLEMENTATION:
{feedback}

{project_context_prompt(repository_path)}Keep PROJECT.md accurate if addressing these findings also
changes durable project knowledge.
Do not update it for trivial details; current repository behavior is authoritative.

Correct the existing implementation in this workspace to address every blocking review finding.
Use the findings, blocking findings, and suggestions above as review feedback; do not start over
or revert unrelated existing changes.
Do not commit, push, create a pull request, or merge; the orchestrator owns those operations.
"""
        return self._run_prompt(prompt, repository_path)

    def _run_prompt(self, prompt: str, repository_path: Path) -> ImplementationResult:
        agent_result = self._provider.execute(
            AgentRequest(
                instructions=prompt,
                title="Orchestrator implementation",
                workspace_path=repository_path,
            )
        )
        result = ImplementationResult(
            success=agent_result.success,
            summary=agent_result.output,
            error_message=agent_result.error_message,
            workspace_changes_may_exist=agent_result.workspace_changes_may_exist,
        )
        if result.success or result.workspace_changes_may_exist:
            result.changed_files = self._git.changed_files()
        if result.workspace_changes_may_exist and not result.success:
            return result
        if result.success:
            if not result.changed_files:
                result.success = False
                result.error_message = "Implementation provider completed without changing files"
        return result

    @staticmethod
    def _render_validation_failures(validation: ValidationResult) -> str:
        rendered: list[str] = []
        for check in validation.checks:
            if check.success:
                continue
            output = check.stderr or check.stdout or "no output"
            rendered.append(
                "\n".join(
                    (
                        f"Command: {format_command(check.command)}",
                        f"Exit code: {check.exit_code}",
                        "Output:",
                        redact_text(output)[:_CORRECTION_OUTPUT_LIMIT],
                    )
                )
            )
        return "\n\n".join(rendered) or (validation.error_message or "Validation failed")
