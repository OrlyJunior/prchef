from pathlib import Path

from orchestrator.clients.codex_client import CodexClient
from orchestrator.clients.git_client import GitClient
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.implementation_result import ImplementationResult
from orchestrator.models.repository_spec import RepositorySpec


class ImplementationService:
    def __init__(self, codex_client: CodexClient, git_client: GitClient) -> None:
        self._codex = codex_client
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
        result = self._codex.implement(prompt, repository_path)
        if result.success:
            result.changed_files = self._git.changed_files()
            if not result.changed_files:
                result.success = False
                result.error_message = "Codex completed without changing files"
        return result

