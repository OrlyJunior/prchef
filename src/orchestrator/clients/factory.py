from dataclasses import dataclass

from orchestrator.clients.codex_client import CodexClient
from orchestrator.clients.command_client import CommandClient
from orchestrator.clients.git_client import GitClient
from orchestrator.clients.github_client import GitHubClient
from orchestrator.clients.jules_client import JulesClient
from orchestrator.config.project_config import ProjectConfig


@dataclass(slots=True)
class WorkflowClients:
    commands: CommandClient
    jules: JulesClient
    codex: CodexClient
    git: GitClient
    github: GitHubClient


class ClientFactory:
    def __init__(self, jules_api_key: str | None) -> None:
        self._jules_api_key = jules_api_key

    def create(self, config: ProjectConfig) -> WorkflowClients:
        commands = CommandClient()
        return WorkflowClients(
            commands=commands,
            jules=JulesClient(self._jules_api_key, config.integrations.jules),
            codex=CodexClient(
                commands,
                config.integrations.codex.command,
                config.integrations.codex.timeout_seconds,
            ),
            git=GitClient(commands, config.path),
            github=GitHubClient(
                commands,
                config.path,
                config.integrations.github.command,
                config.integrations.github.timeout_seconds,
            ),
        )
