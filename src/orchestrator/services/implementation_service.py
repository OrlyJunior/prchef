from pathlib import Path

from orchestrator.clients.git_client import GitClient
from orchestrator.models.agent import AgentRequest
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.implementation_result import ImplementationResult
from orchestrator.models.repository_spec import RepositorySpec
from orchestrator.providers.agent_provider import AgentProvider


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
Do not commit, push, create a pull request, or merge; the orchestrator owns those operations.
"""
        return self._run_prompt(safe_prompt, repository_path)

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
        )
        if result.success:
            result.changed_files = self._git.changed_files()
            if not result.changed_files:
                result.success = False
                result.error_message = "Implementation provider completed without changing files"
        return result
