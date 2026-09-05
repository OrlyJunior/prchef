import logging
from dataclasses import dataclass
from pathlib import Path

from orchestrator.clients.command_client import CommandClient
from orchestrator.clients.git_client import GitClient
from orchestrator.clients.github_client import GitHubAppAuthenticator, GitHubClient
from orchestrator.config.project_config import ProjectConfig
from orchestrator.models.agent import AgentRole
from orchestrator.providers.agent_provider import AgentProvider
from orchestrator.providers.registry import AgentProviderRegistry

logger = logging.getLogger("orchestrator")


@dataclass(slots=True)
class WorkflowClients:
    commands: CommandClient
    agents: dict[AgentRole, AgentProvider]
    git: GitClient
    github: GitHubClient

    def agent(self, role: AgentRole) -> AgentProvider:
        try:
            return self.agents[role]
        except KeyError as exc:
            raise RuntimeError(f"Agent provider for role {role.value!r} was not created") from exc

    def at_path(self, repository_path: Path) -> "WorkflowClients":
        """Bind mutable CLI operations to an execution worktree."""
        return WorkflowClients(
            commands=self.commands,
            agents=self.agents,
            git=self.git.at_path(repository_path),
            github=self.github.at_path(repository_path),
        )


class ClientFactory:
    def __init__(self, jules_api_key: str | None) -> None:
        self._agent_providers = AgentProviderRegistry(jules_api_key)

    def create(
        self,
        config: ProjectConfig,
        *,
        agent_roles: set[AgentRole] | None = None,
    ) -> WorkflowClients:
        commands = CommandClient()
        selected_roles = agent_roles if agent_roles is not None else set(AgentRole)
        role_names = {
            AgentRole.PLANNER: config.agents.planner.provider,
            AgentRole.IMPLEMENTER: config.agents.implementer.provider,
            AgentRole.REVIEWER: config.agents.reviewer.provider,
        }
        instances: dict[str, AgentProvider] = {}
        agents: dict[AgentRole, AgentProvider] = {}
        for role in selected_roles:
            provider_name = role_names[role]
            if provider_name not in instances:
                instances[provider_name] = self._agent_providers.create(
                    provider_name, config.providers, commands
                )
            agents[role] = instances[provider_name]
        resolved = "; ".join(
            f"{role.value} -> {role_names[role]} ({config.providers[role_names[role]].type})"
            for role in AgentRole
            if role in agents
        )
        logger.debug("[config] Resolved agent roles: %s", resolved)
        reviewer_app = config.integrations.github.app
        implementer_app = config.integrations.github.implementer_app
        reviewer_authenticator = (
            GitHubAppAuthenticator(reviewer_app) if reviewer_app.is_configured else None
        )
        implementer_authenticator = (
            GitHubAppAuthenticator(implementer_app) if implementer_app.is_configured else None
        )
        return WorkflowClients(
            commands=commands,
            agents=agents,
            git=GitClient(
                commands,
                config.path,
                implementer_authenticator=implementer_authenticator,
            ),
            github=GitHubClient(
                commands,
                config.path,
                config.integrations.github.command,
                config.integrations.github.timeout_seconds,
                app=reviewer_app,
                implementer_app=implementer_app,
                review_authenticator=reviewer_authenticator,
                implementer_authenticator=implementer_authenticator,
            ),
        )
